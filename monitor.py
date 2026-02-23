#!/usr/bin/env python3
"""
monitor.py - Version 15.10 統合戦略監視
変更点:
- ATRベースの動的SL/TPをPositionManagerに渡すよう修正
- エントリー判定ロジックをVer 15.10と完全同期
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

# ロギング設定
logging.basicConfig(
    filename=LOG_FILE, filemode='a', encoding='utf-8',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

position_manager = PositionManager()

def load_config() -> Optional[Dict]:
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return None

def check_new_signal(ticker: str, df: pd.DataFrame, detail: Dict):
    if position_manager.has_position(ticker): return
    
    row = df.iloc[-1]
    side_params = detail['params']
    
    for side in ['long', 'short']:
        params = side_params[side]
        if detail.get(f'{side}_disabled', False): continue
        
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        
        if params.get('use_rsi_filter', True):
            if side == 'long' and rsi_val >= 75: continue
            if side == 'short' and rsi_val <= 25: continue
            
        if params.get('use_vwap_filter', True):
            if side == 'long' and vwap_dev >= 3.0: continue
            if side == 'short' and vwap_dev <= -3.0: continue
            
        score = 0.0
        if side == 'long':
            if rsi_val < 40: score += params['w_rsi'] * 1.2
            if vwap_dev < -0.5: score += params['w_vwap'] * 1.2
        else:
            if rsi_val > 60: score += params['w_rsi'] * 1.2
            if vwap_dev > 0.5: score += params['w_vwap'] * 1.2
            
        if safe_get(row, 'rvol', 1.0) > 1.8: score += params['w_rvol'] * 2.5
        if safe_get(row, 'adx_14', 0) > 25: score += params['w_adx'] * 2.0
        
        if score >= params['threshold']:
            detail['atr'] = row.get('atr_14', row['close'] * 0.02)
            entry_msg = f"[ENTRY] {ticker} ({side.upper()}) ｜ スコア: {score:.1f} ｜ RSI:{rsi_val:.1f}, VWAP:{vwap_dev:.2f}"
            logger.info(entry_msg)
            send_discord_notification(WEBHOOK_URL, entry_msg)
            position_manager.add_position(ticker, side.upper(), row['close'], detail)
            break

def monitor():
    config = load_config()
    if not config: return
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    
    start_msg = f"📡 **Version 15.10 統合戦略監視 起動** ({len(tickers)}銘柄)"
    print(start_msg); logger.info(start_msg)
    send_discord_notification(WEBHOOK_URL, start_msg)

    try:
        while True:
            now = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            if now >= dt_time(15, 0): break

            try:
                raw_data = fetch_yfinance_data(tickers, period='2d', interval='5m')
                for ticker in tickers:
                    df = super_flatten_columns(raw_data[ticker] if len(tickers)>1 else raw_data)
                    if df.empty: continue
                    if position_manager.has_position(ticker):
                        current_price = df['close'].iloc[-1]
                        exit_reason = position_manager.update_price(ticker, current_price)
                        if exit_reason:
                            res = position_manager.close_position(ticker, current_price, exit_reason)
                            send_discord_notification(WEBHOOK_URL, f"🛑 [EXIT] {res['ticker']} | {exit_reason} | 損益: {res['profit_pct']:+.2f}%")
                    else:
                        check_new_signal(ticker, df, details[ticker])
            except Exception as e: logger.error(f"Loop error: {e}")
            time.sleep(60)
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    monitor()
