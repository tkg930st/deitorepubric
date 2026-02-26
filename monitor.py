#!/usr/bin/env python3
"""
monitor.py - Version 15.15 統合戦略監視 (最終安定版)
変更点:
- AM/PM 監視セッション連携 (自動ハンドオーバー)
- 実行ログの軽量化 (1MB以内) & 通信ノイズ遮断
- 取引記録 (ジャーナル・結果) の整合性ガード
- ダイバージェンス & 出来高加速スコアリング統合
- デイリー・ストップロス (-3.0%) 安全装置実装
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

# ロギング設定
logging.basicConfig(
    filename=LOG_FILE, filemode='a', encoding='utf-8',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ライブラリのノイズを抑制 (サイズ削減)
logging.getLogger('yfinance').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('peewee').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

position_manager = PositionManager()
last_structure_signals: Dict[str, str] = {}
current_macro_adjustments: Dict[str, Any] = {}
current_macro_sentiment: Dict[str, Any] = {} # マクロ指標保持用

def record_trade_journal(entry: Dict) -> None:
    """エントリー時の詳細を記録 (22カラム対応)"""
    journal_file = 'trade_journal.csv'
    # 22カラムの厳密な定義
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
            # extrasaction='ignore' で余計なキー（logic_type等）を落とし、順序を守る
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(entry)
    except Exception as e:
        logger.error(f"Journal error: {e}")

def git_sync(action: str = 'pull'):
    """GitHub Actions環境での同期用"""
    try:
        if action == 'pull':
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        elif action == 'push':
            subprocess.run(["git", "add", "positions.json"], check=True)
            subprocess.run(["git", "config", "--local", "user.email", "action@github.com"], check=True)
            subprocess.run(["git", "config", "--local", "user.name", "GitHub Action"], check=True)
            subprocess.run(["git", "commit", "-m", "Update pm_active flag [skip ci]"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
    except Exception as e:
        logger.error(f"Git sync error: {e}")

# セッション制御用環境変数の読み込み (config.py からのフォールバック付き)
SESSION_TYPE = os.getenv('SESSION_TYPE', 'AM')
MONITOR_START_TIME = os.getenv('MONITOR_START_TIME', MONITORING_LOOP['start_time'])
MONITOR_END_TIME = os.getenv('MONITOR_END_TIME', MONITORING_LOOP['end_time'])
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
    
    # 基本調整
    threshold_add = 0.0
    tp_mul = 1.0
    sl_mul = 1.0
    
    # 地合いによるブレーキ (Roadmap 6.1)
    if ms < RISK_MANAGEMENT.get('sentiment_brake_threshold', -0.3):
        threshold_add += RISK_MANAGEMENT.get('sentiment_brake_penalty', 15.0) # LONGを大幅に抑制
        sl_mul *= 1.2 # 損切り幅を広げてノイズを回避
    elif ms > 0.3:
        threshold_add -= 5.0 # エントリーしやすくする
        
    if vix > 20:
        adjustment_msg = f"ℹ️ **市場ボラティリティ上昇検知 (VIX:{vix:.1f}, Sentiment:{ms:+.1f})**\n"
        adjustment_msg += "リスク管理調整を自動適用しました：\n"
        adjustment_msg += f"• エントリー閾値修正: {threshold_add:+.1f} (基礎スコア)\n"
        adjustment_msg += f"• 利確幅(TP)倍率: ×{1.25 if vix > 20 else 1.0:.2f}\n"
        adjustment_msg += f"• 損切幅(SL)倍率: ×{sl_mul * (1.15 if vix > 20 else 1.0):.2f}"
        
        current_macro_adjustments = {
            'threshold_add': threshold_add, 
            'tp_mul': 1.25, 
            'sl_mul': sl_mul * 1.15
        }
        send_discord_notification(WEBHOOK_URL, adjustment_msg)
    else:
        current_macro_adjustments = {
            'threshold_add': threshold_add, 
            'tp_mul': 1.0, 
            'sl_mul': sl_mul
        }

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
                   f"節目価格：¥{structure['price']:,.1f}")
            send_discord_notification(WEBHOOK_URL, msg)
            last_structure_signals[ticker] = sig_key

def get_today_closed_trades() -> List[Dict]:
    """本日既に決済された取引の詳細を取得 (JST基準)"""
    results_file = POSITION_MANAGEMENT.get('trade_results_file', 'trade_results.csv')
    if not os.path.exists(results_file): return []
    try:
        df = pd.read_csv(results_file, on_bad_lines='warn', engine='python')
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
    
    # 本日既に決済済みの銘柄チェック (Design 5)
    closed_trades = get_today_closed_trades()
    for trade in closed_trades:
        if trade['ticker'] == ticker:
            # 1. 同一銘柄は理由を問わず1日1回に制限（より厳格な運用）
            # もし「利確後はOK」とする場合は、ここで exit_reason == 'STOP_LOSS' の時のみ return する
            return

    # 時刻によるエントリー制限
    now_jst = datetime.now(pytz.timezone('Asia/Tokyo')).time()
    if now_jst >= dt_time.fromisoformat(ENTRY_CUTOFF_TIME):
        return

    row = df.iloc[-1]
    side_params = detail['params']
    tp1_mul_base = POSITION_MANAGEMENT.get('tp1_multiplier', 1.5)
    trailing_atr_mul = POSITION_MANAGEMENT.get('trailing_atr_multiplier', 1.0)
    adj = current_macro_adjustments
    
    # ボーナススコアの算出 (Roadmap 6.3: 地合いが悪い時はボーナスを無効化)
    ms_val = current_macro_sentiment.get('market_sentiment', 0.0)
    bonus_multiplier = 1.0 if ms_val > 0.1 else (0.5 if ms_val >= -0.2 else 0.0)

    vol_accel_bonus = 0.0
    if SECTOR_ALIGNMENT['enabled'] and bonus_multiplier > 0:
        rvol = safe_get(row, 'rvol', 1.0)
        if rvol > SECTOR_ALIGNMENT.get('volume_accel_rvol_threshold', 1.2):
            vol_accel_bonus = SECTOR_ALIGNMENT.get('volume_accel_score', 10) * bonus_multiplier

    div_res = check_divergence(df) if DIVERGENCE['enabled'] else {'bullish': False, 'bearish': False, 'reverse_bullish': False, 'reverse_bearish': False}

    for side in ['long', 'short']:
        params = side_params[side]
        if detail.get(f'{side}_disabled', False): continue
        
        st = SIGNAL_THRESHOLDS
        rsi_val = safe_get(row, 'rsi_14', 50)
        vwap_dev = safe_get(row, 'vwap_dev', 0)
        if params.get('use_rsi_filter', True):
            if (side == 'long' and rsi_val >= st['rsi_filter_long_max']) or (side == 'short' and rsi_val <= st['rsi_filter_short_min']): continue
        if params.get('use_vwap_filter', True):
            if (side == 'long' and vwap_dev >= st['vwap_filter_max']) or (side == 'short' and vwap_dev <= -st['vwap_filter_max']): continue

        score = 0.0
        current_div_bonus = 0.0

        # 1. 基礎スコア (backtest_engine.py と垂直同期, SIGNAL_THRESHOLDS参照)
        if side == 'long':
            if rsi_val < st['rsi_oversold']: score += params['w_rsi'] * 1.5
            elif rsi_val < st['rsi_buy_moderate']: score += params['w_rsi'] * 1.0

            if vwap_dev < -st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
            elif vwap_dev < -st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0

            if div_res['bullish'] or div_res['reverse_bullish']:
                current_div_bonus = SECTOR_ALIGNMENT.get('divergence_bonus_score', 20) * bonus_multiplier
        else:
            if rsi_val > st['rsi_overbought']: score += params['w_rsi'] * 1.5
            elif rsi_val > st['rsi_sell_moderate']: score += params['w_rsi'] * 1.0

            if vwap_dev > st['vwap_dev_strong']: score += params['w_vwap'] * 1.5
            elif vwap_dev > st['vwap_dev_moderate']: score += params['w_vwap'] * 1.0

            if div_res['bearish'] or div_res['reverse_bearish']:
                current_div_bonus = SECTOR_ALIGNMENT.get('divergence_bonus_score', 20) * bonus_multiplier

        # ボリューム評価 (段階的)
        rvol_val = safe_get(row, 'rvol', 1.0)
        if rvol_val > st['rvol_strong']: score += params['w_rvol'] * 2.0
        elif rvol_val > st['rvol_moderate']: score += params['w_rvol'] * 1.0
        elif rvol_val > st['rvol_weak']: score += params['w_rvol'] * 0.5

        # トレンド強度 (段階的)
        adx_val = safe_get(row, 'adx_14', 0)
        if adx_val > st['adx_strong']: score += params['w_adx'] * 1.5
        elif adx_val > st['adx_moderate']: score += params['w_adx'] * 1.0
        
        # 合計スコア (地合いによる閾値調整を適用)
        total_score = score + vol_accel_bonus + current_div_bonus
        actual_threshold = params['threshold'] + adj.get('threshold_add', 0)
        
        if total_score >= actual_threshold:
            if TREND_FILTER['enabled']:
                ma15 = row.get('ma_15m_20', 0)
                if not check_trend_filter(row['close'], ma15, side.upper()): continue

            atr = row.get('atr_14', row['close'] * 0.02)
            detail['atr'] = atr
            entry_price = row['close']
            
            # SL下限ガード (Roadmap 5.1 / Config同期)
            min_sl = RISK_MANAGEMENT.get('min_sl_multiplier', 0.7)
            actual_sl_mul = max(params['sl_mul'], min_sl) * adj.get('sl_mul', 1.0)
            sl_dist = atr * actual_sl_mul
            sl = entry_price - sl_dist if side == 'long' else entry_price + sl_dist
            
            tp1_dist = atr * tp1_mul_base * adj.get('tp_mul', 1.0)
            tp1 = entry_price + tp1_dist if side == 'long' else entry_price - tp1_dist
            
            ma15_val = row.get('ma_15m_20', row['close'])
            ma15_diff = ((row['close'] / ma15_val - 1) * 100) if ma15_val else 0
            ms = current_macro_sentiment
            
            # ジャーナルへの詳細記録 (22カラム完全版)
            journal_entry = {
                'ticker': ticker, 
                'side': side.upper(), 
                'entry_price': entry_price,
                'entry_time': datetime.now(pytz.timezone('Asia/Tokyo')).isoformat(),
                'market_sentiment': ms.get('market_sentiment', 0.0),
                'rsi': rsi_val, 
                'vwap_dev': vwap_dev, 
                'rvol': safe_get(row, 'rvol', 1.0),
                'adx': safe_get(row, 'adx_14', 0), 
                'ma15_value': ma15_val,
                'ma15_diff_pct': ma15_diff,
                'vix_value': ms.get('vix_value', 0.0),
                'sox_chg': ms.get('sox_chg', 0.0),
                'tnx_chg': ms.get('tnx_chg', 0.0),
                'divergence_bullish': div_res['bullish'],
                'divergence_bearish': div_res['bearish'],
                'cooldown_overridden': False,
                'score': total_score, 
                'threshold': actual_threshold,
                'sector_alignment': 0.0, 
                'volume_accel': vol_accel_bonus,
                'divergence_bonus': current_div_bonus
            }
            record_trade_journal(journal_entry)

            msg = (f"🛡️ **新規シグナル (Ver 15.15): {side.upper()}**\n"
                   f"銘柄: {ticker} ({detail['logic_type']})\n"
                   f"価格: ¥{entry_price:,.1f}\n"
                   f"TP1: ¥{tp1:,.1f} (ATR×{tp1_mul_base * adj.get('tp_mul', 1.0):.1f}) → 50%決済\n"
                   f"TP2: トレーリング (ATR×{trailing_atr_mul}幅)\n"
                   f"SL: ¥{sl:,.1f} (ATR×{params['sl_mul'] * adj.get('sl_mul', 1.0):.1f})\n"
                   f"スコア: {total_score:.1f} (基礎:{score:.1f} + Bonus:{vol_accel_bonus+current_div_bonus:.1f})\n"
                   f"指標: RSI:{rsi_val:.1f}, VWAP:{vwap_dev:.2f}")
            
            send_discord_notification(WEBHOOK_URL, msg)
            position_manager.add_position(ticker, side.upper(), entry_price, detail, sl=sl, tp1=tp1)
            break
        elif total_score >= (actual_threshold - 10):
            # 惜しくも届かなかったシグナルを記録
            logger.info(
                f"Signal missed: {ticker} ({side.upper()}) "
                f"Score: {total_score:.1f} (Req: {actual_threshold:.1f}) "
                f"[RSI:{rsi_val:.1f}, VWAP:{vwap_dev:.2f}, "
                f"RVOL:{safe_get(row, 'rvol', 1.0):.2f}, ADX:{safe_get(row, 'adx_14', 0):.1f}]"
            )

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
               f"・リスクを半分（建値近辺）に縮小しました\n"
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
        df = pd.read_csv(results_file, on_bad_lines='warn', engine='python')
        # 不正な日付や数値をクレンジング
        if 'exit_time' not in df.columns or 'total_profit' not in df.columns:
            logger.warning("Summary: trade_results.csv format is invalid.")
            return
        df['exit_time'] = pd.to_datetime(df['exit_time'], utc=True, errors='coerce')
        df = df.dropna(subset=['exit_time'])

        today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
        df_today = df[df['exit_time'].dt.tz_convert('Asia/Tokyo').dt.date == today].copy()
        if df_today.empty: return
        
        df_today['total_profit'] = pd.to_numeric(df_today['total_profit'], errors='coerce')
        total_profit = df_today['total_profit'].sum()
        
        msg = f"📊 **本日の最終結果サマリー**\n\n💰 **総合損益: {total_profit:+.2f}%**\n━━━━━━━━━━━━━━\n"
        for label in ["Monthly", "Weekly"]:
            res = df_today[df_today['logic_type'] == label]
            msg += f"📅 **{label} 戦略結果**\n"
            if not res.empty:
                for _, r in res.iterrows():
                    msg += f"• {r['ticker']} ({r['side']}): {r['total_profit']:+.2f}% [{r['exit_reason']}]\n"
            else: msg += "• 取引なし\n"
            msg += "\n"
        send_discord_notification(WEBHOOK_URL, msg)
    except Exception as e: logger.error(f"Summary error: {e}")

def get_today_total_profit() -> float:
    """本日の累計損益を計算 (JST基準)"""
    results_file = POSITION_MANAGEMENT['trade_results_file']
    if not os.path.exists(results_file): return 0.0
    try:
        df = pd.read_csv(results_file, on_bad_lines='warn', engine='python')
        if 'exit_time' not in df.columns or 'total_profit' not in df.columns:
            return 0.0
        df['exit_time'] = pd.to_datetime(df['exit_time'], utc=True, errors='coerce')
        today = datetime.now(pytz.timezone('Asia/Tokyo')).date()
        df_today = df[df['exit_time'].dt.tz_convert('Asia/Tokyo').dt.date == today]
        if df_today.empty: return 0.0
        return pd.to_numeric(df_today['total_profit'], errors='coerce').sum()
    except Exception as e:
        logger.error(f"Error calculating daily profit: {e}")
        return 0.0

def monitor():
    config = load_config()
    if not config:
        logger.error("Config not found. Exiting.")
        return
    details = {d['t']: d for d in config['details']}
    tickers = list(details.keys())
    
    # 1. 開始時刻まで待機
    tz = pytz.timezone('Asia/Tokyo')
    start_time_dt = dt_time.fromisoformat(MONITOR_START_TIME)
    end_time_dt = dt_time.fromisoformat(MONITOR_END_TIME)
    
    now_jst = datetime.now(tz).time()
    if now_jst >= end_time_dt:
        logger.info(f"Current time {now_jst} is past MONITOR_END_TIME {MONITOR_END_TIME}. Market already closed.")
        return

    logger.info(f"Waiting for start time: {MONITOR_START_TIME} (Session: {SESSION_TYPE})")
    while True:
        now_jst = datetime.now(tz).time()
        if now_jst >= start_time_dt:
            break
        time.sleep(60)

    # 2. PMセッション起動通知 (AMに知らせる)
    if SESSION_TYPE == 'PM':
        today_str = datetime.now(tz).strftime('%Y-%m-%d')
        logger.info(f"PM session starting. Setting active flag for {today_str}.")
        position_manager.set_pm_active(today_str)
        git_sync('push')

    sentiment = fetch_macro_sentiment()
    global current_macro_sentiment
    current_macro_sentiment = sentiment
    
    start_msg = (f"📡 **Version 15.15 統合戦略監視 ({SESSION_TYPE}) 起動**\n"
                 f"━━━━━━━━━━━━━━\n"
                 f"🌍 **マクロ地合い情報**:\n"
                 f"• VIX: {sentiment['vix_value']} ({sentiment['vix_chg']:+.2f}%)\n"
                 f"• SOX: {sentiment['sox_chg']:+.2f}%\n"
                 f"• JPY: {sentiment['jpy_chg']:+.2f}%\n\n"
                 f"🎯 **監視対象銘柄**: {', '.join(tickers)}")
    send_discord_notification(WEBHOOK_URL, start_msg)
    apply_macro_adjustments(sentiment)

    # マクロ指標の周期的更新用
    last_macro_update = 0
    macro_update_interval = RISK_MANAGEMENT.get('macro_update_interval_sec', 3600) # 1時間ごとに更新

    try:
        while True:
            now_jst = datetime.now(tz).time()
            today_str = datetime.now(tz).strftime('%Y-%m-%d')
            
            # マクロ指標の更新 (Roadmap 6.1)
            if time.time() - last_macro_update > macro_update_interval:
                sentiment = fetch_macro_sentiment()
                current_macro_sentiment = sentiment
                apply_macro_adjustments(sentiment)
                last_macro_update = time.time()
                logger.info(f"Macro sentiment updated: {sentiment['market_sentiment']}")

            # デイリー・ストップロスチェック
            today_profit = get_today_total_profit()
            limit = RISK_MANAGEMENT.get('daily_stop_loss_pct', -3.0)
            if today_profit <= limit:
                msg = (f"🚨 **デイリー・ストップロス発動**\n"
                       f"本日の累計損益 ({today_profit:+.2f}%) が制限値 ({limit:.2f}%) に達しました。\n"
                       f"安全のため、本日の全自動監視を緊急停止します。")
                send_discord_notification(WEBHOOK_URL, msg)
                logger.warning(f"Daily stop loss hit: {today_profit:.2f}%")
                break

            # 3. 終了・強制決済判定
            # 3a. 強制決済 (14:55など) - ポジションがある場合のみ実行
            if now_jst >= dt_time.fromisoformat(FORCE_CLOSE_TIME) and position_manager.positions:
                raw_data = fetch_yfinance_data(tickers, period='1d', interval=DATA_FETCH['monitor_interval'])
                prices = {}
                for t in tickers:
                    df_raw = raw_data[t] if len(tickers)>1 else raw_data
                    df_flat = super_flatten_columns(df_raw)
                    if not df_flat.empty: prices[t] = df_flat['close'].iloc[-1]

                results = position_manager.force_close_all(prices, '時間切れ強制決済')
                for r in results:
                    send_discord_notification(WEBHOOK_URL, f"🛑 **[EXIT] {r['ticker']}**\n理由：時間切れ強制決済\n損益：{r['profit_pct']:+.2f}% ({r['logic_type']})")
            
            # 3b. セッション終了判定
            if now_jst >= dt_time.fromisoformat(MONITOR_END_TIME):
                if not SKIP_DAILY_SUMMARY:
                    send_daily_summary()
                break

            # 3c. AMからPMへの引き継ぎチェック (11:30以降)
            if SESSION_TYPE == 'AM' and now_jst >= dt_time(11, 30):
                git_sync('pull') # PMのフラグを確認するためにプル
                if position_manager.get_pm_active_date() == today_str:
                    logger.info("PM session detected for today. AM session handing over.")
                    send_discord_notification(WEBHOOK_URL, "🔄 **前場監視終了 (後場へ引き継ぎ)**")
                    break

            # 4. 監視メインロジック (ハイブリッド1m/5m方式)
            try:
                # 1分足データを取得 (トリガー用)
                raw_data = fetch_yfinance_data(
                    tickers, 
                    period=DATA_FETCH['monitor_period'], 
                    interval=DATA_FETCH['monitor_interval']
                )
                for ticker in tickers:
                    ticker_data_1m = raw_data[ticker] if len(tickers) > 1 else raw_data
                    df_1m = super_flatten_columns(ticker_data_1m)
                    if df_1m.empty: continue
                    
                    # 1分足から5分足へリサンプル (テクニカル指標計算用)
                    df_5m = df_1m.resample('5min').agg({
                        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
                    }).dropna()
                    
                    if df_5m.empty: continue
                    
                    # 指標計算は5分足ベース
                    df_5m_inds = calculate_technical_indicators(df_5m)
                    df_5m_inds['ma_15m_20'] = calculate_ma_from_higher_timeframe(df_1m, 20) # 15mは1mから計算可能
                    
                    # 最新価格は1分足の終値を使用
                    latest_price_1m = df_1m['close'].iloc[-1]
                    
                    # シグナル判定
                    check_structure_signal(ticker, df_5m_inds)
                    if position_manager.has_position(ticker):
                        monitor_positions(ticker, latest_price_1m)
                    else:
                        # 判定関数に最新の1分足価格を考慮させるためにdfを微調整
                        # (closeだけ最新1分足に差し替えた行を判定に使う)
                        check_df = df_5m_inds.copy()
                        check_df.loc[check_df.index[-1], 'close'] = latest_price_1m
                        check_new_signal(ticker, check_df, details[ticker])
            except Exception as e:
                logger.error(f"Loop error: {e}")
            
            time.sleep(MONITORING_LOOP['loop_interval'])
    except KeyboardInterrupt:
        pass
    finally:
        if SESSION_TYPE == 'PM':
            # 翌日のためにフラグをリセット
            position_manager.set_pm_active(False)

if __name__ == "__main__":
    monitor()
