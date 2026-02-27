#!/usr/bin/env python3
"""
monitor.py - Version 15.15+ リファクタリング版
変更点:
- スコア加点ボーナスを全廃 (Analyzerとの同期)
- 地合い絶対フィルター (-0.2 / +0.2) 実装
- セクター連動フィルター (yf.downloadによる当日騰落確認) 実装
- SL下限ガード (0.7 ATR) 実装
- 当日再エントリー制限 (STOP_LOSS後禁止) 実装
"""
import json
import logging
import time
import csv
import os
import subprocess
from datetime import datetime, time as dt_time
from typing import Dict, Optional, Set, List, Any
import pytz
import numpy as np
import pandas as pd
import yfinance as yf

from config import (
    WEBHOOK_URL, LOG_FILE, LOG_LEVEL, OUTPUT_CONFIG, DATA_FETCH,
    MONITORING_LOOP, TREND_FILTER, POSITION_MANAGEMENT, SIGNAL_THRESHOLDS,
    RISK_MANAGEMENT, SECTOR_ALIGNMENT, DIVERGENCE
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, calculate_ma_from_higher_timeframe,
    send_discord_notification, detect_market_structure, check_trend_filter,
    safe_get, fetch_macro_sentiment, check_divergence
)
from position_manager import PositionManager
from backtest_engine import calculate_single_score

# ロギング設定
logging.basicConfig(
    filename=LOG_FILE, filemode='a', encoding='utf-8',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ライブラリのノイズを抑制
logging.getLogger('yfinance').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

position_manager = PositionManager()
last_structure_signals: Dict[str, str] = {}
current_macro_adjustments: Dict[str, Any] = {}
current_macro_sentiment: Dict[str, Any] = {}
sector_data_cache: Dict[str, Any] = {'data': None, 'timestamp': 0}

def record_trade_journal(entry: Dict) -> None:
    journal_file = 'trade_journal.csv'
    fieldnames = [
        'ticker', 'side', 'entry_price', 'entry_time', 'market_sentiment', 
        'rsi', 'vwap_dev', 'rvol', 'adx', 'ma15_value', 'ma15_diff_pct', 
        'vix_value', 'sox_chg', 'tnx_chg', 'divergence_bullish', 
        'divergence_bearish', 'cooldown_overridden', 'score', 'threshold', 
        'sector_alignment', 'volume_accel', 'divergence_bonus'
    ]
    file_exists = os.path.exists(journal_file)
    try:
        with open(journal_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(entry)
    except Exception as e:
        logger.error(f"Journal error: {e}")

def git_sync(action: str = 'pull'):
    try:
        if action == 'pull':
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        elif action == 'push':
            subprocess.run(["git", "add", "positions.json"], check=True)
            subprocess.run(["git", "commit", "-m", "Update positions [skip ci]"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
    except Exception as e:
        logger.error(f"Git sync error: {e}")

SESSION_TYPE = os.getenv('SESSION_TYPE', 'AM')
# セッションタイプに応じたデフォルト時刻の設定 (AM: 09:30-11:40, PM: 12:30-15:10)
default_start = MONITORING_LOOP['start_time'] if SESSION_TYPE == 'AM' else '12:30'
default_end = '11:40' if SESSION_TYPE == 'AM' else MONITORING_LOOP['end_time']

MONITOR_START_TIME = os.getenv('MONITOR_START_TIME', default_start)
MONITOR_END_TIME = os.getenv('MONITOR_END_TIME', default_end)
ENTRY_CUTOFF_TIME = os.getenv('ENTRY_CUTOFF_TIME', 
                              MONITORING_LOOP['pm_entry_cutoff'] if SESSION_TYPE == 'PM' else MONITORING_LOOP['am_entry_cutoff'])
FORCE_CLOSE_TIME = os.getenv('FORCE_CLOSE_TIME', '14:55' if SESSION_TYPE == 'PM' else '23:59')
SKIP_DAILY_SUMMARY = os.getenv('SKIP_DAILY_SUMMARY', 'false').lower() == 'true'

def load_config() -> Optional[Dict]:
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return None

def apply_macro_adjustments(sentiment: Dict[str, float]):
    global current_macro_adjustments
    vix = sentiment.get('vix_value', 18.0)
    ms = sentiment.get('market_sentiment', 0.0)
    
    threshold_add = 0.0
    tp_mul = 1.0
    sl_mul = 1.0
    
    if ms < RISK_MANAGEMENT.get('sentiment_brake_threshold', -0.3):
        threshold_add += RISK_MANAGEMENT.get('sentiment_brake_penalty', 15.0)
        sl_mul *= 1.2
    elif ms > 0.3:
        threshold_add -= 5.0
        
    if vix > 20:
        adjustment_msg = f"ℹ️ **市場ボラティリティ上昇検知 (VIX:{vix:.1f}, Sentiment:{ms:+.1f})**\n"
        adjustment_msg += "リスク管理調整を自動適用しました：\n"
        adjustment_msg += f"• 閾値修正: {threshold_add:+.1f}\n"
        adjustment_msg += f"• 利確幅倍率: ×{1.25:.2f}\n"
        adjustment_msg += f"• 損切幅倍率: ×{sl_mul * 1.15:.2f}"
        current_macro_adjustments = {'threshold_add': threshold_add, 'tp_mul': 1.25, 'sl_mul': sl_mul * 1.15}
        send_discord_notification(WEBHOOK_URL, adjustment_msg)
    else:
        current_macro_adjustments = {'threshold_add': threshold_add, 'tp_mul': 1.0, 'sl_mul': sl_mul}

def check_structure_signal(ticker: str, df: pd.DataFrame):
    global last_structure_signals
    structure = detect_market_structure(df)
    if structure['type']:
        sig_key = f"{ticker}_{structure['type']}_{structure['direction']}"
        if last_structure_signals.get(ticker) != sig_key:
            desc = "トレンド継続" if structure['type'] == 'BOS' else "トレンド転換"
            emoji = "📈" if structure['direction'] == 'LONG' else "📉"
            msg = (f"{emoji} **[STRUCTURE] {ticker}**\n検出：{structure['type']} ({structure['direction']})\n状況：{desc}\n価格：¥{structure['price']:,.1f}")
            send_discord_notification(WEBHOOK_URL, msg)
            last_structure_signals[ticker] = sig_key

def get_today_closed_trades() -> List[Dict]:
    results_file = POSITION_MANAGEMENT.get('trade_results_file', 'trade_results.csv')
    if not os.path.exists(results_file): return []
    try:
        df = pd.read_csv(results_file, on_bad_lines='skip', engine='python')
        if df.empty: return []
        df['exit_time'] = pd.to_datetime(df['exit_time'], utc=True, errors='coerce')
        df = df.dropna(subset=['exit_time'])
        today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
        today_trades = df[df['exit_time'].dt.tz_convert('Asia/Tokyo').dt.date == today]
        return today_trades[['ticker', 'side', 'exit_reason']].to_dict('records')
    except Exception as e:
        logger.error(f"Error reading today closed trades: {e}")
        return []

def check_new_signal(ticker: str, df: pd.DataFrame, detail: Dict):
    if position_manager.has_position(ticker): return
    
    closed_trades = get_today_closed_trades()
    for trade in closed_trades:
        if trade['ticker'] == ticker: return

    now_jst = datetime.now(pytz.timezone('Asia/Tokyo')).time()
    if now_jst >= dt_time.fromisoformat(ENTRY_CUTOFF_TIME): return

    row = df.iloc[-1]
    side_params = detail['params']
    tp1_mul_base = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    adj = current_macro_adjustments
    ms_val = current_macro_sentiment.get('market_sentiment', 0.0)
    div_res = check_divergence(df) if DIVERGENCE['enabled'] else {'bullish': False, 'bearish': False}

    for side in ['long', 'short']:
        params = side_params[side]
        if detail.get(f'{side}_disabled', False): continue
        
        # 1. 厳格なデータバリデーション (backtest_engine.pyと完全同期)
        rsi_val = safe_get(row, 'rsi_14', np.nan)
        vwap_dev = safe_get(row, 'vwap_dev', np.nan)
        adx_val = safe_get(row, 'adx_14', np.nan)
        if pd.isna(rsi_val) or pd.isna(vwap_dev) or pd.isna(adx_val) or adx_val == 0.0:
            continue  # 指標データ不完全な場合はスキップ

        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= SIGNAL_THRESHOLDS['rsi_filter_long_max']) or (side == 'short' and rsi_val <= SIGNAL_THRESHOLDS['rsi_filter_short_min']): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= SIGNAL_THRESHOLDS['vwap_filter_max']) or (side == 'short' and vwap_dev <= -SIGNAL_THRESHOLDS['vwap_filter_max']): continue

        # 2. テクニカルスコアリング (backtest_engine.calculate_single_scoreと完全同期)
        score, indicators = calculate_single_score(row, params, side, div_res if DIVERGENCE['enabled'] else None)
        rvol_val = indicators.get('rvol', 1.0)
        
        actual_threshold = params['threshold'] + adj.get('threshold_add', 0)
        if score < actual_threshold: continue

        # 2. 【最終関門】属性別・指数連動フィルター (Design 6.1 改良)
        ms = current_macro_sentiment
        sector = detail.get('sector', '')
        size = detail.get('size_category', '')
        
        # A. ハイテク・半導体関連 (SOX連動)
        if side == 'long' and sector in ['電気機器', '精密機器', '機械']:
            if ms.get('sox_chg', 0) < -1.5:
                logger.info(f"Filter blocked {ticker} (High-Tech): SOX too weak ({ms['sox_chg']}%)")
                continue
        
        # B. 大型主力株 (N225/TOPIX連動)
        if side == 'long' and any(x in size for x in ['Core30', 'Large70', 'Mid400']):
            if ms.get('n225_chg', 0) < -0.8 or ms.get('topix_chg', 0) < -0.8:
                logger.info(f"Filter blocked {ticker} (Large-Cap): Market indices too weak")
                continue
        
        # C. 地合い絶対フィルター (小型株は緩和)
        ms_limit = -0.2 if 'Small' not in size else -0.4
        if side == 'long' and ms_val < ms_limit:
            logger.info(f"Filter blocked {ticker}: Sentiment too low ({ms_val}) for limit {ms_limit}")
            continue
        if side == 'short' and ms_val > 0.2: continue

        if SECTOR_ALIGNMENT['enabled']:
            rivals = detail.get('rivals', [])
            if rivals:
                try:
                    # セクターデータのキャッシュ利用 (5分間)
                    global sector_data_cache
                    now = time.time()
                    if sector_data_cache['data'] is None or (now - sector_data_cache['timestamp'] > 300):
                        # 全監視銘柄の全ライバルを一括取得するのが理想だが、ここでは現銘柄のライバルを取得
                        sector_data_cache['data'] = yf.download(rivals, period='1d', interval='1m', progress=False, threads=False)
                        sector_data_cache['timestamp'] = now
                    
                    rival_data = sector_data_cache['data']
                    aligned = 0
                    for r_t in rivals:
                        # マルチインデックス対応
                        r_df = rival_data[r_t] if len(rivals) > 1 else rival_data
                        if not r_df.empty:
                            # 寄り付き比での騰落確認
                            close_col = 'Close' if 'Close' in r_df.columns else r_df.columns[0]
                            chg = (r_df[close_col].iloc[-1] / r_df[close_col].iloc[0]) - 1
                            if (side == 'long' and chg > 0) or (side == 'short' and chg < 0): aligned += 1
                    
                    if aligned < SECTOR_ALIGNMENT.get('min_aligned_rivals', 1):
                        logger.info(f"Filter blocked {ticker} ({side.upper()}): Sector not aligned ({aligned}/{len(rivals)})")
                        continue
                except Exception as e:
                    logger.warning(f"Sector filter error for {ticker}: {e}")

        if TREND_FILTER['enabled']:
            ma15 = row.get('ma_15m_20', 0)
            if not check_trend_filter(row['close'], ma15, side.upper()): continue

        atr = row.get('atr_14', row['close'] * 0.02)
        min_sl = RISK_MANAGEMENT.get('min_sl_multiplier', 0.7)
        actual_sl_mul = max(params['sl_mul'], min_sl) * adj.get('sl_mul', 1.0)
        sl = row['close'] - (atr * actual_sl_mul) if side == 'long' else row['close'] + (atr * actual_sl_mul)
        tp1 = row['close'] + (atr * tp1_mul_base * adj.get('tp_mul', 1.0)) if side == 'long' else row['close'] - (atr * tp1_mul_base * adj.get('tp_mul', 1.0))
        
        ma15_val = row.get('ma_15m_20', row['close'])
        journal_entry = {
            'ticker': ticker, 'side': side.upper(), 'entry_price': row['close'],
            'entry_time': datetime.now(pytz.timezone('Asia/Tokyo')).isoformat(),
            'market_sentiment': ms_val, 'rsi': rsi_val, 'vwap_dev': vwap_dev, 'rvol': rvol_val,
            'adx': adx_val, 'ma15_value': ma15_val, 'ma15_diff_pct': ((row['close']/ma15_val-1)*100) if ma15_val else 0,
            'vix_value': current_macro_sentiment.get('vix_value', 0),
            'sox_chg': current_macro_sentiment.get('sox_chg', 0),
            'tnx_chg': current_macro_sentiment.get('tnx_chg', 0),
            'divergence_bullish': div_res.get('bullish', False),
            'divergence_bearish': div_res.get('bearish', False),
            'cooldown_overridden': False,
            'score': score, 'threshold': actual_threshold,
            'sector_alignment': 1.0, 'volume_accel': 0.0, 'divergence_bonus': 0.0
        }
        record_trade_journal(journal_entry)
        
        # 通知: 新規シグナル (Ver 15.15)
        logic_type = detail.get('logic_type', 'Unknown')
        tp1_mul = tp1_mul_base * adj.get('tp_mul', 1.0)
        trailing_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
        signal_msg = (
            f"🛡️ **新規シグナル (Ver 15.15): {side.upper()}**\n"
            f"銘柄: {ticker} ({logic_type})\n"
            f"価格: ¥{row['close']:,.1f}\n"
            f"TP1: ¥{tp1:,.1f} (ATR×{tp1_mul:.2f}) → 50%決済\n"
            f"TP2: トレーリング (ATR×{trailing_mul:.2f}幅)\n"
            f"SL: ¥{sl:,.1f} (ATR×{actual_sl_mul:.2f})\n"
            f"スコア: {score:.1f} (判定閾値: {actual_threshold:.1f})\n"
            f"指標: RSI:{rsi_val:.1f}, VWAP:{vwap_dev:+.2f}%"
        )
        send_discord_notification(WEBHOOK_URL, signal_msg)
        
        position_manager.add_position(ticker, side.upper(), row['close'], detail, sl=sl, tp1=tp1)
        break

def monitor_positions(ticker: str, current_price: float, df_inds: pd.DataFrame, detail: Dict):
    pos = position_manager.get_position(ticker)
    if not pos: return
    
    # タイムディケイ監視用：逆方向のスコアを計算
    side = pos['side'].lower()
    opposite_side = 'short' if side == 'long' else 'long'
    params = detail['params'].get(opposite_side, {})
    counter_score = 0.0
    limit_score = 50.0  # デフォルト
    
    if params and not df_inds.empty:
        limit_score = params.get('threshold', 50.0)
        try:
            # 最新の指標行を使って逆方向スコアを評価
            row = df_inds.iloc[-1]
            counter_score, _ = calculate_single_score(row, params, opposite_side)
        except Exception as e:
            logger.warning(f"Counter score calc error for {ticker}: {e}")

    tz = pytz.timezone('Asia/Tokyo')
    event = position_manager.update_price(
        ticker, 
        current_price, 
        current_time=datetime.now(tz),
        counter_signal_score=counter_score,
        limit_score=limit_score
    )
    
    if event == 'TP1_HIT':
        pos_info = position_manager.get_position(ticker)
        tp1_profit = pos_info.get('tp1_profit', 0.0) if pos_info else 0.0
        trailing_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
        tp1_msg = (
            f"✅ **TP1達成: {ticker}**\n"
            f"🎯 50%利確完了\n"
            f"・価格: ¥{current_price:,.1f}\n"
            f"・損益: {tp1_profit:+.2f}%\n"
            f"・リスクを半分（建値近辺）に縮小しました\n"
            f"・残り50%はトレーリングTP (ATR×{trailing_mul:.1f}) で追従中"
        )
        send_discord_notification(WEBHOOK_URL, tp1_msg)
    elif event in ['STOP_LOSS', 'TIME_MAX_LIMIT', 'TIME_DECAY_COUNTER']:
        res = position_manager.close_position(ticker, current_price, event)
        reason_msg = "損切り / トレーリング" if event == 'STOP_LOSS' else ("120分強制決済" if event == 'TIME_MAX_LIMIT' else "60分経過＆逆シグナル検知決済")
        exit_msg = (
            f"🛑 **[EXIT] {res['ticker']}**\n"
            f"理由：{reason_msg}\n"
            f"損益：{res['profit_pct']:+.2f}% ({res.get('logic_type', 'Unknown')})\n"
            f"決済単価：¥{res['exit_price']:,.1f}"
        )
        send_discord_notification(WEBHOOK_URL, exit_msg)

def send_daily_summary():
    results_file = POSITION_MANAGEMENT['trade_results_file']
    try:
        df = pd.read_csv(results_file, on_bad_lines='warn', engine='python')
        today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
        df['exit_time'] = pd.to_datetime(df['exit_time'], utc=True, errors='coerce')
        df_today = df[df['exit_time'].dt.tz_convert('Asia/Tokyo').dt.date == today]
        if df_today.empty: return
        total_profit = pd.to_numeric(df_today['total_profit'], errors='coerce').sum()
        
        msg = f"📊 **本日の最終結果サマリー**\n\n💰 **総合損益: {total_profit:+.2f}%**\n━━━━━━━━━━━━━━\n"
        
        monthly_trades = df_today[df_today['logic_type'] == 'Monthly']
        if not monthly_trades.empty:
            msg += "📅 **Monthly 戦略結果**\n"
            for _, row in monthly_trades.iterrows():
                msg += f"• {row['ticker']} ({row['side']}): {row['total_profit']:+.2f}% [{row['exit_reason']}]\n"
            msg += "\n"
            
        weekly_trades = df_today[df_today['logic_type'] == 'Weekly']
        if not weekly_trades.empty:
            msg += "📅 **Weekly 戦略結果**\n"
            for _, row in weekly_trades.iterrows():
                msg += f"• {row['ticker']} ({row['side']}): {row['total_profit']:+.2f}% [{row['exit_reason']}]\n"
                
        send_discord_notification(WEBHOOK_URL, msg.strip())
    except Exception as e: logger.error(f"Summary error: {e}")

def get_today_total_profit() -> float:
    results_file = POSITION_MANAGEMENT['trade_results_file']
    try:
        df = pd.read_csv(results_file, on_bad_lines='skip', engine='python')
        today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
        df['exit_time'] = pd.to_datetime(df['exit_time'], utc=True, errors='coerce')
        df_today = df[df['exit_time'].dt.tz_convert('Asia/Tokyo').dt.date == today]
        return pd.to_numeric(df_today['total_profit'], errors='coerce').sum()
    except Exception: return 0.0

def monitor():
    config = load_config()
    if not config: return
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    tz = pytz.timezone('Asia/Tokyo')
    
    sentiment = fetch_macro_sentiment()
    global current_macro_sentiment
    current_macro_sentiment = sentiment
    apply_macro_adjustments(sentiment)
    
    last_macro_update = time.time()
    macro_update_interval = RISK_MANAGEMENT.get('macro_update_interval_sec', 3600)

    try:
        # 1. 開始時刻までの待機
        if SESSION_TYPE == 'PM':
            position_manager.set_pm_active(datetime.now(tz).strftime('%Y-%m-%d'))
            logger.info(f"PM session active flag set for {datetime.now(tz).strftime('%Y-%m-%d')} (Waiting/Running)")

        while True:
            now_jst = datetime.now(tz).time()
            if now_jst >= dt_time.fromisoformat(MONITOR_START_TIME):
                break
            
            # 待機ログ (5分おき)
            if now_jst.minute % 5 == 0 and now_jst.second < 30:
                logger.info(f"Waiting for start time: {MONITOR_START_TIME} (Session: {SESSION_TYPE})")
            
            time.sleep(30)

        logger.info(f"Market monitor started. Session: {SESSION_TYPE} (End: {MONITOR_END_TIME})")

        while True:
            now_jst = datetime.now(tz).time()
            if now_jst >= dt_time.fromisoformat(MONITOR_END_TIME):
                if not SKIP_DAILY_SUMMARY: send_daily_summary()
                break
            
            if time.time() - last_macro_update > macro_update_interval:
                try:
                    sentiment = fetch_macro_sentiment()
                    current_macro_sentiment = sentiment
                    apply_macro_adjustments(sentiment)
                except Exception as e:
                    logger.warning(f"Macro sentiment update failed (使用中の値を継続): {e}")
                last_macro_update = time.time()

            today_profit = get_today_total_profit()
            if today_profit <= RISK_MANAGEMENT.get('daily_stop_loss_pct', -3.0):
                send_discord_notification(WEBHOOK_URL, "🚨 **デイリー・ストップロス発動**")
                break

            try:
                raw_data = fetch_yfinance_data(tickers, period='1d', interval='1m')
                for ticker in tickers:
                    ticker_data = raw_data[ticker] if len(tickers)>1 else raw_data
                    df_1m = super_flatten_columns(ticker_data)
                    if df_1m.empty: continue
                    df_5m = df_1m.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
                    if df_5m.empty: continue
                    df_inds = calculate_technical_indicators(df_5m)
                    df_inds['ma_15m_20'] = calculate_ma_from_higher_timeframe(df_1m, 20)
                    latest_price = df_1m['close'].iloc[-1]
                    
                    check_structure_signal(ticker, df_inds)
                    if position_manager.has_position(ticker):
                        monitor_positions(ticker, latest_price, df_inds, details[ticker])
                    else:
                        check_df = df_inds.copy()
                        check_df.loc[check_df.index[-1], 'close'] = latest_price
                        check_new_signal(ticker, check_df, details[ticker])
            except Exception as e: logger.error(f"Loop error: {e}")
            time.sleep(MONITORING_LOOP['loop_interval'])
    finally:
        # PMセッション終了時は必ずフラグをリセットし、サマリー通知を試みる
        if SESSION_TYPE == 'PM':
            position_manager.set_pm_active(False)
            if not SKIP_DAILY_SUMMARY:
                try:
                    send_daily_summary()
                except Exception as e:
                    logger.error(f"Final summary failed: {e}")

if __name__ == "__main__":
    tz = pytz.timezone('Asia/Tokyo')
    now = datetime.now(tz)
    
    # --- AMセッション(12時前)の起動時のみ、前日の不要なログを削除 ---
    if now.hour < 12:
        if os.path.exists(LOG_FILE):
            try:
                os.remove(LOG_FILE)
            except Exception:
                pass
                
    monitor()
