"""
バックテストエンジン (Version 15.3)
pandas-ta依存を排除し、詳細なデバッグログ出力を追加。
"""
import numpy as np
import pandas as pd
import logging
from typing import Dict, Tuple, List
from datetime import datetime, time as dt_time
from config import (
    SIGNAL_THRESHOLDS, POSITION_MANAGEMENT, TREND_FILTER, 
    DIVERGENCE, DAILY_COOLDOWN, TRADING_HOURS, MIN_SCORE_THRESHOLD,
    SLIPPAGE, SECTOR_ALIGNMENT
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
    """1行分のスコア計算"""
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


def calculate_boost_score(df: pd.DataFrame, idx: int, side: str,
                          rival_dfs: Dict[str, pd.DataFrame] = None) -> float:
    boost = 0.0
    row = df.iloc[idx]
    
    # セクター・アライメント
    if SECTOR_ALIGNMENT['enabled'] and rival_dfs:
        aligned_count = 0
        curr_dt = row.name
        for _, rival_df in rival_dfs.items():
            if rival_df is None or rival_df.empty: continue
            if curr_dt in rival_df.index:
                rival_row = rival_df.loc[curr_dt]
                if side == 'long' and rival_row['close'] > rival_row['open']: aligned_count += 1
                elif side == 'short' and rival_row['close'] < rival_row['open']: aligned_count += 1
        if aligned_count >= SECTOR_ALIGNMENT['min_aligned_rivals']:
            boost += SECTOR_ALIGNMENT['alignment_score']
    
    # 出来高加速
    if idx >= 1:
        prev_v = df.iloc[idx - 1].get('volume', 0)
        curr_v = row.get('volume', 0)
        if prev_v > 0 and curr_v > prev_v and safe_get(row, 'rvol', 1.0) > SECTOR_ALIGNMENT['volume_accel_rvol_threshold']:
            boost += SECTOR_ALIGNMENT['volume_accel_score']
    
    # ダイバージェンス
    if DIVERGENCE['enabled'] and idx >= DIVERGENCE['lookback']:
        df_recent = df.iloc[idx - DIVERGENCE['lookback'] + 1: idx + 1]
        div = check_divergence(df_recent, DIVERGENCE['lookback'])
        if (side == 'long' and div.get('bullish')) or (side == 'short' and div.get('bearish')):
            boost += SECTOR_ALIGNMENT['divergence_bonus_score']
    
    return boost


def run_precise_backtest(df: pd.DataFrame, df_15m: pd.DataFrame, params: Dict, side: str,
                         rival_dfs: Dict[str, pd.DataFrame] = None) -> Tuple[float, List[Dict]]:
    """精密バックテスト (Version 15.3: 詳細ログ対応)"""
    if df.empty: return 0.0, []
    
    trades = []; position = None; last_date = None; total_profit = 0.0
    boost_score = 0; dynamic_tp_mul = params['tp_mul']
    entry_allowed_time = dt_time(9, 30)
    cooldown_active = False
    
    df['atr_ma'] = df['atr_14'].rolling(window=50).mean()
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    
    for idx in range(len(df)):
        row = df.iloc[idx]
        curr_dt = row.name
        
        if last_date is None or curr_dt.date() != last_date:
            boost_score = 0; dynamic_tp_mul = params['tp_mul']
            cooldown_active = False
            last_date = curr_dt.date()
            # オープニングドライブ分析
            day_data = df[df.index.date == curr_dt.date()]
            or_data = day_data[(day_data.index.time >= dt_time(9, 0)) & (day_data.index.time <= dt_time(9, 30))]
            if not or_data.empty:
                drive_pct = (or_data['close'].iloc[-1] / or_data['open'].iloc[0] - 1) * 100
                if (side == 'long' and drive_pct > 0.5) or (side == 'short' and drive_pct < -0.5): boost_score = 20
                if abs(drive_pct) > 1.0: dynamic_tp_mul = params['tp_mul'] * 2.0

        if position:
            h, l = row['high'], row['low']
            # TP1
            if not position['tp1_hit']:
                if (side == 'long' and h >= position['tp1']) or (side == 'short' and l <= position['tp1']):
                    position['tp1_hit'] = True
                    position['sl'] = position['entry_price']
                    logger.debug(f"      [BACKTEST] TP1達成 @ {curr_dt}")
            
            # 決済判定 (TP2/Trailing or SL)
            if position['tp1_hit']:
                if (side == 'long' and l <= position['sl']) or (side == 'short' and h >= position['sl']):
                    p = ((position['sl']/position['entry_price'])-1-SLIPPAGE)*100 if side == 'long' else (1-(position['sl']/position['entry_price'])-SLIPPAGE)*100
                    total_profit += p; trades.append(p); position = None; cooldown_active = True
                    continue
            else:
                if (side == 'long' and l <= position['sl']) or (side == 'short' and h >= position['sl']):
                    p = ((position['sl']/position['entry_price'])-1-SLIPPAGE)*100 if side == 'long' else (1-(position['sl']/position['entry_price'])-SLIPPAGE)*100
                    total_profit += p; trades.append(p); position = None; cooldown_active = True
                    continue
            continue

        if curr_dt.time() < entry_allowed_time or cooldown_active: continue
        
        # エントリーフィルター
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        if (side == 'long' and (rsi_val >= 70 or vwap_dev >= 2.5)) or (side == 'short' and (rsi_val <= 30 or vwap_dev <= -2.5)): continue
        if row['atr_14'] < row.get('atr_ma', 0): continue

        v13_boost = calculate_boost_score(df, idx, side, rival_dfs)
        if calculate_single_score(row, params, side, boost_score + v13_boost) >= params['threshold']:
            if TREND_FILTER['enabled'] and not check_trend_filter(row['close'], row.get('ma_15m_20', 0), side.upper()): continue
            atr = row['atr_14']
            if atr <= 0: continue
            position = {
                'entry_price': row['close'], 'entry_atr': atr, 'tp1_hit': False,
                'tp1': row['close'] + (atr * tp1_mul) if side == 'long' else row['close'] - (atr * tp1_mul),
                'sl': row['close'] - (atr * params['sl_mul']) if side == 'long' else row['close'] + (atr * params['sl_mul'])
            }
            logger.debug(f"      [BACKTEST] エントリー {side.upper()} @ {row['close']} ({curr_dt})")
            
    return total_profit, trades


def optimize_parameters(df: pd.DataFrame, df_15m: pd.DataFrame, side: str,
                        iterations: int = 500, precise_check: int = 20,
                        rival_dfs: Dict[str, pd.DataFrame] = None) -> Dict:
    """ハイブリッド最適化 (Ver 15.3)"""
    from config import PARAM_RANGES
    import random
    candidates = []
    
    for _ in range(iterations):
        sl_mul = random.uniform(*PARAM_RANGES['sl_mul'])
        tp_mul = max(random.uniform(*PARAM_RANGES['tp_mul']), sl_mul * 1.5, 2.0)
        p = {
            'w_rsi': random.randint(*PARAM_RANGES['w_rsi']), 'w_vwap': random.randint(*PARAM_RANGES['w_vwap']),
            'w_rvol': random.randint(*PARAM_RANGES['w_rvol']), 'w_adx': random.randint(*PARAM_RANGES['w_adx']),
            'threshold': max(random.randint(*PARAM_RANGES['threshold']), MIN_SCORE_THRESHOLD),
            'sl_mul': sl_mul, 'tp_mul': tp_mul
        }
        
        scores = calculate_score_vectorized(df, p, side)
        signals = scores >= p['threshold']
        prof = 0.0; trds = 0
        if signals.any():
            for i in np.where(signals)[0]:
                if i + 10 >= len(df): continue
                entry = df['close'].iloc[i]; atr = df['atr_14'].iloc[i]
                if atr <= 0: continue
                sl = entry - (atr * p['sl_mul']) if side == 'long' else entry + (atr * p['sl_mul'])
                tp = entry + (atr * p['tp_mul']) if side == 'long' else entry - (atr * p['tp_mul'])
                for j in range(i+1, min(i+11, len(df))):
                    curr_h, curr_l = df['high'].iloc[j], df['low'].iloc[j]
                    if side == 'long':
                        if curr_l <= sl: prof += ((sl/entry)-1-SLIPPAGE)*100; trds += 1; break
                        elif curr_h >= tp: prof += ((tp/entry)-1-SLIPPAGE)*100; trds += 1; break
                    else:
                        if curr_h >= sl: prof += (1-(sl/entry)-SLIPPAGE)*100; trds += 1; break
                        elif curr_l <= tp: prof += (1-(tp/entry)-SLIPPAGE)*100; trds += 1; break
        candidates.append({'params': p, 'profit': prof, 'trade_count': trds})
    
    top_candidates = sorted([c for c in candidates if c['trade_count'] > 0], key=lambda x: x['profit'], reverse=True)[:precise_check]
    if not top_candidates: return {'params': p, 'profit': 0.0, 'trade_count': 0}

    results = []
    for cand in top_candidates:
        prof, trds = run_precise_backtest(df, df_15m, cand['params'], side, rival_dfs)
        t_count = len(trds)
        results.append({'params': cand['params'], 'profit': prof if t_count >= 5 else prof * 0.1, 'trade_count': t_count})
        
    return sorted(results, key=lambda x: x['profit'], reverse=True)[0]
