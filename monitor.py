#!/usr/bin/env python3
"""
monitor.py - Version 15.14 統合戦略監視 (最終安定版)
変更点:
- 監視ループ内に指標計算 (RSI, ATR, VWAP等) と 15分MA計算を追加 (致命的バグ修正)
- PositionManager.get_position への対応
- 全通知フォーマットの再確認と安定化
"""
import json
import logging
import time
import csv
import os
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Set, List, Any
import pytz
import numpy as np
import pandas as pd

from config import (
    WEBHOOK_URL, LOG_FILE, LOG_LEVEL, OUTPUT_CONFIG, DATA_FETCH,
    MONITORING_LOOP, TREND_FILTER, POSITION_MANAGEMENT, SIGNAL_THRESHOLDS
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, calculate_ma_from_higher_timeframe,
    send_discord_notification, detect_market_structure, check_trend_filter,
    safe_get, fetch_macro_sentiment
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
last_structure_signals: Dict[str, str] = {}
current_macro_adjustments: Dict[str, Any] = {}

def load_config() -> Optional[Dict]:
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return None

def apply_macro_adjustments(sentiment: Dict[str, float]):
    global current_macro_adjustments
    vix = sentiment.get('vix_value', 18.0)
    if vix > 20:
        adjustment_msg = "ℹ️ **市場ボラティリティ上昇検知 (VIX > 20)**\n"
        adjustment_msg += "リスク管理のため以下の調整を自動適用しました：\n"
        adjustment_msg += "• エントリー閾値: +5.0 (厳格化)\n"
        adjustment_msg += "• 利確幅(TP): ×1.25 (拡大)\n"
        adjustment_msg += "• 損切幅(SL): ×1.15 (拡大)"
        current_macro_adjustments = {'threshold_add': 5.0, 'tp_mul': 1.25, 'sl_mul': 1.15}
        send_discord_notification(WEBHOOK_URL, adjustment_msg)
    else:
        current_macro_adjustments = {'threshold_add': 0.0, 'tp_mul': 1.0, 'sl_mul': 1.0}

def check_structure_signal(ticker: str, df: pd.DataFrame):
    global last_structure_signals
    structure = detect_market_structure(df)
    if structure['type']:
        sig_key = f"{ticker}_{structure['type']}_{structure['direction']}"
        if last_structure_signals.get(ticker) != sig_key:
            desc = "トレンド継続" if structure['type'] == 'BOS' else "トレンド転換"
            emoji = "📈" if structure['direction'] == 'LONG' else "📉"
            msg = (f"{emoji} **[STRUCTURE] {ticker}**\n"
                   f"検出：{structure['type']} ({structure['direction']})\n"
                   f"状況：{desc}\n"
                   f"価格：¥{structure['price']:,.1f}")
            send_discord_notification(WEBHOOK_URL, msg)
            last_structure_signals[ticker] = sig_key

def check_new_signal(ticker: str, df: pd.DataFrame, detail: Dict):
    if position_manager.has_position(ticker): return
    
    row = df.iloc[-1]
    side_params = detail['params']
    tp1_mul_base = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    trailing_atr_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
    adj = current_macro_adjustments
    
    for side in ['long', 'short']:
        params = side_params[side]
        if detail.get(f'{side}_disabled', False): continue
        
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= 75) or (side == 'short' and rsi_val <= 25): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= 3.0) or (side == 'short' and vwap_dev <= -3.0): continue
            
        score = 0.0
        if side == 'long':
            if rsi_val < 40: score += params['w_rsi'] * 1.2
            if vwap_dev < -0.5: score += params['w_vwap'] * 1.2
        else:
            if rsi_val > 60: score += params['w_rsi'] * 1.2
            if vwap_dev > 0.5: score += params['w_vwap'] * 1.2
        if safe_get(row, 'rvol', 1.0) > 1.8: score += params['w_rvol'] * 2.5
        if safe_get(row, 'adx_14', 0) > 25: score += params['w_adx'] * 2.0
        
        if score >= (params['threshold'] + adj.get('threshold_add', 0)):
            if TREND_FILTER['enabled']:
                ma15 = row.get('ma_15m_20', 0)
                if not check_trend_filter(row['close'], ma15, side.upper()): continue

            atr = row.get('atr_14', row['close'] * 0.02)
            detail['atr'] = atr
            entry_price = row['close']
            
            sl_dist = atr * params['sl_mul'] * adj.get('sl_mul', 1.0)
            sl = entry_price - sl_dist if side == 'long' else entry_price + sl_dist
            tp1_dist = atr * tp1_mul_base * adj.get('tp_mul', 1.0)
            tp1 = entry_price + tp1_dist if side == 'long' else entry_price - tp1_dist
            
            msg = (f"🛡️ **新規シグナル (Ver 15.14): {side.upper()}**\n"
                   f"銘柄: {ticker} ({detail['logic_type']})\n"
                   f"価格: ¥{entry_price:,.1f}\n"
                   f"TP1: ¥{tp1:,.1f} (ATR×{tp1_mul_base * adj.get('tp_mul', 1.0):.1f}) → 50%決済\n"
                   f"TP2: トレーリング (ATR×{trailing_atr_mul}幅)\n"
                   f"SL: ¥{sl:,.1f}\n"
                   f"スコア: {score:.1f} (RSI:{rsi_val:.1f}, VWAP:{vwap_dev:.2f})")
            
            send_discord_notification(WEBHOOK_URL, msg)
            position_manager.add_position(ticker, side.upper(), entry_price, detail)
            break

def monitor_positions(ticker: str, current_price: float):
    event = position_manager.update_price(ticker, current_price)
    pos = position_manager.get_position(ticker)
    if not pos: return

    if event == 'TP1_HIT':
        trailing_atr_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
        profit = ((current_price / pos['entry_price'] - 1) * 100) if pos['side'] == 'LONG' else ((1 - current_price / pos['entry_price']) * 100)
        msg = (f"✅ **TP1達成: {ticker}**\n"
               f"🎯 50%利確完了\n"
               f"・価格: ¥{current_price:,.1f}\n"
               f"・損益: {profit:+.2f}%\n"
               f"・リスクを半分に縮小しました\n"
               f"・残り50%はトレーリングTP (ATR×{trailing_atr_mul}) で追従中")
        send_discord_notification(WEBHOOK_URL, msg)
    elif event == 'STOP_LOSS':
        res = position_manager.close_position(ticker, current_price, 'STOP_LOSS')
        msg = (f"🛑 **[EXIT] {res['ticker']}**\n"
               f"理由：STOP_LOSS (逆指値決済)\n"
               f"損益：{res['profit_pct']:+.2f}% ({res['logic_type']})\n"
               f"決済単価：¥{res['exit_price']:,.1f}")
        send_discord_notification(WEBHOOK_URL, msg)

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
        msg = f"📊 **本日の最終結果サマリー**\n\n💰 **総合損益: {total_profit:+.2f}%**\n━━━━━━━━━━━━━━\n"
        for label in ["Monthly", "Weekly"]:
            res = df_today[df_today['logic_type'] == label]
            msg += f"📅 **{label} 戦略結果**\n"
            if not res.empty:
                for _, r in res.iterrows():
                    msg += f"• {r['ticker']} ({r['side']}): {r['profit_pct']:+.2f}% [{r['exit_reason']}]\n"
            else: msg += "• 取引なし\n"
            msg += "\n"
        send_discord_notification(WEBHOOK_URL, msg)
    except Exception as e: logger.error(f"Summary error: {e}")

def monitor():
    config = load_config()
    if not config: return
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    
    sentiment = fetch_macro_sentiment()
    start_msg = (f"📡 **Version 15.14 統合戦略監視 起動**\n"
                 f"━━━━━━━━━━━━━━\n"
                 f"🌍 **マクロ地合い情報**:\n"
                 f"• VIX: {sentiment['vix_value']} ({sentiment['vix_chg']:+.2f}%)\n"
                 f"• SOX: {sentiment['sox_chg']:+.2f}%\n"
                 f"• JPY: {sentiment['jpy_chg']:+.2f}%\n\n"
                 f"🎯 **監視対象銘柄**: {', '.join(tickers)}")
    send_discord_notification(WEBHOOK_URL, start_msg)
    apply_macro_adjustments(sentiment)

    try:
        while True:
            now = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            if now >= dt_time(15, 0):
                raw_data = fetch_yfinance_data(tickers, period='1d', interval='5m')
                prices = {}
                for t in tickers:
                    df_raw = raw_data[t] if len(tickers)>1 else raw_data
                    df_flat = super_flatten_columns(df_raw)
                    if not df_flat.empty: prices[t] = df_flat['close'].iloc[-1]
                results = position_manager.force_close_all(prices, '大引け強制決済')
                for r in results:
                    send_discord_notification(WEBHOOK_URL, f"🛑 **[EXIT] {r['ticker']}**\n理由：大引け強制決済\n損益：{r['profit_pct']:+.2f}% ({r['logic_type']})")
                send_daily_summary(); break

            try:
                raw_data = fetch_yfinance_data(tickers, period='2d', interval='5m')
                for ticker in tickers:
                    ticker_data = raw_data[ticker] if len(tickers) > 1 else raw_data
                    df = super_flatten_columns(ticker_data)
                    if df.empty: continue
                    
                    # テクニカル指標計算 (RSI, ATR, VWAP等) を追加
                    df = calculate_technical_indicators(df)
                    # 15分MA計算を追加
                    df['ma_15m_20'] = calculate_ma_from_higher_timeframe(df, 20)
                    
                    check_structure_signal(ticker, df)
                    if position_manager.has_position(ticker):
                        monitor_positions(ticker, df['close'].iloc[-1])
                    else:
                        check_new_signal(ticker, df, details[ticker])
            except Exception as e: logger.error(f"Loop error: {e}")
            time.sleep(60)
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    monitor()
