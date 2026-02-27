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

def run_precise_backtest(df_1m: pd.DataFrame, df_inds: pd.DataFrame, params: Dict, side: str, market_df: pd.DataFrame = None) -> Dict:
    if df_1m.empty or df_inds.empty: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}
    
    trades = []; position = None; total_profit = 0.0
    tp1_mul = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    tp1_exit_ratio = POSITION_MANAGEMENT.get('tp1_exit_ratio', 0.5)
    trailing_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
    lookback = DIVERGENCE.get('lookback', 25)

    # df_inds のインデックスをリスト化し、高速に直近の5分足指標を検索できるようにする
    ind_times = df_inds.index

    for idx in range(len(df_1m)):
        row = df_1m.iloc[idx]
        curr_dt = row.name

        # 現在時刻以前の最新の5分足指標を取得 (ポジション保有中もカウンターシグナル判定に使用)
        valid_inds = ind_times[ind_times <= curr_dt]
        if len(valid_inds) == 0: continue
        latest_ind_time = valid_inds[-1]
        ind_row = df_inds.loc[latest_ind_time]

        if position:
            h, l, c = row['high'], row['low'], row['close']
            exit_reason = None

            # 0. タイムディケイ（時間経過）判定
            duration_min = (curr_dt - position['entry_time']).total_seconds() / 60.0
            if duration_min >= 120:
                exit_reason = "TIME_MAX_LIMIT"
            elif duration_min >= 60:
                # カウンターシグナル判定
                opp_side = 'short' if side == 'long' else 'long'
                opp_thresh = params.get('threshold', 50.0)
                try:
                    c_score, _ = calculate_single_score(ind_row, params, opp_side) # パラメータは共通とする
                    if c_score >= opp_thresh:
                        exit_reason = "TIME_DECAY_COUNTER"
                except: pass

            # 1. 最高値/最安値の更新 (トレーリング用)
            if not exit_reason:
                if side == 'long':
                    if c > position['highest_price']:
                        position['highest_price'] = c
                        if position['tp1_hit']:
                            position['sl'] = max(position['sl'], c - (position['atr'] * trailing_mul))
                else:
                    if c < position['lowest_price']:
                        position['lowest_price'] = c
                        if position['tp1_hit']:
                            position['sl'] = min(position['sl'], c + (position['atr'] * trailing_mul))

                # 2. TP1ヒット判定
                if not position['tp1_hit']:
                    if (side == 'long' and h >= position['tp1']) or (side == 'short' and l <= position['tp1']):
                        position['tp1_hit'] = True
                        # TP1損益を記録
                        if side == 'long':
                            position['tp1_profit'] = ((position['tp1'] / position['entry_price']) - 1 - SLIPPAGE) * 100
                        else:
                            position['tp1_profit'] = (1 - (position['tp1'] / position['entry_price']) - SLIPPAGE) * 100
                        # TP1後はリスクを半分以下(建値付近)に縮小
                        if side == 'long': position['sl'] = max(position['sl'], position['entry_price'] - (position['atr'] * 0.2))
                        else: position['sl'] = min(position['sl'], position['entry_price'] + (position['atr'] * 0.2))

                # 3. 決済判定 (STOP_LOSS 優先)
                if (side == 'long' and l <= position['sl']) or (side == 'short' and h >= position['sl']):
                    exit_reason = "STOP_LOSS"
                elif idx == len(df_1m) - 1:
                    exit_reason = "SESSION_CLOSE"

            if exit_reason:
                exit_price = position['sl'] if exit_reason == "STOP_LOSS" else c
                remaining_p = ((exit_price / position['entry_price']) - 1 - SLIPPAGE) * 100 if side == 'long' else (1 - (exit_price / position['entry_price']) - SLIPPAGE) * 100

                # TP1到達済み: 50%×TP1損益 + 50%×残ポジション損益
                if position['tp1_hit']:
                    p = (tp1_exit_ratio * position['tp1_profit']) + ((1 - tp1_exit_ratio) * remaining_p)
                else:
                    p = remaining_p

                total_profit += p; trades.append(p)
                position = None
            continue

        # エントリー判定ロジック (ポジションなし時)
        # 【厳格なデータバリデーション】(NaN/0.0ブロック) Monitorと完全同期
        rsi_val = safe_get(ind_row, 'rsi_14', np.nan)
        vwap_dev = safe_get(ind_row, 'vwap_dev', np.nan)
        ma15 = safe_get(ind_row, 'ma_15m_20', np.nan)
        adx_val = safe_get(ind_row, 'adx_14', np.nan)
        
        if pd.isna(rsi_val) or pd.isna(vwap_dev) or pd.isna(ma15) or pd.isna(adx_val) or adx_val == 0.0:
            continue # 指標データが不完全な場合はスコアリングを完全ブロック

        # トレンドフィルター (MA15による乖離判定)
        if params.get('use_trend_filter', True):
            price_for_ma = row['close']
            if not check_trend_filter(price_for_ma, ma15, params, side):
                continue
                
        # --- 地合いシミュレーション (Monitorと同期) ---
        if market_df is not None:
            try:
                # バックテスト期間内の当日寄り付き比騰落を計算
                m_row = market_df.asof(curr_dt)
                day_start = curr_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                m_open = market_df.asof(day_start)['close']
                m_chg = (m_row['close'] / m_open) - 1
                
                # 悪地合い時の逆行エントリーを遮断
                if side == 'long' and m_chg < -0.008: continue
                if side == 'short' and m_chg > 0.008: continue
            except: pass

        # エントリーフィルター (SIGNAL_THRESHOLDS参照)
        st = SIGNAL_THRESHOLDS
        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= st['rsi_filter_long_max']) or (side == 'short' and rsi_val <= st['rsi_filter_short_min']): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= st['vwap_filter_max']) or (side == 'short' and vwap_dev <= -st['vwap_filter_max']): continue

        # ダイバージェンス算出 (その時点までの過去5分足データを使用)
        div_res = None
        ind_idx = df_inds.index.get_loc(latest_ind_time)
        if isinstance(ind_idx, slice): ind_idx = ind_idx.stop - 1 # 重複対処
        elif isinstance(ind_idx, np.ndarray): ind_idx = np.where(ind_idx)[0][-1]
        
        if ind_idx >= lookback:
            div_res = check_divergence(df_inds.iloc[ind_idx-lookback:ind_idx+1])

        score, _ = calculate_single_score(ind_row, params, side, div_res=div_res)
        if score >= params['threshold']:
            atr = ind_row['atr_14']
            if atr <= 0 or pd.isna(atr): continue
            
            # SL下限ガードを適用 (Monitorと同期)
            actual_sl_mul = max(params['sl_mul'], RISK_MANAGEMENT.get('min_sl_multiplier', 0.7))
            
            position = {
                'entry_price': row['close'], 'entry_time': curr_dt, 'atr': atr, 
                'tp1_hit': False, 'tp1_profit': 0.0,
                'highest_price': row['close'], 'lowest_price': row['close'],
                'tp1': row['close'] + (atr * tp1_mul) if side == 'long' else row['close'] - (atr * tp1_mul),
                'sl': row['close'] - (atr * actual_sl_mul) if side == 'long' else row['close'] + (atr * actual_sl_mul)
            }

    trade_count = len(trades)
    if trade_count == 0: return {'profit': 0.0, 'rr_score': 0.0, 'fitness': 0.0, 'trade_count': 0}

    # --- 改善されたフィットネス関数 ---
    # 問題: 旧式 fitness = total_profit * rr_efficiency * log(trade_count) は
    #       「取引を増やせば有利」な構造で、低閾値・多エントリー設定が優位になっていた。
    # 改善: 勝率 × プロフィットファクター × 適切取引数ペナルティ で評価する。

    win_count = sum(1 for t in trades if t > 0)
    win_rate  = win_count / trade_count

    gross_profit = sum(t for t in trades if t > 0)
    gross_loss   = -sum(t for t in trades if t < 0)
    profit_factor = gross_profit / (gross_loss + 1e-6)  # ゼロ除算防止
    profit_factor = min(profit_factor, 4.0)              # 上限キャップ（外れ値対策）

    # 理想取引数ウィンドウ: 月次3〜12件, 週次2〜6件
    # 範囲外は取引数に比例してペナルティ（過剰取引を抑制）
    ideal_min, ideal_max = 3, 12
    if trade_count < ideal_min:
        trade_factor = max(0.3, trade_count / ideal_min)   # 少なすぎは軽微ペナルティ
    elif trade_count > ideal_max:
        over = trade_count - ideal_max
        trade_factor = max(0.2, 1.0 - 0.05 * over)        # 多すぎは急速にペナルティ
    else:
        trade_factor = 1.0

    # sl_mulが実効値(min_sl_multiplier=0.7)に切り上げられるため、
    # rr_efficiencyの計算も実効SL倍率で行う
    effective_sl = max(params['sl_mul'], RISK_MANAGEMENT.get('min_sl_multiplier', 0.7))
    rr_efficiency = np.mean(trades) / effective_sl

    fitness = total_profit * win_rate * profit_factor * trade_factor
    if trade_count < 2: fitness *= 0.05  # 1件のみは信頼性ゼロ扱い

    return {'profit': total_profit, 'rr_score': rr_efficiency, 'fitness': fitness,
            'trade_count': trade_count, 'win_rate': win_rate, 'profit_factor': profit_factor}

def get_random_params() -> Dict:
    r = PARAM_RANGES
    # 閾値を3ゾーンに均等分散して探索（旧: 下限付近のみ探索し低閾値設定が優位になっていた）
    # 低(30-70): 高頻度シグナル候補 / 中(70-130): バランス候補 / 高(130-180): 厳選候補
    zone = random.choice(['low', 'mid', 'high'])
    threshold_by_zone = {'low': (30, 70), 'mid': (70, 130), 'high': (130, 180)}
    t_min, t_max = threshold_by_zone[zone]
    # PARAM_RANGESの上限を超えないようクリップ
    t_max = min(t_max, r['threshold'][1])
    return {
        'w_rsi': random.randint(r['w_rsi'][0], r['w_rsi'][1]),
        'w_vwap': random.randint(r['w_vwap'][0], r['w_vwap'][1]),
        'w_rvol': random.randint(r['w_rvol'][0], r['w_rvol'][1]),
        'w_adx': random.randint(r['w_adx'][0], r['w_adx'][1]),
        'threshold': random.randint(t_min, t_max),
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
                        iterations: int = 500, precise_check: int = 20, market_df: pd.DataFrame = None) -> Dict:
    candidates = []
    # 1. 初期探索 (30%の試行で多様な種を生成。3ゾーン均等分散で閾値の偏りを排除)
    for _ in range(int(iterations * 0.3)):
        p = get_random_params()
        res = run_precise_backtest(df, df_15m, p, side, market_df=market_df)
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
            res = run_precise_backtest(df, df_15m, p, side, market_df=market_df)
            candidates.append({'params': p, **res})
        
    best = sorted(candidates, key=lambda x: x['fitness'], reverse=True)[0]

    # 3. 閾値下限ガード: MIN_SCORE_THRESHOLDを適用
    #    最適化結果が極端に低い閾値を出力しても、best_config.jsonへの書き込みを防ぐ
    MIN_THRESHOLD = PARAM_RANGES.get('threshold', (10, 200))[0]  # config定義の下限
    PRACTICAL_MIN = 30  # 実運用上の最低閾値 (過剰エントリーを防ぐ安全ネット)
    effective_min = max(MIN_THRESHOLD, PRACTICAL_MIN)
    if best['params']['threshold'] < effective_min:
        logger.warning(
            f"Optimizer returned threshold={best['params']['threshold']}, "
            f"applying floor={effective_min} to prevent over-trading."
        )
        best['params']['threshold'] = effective_min

    return best
