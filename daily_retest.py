#!/usr/bin/env python3
"""
daily_retest.py - 当日実データを用いたロジック精査・改善検証ツール
"""
import json
import logging
import os
import sys
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time as dt_time
import pytz
import time

# 既存モジュールからのインポート
from config import (
    OUTPUT_CONFIG, SIGNAL_THRESHOLDS, POSITION_MANAGEMENT,
    RISK_MANAGEMENT, SECTOR_ALIGNMENT, DIVERGENCE, TREND_FILTER
)
from utils import (
    safe_get, calculate_technical_indicators,
    super_flatten_columns, check_trend_filter, calculate_ma_from_higher_timeframe,
    fetch_macro_sentiment
)
from backtest_engine import calculate_single_score

# ─── ログ設定 (retest_log.txt への全集約) ────────────────
RETEST_LOG = "retest_log.txt"

def init_log():
    with open(RETEST_LOG, 'w', encoding='utf-8') as f:
        f.write("=== Daily Retest Session ===\n")

logging.basicConfig(
    filename=RETEST_LOG, filemode='a', encoding='utf-8',
    level=logging.INFO,
    format='%(asctime)s - [RETEST] - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def log_virtual_file_write(filename, data):
    logger.info(f"--- VIRTUAL WRITE TO {filename} ---")
    logger.info(json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, dict) else str(data))
    logger.info("------------------------------------")

def run_retest(target_date_str: str = None):
    init_log()
    tz = pytz.timezone('Asia/Tokyo')
    if target_date_str is None:
        target_date_str = datetime.now(tz).strftime('%Y-%m-%d')
    
    print(f"[*] {target_date_str} の実データによるロジック再検証を開始...")
    logger.info(f"Retest Target Date: {target_date_str}")

    if not os.path.exists(OUTPUT_CONFIG):
        logger.error(f"Config file {OUTPUT_CONFIG} not found.")
        return
    
    with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
        config_data = json.load(f)
    
    details = {d['t']: d for d in config_data['details']}
    tickers = list(details.keys())
    
    sentiment = fetch_macro_sentiment()
    ms_val = sentiment.get('market_sentiment', 0.0)
    logger.info(f"Retest Macro Sentiment: {ms_val}")

    print(f"[*] {len(tickers)} 銘柄の当日データを取得中...")
    raw_data = yf.download(tickers, period='5d', interval='1m', progress=False, group_by='ticker')
    
    total_results = []
    
    for ticker in tickers:
        logger.info(f">>> Processing {ticker} <<<")
        try:
            ticker_data = raw_data[ticker] if len(tickers) > 1 else raw_data
        except KeyError:
            logger.error(f"Ticker {ticker} not found in raw_data")
            continue
            
        df_1m = super_flatten_columns(ticker_data)
        df_1m = df_1m[df_1m.index.strftime('%Y-%m-%d') == target_date_str]
        if df_1m.empty: 
            logger.warning(f"No data for {ticker} on {target_date_str}")
            continue

        active_pos = None
        trades = []
        detail = details[ticker]
        side_params = detail['params']
        
        df_5m = df_1m.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
        if df_5m.empty: continue
        df_inds = calculate_technical_indicators(df_5m)
        df_inds['ma_15m_20'] = calculate_ma_from_higher_timeframe(df_1m, 20)
        logger.info(f"{ticker}: df_inds cols={list(df_inds.columns)}, rows={len(df_inds)}, adx_min={df_inds['adx_14'].min() if 'adx_14' in df_inds.columns else 'N/A'}")

        for i in range(len(df_1m)):
            curr_time = df_1m.index[i]
            curr_price = df_1m['close'].iloc[i]
            h, l = df_1m['high'].iloc[i], df_1m['low'].iloc[i]
            
            # 常に最新の指標を利用するために先頭で引く
            idx_5m = df_inds.index.asof(curr_time)
            if pd.isna(idx_5m): continue
            row_5m = df_inds.loc[idx_5m]
            
            if active_pos:
                exit_reason = None
                exit_price = curr_price
                
                # 0. タイムディケイ（時間経過）判定
                duration_min = (curr_time - active_pos['entry_time']).total_seconds() / 60.0
                if duration_min >= 120:
                    exit_reason = "TIME_MAX_LIMIT"
                elif duration_min >= 60:
                    opp_side = 'short' if active_pos['side'] == 'LONG' else 'long'
                    opp_params = side_params.get(opp_side, {})
                    opp_thresh = opp_params.get('threshold', 50.0)
                    try:
                        c_score, _ = calculate_single_score(row_5m, opp_params, opp_side)
                        if c_score >= opp_thresh:
                            exit_reason = "TIME_DECAY_COUNTER"
                    except: pass
                
                if not exit_reason:
                    if not active_pos['tp1_hit']:
                        if (active_pos['side'] == 'LONG' and h >= active_pos['tp1']) or (active_pos['side'] == 'SHORT' and l <= active_pos['tp1']):
                            active_pos['tp1_hit'] = True
                            active_pos['tp1_profit'] = ((active_pos['tp1']/active_pos['entry_price'])-1-0.003)*100 if active_pos['side'] == 'LONG' else (1-(active_pos['tp1']/active_pos['entry_price'])-0.003)*100
                            active_pos['sl'] = active_pos['entry_price']
                    if (active_pos['side'] == 'LONG' and l <= active_pos['sl']) or (active_pos['side'] == 'SHORT' and h >= active_pos['sl']):
                        exit_reason = "STOP_LOSS"; exit_price = active_pos['sl']
                    elif i == len(df_1m) - 1:
                        exit_reason = "SESSION_CLOSE"; exit_price = curr_price
                
                if exit_reason:
                    rem_p = ((exit_price/active_pos['entry_price'])-1-0.003)*100 if active_pos['side'] == 'LONG' else (1-(exit_price/active_pos['entry_price'])-0.003)*100
                    final_p = (0.5 * active_pos.get('tp1_profit', 0.0) + 0.5 * rem_p) if active_pos['tp1_hit'] else rem_p
                    trades.append({'ticker': ticker, 'side': active_pos['side'], 'profit': final_p, 'reason': exit_reason, 'entry_time': active_pos['entry_time'], 'exit_time': curr_time})
                    active_pos = None
                    continue # 次のローソクへ
            
            for side in ['long', 'short']:
                params = side_params[side]
                if detail.get(f'{side}_disabled', False): continue
                st = SIGNAL_THRESHOLDS
                rsi_val = safe_get(row_5m, 'rsi_14', np.nan)
                vwap_dev = safe_get(row_5m, 'vwap_dev', np.nan)
                ma15 = safe_get(row_5m, 'ma_15m_20', 0.0)
                adx_val = safe_get(row_5m, 'adx_14', 0.0)
                rvol_val = safe_get(row_5m, 'rvol', 1.0)

                # --- 厳格なデータバリデーション (NaN, adx=0.0のブロック) ---
                if pd.isna(rsi_val) or pd.isna(vwap_dev) or pd.isna(ma15) or adx_val == 0.0:
                    continue

                if params.get('use_rsi_filter', True):
                    if (side == 'long' and rsi_val >= st['rsi_filter_long_max']) or (side == 'short' and rsi_val <= st['rsi_filter_short_min']): continue
                if params.get('use_vwap_filter', True):
                    if (side == 'long' and vwap_dev >= st['vwap_filter_max']) or (side == 'short' and vwap_dev <= -st['vwap_filter_max']): continue
                
                # トレンドフィルター
                if TREND_FILTER['enabled']:
                    if not check_trend_filter(curr_price, ma15, side.upper()): continue

                score = 0.0
                if side == 'long':
                    if rsi_val < st['rsi_oversold']: score += params['w_rsi'] * 1.5
                    elif rsi_val < st['rsi_buy_moderate']: score += params['w_rsi'] * 1.0
                    if vwap_dev < -st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
                    elif vwap_dev < -st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0
                else:
                    if rsi_val > st['rsi_overbought']: score += params['w_rsi'] * 1.5
                    elif rsi_val > st['rsi_sell_moderate']: score += params['w_rsi'] * 1.0
                    if vwap_dev > st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
                    elif vwap_dev > st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0
                
                rvol_val = safe_get(row_5m, 'rvol', 1.0)
                if rvol_val > st['rvol_strong']: score += params['w_rvol'] * 2.0
                elif rvol_val > st['rvol_moderate']: score += params['w_rvol'] * 1.0
                
                adx_val = safe_get(row_5m, 'adx_14', 0)
                if adx_val > st['adx_strong']: score += params['w_adx'] * 1.5
                elif adx_val > st['adx_moderate']: score += params['w_adx'] * 1.0

                threshold_add = 0.0
                if ms_val < RISK_MANAGEMENT.get('sentiment_brake_threshold', -0.3):
                    threshold_add += RISK_MANAGEMENT.get('sentiment_brake_penalty', 15.0)
                elif ms_val > 0.3:
                    threshold_add -= 5.0
                actual_threshold = params['threshold'] + threshold_add

                if score >= actual_threshold:
                    if (side == 'long' and ms_val < -0.2) or (side == 'short' and ms_val > 0.2): continue
                    
                    # セクター連動確認 (リテスト内簡易版) - 歴史データのためライバル銘柄データは利用不可のときはスキップ
                    rivals = detail.get('rivals', [])
                    if SECTOR_ALIGNMENT['enabled'] and rivals:
                        aligned = 0
                        for r_t in rivals:
                            try:
                                r_df = raw_data[r_t] if len(tickers)>1 else raw_data
                                r_today = r_df[r_df.index.strftime('%Y-%m-%d') == target_date_str]
                                if not r_today.empty:
                                    r_curr = r_today['Close'].asof(curr_time)
                                    r_open = r_today['Close'].iloc[0]
                                    chg = (r_curr / r_open) - 1
                                    if (side=='long' and chg>0) or (side=='short' and chg<0): aligned += 1
                            except: continue
                        if aligned < SECTOR_ALIGNMENT.get('min_aligned_rivals', 1): continue
                    
                    atr = row_5m['atr_14']
                    min_sl = RISK_MANAGEMENT.get('min_sl_multiplier', 0.7)
                    actual_sl_mul = max(params['sl_mul'], min_sl)
                    active_pos = {
                        'ticker': ticker, 'side': side.upper(), 'entry_price': curr_price,
                        'entry_time': curr_time, 'sl': curr_price - (atr * actual_sl_mul) if side == 'long' else curr_price + (atr * actual_sl_mul),
                        'tp1': curr_price + (atr * 1.5) if side == 'long' else curr_price - (atr * 1.5),
                        'tp1_hit': False, 'tp1_profit': 0.0
                    }
                    break
        total_results.extend(trades)

    print("\n" + "="*45)
    print(f"[*] {target_date_str} リテスト損益結果サマリー")
    print("="*45)
    if not total_results:
        print("シグナル発生なし")
    else:
        df_res = pd.DataFrame(total_results)
        print(f"合計損益: {df_res['profit'].sum():+.2f}%")
        print(f"取引件数: {len(df_res)} 件 (勝率: {(df_res['profit']>0).mean()*100:.1f}%)")
        print("\n\n=== 最終結果 ===")
        for _, r in df_res.iterrows():
            print(f"[{r['entry_time'].strftime('%H:%M')}] {r['ticker']} ({r['side']}): {r['profit']:+.2f}% ({r['reason']})")
    print("="*45)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    run_retest(target)
