#!/usr/bin/env python3
"""
monitor.py - Version 15.2 統合戦略監視
変更点:
- 動的フィルター最適化フラグ (RSI/VWAP ON/OFF) を反映
- 実行ログ出力の強化
"""
import json
import logging
import time
import csv
import os
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Set, List
import pytz
import numpy as np
import pandas as pd

from config import (
    WEBHOOK_URL, LOG_FILE, LOG_LEVEL, OUTPUT_CONFIG, DATA_FETCH,
    MONITORING_LOOP, TREND_FILTER, POSITION_MANAGEMENT
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, calculate_ma_from_higher_timeframe,
    send_discord_notification, detect_market_structure, check_trend_filter,
    safe_get
)
from position_manager import PositionManager

# ロギング設定 (UTF-8, 追記モード)
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',
    encoding='utf-8',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

processed_timestamps: Set[str] = set()
position_manager = PositionManager()
last_structure_signals: Dict[str, str] = {}

def load_config() -> Optional[Dict]:
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return None

def check_structure_signal(ticker: str, df: pd.DataFrame):
    global last_structure_signals
    structure = detect_market_structure(df)
    if structure['type']:
        sig_key = f"{ticker}_{structure['type']}_{structure['direction']}"
        if last_structure_signals.get(ticker) != sig_key:
            desc = "トレンド継続" if structure['type'] == 'BOS' else "トレンド転換"
            msg = f"[SIGNAL] {ticker} ｜ 検出：{structure['type']} ({structure['direction']} / {desc})"
            logger.info(msg)
            send_discord_notification(WEBHOOK_URL, msg)
            last_structure_signals[ticker] = sig_key

def check_new_signal(ticker: str, df: pd.DataFrame, detail: Dict):
    if position_manager.has_position(ticker): return
    
    row = df.iloc[-1]
    side_params = detail['params']
    
    # 判定ロジック
    for side in ['long', 'short']:
        params = side_params[side]
        if detail.get(f'{side}_disabled', False): continue
        
        # フィルター最適化フラグの反映
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        
        if params.get('use_rsi_filter', True):
            if side == 'long' and rsi_val >= 70: continue
            if side == 'short' and rsi_val <= 30: continue
            
        if params.get('use_vwap_filter', True):
            if side == 'long' and vwap_dev >= 2.5: continue
            if side == 'short' and vwap_dev <= -2.5: continue
            
        # スコア計算 (簡易版 - backtest_engineと共通)
        score = 0.0
        if side == 'long':
            if rsi_val < 35: score += params['w_rsi'] * 1.5
            if vwap_dev < -1.0: score += params['w_vwap'] * 1.5
        else:
            if rsi_val > 65: score += params['w_rsi'] * 1.5
            if vwap_dev > 1.0: score += params['w_vwap'] * 1.5
            
        if safe_get(row, 'rvol', 1.0) > 1.5: score += params['w_rvol'] * 2.0
        if safe_get(row, 'adx_14', 0) > 25: score += params['w_adx'] * 1.0
        
        if score >= params['threshold']:
            if TREND_FILTER['enabled'] and not check_trend_filter(row['close'], row.get('ma_15m_20', 0), side.upper()): continue
            
            # エントリー実行
            entry_msg = f"[ENTRY] {ticker} ({side.upper()}) ｜ 選定ロジック：{detail['logic_type']} ｜ スコア: {score:.1f}"
            logger.info(entry_msg)
            send_discord_notification(WEBHOOK_URL, entry_msg)
            position_manager.add_position(ticker, side.upper(), row['close'], detail)
            break

def send_daily_summary():
    results_file = POSITION_MANAGEMENT['trade_results_file']
    if not os.path.exists(results_file): return
    try:
        df = pd.read_csv(results_file)
        df['exit_time'] = pd.to_datetime(df['exit_time'])
        today = datetime.now().date()
        df_today = df[df['exit_time'].dt.date == today]
        if df_today.empty: return

        total_profit = df_today['profit_pct'].sum()
        msg = f"📊 **本日の最終結果サマリー**\n\n💰 **総合結果: {total_profit:+.2f}%**\n━━━━━━━━━━━━━━\n"
        for label, count in [("Long(1Month)", 7), ("Short(1Week)", 3)]:
            res = df_today[df_today['logic_type'] == label]
            msg += f"📅 **{label} - {count}銘柄対象**\n"
            if not res.empty:
                for _, r in res.iterrows(): msg += f"• {r['ticker']}: {r['profit_pct']:+.2f}% ({r['exit_reason']})\n"
            else: msg += "• 取引なし\n"
            msg += "\n"
        send_discord_notification(WEBHOOK_URL, msg)
    except Exception as e: logger.error(f"Summary error: {e}")

def monitor():
    config = load_config()
    if not config: return
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    
    start_msg = f"📡 **Version 15.2 統合戦略監視 起動** ({len(tickers)}銘柄)"
    print(start_msg); logger.info(start_msg)
    send_discord_notification(WEBHOOK_URL, start_msg)

    try:
        while True:
            now = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            if now >= dt_time(15, 0):
                prices = {}
                raw_data = fetch_yfinance_data(tickers, period='1d', interval='5m')
                for t in tickers:
                    df = super_flatten_columns(raw_data[t] if len(tickers)>1 else raw_data)
                    if not df.empty: prices[t] = df['close'].iloc[-1]
                results = position_manager.force_close_all(prices, '大引け強制決済')
                for r in results:
                    send_discord_notification(WEBHOOK_URL, f"🛑 [EXIT] {r['ticker']} | 損益: {r['profit_pct']:+.2f}% ({r['logic_type']})")
                send_daily_summary(); break

            try:
                raw_data = fetch_yfinance_data(tickers, period='2d', interval='5m')
                for ticker in tickers:
                    df = super_flatten_columns(raw_data[ticker] if len(tickers)>1 else raw_data)
                    if df.empty: continue
                    check_structure_signal(ticker, df)
                    if position_manager.has_position(ticker):
                        current_price = df['close'].iloc[-1]
                        exit_reason = position_manager.update_price(ticker, current_price)
                        if exit_reason:
                            res = position_manager.close_position(ticker, current_price, exit_reason)
                            send_discord_notification(WEBHOOK_URL, f"🛑 [EXIT] {res['ticker']} | 理由: {exit_reason} | 損益: {res['profit_pct']:+.2f}% ({res['logic_type']})")
                    else:
                        check_new_signal(ticker, df, details[ticker])
            except Exception as e: logger.error(f"Loop error: {e}")
            time.sleep(60)
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    monitor()
