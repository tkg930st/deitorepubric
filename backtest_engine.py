"""
バックtestエンジン (Version 15.15)
変更点:
- ダイバージェンス & 出来高加速スコアリングの統合
- パラメータ探索範囲を config.py と完全同期
"""
import numpy as np
import pandas as pd
import logging
import random
from typing import Dict, Tuple, List
from datetime import datetime, time as dt_time
from config import (
    SIGNAL_THRESHOLDS, POSITION_MANAGEMENT, TREND_FILTER,
    PARAM_RANGES, SLIPPAGE, MIN_SCORE_THRESHOLD, SECTOR_ALIGNMENT, DIVERGENCE,
    RISK_MANAGEMENT
)
from utils import safe_get, check_trend_filter, check_divergence

logger = logging.getLogger(__name__)

def calculate_single_score(row: pd.Series, params: Dict, side: str, div_res: Dict = None) -> Tuple[float, Dict]:
    """1行分のスコア算出 (多段階評価で最適化を促進, SIGNAL_THRESHOLDS参照)"""
    st = SIGNAL_THRESHOLDS
    score = 0.0
    rsi = safe_get(row, 'rsi_14', 50)
    vwap_dev = safe_get(row, 'vwap_dev', 0)
    rvol = safe_get(row, 'rvol', 1.0)
    adx = safe_get(row, 'adx_14', 0)

    # 1. 基礎スコア (段階的加点)
    if side == 'long':
        if rsi < st['rsi_oversold']: score += params['w_rsi'] * 1.5
        elif rsi < st['rsi_buy_moderate']: score += params['w_rsi'] * 1.0

        if vwap_dev < -st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
        elif vwap_dev < -st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0
    else:
        if rsi > st['rsi_overbought']: score += params['w_rsi'] * 1.5
        elif rsi > st['rsi_sell_moderate']: score += params['w_rsi'] * 1.0

        if vwap_dev > st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
        elif vwap_dev > st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0

    # ボリューム評価
    if rvol > st['rvol_strong']: score += params['w_rvol'] * 2.0
    elif rvol > st['rvol_moderate']: score += params['w_rvol'] * 1.0
    elif rvol > st['rvol_weak']: score += params['w_rvol'] * 0.5

    # トレンド強度
    if adx > st['adx_strong']: score += params['w_adx'] * 1.5
    elif adx > st['adx_moderate']: score += params['w_adx'] * 1.0
    
    # 2. ボーナススコアの廃止 (加点方式は最適化を破壊するため)
    total_score = score
    
    indicators = {
        'rsi': rsi, 'vwap_dev': vwap_dev, 'rvol': rvol, 'adx': adx
    }
    
    return total_score, indicators

def run_precise_backtest(df: pd.DataFrame, df_15m: pd.DataFrame, params: Dict, side: str) -> Dict:
    if df.empty: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}
    
    trades = []; position = None; total_profit = 0.0
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    tp1_exit_ratio = POSITION_MANAGEMENT.get('tp1_exit_ratio', 0.5)

    lookback = DIVERGENCE.get('lookback', 25)
    for idx in range(len(df)):
        row = df.iloc[idx]
        curr_dt = row.name

        if position:
            h, l = row['high'], row['low']
            exit_reason = None

            if not position['tp1_hit']:
                if (side == 'long' and h >= position['tp1']) or (side == 'short' and l <= position['tp1']):
                    position['tp1_hit'] = True
                    # TP1損益を記録
                    if side == 'long':
                        position['tp1_profit'] = ((position['tp1'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                    else:
                        position['tp1_profit'] = (1 - (position['tp1'] / position['entry_price']) - SLIPPAGE) * 100
                    # TP1後はリスクを半分に縮小した位置にSLを移動
                    if side == 'long': position['sl'] = max(position['sl'], position['entry_price'] - (position['atr'] * 0.5))
                    else: position['sl'] = min(position['sl'], position['entry_price'] + (position['atr'] * 0.5))

            # 決済判定
            if (side == 'long' and l <= position['sl']) or (side == 'short' and h >= position['sl']):
                exit_reason = "STOP_LOSS"
            elif idx == len(df) - 1:
                exit_reason = "SESSION_CLOSE"

            if exit_reason:
                exit_price = position['sl'] if exit_reason == "STOP_LOSS" else row['close']
                remaining_p = ((exit_price / position['entry_price']) - 1 - SLIPPAGE) * 100 if side == 'long' else (1 - (exit_price / position['entry_price']) - SLIPPAGE) * 100

                # TP1到達済み: 50%×TP1損益 + 50%×残ポジション損益
                if position['tp1_hit']:
                    p = (tp1_exit_ratio * position['tp1_profit']) + ((1 - tp1_exit_ratio) * remaining_p)
                else:
                    p = remaining_p

                total_profit += p; trades.append(p)
                position = None
            continue

        # エントリーフィルター (SIGNAL_THRESHOLDS参照)
        st = SIGNAL_THRESHOLDS
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= st['rsi_filter_long_max']) or (side == 'short' and rsi_val <= st['rsi_filter_short_min']): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= st['vwap_filter_max']) or (side == 'short' and vwap_dev <= -st['vwap_filter_max']): continue

        # ダイバージェンス算出 (その時点までの過去データを使用)
        div_res = None
        if idx >= lookback:
            div_res = check_divergence(df.iloc[idx-lookback:idx+1])

        score, inds = calculate_single_score(row, params, side, div_res=div_res)
        if score >= params['threshold']:
            atr = row['atr_14']
            if atr <= 0: continue
            
            # SL下限ガードを適用 (Monitorと同期)
            actual_sl_mul = max(params['sl_mul'], RISK_MANAGEMENT.get('min_sl_multiplier', 0.7))
            
            position = {
                'entry_price': row['close'], 'atr': atr, 'tp1_hit': False, 'tp1_profit': 0.0,
                'tp1': row['close'] + (atr * tp1_mul) if side == 'long' else row['close'] - (atr * tp1_mul),
                'sl': row['close'] - (atr * actual_sl_mul) if side == 'long' else row['close'] + (atr * actual_sl_mul)
            }

    trade_count = len(trades)
    if trade_count == 0: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}
    
    avg_profit = np.mean(trades)
    rr_efficiency = avg_profit / params['sl_mul']
    fitness = total_profit * (rr_efficiency if rr_efficiency > 0 else 0.1) * np.log1p(trade_count)
    if trade_count < 2: fitness *= 0.05
    
    return {'profit': total_profit, 'rr_score': rr_efficiency, 'fitness': fitness, 'trade_count': trade_count}

def get_random_params() -> Dict:
    r = PARAM_RANGES
    return {
        'w_rsi': random.randint(r['w_rsi'][0], r['w_rsi'][1]),
        'w_vwap': random.randint(r['w_vwap'][0], r['w_vwap'][1]),
        'w_rvol': random.randint(r['w_rvol'][0], r['w_rvol'][1]),
        'w_adx': random.randint(r['w_adx'][0], r['w_adx'][1]),
        # 閾値を低めに開始して取引を発生させる (探索範囲の下限付近から)
        'threshold': random.randint(r['threshold'][0], r['threshold'][0] + 40),
        'sl_mul': random.uniform(r['sl_mul'][0], r['sl_mul'][1]),
        'tp_mul': random.uniform(r['tp_mul'][0], r['tp_mul'][1]),
        # フィルタは最初はオフ寄りにする
        'use_rsi_filter': random.random() > 0.7,
        'use_vwap_filter': random.random() > 0.7
    }

def mutate_params(p: Dict) -> Dict:
    new_p = p.copy()
    key = random.choice(['w_rsi', 'w_vwap', 'w_rvol', 'w_adx', 'threshold', 'sl_mul', 'tp_mul'])
    r = PARAM_RANGES
    if key in ['sl_mul', 'tp_mul']:
        new_p[key] = max(r[key][0], min(r[key][1], new_p[key] + random.uniform(-0.3, 0.3)))
    else:
        new_p[key] = max(r[key][0], min(r[key][1], new_p[key] + random.randint(-10, 10)))
    
    # フィルタ設定もたまに反転させる
    if random.random() < 0.1:
        f_key = random.choice(['use_rsi_filter', 'use_vwap_filter'])
        new_p[f_key] = not new_p[f_key]
    return new_p

def optimize_parameters(df: pd.DataFrame, df_15m: pd.DataFrame, side: str, 
                        iterations: int = 500, precise_check: int = 20) -> Dict:
    candidates = []
    # 1. 初期探索 (30%の試行で多様な種を生成)
    for _ in range(int(iterations * 0.3)):
        p = get_random_params()
        res = run_precise_backtest(df, df_15m, p, side)
        candidates.append({'params': p, **res})
    
    # 2. 進化 (取引が1回以上あるものを優先)
    top_elite = sorted([c for c in candidates if c['trade_count'] >= 1], key=lambda x: x['fitness'], reverse=True)[:10]
    if not top_elite: 
        top_elite = sorted(candidates, key=lambda x: x['profit'], reverse=True)[:10]
    
    if top_elite:
        # 70%の試行を進化に割り当てる
        for _ in range(int(iterations * 0.7)):
            parent = random.choice(top_elite)
            p = mutate_params(parent['params'])
            res = run_precise_backtest(df, df_15m, p, side)
            candidates.append({'params': p, **res})
        
    best = sorted(candidates, key=lambda x: x['fitness'], reverse=True)[0]
    return best
