#!/usr/bin/env python3
"""
monitor.py - Version 15.1 統合戦略監視
変更点:
- 長期（1ヶ月）/短期（1週間）ロジックの統合監視
- 損切りロジックの刷新（固定-5% / トレーリング-5%）
- BOS / CHoCH 独立検知シグナル
- 詳細サマリー通知（統合・長期個別・短期個別）
- 実行ログ出力の強化 (execution_log.txtへの追記)
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
    send_discord_notification, detect_market_structure, check_trend_filter
)
from position_manager import PositionManager

# ロギング設定 (追記モード)
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

processed_timestamps: Set[str] = set()
position_manager = PositionManager()
last_structure_signals: Dict[str, str] = {} # 銘柄ごとの最新構造シグナル

def load_config() -> Optional[Dict]:
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return None

def check_structure_signal(ticker: str, df: pd.DataFrame):
    """BOS / CHoCH の検知と通知"""
    global last_structure_signals
    structure = detect_market_structure(df)
    if structure['type']:
        sig_key = f"{ticker}_{structure['type']}_{structure['direction']}"
        # 重複通知防止
        if last_structure_signals.get(ticker) != sig_key:
            desc = "トレンド継続" if structure['type'] == 'BOS' else "トレンド転換"
            msg = f"[SIGNAL] {ticker} ｜ 検出：{structure['type']} ({structure['direction']} / {desc})"
            logger.info(msg)
            send_discord_notification(WEBHOOK_URL, msg)
            last_structure_signals[ticker] = sig_key

def check_new_signal(ticker: str, df: pd.DataFrame, detail: Dict):
    """エントリー判定 (実装の枠組み)"""
    if position_manager.has_position(ticker): return
    pass

def send_daily_summary():
    """詳細サマリー通知"""
    results_file = POSITION_MANAGEMENT['trade_results_file']
    if not os.path.exists(results_file): return

    df = pd.read_csv(results_file)
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    today = datetime.now().date()
    df_today = df[df['exit_time'].dt.date == today]

    if df_today.empty:
        msg = "📊 本日の取引はありませんでした。"
        logger.info(msg)
        send_discord_notification(WEBHOOK_URL, msg)
        return

    total_profit = df_today['profit_pct'].sum()
    
    # ロジック別集計
    long_results = df_today[df_today['logic_type'] == 'Long(1Month)']
    short_results = df_today[df_today['logic_type'] == 'Short(1Week)']

    msg = f"📊 **本日の最終結果サマリー**\n\n"
    msg += f"💰 **総合結果: {total_profit:+.2f}%**\n"
    msg += f"━━━━━━━━━━━━━━\n"
    
    msg += f"📅 **長期ロジック (1Month) - 7銘柄対象**\n"
    if not long_results.empty:
        for _, r in long_results.iterrows():
            msg += f"• {r['ticker']}: {r['profit_pct']:+.2f}% ({r['exit_reason']})\n"
    else: msg += "• 取引なし\n"
    
    msg += f"\n⏱️ **短期ロジック (1Week) - 3銘柄対象**\n"
    if not short_results.empty:
        for _, r in short_results.iterrows():
            msg += f"• {r['ticker']}: {r['profit_pct']:+.2f}% ({r['exit_reason']})\n"
    else: msg += "• 取引なし\n"

    logger.info("日次サマリー送信完了")
    send_discord_notification(WEBHOOK_URL, msg)

def monitor():
    config = load_config()
    if not config: 
        logger.error("設定ファイルの読み込みに失敗しました。")
        return
    
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    
    start_msg = "📡 **Version 15.1 統合戦略監視 起動**"
    print(start_msg); logger.info(start_msg)
    send_discord_notification(WEBHOOK_URL, start_msg)

    try:
        while True:
            # 取引時間チェック
            now = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            if now >= dt_time(15, 0):
                msg = "大引け時刻に達しました。強制決済を実行します。"
                print(msg); logger.info(msg)
                
                prices = {}
                raw_data = fetch_yfinance_data(tickers, period='1d', interval='5m')
                for t in tickers:
                    df = super_flatten_columns(raw_data[t] if len(tickers)>1 else raw_data)
                    if not df.empty: prices[t] = df['close'].iloc[-1]
                
                results = position_manager.force_close_all(prices, '大引け強制決済')
                for r in results:
                    exit_msg = f"🛑 [EXIT] {r['ticker']} | 損益: {r['profit_pct']:+.2f}% ({r['logic_type']})"
                    logger.info(exit_msg)
                    send_discord_notification(WEBHOOK_URL, exit_msg)
                
                send_daily_summary()
                break

            # データ取得と監視
            try:
                raw_data = fetch_yfinance_data(tickers, period='2d', interval='5m')
                for ticker in tickers:
                    df = super_flatten_columns(raw_data[ticker] if len(tickers)>1 else raw_data)
                    if df.empty: continue
                    
                    # 1. BOS / CHoCH 検知
                    check_structure_signal(ticker, df)
                    
                    # 2. 保有ポジションの監視 (損切り判定)
                    if position_manager.has_position(ticker):
                        current_price = df['close'].iloc[-1]
                        exit_reason = position_manager.update_price(ticker, current_price)
                        if exit_reason:
                            res = position_manager.close_position(ticker, current_price, exit_reason)
                            exit_msg = f"🛑 [EXIT] {res['ticker']} | 理由: {exit_reason} | 損益: {res['profit_pct']:+.2f}% ({res['logic_type']})"
                            logger.info(exit_msg)
                            send_discord_notification(WEBHOOK_URL, exit_msg)
                    
            except Exception as e:
                logger.error(f"ループ内エラー: {str(e)}")

            time.sleep(60)

    except KeyboardInterrupt:
        msg = "監視を中断しました。"
        print(msg); logger.info(msg)

if __name__ == "__main__":
    monitor()
