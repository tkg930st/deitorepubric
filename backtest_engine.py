"""
バックテストエンジン (Version 15.10)
ログ出力を大幅強化：エントリー時の指標生データとエグジット詳細を記録。
"""
import numpy as np
import pandas as pd
import logging
import random
from typing import Dict, Tuple, List
from datetime import datetime, time as dt_time
from config import (
    SIGNAL_THRESHOLDS, POSITION_MANAGEMENT, TREND_FILTER, 
    PARAM_RANGES, SLIPPAGE, MIN_SCORE_THRESHOLD
)
from utils import safe_get, check_trend_filter

logger = logging.getLogger(__name__)

def calculate_single_score(row: pd.Series, params: Dict, side: str) -> Tuple[float, Dict]:
    """1行分のスコア算出と指標の記録"""
    score = 0.0
    rsi = safe_get(row, 'rsi_14', 50)
    vwap_dev = safe_get(row, 'vwap_dev', 0)
    rvol = safe_get(row, 'rvol', 1.0)
    adx = safe_get(row, 'adx_14', 0)
    
    indicators = {'rsi': rsi, 'vwap_dev': vwap_dev, 'rvol': rvol, 'adx': adx}

    if side == 'long':
        if rsi < 40: score += params['w_rsi'] * 1.2
        if vwap_dev < -0.5: score += params['w_vwap'] * 1.2
    else:
        if rsi > 60: score += params['w_rsi'] * 1.2
        if vwap_dev > 0.5: score += params['w_vwap'] * 1.2
        
    if rvol > 1.8: score += params['w_rvol'] * 2.5
    if adx > 25: score += params['w_adx'] * 2.0
    
    return score, indicators

def run_precise_backtest(df: pd.DataFrame, df_15m: pd.DataFrame, params: Dict, side: str) -> Dict:
    if df.empty: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}
    
    trades = []; position = None; total_profit = 0.0
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    
    for idx in range(len(df)):
        row = df.iloc[idx]
        curr_dt = row.name

        if position:
            h, l = row['high'], row['low']
            exit_reason = None
            
            if not position['tp1_hit']:
                if (side == 'long' and h >= position['tp1']) or (side == 'short' and l <= position['tp1']):
                    position['tp1_hit'] = True
                    # TP1後はリスクを半分に縮小した位置にSLを移動
                    if side == 'long': position['sl'] = max(position['sl'], position['entry_price'] - (position['atr'] * 0.5))
                    else: position['sl'] = min(position['sl'], position['entry_price'] + (position['atr'] * 0.5))
            
            # 決済判定
            if (side == 'long' and l <= position['sl']) or (side == 'short' and h >= position['sl']):
                exit_reason = "STOP_LOSS"
            elif idx == len(df) - 1:
                exit_reason = "SESSION_CLOSE"

            if exit_reason:
                p = ((position['sl']/position['entry_price'])-1-SLIPPAGE)*100 if side == 'long' else (1-(position['sl']/position['entry_price'])-SLIPPAGE)*100
                if exit_reason == "SESSION_CLOSE":
                    p = ((row['close']/position['entry_price'])-1-SLIPPAGE)*100 if side == 'long' else (1-(row['close']/position['entry_price'])-SLIPPAGE)*100
                
                total_profit += p; trades.append(p)
                position = None
            continue

        # エントリーフィルター
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= 75) or (side == 'short' and rsi_val <= 25): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= 3.0) or (side == 'short' and vwap_dev <= -3.0): continue

        score, inds = calculate_single_score(row, params, side)
        if score >= params['threshold']:
            atr = row['atr_14']
            if atr <= 0: continue
            position = {
                'entry_price': row['close'], 'atr': atr, 'tp1_hit': False,
                'tp1': row['close'] + (atr * tp1_mul) if side == 'long' else row['close'] - (atr * tp1_mul),
                'sl': row['close'] - (atr * params['sl_mul']) if side == 'long' else row['close'] + (atr * params['sl_mul'])
            }

    trade_count = len(trades)
    if trade_count == 0: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}
    
    avg_profit = np.mean(trades)
    rr_efficiency = avg_profit / params['sl_mul']
    fitness = total_profit * (rr_efficiency if rr_efficiency > 0 else 0.1) * np.log1p(trade_count)
    if trade_count < 2: fitness *= 0.05
    
    return {'profit': total_profit, 'rr_score': rr_efficiency, 'fitness': fitness, 'trade_count': trade_count}

def get_random_params() -> Dict:
    return {
        'w_rsi': random.randint(10, 40), 'w_vwap': random.randint(10, 40),
        'w_rvol': random.randint(20, 50), 'w_adx': random.randint(20, 50),
        'threshold': random.randint(40, 80),
        'sl_mul': random.uniform(1.2, 3.0), 'tp_mul': random.uniform(2.5, 6.0),
        'use_rsi_filter': random.choice([True, False]), 'use_vwap_filter': random.choice([True, False])
    }

def mutate_params(p: Dict) -> Dict:
    new_p = p.copy()
    key = random.choice(['w_rsi', 'w_vwap', 'w_rvol', 'w_adx', 'threshold', 'sl_mul', 'tp_mul'])
    if key in ['sl_mul', 'tp_mul']:
        new_p[key] = max(1.0, min(8.0, new_p[key] + random.uniform(-0.3, 0.3)))
    else:
        new_p[key] = max(10, min(100, new_p[key] + random.randint(-5, 5)))
    return new_p

def optimize_parameters(df: pd.DataFrame, df_15m: pd.DataFrame, side: str, 
                        iterations: int = 500, precise_check: int = 20) -> Dict:
    candidates = []
    for _ in range(int(iterations * 0.4)):
        p = get_random_params()
        res = run_precise_backtest(df, df_15m, p, side)
        candidates.append({'params': p, **res})
    
    top_elite = sorted([c for c in candidates if c['trade_count'] >= 2], key=lambda x: x['fitness'], reverse=True)[:5]
    if not top_elite: 
        top_elite = sorted(candidates, key=lambda x: x['profit'], reverse=True)[:5]
    
    if top_elite:
        for _ in range(int(iterations * 0.6)):
            parent = random.choice(top_elite)
            p = mutate_params(parent['params'])
            res = run_precise_backtest(df, df_15m, p, side)
            candidates.append({'params': p, **res})
        
    best = sorted(candidates, key=lambda x: x['fitness'], reverse=True)[0]
    return best
