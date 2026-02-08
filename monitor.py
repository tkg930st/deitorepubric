#!/usr/bin/env python3
"""
monitor.py - Version 11.0 プロ版 (利益ロック & 動的TP & ボラティリティ選別)
"""
import json
import logging
import time
import csv
import os
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Optional, Set
import pytz
import numpy as np

import pandas as pd
import yfinance as yf

from config import (
    WEBHOOK_URL, LOG_FILE, LOG_LEVEL, OUTPUT_CONFIG, DATA_FETCH,
    MARKET_SENTIMENT, MONITORING_LOOP, POSITION_MANAGEMENT,
    DAILY_COOLDOWN, DIVERGENCE, TREND_FILTER, TRADE_JOURNAL,
    SIGNAL_THRESHOLDS
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, calculate_ma_from_higher_timeframe,
    send_discord_notification, safe_get,
    check_divergence, check_trend_filter
)
from backtest_engine import calculate_single_score
from position_manager import PositionManager

# ロギング設定
logging.basicConfig(
    filename=LOG_FILE,
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 処理済みタイムスタンプ
processed_timestamps: Set[str] = set()
position_manager = PositionManager()

# プロ版用グローバル状態
opening_drives: Dict[str, Dict] = {}
macro_sentiment: Dict[str, float] = {}  # V3: マクロ指標（Ver 11.0）


def load_config() -> Optional[Dict]:
    """設定ファイルを読み込み"""
    try:
        with open(OUTPUT_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def load_daily_cooldown() -> Dict:
    """当日制限リストを読み込み（Ver 12.0）"""
    cooldown_file = DAILY_COOLDOWN.get('cooldown_file', 'daily_cooldown.json')
    
    if not os.path.exists(cooldown_file):
        return {}
    
    try:
        with open(cooldown_file, 'r', encoding='utf-8') as f:
            cooldown = json.load(f)
        
        # 今日の日付を取得
        today = datetime.now().date()
        
        # 古いエントリーを削除（日付が変わった場合）
        to_remove = []
        for ticker, info in cooldown.items():
            cooldown_date = datetime.fromisoformat(info['time']).date()
            if cooldown_date != today:
                to_remove.append(ticker)
        
        for ticker in to_remove:
            del cooldown[ticker]
        
        # クリーンアップしたデータを保存
        if to_remove:
            save_daily_cooldown(cooldown)
        
        return cooldown
    
    except Exception as e:
        logger.error(f"当日制限読み込みエラー: {str(e)}")
        return {}


def save_daily_cooldown(cooldown: Dict) -> None:
    """当日制限リストを保存（Ver 12.0）"""
    cooldown_file = DAILY_COOLDOWN.get('cooldown_file', 'daily_cooldown.json')
    
    try:
        with open(cooldown_file, 'w', encoding='utf-8') as f:
            json.dump(cooldown, f, indent=2, ensure_ascii=False)
        logger.info(f"当日制限保存: {list(cooldown.keys())}")
    except Exception as e:
        logger.error(f"当日制限保存エラー: {str(e)}")


def add_to_cooldown(ticker: str, reason: str, cooldown: Dict) -> None:
    """当日制限リストに追加（Ver 12.0）"""
    cooldown[ticker] = {
        'reason': reason,
        'time': datetime.now().isoformat()
    }
    save_daily_cooldown(cooldown)
    logger.info(f"{ticker} を当日制限リストに追加（理由: {reason}）")


def is_cooldown_active(ticker: str, cooldown: Dict) -> bool:
    """当日制限中かチェック（Ver 12.0）"""
    return ticker in cooldown


def record_trade_journal(entry: Dict) -> None:
    """トレードジャーナルに記録（Ver 12.0）"""
    if not TRADE_JOURNAL.get('enabled', False):
        return
    
    journal_file = TRADE_JOURNAL.get('journal_file', 'trade_journal.csv')
    file_exists = os.path.exists(journal_file)
    
    try:
        with open(journal_file, 'a', newline='', encoding='utf-8') as f:
            fieldnames = [
                'ticker', 'side', 'entry_price', 'entry_time',
                'market_sentiment', 'rsi', 'vwap_dev', 'rvol', 'adx',
                'ma15_value', 'ma15_diff_pct',
                'vix_value', 'sox_chg', 'tnx_chg',
                'divergence_bullish', 'divergence_bearish',
                'cooldown_overridden', 'score', 'threshold'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            if not file_exists:
                writer.writeheader()
            
            writer.writerow(entry)
        
        logger.info(f"ジャーナル記録: {entry['ticker']} {entry['side']}")
    
    except Exception as e:
        logger.error(f"ジャーナル記録エラー: {str(e)}")


def calculate_opening_analysis(tickers: list):
    """09:00〜09:30のドライブ分析 (プロ版)"""
    global opening_drives
    print(f"\n🔍 オープニング・ドライブ分析中...")
    raw_data = fetch_yfinance_data(tickers, period='1d', interval='5m')
    
    for ticker in tickers:
        try:
            df = super_flatten_columns(raw_data[ticker] if len(tickers)>1 else raw_data)
            or_data = df[(df.index.time >= dt_time(9, 0)) & (df.index.time <= dt_time(9, 30))]
            if not or_data.empty:
                open_900 = or_data['open'].iloc[0]
                close_930 = or_data['close'].iloc[-1]
                drive_pct = (close_930 / open_900 - 1) * 100
                opening_drives[ticker] = {'drive_pct': drive_pct}
                print(f"   {ticker}: Drive {drive_pct:+.2f}%")
            else:
                opening_drives[ticker] = {'drive_pct': 0.0}
        except:
            opening_drives[ticker] = {'drive_pct': 0.0}


def check_new_signal(ticker: str, df: pd.DataFrame, params_long: Dict,
                     params_short: Dict, threshold_adjustment: float,
                     cooldown: Dict, market_sentiment_value: float,
                     sector: str = '', disabled: Dict = None) -> None:
    """新規シグナルチェック (プロ版: ボラティリティ選別 & ブースト適用 + V3マクロ統合)"""
    if disabled is None:
        disabled = {'long': False, 'short': False}
    if df.empty or len(df) < 50: return
    
    # ボラティリティフィルター
    atr_ma = df['atr_14'].rolling(window=50).mean().iloc[-1]
    current_atr = df['atr_14'].iloc[-1]
    if current_atr < atr_ma: return # 活気がない場合はスキップ

    confirmed_row = df.iloc[-2]
    check_key = f"{ticker}_{confirmed_row.name}"
    if check_key in processed_timestamps: return
    processed_timestamps.add(check_key)
    
    if position_manager.has_position(ticker): return
    
    # Ver 12.0: 当日制限チェック
    is_cooldown = is_cooldown_active(ticker, cooldown)
    cooldown_overridden = False
    
    if is_cooldown:
        # ダイバージェンス検知で制限解除可能
        if DIVERGENCE['enabled'] and len(df) >= DIVERGENCE['lookback']:
            df_recent = df.iloc[-DIVERGENCE['lookback']:]
            div = check_divergence(df_recent, DIVERGENCE['lookback'])
            
            if div.get('bullish') or div.get('bearish'):
                # ダイバージェンス検知 → 制限解除
                del cooldown[ticker]
                save_daily_cooldown(cooldown)
                cooldown_overridden = True
                logger.info(f"{ticker}: ダイバージェンス検知により当日制限解除")
            else:
                # ダイバージェンスなし → スキップ
                logger.debug(f"{ticker}: 当日制限中のためスキップ")
                return
        else:
            logger.debug(f"{ticker}: 当日制限中のためスキップ")
            return

    drive_pct = opening_drives.get(ticker, {}).get('drive_pct', 0.0)
    ma15_value = confirmed_row.get('ma_15m_20', 0)
    current_price = confirmed_row['close']
    
    # V3: マクロ指標による動的調整
    global macro_sentiment
    vix_value = macro_sentiment.get('vix_value', 0.0)
    sox_chg = macro_sentiment.get('sox_chg', 0.0)
    tnx_chg = macro_sentiment.get('tnx_chg', 0.0)
    
    # V3-A: VIX > 20 → 閾値厳格化、TP/SL拡張
    v3_threshold_adj = 0.0
    v3_tp_multiplier = 1.0
    v3_sl_multiplier = 1.0
    
    if vix_value > 20:
        v3_threshold_adj += 5.0  # エントリーを厳しく
        v3_tp_multiplier = 1.25  # 利確幅を拡張
        v3_sl_multiplier = 1.15  # 損切幅を緩和（ノイズ回避）
        logger.info(f"{ticker}: VIX > 20 → 閾値+5.0, TP×1.25, SL×1.15")
    
    # V3-B: TNXによるセクター順張り
    if abs(tnx_chg) > 0.5:
        if sector in ['銀行業', '保険業'] and tnx_chg > 0.5:
            v3_threshold_adj -= 7.0  # 金利高で銀行・保険は買い
            logger.info(f"{ticker} ({sector}): TNX↑ → 閾値-7.0（金利高恩恵）")
        elif sector == '電気機器' and tnx_chg > 0.5:
            v3_threshold_adj += 7.0  # 金利高で電気機器は警戒
            logger.info(f"{ticker} ({sector}): TNX↑ → 閾値+7.0（金利高警戒）")
    
    # V3-C: SOXアライメント確認
    if sector in ['電気機器', '情報・通信業']:
        if sox_chg > 1.5:  # SOX大幅上昇
            if drive_pct > 0.5:
                # 日米一致（強気）
                v3_threshold_adj -= 10.0
                logger.info(f"{ticker} ({sector}): SOX↑ & 日本株↑ → 閾値-10.0（アライメント）")
            elif drive_pct < -0.5:
                # 逆行（罠）
                v3_threshold_adj += 15.0
                logger.info(f"{ticker} ({sector}): SOX↑ だが日本株↓ → 閾値+15.0（逆行警戒）")

    # LONG
    if not disabled.get('long', False):  # Ver 11.0: LONG禁止チェック
        boost_long = 20 if drive_pct > 0.5 else 0
        score_long = calculate_single_score(confirmed_row, params_long, 'long', boost_long)
        threshold_long = params_long['threshold'] + threshold_adjustment + v3_threshold_adj
        
        if score_long >= threshold_long:
            if check_trend_filter(current_price, ma15_value, 'LONG'):
                # Ver 12.0: ジャーナル記録
                journal_entry = {
                    'ticker': ticker, 'side': 'LONG', 'entry_price': current_price,
                    'entry_time': confirmed_row.name.isoformat(),
                    'market_sentiment': market_sentiment_value,
                    'rsi': safe_get(confirmed_row, 'rsi_14', 50),
                    'vwap_dev': safe_get(confirmed_row, 'vwap_dev', 0),
                    'rvol': safe_get(confirmed_row, 'rvol', 1.0),
                    'adx': safe_get(confirmed_row, 'adx_14', 0),
                    'ma15_value': ma15_value,
                    'ma15_diff_pct': ((current_price / ma15_value - 1) * 100) if ma15_value > 0 else 0,
                    'vix_value': vix_value,
                    'sox_chg': sox_chg,
                    'tnx_chg': tnx_chg,
                    'divergence_bullish': False,
                    'divergence_bearish': False,
                    'cooldown_overridden': cooldown_overridden,
                    'score': score_long,
                    'threshold': threshold_long
                }
                record_trade_journal(journal_entry)
                
                # ドライブによる通常のTP倍率調整
                tp_mul = params_long['tp_mul'] * 2.0 if drive_pct > 1.0 else params_long['tp_mul']
                # V3: VIX高時のTP拡張を適用
                tp_mul = tp_mul * v3_tp_multiplier
                sl_mul = params_long['sl_mul'] * v3_sl_multiplier
                send_new_signal_pro(ticker, confirmed_row, score_long, params_long, 'LONG', tp_mul, sl_mul)
    else:
        logger.debug(f"{ticker}: LONGはエントリー禁止（利益<5%）")

    # SHORT
    if not disabled.get('short', False):  # Ver 11.0: SHORT禁止チェック
        boost_short = 20 if drive_pct < -0.5 else 0
        score_short = calculate_single_score(confirmed_row, params_short, 'short', boost_short)
        threshold_short = params_short['threshold'] + threshold_adjustment + v3_threshold_adj
        
        if score_short >= threshold_short:
            if check_trend_filter(current_price, ma15_value, 'SHORT'):
                # Ver 12.0: ジャーナル記録
                journal_entry = {
                    'ticker': ticker, 'side': 'SHORT', 'entry_price': current_price,
                    'entry_time': confirmed_row.name.isoformat(),
                    'market_sentiment': market_sentiment_value,
                    'rsi': safe_get(confirmed_row, 'rsi_14', 50),
                    'vwap_dev': safe_get(confirmed_row, 'vwap_dev', 0),
                    'rvol': safe_get(confirmed_row, 'rvol', 1.0),
                    'adx': safe_get(confirmed_row, 'adx_14', 0),
                    'ma15_value': ma15_value,
                    'ma15_diff_pct': ((current_price / ma15_value - 1) * 100) if ma15_value > 0 else 0,
                    'vix_value': vix_value,
                    'sox_chg': sox_chg,
                    'tnx_chg': tnx_chg,
                    'divergence_bullish': False,
                    'divergence_bearish': False,
                    'cooldown_overridden': cooldown_overridden,
                    'score': score_short,
                    'threshold': threshold_short
                }
                record_trade_journal(journal_entry)
                
                # ドライブによる通常のTP倍率調整
                tp_mul = params_short['tp_mul'] * 2.0 if drive_pct < -1.0 else params_short['tp_mul']
                # V3: VIX高時のTP拡張を適用
                tp_mul = tp_mul * v3_tp_multiplier
                sl_mul = params_short['sl_mul'] * v3_sl_multiplier
                send_new_signal_pro(ticker, confirmed_row, score_short, params_short, 'SHORT', tp_mul, sl_mul)
    else:
        logger.debug(f"{ticker}: SHORTはエントリー禁止（利益<5%）")


def send_new_signal_pro(ticker: str, row: pd.Series, score: float, params: Dict, side: str, tp_mul: float, sl_mul: float = None):
    """ポジション登録 (Ver 12.0: TP1/TP2分割決済対応)"""
    if sl_mul is None:
        sl_mul = params['sl_mul']
    
    atr = safe_get(row, 'atr_14', row['close'] * 0.02)
    entry_price = row['close']
    
    # Ver 12.0: TP1とTP2を分けて計算
    tp1_mul = POSITION_MANAGEMENT['tp1_multiplier']  # 1.5
    tp2_mul = POSITION_MANAGEMENT['tp2_multiplier']  # 3.0
    
    if side == 'LONG':
        tp1 = entry_price + (atr * tp1_mul)
        tp2 = entry_price + (atr * tp2_mul)
        sl = entry_price - (atr * sl_mul)
    else:  # SHORT
        tp1 = entry_price - (atr * tp1_mul)
        tp2 = entry_price - (atr * tp2_mul)
        sl = entry_price + (atr * sl_mul)
    
    msg = f"🛡️ **新規シグナル (Ver 12.0): {side}**\n銘柄: {ticker}\n価格: ¥{entry_price:,.1f}\nTP1: ¥{tp1:,.1f} (ATR×{tp1_mul})\nTP2: ¥{tp2:,.1f} (ATR×{tp2_mul})\nSL: ¥{sl:,.1f}\nスコア: {score:.1f}"
    
    if send_discord_notification(WEBHOOK_URL, msg):
        # PositionManagerに登録
        pos_data = {
            'entry_atr': atr, 'tp_mul_actual': tp_mul, 'sl_mul_actual': sl_mul
        }
        extended_params = {**params, **pos_data}
        position_manager.add_position(ticker, side, entry_price, atr, sl, tp1, tp2, extended_params)


def check_exit_signal(ticker: str, df: pd.DataFrame, cooldown: Dict) -> None:
    """エグジット監視 (Ver 12.0: 分割決済 TP1/TP2)"""
    position = position_manager.get_position(ticker)
    if not position: return
    
    latest_row = df.iloc[-1]
    current_price = latest_row['close']
    high_price = latest_row['high']
    low_price = latest_row['low']
    side = position['side']
    
    stop_loss = position['stop_loss']
    tp1 = position.get('tp1', position['tp2'])  # デフォルトはtp2と同じ
    tp2 = position['tp2']
    tp1_hit = position.get('tp1_hit', False)
    
    try:
        # 損切チェック
        if (side == 'LONG' and low_price <= stop_loss) or \
           (side == 'SHORT' and high_price >= stop_loss):
            result = position_manager.close_position(ticker, stop_loss, 'SL')
            send_exit_notification(result)
            # Ver 12.0: 当日制限に追加
            add_to_cooldown(ticker, 'SL', cooldown)
            return
        
        # TP1チェック（まだTP1に達していない場合）
        if not tp1_hit:
            if (side == 'LONG' and high_price >= tp1) or \
               (side == 'SHORT' and low_price <= tp1):
                # TP1で50%利確
                profit = position_manager.execute_tp1(ticker, tp1)
                send_tp1_notification(ticker, tp1, profit)
                return
        
        # TP2チェック（TP1後の残り50%）
        if tp1_hit:
            if (side == 'LONG' and high_price >= tp2) or \
               (side == 'SHORT' and low_price <= tp2):
                result = position_manager.close_position(ticker, tp2, 'TP2')
                send_exit_notification(result)
                return
    
    except Exception as e:
        logger.error(f"エグジット監視エラー ({ticker}): {str(e)}")


def send_tp1_notification(ticker: str, tp1_price: float, profit: float) -> None:
    """TP1達成通知（Ver 12.0）"""
    message = f"✅ **TP1達成: {ticker}**\n🎯 50%利確完了\n・価格: ¥{tp1_price:,.1f}\n・損益: {profit:+.2f}%\n・損切を建値に移動しました"
    send_discord_notification(WEBHOOK_URL, message)


def send_exit_notification(result: Dict) -> None:
    """エグジット通知（Ver 12.0）"""
    ticker = result['ticker']
    reason = result['exit_reason']
    profit = result['total_profit']
    tp1_hit = result.get('tp1_hit', False)
    
    if tp1_hit:
        message = f"🛑 **ポジションクローズ: {ticker}**\n理由: {reason}\n総合損益: {profit:+.2f}%\n（TP1: 50%利確済み + 残り50%）"
    else:
        message = f"🛑 **ポジションクローズ: {ticker}**\n理由: {reason}\n総合損益: {profit:+.2f}%"
    
    send_discord_notification(WEBHOOK_URL, message)


def send_daily_summary() -> None:
    """15:00サマリー通知（Ver 12.0）"""
    try:
        results_file = POSITION_MANAGEMENT['trade_results_file']
        
        if not os.path.exists(results_file):
            logger.info("取引結果ファイルが存在しません")
            return
        
        # CSVから当日分を抽出
        df_results = pd.read_csv(results_file)
        df_results['entry_time'] = pd.to_datetime(df_results['entry_time'])
        
        today = datetime.now().date()
        df_today = df_results[df_results['entry_time'].dt.date == today]
        
        if df_today.empty:
            send_discord_notification(WEBHOOK_URL, "📊 **本日の取引サマリー**\n本日は取引がありませんでした")
            return
        
        # 統計計算
        total_profit = df_today['total_profit'].sum()
        trade_count = len(df_today)
        win_count = len(df_today[df_today['total_profit'] > 0])
        win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0
        
        # 最大利益銘柄
        max_profit_row = df_today.loc[df_today['total_profit'].idxmax()]
        max_ticker = max_profit_row['ticker']
        max_profit = max_profit_row['total_profit']
        
        # 最大損失銘柄
        min_profit_row = df_today.loc[df_today['total_profit'].idxmin()]
        min_ticker = min_profit_row['ticker']
        min_profit = min_profit_row['total_profit']
        
        # メッセージ作成
        message = f"""📊 **本日の取引サマリー**

💰 総損益: {total_profit:+.2f}%
🔄 取引回数: {trade_count}回
✅ 勝率: {win_rate:.1f}% ({win_count}/{trade_count})

📈 最大利益: {max_ticker} ({max_profit:+.2f}%)
📉 最大損失: {min_ticker} ({min_profit:+.2f}%)"""
        
        send_discord_notification(WEBHOOK_URL, message)
        logger.info(f"サマリー通知完了: 総損益={total_profit:.2f}%, 取引数={trade_count}")
    
    except Exception as e:
        logger.error(f"サマリー通知エラー: {str(e)}")


def monitor():
    """メイン監視ループ (V3プロ版)"""
    config = load_config()
    if not config: return
    tickers = [d['t'] for d in config['details']]
    params_all = {d['t']: d['params'] for d in config['details']}
    sectors_map = {d['t']: d.get('sector', '') for d in config['details']}
    
    # Ver 11.0: 禁止フラグマップの作成
    disabled_map = {
        d['t']: {
            'long': d.get('long_disabled', False),
            'short': d.get('short_disabled', False)
        }
        for d in config['details']
    }
    
    # 禁止銘柄の通知
    disabled_info = []
    for ticker, flags in disabled_map.items():
        if flags['long'] or flags['short']:
            disabled_sides = []
            if flags['long']: disabled_sides.append('LONG')
            if flags['short']: disabled_sides.append('SHORT')
            disabled_info.append(f"{ticker}: {'/'.join(disabled_sides)}禁止")
    
    startup_msg = "📡 **Version 11.0 V3プロ版 監視起動**\n🌐 マクロ・アライメント戦略搭載"
    if disabled_info:
        startup_msg += f"\n\n🚫 **エントリー禁止設定:**\n" + "\n".join(disabled_info)
    
    send_discord_notification(WEBHOOK_URL, startup_msg)
    
    try:
        # 地合い判定
        while datetime.now(pytz.timezone('Asia/Tokyo')).time() < dt_time.fromisoformat(MARKET_SENTIMENT['judgment_time']): time.sleep(10)
        threshold_adj = 0.0 # 簡易化
        
        # 監視開始待機
        while datetime.now(pytz.timezone('Asia/Tokyo')).time() < dt_time.fromisoformat(MONITORING_LOOP['start_time']): time.sleep(10)
        
        # V3: マクロ指標取得（09:30に1回実行）
        global macro_sentiment
        from utils import fetch_macro_sentiment
        macro_sentiment = fetch_macro_sentiment()
        
        send_discord_notification(
            WEBHOOK_URL, 
            f"📊 **V3マクロ指標取得完了**\n"
            f"VIX: {macro_sentiment.get('vix_value', 0):.2f}\n"
            f"SOX変化率: {macro_sentiment.get('sox_chg', 0):+.2f}%\n"
            f"TNX変化率: {macro_sentiment.get('tnx_chg', 0):+.2f}%"
        )
        
        # オープニング分析
        calculate_opening_analysis(tickers)
        
        while True:
            current_time = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            
            if current_time >= dt_time.fromisoformat(MONITORING_LOOP['end_time']):
                # Ver 12.0: 15:00サマリー通知
                send_daily_summary()
                break
            
            raw_data = fetch_yfinance_data(tickers, period='5d', interval='5m')
            for ticker in tickers:
                try:
                    df = super_flatten_columns(raw_data[ticker] if len(tickers)>1 else raw_data)
                    df_ind = calculate_technical_indicators(df)
                    if TREND_FILTER['enabled']:
                        df_ind['ma_15m_20'] = calculate_ma_from_higher_timeframe(df_ind, TREND_FILTER['ma_period'])
                    
                    cooldown = load_daily_cooldown()  # Ver 12.0: 毎回読み込み
                    
                    if position_manager.has_position(ticker):
                        check_exit_signal(ticker, df_ind, cooldown)
                    else:
                        p = params_all[ticker]
                        sector = sectors_map.get(ticker, '')
                        disabled = disabled_map.get(ticker, {'long': False, 'short': False})
                        check_new_signal(ticker, df_ind, p['long'], p['short'], threshold_adj, cooldown, 0.0, sector, disabled)
                except: continue
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    monitor()