"""
バックテストエンジン (Version 13.5: トレーリングTP & セクター・アライメント & スコア共通化)
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
    """1行分のスコア計算（Ver 13.5: ブースト対応 — monitor.pyと共通）"""
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
    """
    Ver 13.5: ブーストスコア計算（monitor.pyと共通ロジック）
    
    - セクター・アライメント (+15点)
    - 出来高加速 (+10点)
    - ダイバージェンス・ボーナス (+20点)
    
    Args:
        df: 対象銘柄のDataFrame
        idx: 現在の行インデックス
        side: 'long' or 'short'
        rival_dfs: ライバル銘柄のDataFrame辞書 {ticker: df}
    """
    boost = 0.0
    row = df.iloc[idx]
    
    # --- セクター・アライメント (+15点) ---
    if SECTOR_ALIGNMENT['enabled'] and rival_dfs:
        aligned_count = 0
        for rival_ticker, rival_df in rival_dfs.items():
            if rival_df is None or rival_df.empty:
                continue
            # 同じタイムスタンプのデータを探す
            curr_dt = row.name
            if curr_dt in rival_df.index:
                rival_row = rival_df.loc[curr_dt]
            else:
                # 最も近いインデックスを使用
                rival_idx_pos = rival_df.index.get_indexer([curr_dt], method='ffill')[0]
                if rival_idx_pos < 0 or rival_idx_pos >= len(rival_df):
                    continue
                rival_row = rival_df.iloc[rival_idx_pos]
            
            try:
                rival_open = rival_row['open']
                rival_close = rival_row['close']
                if rival_open == 0:
                    continue
                # 陽線/陰線の判定
                if side == 'long' and rival_close > rival_open:
                    aligned_count += 1
                elif side == 'short' and rival_close < rival_open:
                    aligned_count += 1
            except (KeyError, TypeError):
                continue
        
        if aligned_count >= SECTOR_ALIGNMENT['min_aligned_rivals']:
            boost += SECTOR_ALIGNMENT['alignment_score']
    
    # --- 出来高加速 (+10点) ---
    if idx >= 1:
        prev_volume = df.iloc[idx - 1].get('volume', 0)
        curr_volume = row.get('volume', 0)
        curr_rvol = safe_get(row, 'rvol', 1.0)
        if prev_volume > 0 and curr_volume > prev_volume and curr_rvol > SECTOR_ALIGNMENT['volume_accel_rvol_threshold']:
            boost += SECTOR_ALIGNMENT['volume_accel_score']
    
    # --- ダイバージェンス・ボーナス (+20点) ---
    if DIVERGENCE['enabled'] and idx >= DIVERGENCE['lookback']:
        df_recent = df.iloc[idx - DIVERGENCE['lookback'] + 1: idx + 1]
        div = check_divergence(df_recent, DIVERGENCE['lookback'])
        if side == 'long' and div.get('bullish'):
            boost += SECTOR_ALIGNMENT['divergence_bonus_score']
        elif side == 'short' and div.get('bearish'):
            boost += SECTOR_ALIGNMENT['divergence_bonus_score']
    
    return boost


def run_precise_backtest(df: pd.DataFrame, df_15m: pd.DataFrame, params: Dict, side: str,
                         rival_dfs: Dict[str, pd.DataFrame] = None) -> Tuple[float, List[Dict]]:
    """精密バックテスト (Version 13.5: トレーリングTP & セクター・アライメント対応)"""
    if df.empty: return 0.0, []
    
    trades = []; position = None; last_date = None; total_profit = 0.0
    boost_score = 0; dynamic_tp_mul = params['tp_mul']
    entry_allowed_time = dt_time(9, 30)
    cooldown_active = False # 当日制限フラグ
    
    # ボラティリティMA計算
    df['atr_ma'] = df['atr_14'].rolling(window=50).mean()
    
    # Ver 13.5設定
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    trailing_atr_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
    
    for idx in range(len(df)):
        row = df.iloc[idx]
        curr_dt = row.name
        
        # 日次分析 & 制限リセット
        if last_date is None or curr_dt.date() != last_date:
            boost_score = 0; dynamic_tp_mul = params['tp_mul']
            cooldown_active = False
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
            h, l = row['high'], row['low']
            
            # TP1チェック（利益ロック・建値移動）
            if not position['tp1_hit']:
                if (side == 'long' and h >= position['tp1']) or \
                   (side == 'short' and l <= position['tp1']):
                    position['tp1_hit'] = True
                    position['sl'] = position['entry_price'] # 建値に移動
                    # Ver 13.5: トレーリングストップの初期値を設定
                    if side == 'long':
                        position['trailing_stop'] = h - (position['entry_atr'] * trailing_atr_mul)
                    else:
                        position['trailing_stop'] = l + (position['entry_atr'] * trailing_atr_mul)
            
            # --- Ver 13.5: TP1後はトレーリングストップで追従 ---
            if position['tp1_hit']:
                atr_trail = position['entry_atr'] * trailing_atr_mul
                if side == 'long':
                    # 高値更新でトレーリングストップを引き上げ
                    new_trail = h - atr_trail
                    if new_trail > position['trailing_stop']:
                        position['trailing_stop'] = new_trail
                    # トレーリングストップに安値がタッチ → 決済
                    if l <= position['trailing_stop']:
                        exit_price = position['trailing_stop']
                        p_exit = ((exit_price / position['entry_price']) - 1 - SLIPPAGE) * 100
                        p_tp1 = ((position['tp1'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                        p_final = (p_tp1 + p_exit) / 2
                        total_profit += p_final; trades.append(p_final); position = None
                        continue
                else:  # short
                    # 安値更新でトレーリングストップを引き下げ
                    new_trail = l + atr_trail
                    if new_trail < position['trailing_stop']:
                        position['trailing_stop'] = new_trail
                    # トレーリングストップに高値がタッチ → 決済
                    if h >= position['trailing_stop']:
                        exit_price = position['trailing_stop']
                        p_exit = (1 - (exit_price / position['entry_price']) - SLIPPAGE) * 100
                        p_tp1 = (1 - (position['tp1'] / position['entry_price']) - SLIPPAGE) * 100
                        p_final = (p_tp1 + p_exit) / 2
                        total_profit += p_final; trades.append(p_final); position = None
                        continue
            
            # TP1未達時の損切チェック（従来ロジック維持）
            if not position['tp1_hit']:
                if side == 'long':
                    if l <= position['sl']:
                        p_exit = ((position['sl'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                        p_final = p_exit
                        cooldown_active = True
                        total_profit += p_final; trades.append(p_final); position = None
                else:  # short
                    if h >= position['sl']:
                        p_exit = (1 - (position['sl'] / position['entry_price']) - SLIPPAGE) * 100
                        p_final = p_exit
                        cooldown_active = True
                        total_profit += p_final; trades.append(p_final); position = None
            continue

        # エントリー判定
        if curr_dt.time() < entry_allowed_time: continue
        
        # 当日制限チェック（ダイバージェンスで解除可能）
        if cooldown_active:
            if DIVERGENCE['enabled'] and idx >= DIVERGENCE['lookback']:
                df_recent = df.iloc[idx-DIVERGENCE['lookback']+1 : idx+1]
                div = check_divergence(df_recent, DIVERGENCE['lookback'])
                if div.get('bullish') or div.get('bearish'):
                    cooldown_active = False
                else: continue
            else: continue

        if row['atr_14'] < row.get('atr_ma', 0): continue
        
        # Ver 13.5: ブーストスコア計算（セクター・アライメント + 出来高加速 + ダイバージェンス）
        v13_boost = calculate_boost_score(df, idx, side, rival_dfs)
        total_boost = boost_score + v13_boost
        
        if calculate_single_score(row, params, side, total_boost) >= params['threshold']:
            if TREND_FILTER['enabled'] and not check_trend_filter(row['close'], row.get('ma_15m_20', 0), side.upper()): continue
            atr = row['atr_14']
            if atr == 0 or pd.isna(atr): continue
            
            position = {
                'entry_price': row['close'], 'entry_atr': atr, 'tp1_hit': False,
                'tp1': row['close'] + (atr * tp1_mul) if side == 'long' else row['close'] - (atr * tp1_mul),
                'tp2': row['close'] + (atr * dynamic_tp_mul) if side == 'long' else row['close'] - (atr * dynamic_tp_mul),
                'sl': row['close'] - (atr * params['sl_mul']) if side == 'long' else row['close'] + (atr * params['sl_mul']),
                'trailing_stop': 0.0  # Ver 13.5: TP1後に設定される
            }
            
    return total_profit, trades


def optimize_parameters(df: pd.DataFrame, df_15m: pd.DataFrame, side: str,
                        iterations: int = 500, precise_check: int = 20,
                        rival_dfs: Dict[str, pd.DataFrame] = None) -> Dict:
    """ハイブリッド最適化 (Ver 13.5: ライバルデータ供給対応)"""
    from config import PARAM_RANGES
    import random
    candidates = []
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    
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
            close_prices = df['close'].values; atr_values = df['atr_14'].values
            for i in np.where(signals)[0]:
                if i + 10 >= len(df): continue
                entry = close_prices[i]; atr = atr_values[i]
                if atr == 0: continue
                tp1 = entry + (atr * tp1_mul) if side == 'long' else entry - (atr * tp1_mul)
                tp2 = entry + (atr * p['tp_mul']) if side == 'long' else entry - (atr * p['tp_mul'])
                sl = entry - (atr * p['sl_mul']) if side == 'long' else entry + (atr * p['sl_mul'])
                
                # 簡易的な分割決済期待値計算
                for j in range(i+1, i+10):
                    h, l = df['high'].iloc[j], df['low'].iloc[j]
                    if side == 'long':
                        if l <= sl: 
                            prof += ((sl/entry)-1-SLIPPAGE)*100; trds += 1; break
                        elif h >= tp2: 
                            p_tp1 = ((tp1/entry)-1-SLIPPAGE)*100
                            p_tp2 = ((tp2/entry)-1-SLIPPAGE)*100
                            prof += (p_tp1 + p_tp2)/2; trds += 1; break
                    else:
                        if h >= sl: 
                            prof += (1-(sl/entry)-SLIPPAGE)*100; trds += 1; break
                        elif l <= tp2: 
                            p_tp1 = (1-(tp1/entry)-SLIPPAGE)*100
                            p_tp2 = (1-(tp2/entry)-SLIPPAGE)*100
                            prof += (p_tp1 + p_tp2)/2; trds += 1; break
        candidates.append({'params': p, 'profit': prof, 'trade_count': trds})
    
    top_candidates = sorted(candidates, key=lambda x: x['profit'], reverse=True)[:precise_check]
    precise_results = []
    for cand in top_candidates:
        total_profit, trades = run_precise_backtest(df, df_15m, cand['params'], side, rival_dfs)
        trade_count = len(trades)
        adj_profit = total_profit * 0.1 if trade_count < 5 else total_profit
        precise_results.append({'params': cand['params'], 'profit': adj_profit, 'trade_count': trade_count})
        
    return sorted(precise_results, key=lambda x: x['profit'], reverse=True)[0]
