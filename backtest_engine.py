"""
バックテストエンジン (Version 11.0: プロ版 - 利益ロック & 動的TP)
"""
import numpy as np
import pandas as pd
import logging
from typing import Dict, Tuple, List
from datetime import datetime, time as dt_time
from config import (
    SIGNAL_THRESHOLDS, POSITION_MANAGEMENT, TREND_FILTER, 
    DIVERGENCE, DAILY_COOLDOWN, TRADING_HOURS, MIN_SCORE_THRESHOLD,
    SLIPPAGE
)
from utils import safe_get, check_divergence, check_trend_filter

logger = logging.getLogger(__name__)


def calculate_score_vectorized(df: pd.DataFrame, params: Dict, side: str) -> np.ndarray:
    """スコア計算のベクトル化版"""
    scores = np.zeros(len(df))
    rsi = df.get('rsi_14', pd.Series(50, index=df.index)).values
    vwap_dev = df.get('vwap_dev', pd.Series(0, index=df.index)).values
    rvol = df.get('rvol', pd.Series(1.0, index=df.index)).values
    adx = df.get('adx_14', pd.Series(0, index=df.index)).values
    
    if side == 'long':
        scores += np.where(rsi < SIGNAL_THRESHOLDS['rsi_low'], params['w_rsi'] * 1.5, 0)
        scores += np.where(vwap_dev < SIGNAL_THRESHOLDS['vwap_dev_low'], params['w_vwap'] * 1.5, 0)
        scores += np.where(rvol > SIGNAL_THRESHOLDS['rvol_threshold'], params['w_rvol'] * 2.0, 0)
        scores += np.where(adx > SIGNAL_THRESHOLDS['adx_threshold'], params['w_adx'] * 1.0, 0)
    else:
        scores += np.where(rsi > SIGNAL_THRESHOLDS['rsi_high'], params['w_rsi'] * 1.5, 0)
        scores += np.where(vwap_dev > SIGNAL_THRESHOLDS['vwap_dev_high'], params['w_vwap'] * 1.5, 0)
        scores += np.where(rvol > SIGNAL_THRESHOLDS['rvol_threshold'], params['w_rvol'] * 2.0, 0)
        scores += np.where(adx > SIGNAL_THRESHOLDS['adx_threshold'], params['w_adx'] * 1.0, 0)
    return scores


def calculate_single_score(row: pd.Series, params: Dict, side: str, boost_score: float = 0) -> float:
    """1行分のスコア計算（プロ版ブースト対応）"""
    score = float(boost_score)
    rsi = safe_get(row, 'rsi_14', 50)
    vwap_dev = safe_get(row, 'vwap_dev', 0)
    rvol = safe_get(row, 'rvol', 1.0)
    adx = safe_get(row, 'adx_14', 0)
    
    if side == 'long':
        if rsi < SIGNAL_THRESHOLDS['rsi_low']: score += params['w_rsi'] * 1.5
        if vwap_dev < SIGNAL_THRESHOLDS['vwap_dev_low']: score += params['w_vwap'] * 1.5
        if rvol > SIGNAL_THRESHOLDS['rvol_threshold']: score += params['w_rvol'] * 2.0
        if adx > SIGNAL_THRESHOLDS['adx_threshold']: score += params['w_adx'] * 1.0
    else:
        if rsi > SIGNAL_THRESHOLDS['rsi_high']: score += params['w_rsi'] * 1.5
        if vwap_dev > SIGNAL_THRESHOLDS['vwap_dev_high']: score += params['w_vwap'] * 1.5
        if rvol > SIGNAL_THRESHOLDS['rvol_threshold']: score += params['w_rvol'] * 2.0
        if adx > SIGNAL_THRESHOLDS['adx_threshold']: score += params['w_adx'] * 1.0
    return score


def run_precise_backtest(df: pd.DataFrame, df_15m: pd.DataFrame, params: Dict, side: str) -> Tuple[float, List[Dict]]:
    """精密バックテスト (Version 11.0 プロ版: 利益ロック & 動的TP)"""
    if df.empty: return 0.0, []
    
    trades = []; position = None; last_date = None; total_profit = 0.0
    boost_score = 0; dynamic_tp_mul = params['tp_mul']
    entry_allowed_time = dt_time(9, 30)
    
    # ボラティリティMA計算
    df['atr_ma'] = df['atr_14'].rolling(window=50).mean()
    
    for idx in range(len(df)):
        row = df.iloc[idx]
        curr_dt = row.name
        
        # 日次分析
        if last_date is None or curr_dt.date() != last_date:
            boost_score = 0; dynamic_tp_mul = params['tp_mul']
            day_data = df[df.index.date == curr_dt.date()]
            or_data = day_data[(day_data.index.time >= dt_time(9, 0)) & (day_data.index.time <= dt_time(9, 30))]
            if not or_data.empty:
                open_900 = or_data['open'].iloc[0]
                close_930 = or_data['close'].iloc[-1]
                drive_pct = (close_930 / open_900 - 1) * 100
                boost_score = 20 if (side == 'long' and drive_pct > 0.5) or (side == 'short' and drive_pct < -0.5) else 0
                if abs(drive_pct) > 1.0: dynamic_tp_mul = params['tp_mul'] * 2.0
            last_date = curr_dt.date()

        if position is not None:
            curr_p = row['close']; h, l = row['high'], row['low']
            # 利益ロック判定
            if not position['locked']:
                initial_risk = position['entry_atr'] * params['sl_mul']
                if (side == 'long' and (curr_p - position['entry_price']) > initial_risk * 1.5) or \
                   (side == 'short' and (position['entry_price'] - curr_p) > initial_risk * 1.5):
                    position['sl'] = position['entry_price'] + (position['entry_atr'] * 0.1) if side == 'long' else position['entry_price'] - (position['entry_atr'] * 0.1)
                    position['locked'] = True
            # 決済実行
            if side == 'long':
                if l <= position['sl']:
                    p = ((position['sl'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                    total_profit += p; trades.append(p); position = None
                elif h >= position['tp']:
                    p = ((position['tp'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                    total_profit += p; trades.append(p); position = None
            else:
                if h >= position['sl']:
                    p = (1 - (position['sl'] / position['entry_price']) - SLIPPAGE) * 100
                    total_profit += p; trades.append(p); position = None
                elif l <= position['tp']:
                    p = (1 - (position['tp'] / position['entry_price']) - SLIPPAGE) * 100
                    total_profit += p; trades.append(p); position = None
            continue

        if curr_dt.time() < entry_allowed_time: continue
        if row['atr_14'] < row.get('atr_ma', 0): continue
        
        if calculate_single_score(row, params, side, boost_score) >= params['threshold']:
            if TREND_FILTER['enabled'] and not check_trend_filter(row['close'], row.get('ma_15m_20', 0), side.upper()): continue
            atr = row['atr_14']
            if atr == 0 or pd.isna(atr): continue
            position = {
                'entry_price': row['close'], 'entry_atr': atr, 'locked': False,
                'tp': row['close'] + (atr * dynamic_tp_mul) if side == 'long' else row['close'] - (atr * dynamic_tp_mul),
                'sl': row['close'] - (atr * params['sl_mul']) if side == 'long' else row['close'] + (atr * params['sl_mul'])
            }
            
    return total_profit, trades


def optimize_parameters(df: pd.DataFrame, df_15m: pd.DataFrame, side: str, iterations: int = 500, precise_check: int = 20) -> Dict:
    """ハイブリッド最適化 (プロ版同期)"""
    from config import PARAM_RANGES
    import random
    candidates = []
    
    # 既存のベクトル演算による1次スクリーニング
    for _ in range(iterations):
        p = {
            'w_rsi': random.randint(*PARAM_RANGES['w_rsi']),
            'w_vwap': random.randint(*PARAM_RANGES['w_vwap']),
            'w_rvol': random.randint(*PARAM_RANGES['w_rvol']),
            'w_adx': random.randint(*PARAM_RANGES['w_adx']),
            'threshold': max(random.randint(*PARAM_RANGES['threshold']), MIN_SCORE_THRESHOLD),
            'sl_mul': random.uniform(*PARAM_RANGES['sl_mul']),
            'tp_mul': random.uniform(*PARAM_RANGES['tp_mul'])
        }
        # ベクトル演算側は簡易シミュレーション
        scores = calculate_score_vectorized(df, p, side)
        signals = scores >= p['threshold']
        prof = 0.0; trds = 0
        if signals.any():
            # 簡易シミュレーションロジック
            close_prices = df['close'].values; atr_values = df['atr_14'].values
            for i in np.where(signals)[0]:
                if i + 10 >= len(df): continue
                entry = close_prices[i]; atr = atr_values[i]
                if atr == 0: continue
                tp = entry + (atr * p['tp_mul']) if side == 'long' else entry - (atr * p['tp_mul'])
                sl = entry - (atr * p['sl_mul']) if side == 'long' else entry + (atr * p['sl_mul'])
                for j in range(i+1, i+10):
                    h, l = df['high'].iloc[j], df['low'].iloc[j]
                    if side == 'long':
                        if l <= sl: prof += ((sl/entry)-1-SLIPPAGE)*100; trds += 1; break
                        elif h >= tp: prof += ((tp/entry)-1-SLIPPAGE)*100; trds += 1; break
                    else:
                        if h >= sl: prof += (1-(sl/entry)-SLIPPAGE)*100; trds += 1; break
                        elif l <= tp: prof += (1-(tp/entry)-SLIPPAGE)*100; trds += 1; break
        candidates.append({'params': p, 'profit': prof, 'trade_count': trds})
    
    top_candidates = sorted(candidates, key=lambda x: x['profit'], reverse=True)[:precise_check]
    precise_results = []
    for cand in top_candidates:
        total_profit, trades = run_precise_backtest(df, df_15m, cand['params'], side)
        trade_count = len(trades)
        adj_profit = total_profit * 0.1 if trade_count < 5 else total_profit
        precise_results.append({'params': cand['params'], 'profit': adj_profit, 'trade_count': trade_count})
        
    return sorted(precise_results, key=lambda x: x['profit'], reverse=True)[0]