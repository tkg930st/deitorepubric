#!/usr/bin/env python3
"""
monitor.py - Version 14.0 プロ版
変更点:
- スケジューラ遅延対応: MONITOR_START_TIME環境変数で待機時刻を制御（2時間遅延許容）
- ① TP1/TRAILING_EXIT後も当日クールダウンを適用（往復ビンタ防止）
- ② シャンデリア出口: 直近N本高値/安値 - ATR×2.5 でゆったりトレーリング
- ③ エントリーフィルター強化: RSI過熱/VWAP乖離超過/時間帯カットオフ
- ④ VIX高時はSL幅変更なく推奨ロット50%を通知
- ⑤ RR比強制: tp_mul >= sl_mul × 1.5 を送信時に保証
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
    SIGNAL_THRESHOLDS, SECTOR_ALIGNMENT
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, calculate_ma_from_higher_timeframe,
    send_discord_notification, safe_get,
    check_divergence, check_trend_filter
)
from backtest_engine import calculate_single_score, calculate_boost_score
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
rival_data_cache: Dict[str, pd.DataFrame] = {}  # Ver 13.5: ライバルデータキャッシュ


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
                'cooldown_overridden', 'score', 'threshold',
                'sector_alignment', 'volume_accel', 'divergence_bonus'
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


def fetch_rival_data(rivals: list) -> Dict[str, pd.DataFrame]:
    """
    Ver 13.5: ライバル銘柄の最新5分足データを取得し、キャッシュに格納
    
    Args:
        rivals: ライバル銘柄コードリスト
    
    Returns:
        {ticker: DataFrame} の辞書
    """
    global rival_data_cache
    
    if not rivals:
        return {}
    
    try:
        raw_data = fetch_yfinance_data(rivals, period='5d', interval='5m')
        
        for rival_ticker in rivals:
            try:
                if len(rivals) > 1:
                    rival_raw = raw_data[rival_ticker] if rival_ticker in raw_data.columns.get_level_values(0) else pd.DataFrame()
                else:
                    rival_raw = raw_data
                
                rival_df = super_flatten_columns(rival_raw)
                if not rival_df.empty:
                    rival_data_cache[rival_ticker] = rival_df
            except Exception:
                continue
        
        logger.info(f"ライバルデータ取得完了: {len(rival_data_cache)}銘柄")
    except Exception as e:
        logger.error(f"ライバルデータ取得エラー: {str(e)}")
    
    return rival_data_cache


def calculate_realtime_boost(df: pd.DataFrame, side: str,
                              rival_dfs: Dict[str, pd.DataFrame]) -> Dict[str, float]:
    """
    Ver 13.5: リアルタイム・ブーストスコア計算
    
    backtest_engine.calculate_boost_score と同一ロジックをリアルタイムで適用
    
    Returns:
        {'sector_alignment': float, 'volume_accel': float, 'divergence_bonus': float, 'total': float}
    """
    boost_detail = {
        'sector_alignment': 0.0,
        'volume_accel': 0.0,
        'divergence_bonus': 0.0,
        'total': 0.0
    }
    
    if df.empty or len(df) < 2:
        return boost_detail
    
    confirmed_row = df.iloc[-2]
    idx = len(df) - 2
    
    # --- セクター・アライメント (+15点) ---
    if SECTOR_ALIGNMENT['enabled'] and rival_dfs:
        aligned_count = 0
        for rival_ticker, rival_df in rival_dfs.items():
            if rival_df is None or rival_df.empty:
                continue
            curr_dt = confirmed_row.name
            if curr_dt in rival_df.index:
                rival_row = rival_df.loc[curr_dt]
            else:
                rival_idx_pos = rival_df.index.get_indexer([curr_dt], method='ffill')[0]
                if rival_idx_pos < 0 or rival_idx_pos >= len(rival_df):
                    continue
                rival_row = rival_df.iloc[rival_idx_pos]
            
            try:
                rival_open = rival_row['open']
                rival_close = rival_row['close']
                if rival_open == 0:
                    continue
                if side == 'long' and rival_close > rival_open:
                    aligned_count += 1
                elif side == 'short' and rival_close < rival_open:
                    aligned_count += 1
            except (KeyError, TypeError):
                continue
        
        if aligned_count >= SECTOR_ALIGNMENT['min_aligned_rivals']:
            boost_detail['sector_alignment'] = SECTOR_ALIGNMENT['alignment_score']
    
    # --- 出来高加速 (+10点) ---
    if idx >= 1:
        prev_volume = df.iloc[idx - 1].get('volume', 0)
        curr_volume = confirmed_row.get('volume', 0)
        curr_rvol = safe_get(confirmed_row, 'rvol', 1.0)
        if prev_volume > 0 and curr_volume > prev_volume and curr_rvol > SECTOR_ALIGNMENT['volume_accel_rvol_threshold']:
            boost_detail['volume_accel'] = SECTOR_ALIGNMENT['volume_accel_score']
    
    # --- ダイバージェンス・ボーナス (+20点) ---
    if DIVERGENCE['enabled'] and idx >= DIVERGENCE['lookback']:
        df_recent = df.iloc[idx - DIVERGENCE['lookback'] + 1: idx + 1]
        div = check_divergence(df_recent, DIVERGENCE['lookback'])
        if side == 'long' and div.get('bullish'):
            boost_detail['divergence_bonus'] = SECTOR_ALIGNMENT['divergence_bonus_score']
        elif side == 'short' and div.get('bearish'):
            boost_detail['divergence_bonus'] = SECTOR_ALIGNMENT['divergence_bonus_score']
    
    boost_detail['total'] = (boost_detail['sector_alignment'] + 
                              boost_detail['volume_accel'] + 
                              boost_detail['divergence_bonus'])
    
    return boost_detail


def check_new_signal(ticker: str, df: pd.DataFrame, params_long: Dict,
                     params_short: Dict, threshold_adjustment: float,
                     cooldown: Dict, market_sentiment_value: float,
                     sector: str = '', disabled: Dict = None,
                     rivals: list = None) -> None:
    """新規シグナルチェック (Ver 14.0: エントリーフィルター強化 + VIXリスク調整)"""
    if disabled is None:
        disabled = {'long': False, 'short': False}
    if df.empty or len(df) < 50: return

    # ③ 時間帯フィルター: エントリーカットオフ後は新規エントリー禁止
    # ENTRY_CUTOFF_TIME環境変数で上書き可能（AMワークフロー: 10:30, PMワークフロー: 14:00）
    entry_cutoff_str = os.environ.get('ENTRY_CUTOFF_TIME', MONITORING_LOOP.get('am_entry_cutoff', '10:30'))
    entry_cutoff = dt_time.fromisoformat(entry_cutoff_str)
    current_jst_time = datetime.now(pytz.timezone('Asia/Tokyo')).time()
    if current_jst_time >= entry_cutoff:
        logger.debug(f"{ticker}: エントリーカットオフ({entry_cutoff_str})超過 → スキップ")
        return

    # ボラティリティフィルター
    atr_ma = df['atr_14'].rolling(window=50).mean().iloc[-1]
    current_atr = df['atr_14'].iloc[-1]
    if current_atr < atr_ma: return

    confirmed_row = df.iloc[-2]
    check_key = f"{ticker}_{confirmed_row.name}"
    if check_key in processed_timestamps: return
    processed_timestamps.add(check_key)

    if position_manager.has_position(ticker): return

    # Ver 12.0: 当日制限チェック（SL・TP1・TRAILING_EXIT全て対象）
    is_cooldown = is_cooldown_active(ticker, cooldown)
    cooldown_overridden = False

    if is_cooldown:
        # ダイバージェンス検知で制限解除可能
        if DIVERGENCE['enabled'] and len(df) >= DIVERGENCE['lookback']:
            df_recent = df.iloc[-DIVERGENCE['lookback']:]
            div = check_divergence(df_recent, DIVERGENCE['lookback'])

            if div.get('bullish') or div.get('bearish'):
                del cooldown[ticker]
                save_daily_cooldown(cooldown)
                cooldown_overridden = True
                logger.info(f"{ticker}: ダイバージェンス検知により当日制限解除")
            else:
                logger.debug(f"{ticker}: 当日制限中のためスキップ")
                return
        else:
            logger.debug(f"{ticker}: 当日制限中のためスキップ")
            return

    drive_pct = opening_drives.get(ticker, {}).get('drive_pct', 0.0)
    ma15_value = confirmed_row.get('ma_15m_20', 0)
    current_price = confirmed_row['close']

    # ③ RSI過熱感・VWAP乖離チェック（共通）
    rsi_val = safe_get(confirmed_row, 'rsi_14', 50)
    vwap_dev_val = safe_get(confirmed_row, 'vwap_dev', 0)
    rsi_overbought = SIGNAL_THRESHOLDS.get('rsi_overbought', 70)
    rsi_oversold = SIGNAL_THRESHOLDS.get('rsi_oversold', 30)
    vwap_dev_max = SIGNAL_THRESHOLDS.get('vwap_dev_max', 2.5)

    # V3: マクロ指標による動的調整
    global macro_sentiment
    vix_value = macro_sentiment.get('vix_value', 0.0)
    sox_chg = macro_sentiment.get('sox_chg', 0.0)
    tnx_chg = macro_sentiment.get('tnx_chg', 0.0)

    # V3-A: VIX > 20 → 閾値厳格化、TP拡張、④ SL幅は変えずロット半減を通知
    v3_threshold_adj = 0.0
    v3_tp_multiplier = 1.0
    v3_sl_multiplier = 1.0   # ④ SL幅は変更しない
    v3_risk_multiplier = 1.0  # ④ VIX高時: 推奨ロット倍率

    if vix_value > 20:
        v3_threshold_adj += 5.0   # エントリーを厳しく
        v3_tp_multiplier = 1.25   # 利確幅を拡張
        v3_risk_multiplier = 0.5  # ④ ロットを通常の50%に削減
        logger.info(f"{ticker}: VIX > 20 → 閾値+5.0, TP×1.25, 推奨ロット×0.5")
    
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

    # Ver 13.5: ライバルデータからブーストスコアを計算
    global rival_data_cache
    rival_dfs_for_ticker = {}
    if rivals:
        for r in rivals:
            if r in rival_data_cache:
                rival_dfs_for_ticker[r] = rival_data_cache[r]

    # LONG
    if not disabled.get('long', False):
        # ③ RSI過熱感チェック: 既にRSI≥70の場合はオーバーボート → LONGスキップ
        if rsi_val >= rsi_overbought:
            logger.debug(f"{ticker}: LONG スキップ - RSI過熱({rsi_val:.1f} >= {rsi_overbought})")
        # ③ VWAP上方乖離超過: 既に上に乗り過ぎ → LONGスキップ
        elif vwap_dev_val >= vwap_dev_max:
            logger.debug(f"{ticker}: LONG スキップ - VWAP上方乖離超過({vwap_dev_val:.2f}% >= {vwap_dev_max}%)")
        else:
            boost_long = 20 if drive_pct > 0.5 else 0

            # Ver 13.5: リアルタイムブースト計算
            rt_boost_long = calculate_realtime_boost(df, 'long', rival_dfs_for_ticker)
            boost_long += rt_boost_long['total']

            score_long = calculate_single_score(confirmed_row, params_long, 'long', boost_long)
            threshold_long = params_long['threshold'] + threshold_adjustment + v3_threshold_adj

            if score_long >= threshold_long:
                if check_trend_filter(current_price, ma15_value, 'LONG'):
                    journal_entry = {
                        'ticker': ticker, 'side': 'LONG', 'entry_price': current_price,
                        'entry_time': confirmed_row.name.isoformat(),
                        'market_sentiment': market_sentiment_value,
                        'rsi': rsi_val,
                        'vwap_dev': vwap_dev_val,
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
                        'threshold': threshold_long,
                        'sector_alignment': rt_boost_long['sector_alignment'],
                        'volume_accel': rt_boost_long['volume_accel'],
                        'divergence_bonus': rt_boost_long['divergence_bonus']
                    }
                    record_trade_journal(journal_entry)

                    tp_mul = params_long['tp_mul'] * 2.0 if drive_pct > 1.0 else params_long['tp_mul']
                    tp_mul = tp_mul * v3_tp_multiplier
                    sl_mul = params_long['sl_mul'] * v3_sl_multiplier
                    send_new_signal_pro(ticker, confirmed_row, score_long, params_long, 'LONG',
                                        tp_mul, sl_mul, v3_risk_multiplier)
    else:
        logger.debug(f"{ticker}: LONGはエントリー禁止（利益<5%）")

    # SHORT
    if not disabled.get('short', False):
        # ③ RSI売られ過ぎチェック: 既にRSI≤30の場合はオーバーソールド → SHORTスキップ
        if rsi_val <= rsi_oversold:
            logger.debug(f"{ticker}: SHORT スキップ - RSI売られ過ぎ({rsi_val:.1f} <= {rsi_oversold})")
        # ③ VWAP下方乖離超過: 既に下に落ち過ぎ → SHORTスキップ
        elif vwap_dev_val <= -vwap_dev_max:
            logger.debug(f"{ticker}: SHORT スキップ - VWAP下方乖離超過({vwap_dev_val:.2f}% <= -{vwap_dev_max}%)")
        else:
            boost_short = 20 if drive_pct < -0.5 else 0

            # Ver 13.5: リアルタイムブースト計算
            rt_boost_short = calculate_realtime_boost(df, 'short', rival_dfs_for_ticker)
            boost_short += rt_boost_short['total']

            score_short = calculate_single_score(confirmed_row, params_short, 'short', boost_short)
            threshold_short = params_short['threshold'] + threshold_adjustment + v3_threshold_adj

            if score_short >= threshold_short:
                if check_trend_filter(current_price, ma15_value, 'SHORT'):
                    journal_entry = {
                        'ticker': ticker, 'side': 'SHORT', 'entry_price': current_price,
                        'entry_time': confirmed_row.name.isoformat(),
                        'market_sentiment': market_sentiment_value,
                        'rsi': rsi_val,
                        'vwap_dev': vwap_dev_val,
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
                        'threshold': threshold_short,
                        'sector_alignment': rt_boost_short['sector_alignment'],
                        'volume_accel': rt_boost_short['volume_accel'],
                        'divergence_bonus': rt_boost_short['divergence_bonus']
                    }
                    record_trade_journal(journal_entry)

                    tp_mul = params_short['tp_mul'] * 2.0 if drive_pct < -1.0 else params_short['tp_mul']
                    tp_mul = tp_mul * v3_tp_multiplier
                    sl_mul = params_short['sl_mul'] * v3_sl_multiplier
                    send_new_signal_pro(ticker, confirmed_row, score_short, params_short, 'SHORT',
                                        tp_mul, sl_mul, v3_risk_multiplier)
    else:
        logger.debug(f"{ticker}: SHORTはエントリー禁止（利益<5%）")


def send_new_signal_pro(ticker: str, row: pd.Series, score: float, params: Dict, side: str,
                        tp_mul: float, sl_mul: float = None, risk_multiplier: float = 1.0):
    """ポジション登録 (Ver 14.0: RR強制 + シャンデリア出口 + VIXリスク調整通知)"""
    if sl_mul is None:
        sl_mul = params['sl_mul']

    # ⑤ RR比強制: tp_mul >= sl_mul × min_rr_ratio
    min_rr_ratio = MONITORING_LOOP.get('min_rr_ratio', 1.5)
    if tp_mul < sl_mul * min_rr_ratio:
        adjusted_tp = sl_mul * min_rr_ratio
        logger.info(f"{ticker}: RR不足(tp={tp_mul:.2f}/sl={sl_mul:.2f}, RR={tp_mul/sl_mul:.2f}) → tp_mul={adjusted_tp:.2f}に調整")
        tp_mul = adjusted_tp

    atr = safe_get(row, 'atr_14', row['close'] * 0.02)
    entry_price = row['close']

    tp1_mul = POSITION_MANAGEMENT['tp1_multiplier']  # 1.5
    chandelier_mul = POSITION_MANAGEMENT.get('chandelier_atr_multiplier', 2.5)
    chandelier_lookback = POSITION_MANAGEMENT.get('chandelier_lookback', 5)

    if side == 'LONG':
        tp1 = entry_price + (atr * tp1_mul)
        tp2 = entry_price + (atr * tp_mul)  # トレーリング参考値
        sl = entry_price - (atr * sl_mul)
    else:  # SHORT
        tp1 = entry_price - (atr * tp1_mul)
        tp2 = entry_price - (atr * tp_mul)
        sl = entry_price + (atr * sl_mul)

    rr_actual = tp_mul / sl_mul if sl_mul > 0 else 0

    # ④ VIX高時のロット警告
    risk_note = ""
    if risk_multiplier < 1.0:
        risk_note = f"\n⚠️ **VIX高リスク → 推奨ロット: 通常の{risk_multiplier * 100:.0f}% に削減**"

    msg = (f"🛡️ **新規シグナル (Ver 14.0): {side}**\n"
           f"銘柄: {ticker}\n"
           f"価格: ¥{entry_price:,.1f}\n"
           f"TP1: ¥{tp1:,.1f} (ATR×{tp1_mul}) → 50%決済\n"
           f"TP2: シャンデリア出口 (直近{chandelier_lookback}本高値 - ATR×{chandelier_mul})\n"
           f"SL: ¥{sl:,.1f} (ATR×{sl_mul:.2f})\n"
           f"RR比: {rr_actual:.2f}\n"
           f"スコア: {score:.1f}"
           f"{risk_note}")

    if send_discord_notification(WEBHOOK_URL, msg):
        pos_data = {
            'entry_atr': atr, 'tp_mul_actual': tp_mul, 'sl_mul_actual': sl_mul,
            'chandelier_atr_mul': chandelier_mul  # Ver 14.0
        }
        extended_params = {**params, **pos_data}
        position_manager.add_position(ticker, side, entry_price, atr, sl, tp1, tp2, extended_params)


def check_exit_signal(ticker: str, df: pd.DataFrame, cooldown: Dict) -> None:
    """エグジット監視 (Ver 14.0: シャンデリア出口 + TP後クールダウン)"""
    position = position_manager.get_position(ticker)
    if not position: return

    latest_row = df.iloc[-1]
    high_price = latest_row['high']
    low_price = latest_row['low']
    side = position['side']

    stop_loss = position['stop_loss']
    tp1 = position.get('tp1', position['tp2'])
    tp1_hit = position.get('tp1_hit', False)
    atr = position.get('atr', position.get('params', {}).get('entry_atr', 0))

    # ② シャンデリア出口パラメータ（Ver 14.0）
    chandelier_lookback = POSITION_MANAGEMENT.get('chandelier_lookback', 5)
    chandelier_mul = POSITION_MANAGEMENT.get('chandelier_atr_multiplier', 2.5)

    try:
        # --- TP1未達時: 損切チェック ---
        if not tp1_hit:
            if (side == 'LONG' and low_price <= stop_loss) or \
               (side == 'SHORT' and high_price >= stop_loss):
                result = position_manager.close_position(ticker, stop_loss, 'SL')
                send_exit_notification(result)
                add_to_cooldown(ticker, 'SL', cooldown)
                return

        # --- TP1チェック ---
        if not tp1_hit:
            if (side == 'LONG' and high_price >= tp1) or \
               (side == 'SHORT' and low_price <= tp1):
                profit = position_manager.execute_tp1(ticker, tp1)
                # ② シャンデリア出口: TP1後は直近N本の高値/安値からATR×chandelier_mulのゆったりトレーリング
                recent_slice = df.iloc[-chandelier_lookback:]
                if side == 'LONG':
                    highest_high = recent_slice['high'].max()
                    initial_trail = highest_high - (atr * chandelier_mul)
                else:
                    lowest_low = recent_slice['low'].min()
                    initial_trail = lowest_low + (atr * chandelier_mul)
                position_manager.set_trailing_stop(ticker, initial_trail)
                send_tp1_notification(ticker, tp1, profit)
                # ① TP1後も当日制限に追加（同一銘柄の再エントリーを禁止）
                add_to_cooldown(ticker, 'TP1', cooldown)
                return

        # --- Ver 14.0: TP1後はシャンデリア出口で追従 ---
        if tp1_hit:
            trailing_stop = position.get('trailing_stop', stop_loss)

            if side == 'LONG':
                # ② シャンデリア: 直近N本の最高値 - ATR × chandelier_mul
                recent_slice = df.iloc[-chandelier_lookback:]
                highest_high = recent_slice['high'].max()
                new_trail = highest_high - (atr * chandelier_mul)
                if new_trail > trailing_stop:
                    trailing_stop = new_trail
                    position_manager.update_trailing_stop(ticker, trailing_stop)

                if low_price <= trailing_stop:
                    result = position_manager.close_position(ticker, trailing_stop, 'TRAILING_EXIT')
                    send_exit_notification(result)
                    # ① TRAILING_EXIT後も当日制限に追加
                    add_to_cooldown(ticker, 'TRAILING_EXIT', cooldown)
                    return
            else:  # SHORT
                # ② シャンデリア: 直近N本の最安値 + ATR × chandelier_mul
                recent_slice = df.iloc[-chandelier_lookback:]
                lowest_low = recent_slice['low'].min()
                new_trail = lowest_low + (atr * chandelier_mul)
                if new_trail < trailing_stop:
                    trailing_stop = new_trail
                    position_manager.update_trailing_stop(ticker, trailing_stop)

                if high_price >= trailing_stop:
                    result = position_manager.close_position(ticker, trailing_stop, 'TRAILING_EXIT')
                    send_exit_notification(result)
                    # ① TRAILING_EXIT後も当日制限に追加
                    add_to_cooldown(ticker, 'TRAILING_EXIT', cooldown)
                    return

    except Exception as e:
        logger.error(f"エグジット監視エラー ({ticker}): {str(e)}")


def send_tp1_notification(ticker: str, tp1_price: float, profit: float) -> None:
    """TP1達成通知（Ver 14.0: シャンデリア出口移行通知）"""
    chandelier_mul = POSITION_MANAGEMENT.get('chandelier_atr_multiplier', 2.5)
    chandelier_lookback = POSITION_MANAGEMENT.get('chandelier_lookback', 5)
    message = (f"✅ **TP1達成: {ticker}**\n"
               f"🎯 50%利確完了\n"
               f"・価格: ¥{tp1_price:,.1f}\n"
               f"・損益: {profit:+.2f}%\n"
               f"・損切を建値に移動しました\n"
               f"・残り50%はシャンデリア出口で追従中\n"
               f"  （直近{chandelier_lookback}本高値/安値 - ATR×{chandelier_mul}）\n"
               f"⚠️ 当日同銘柄の再エントリーは禁止されました")
    send_discord_notification(WEBHOOK_URL, message)


def send_exit_notification(result: Dict) -> None:
    """エグジット通知（Ver 13.5）"""
    ticker = result['ticker']
    reason = result['exit_reason']
    profit = result['total_profit']
    tp1_hit = result.get('tp1_hit', False)
    
    if tp1_hit:
        message = f"🛑 **ポジションクローズ: {ticker}**\n理由: {reason}\n総合損益: {profit:+.2f}%\n（TP1: 50%利確済み + 残り50%トレーリング決済）"
    else:
        message = f"🛑 **ポジションクローズ: {ticker}**\n理由: {reason}\n総合損益: {profit:+.2f}%"
    
    send_discord_notification(WEBHOOK_URL, message)


def force_close_remaining() -> None:
    """セッション終了時: 全ポジションを強制決済してtrade_results.csvに確実に記録する

    ポジションが残ったままセッションが終了すると、PMワークフローの
    "Clear positions.json" ステップでポジションが消失し、trade_results.csv に
    記録されず「取引なし」と判定されてしまう問題を防止する。
    """
    positions = position_manager.get_all_positions()
    if not positions:
        logger.info("強制決済: 残ポジションなし")
        return

    tickers_to_close = list(positions.keys())
    logger.info(f"セッション終了: {len(tickers_to_close)}件のポジションを強制決済します")

    # 最新価格を取得して決済
    current_prices = {}
    try:
        raw_data = fetch_yfinance_data(tickers_to_close, period='1d', interval='5m')
        for ticker in tickers_to_close:
            try:
                df = super_flatten_columns(raw_data[ticker] if len(tickers_to_close) > 1 else raw_data)
                if not df.empty:
                    current_prices[ticker] = df['close'].iloc[-1]
            except Exception:
                pass
    except Exception as e:
        logger.error(f"強制決済: 価格取得失敗 ({str(e)})")

    # 価格取得できなかった銘柄はエントリー価格をフォールバック（損益0%で記録を残す）
    for ticker in tickers_to_close:
        if ticker not in current_prices:
            current_prices[ticker] = positions[ticker].get('entry_price', 0)
            logger.warning(f"強制決済: {ticker}の最新価格を取得できず、エントリー価格で決済します")

    results = position_manager.force_close_all(current_prices)
    for result in results:
        send_exit_notification(result)

    logger.info(f"強制決済完了: {len(results)}件をtrade_results.csvに記録しました")


def send_daily_summary() -> None:
    """取引サマリー通知（Ver 14.0: exit_timeベースに修正）"""
    try:
        results_file = POSITION_MANAGEMENT['trade_results_file']

        if not os.path.exists(results_file):
            logger.info("取引結果ファイルが存在しません")
            send_discord_notification(WEBHOOK_URL, "📊 **本日の取引サマリー**\n取引結果ファイルが存在しません")
            return

        df_results = pd.read_csv(results_file)
        if df_results.empty:
            send_discord_notification(WEBHOOK_URL, "📊 **本日の取引サマリー**\n本日は取引がありませんでした")
            return

        # exit_time でフィルタ（前場で建てて後場で閉じたケースも正しく集計）
        df_results['exit_time'] = pd.to_datetime(df_results['exit_time'])
        today = datetime.now().date()  # UTC（GitHub Actions環境）
        df_today = df_results[df_results['exit_time'].dt.date == today]

        if df_today.empty:
            send_discord_notification(WEBHOOK_URL, "📊 **本日の取引サマリー**\n本日は決済された取引がありませんでした")
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

        # 決済理由の内訳
        reasons = df_today['exit_reason'].value_counts()
        reason_str = " / ".join([f"{r}: {c}件" for r, c in reasons.items()])

        message = f"""📊 **本日の取引サマリー**

💰 総損益: {total_profit:+.2f}%
🔄 取引回数: {trade_count}回
✅ 勝率: {win_rate:.1f}% ({win_count}/{trade_count})
🔖 決済理由: {reason_str}

📈 最大利益: {max_ticker} ({max_profit:+.2f}%)
📉 最大損失: {min_ticker} ({min_profit:+.2f}%)"""

        send_discord_notification(WEBHOOK_URL, message)
        logger.info(f"サマリー通知完了: 総損益={total_profit:.2f}%, 取引数={trade_count}")

    except Exception as e:
        logger.error(f"サマリー通知エラー: {str(e)}")
        send_discord_notification(WEBHOOK_URL, f"📊 **本日の取引サマリー**\n⚠️ サマリー生成エラー: {str(e)}")


def monitor():
    """メイン監視ループ (Ver 13.5 プロ版)"""
    config = load_config()
    if not config: return
    tickers = [d['t'] for d in config['details']]
    params_all = {d['t']: d['params'] for d in config['details']}
    sectors_map = {d['t']: d.get('sector', '') for d in config['details']}
    rivals_map = {d['t']: d.get('rivals', []) for d in config['details']}  # Ver 13.5: ライバル取得

    # 環境変数から終了時刻・最大実行時間を最初に取得（AMセッション/PMセッション別制御）
    # MONITOR_END_TIME: ワークフロー側からセッションに応じた終了時刻を指定（例: AM='11:25', PM='14:55'）
    # MAX_RUNTIME_MINUTES: ワークフローのtimeout-minutesより短い値を指定してグレースフルに終了
    monitor_end_time_str = os.environ.get('MONITOR_END_TIME', MONITORING_LOOP['end_time'])
    max_runtime_minutes = int(os.environ.get('MAX_RUNTIME_MINUTES', '345'))  # デフォルト: 5時間45分
    monitor_end_time = dt_time.fromisoformat(monitor_end_time_str)
    logger.info(f"監視設定: 終了時刻={monitor_end_time_str}, 最大実行時間={max_runtime_minutes}分")

    # スケジューラ遅延対策: 既に終了時刻を過ぎている場合は即座に終了
    current_jst = datetime.now(pytz.timezone('Asia/Tokyo')).time()
    if current_jst >= monitor_end_time:
        msg = f"⚠️ **監視終了時刻（{monitor_end_time_str}）を既に過ぎています（現在: {current_jst.strftime('%H:%M')}）。スキップします。**"
        logger.info(msg)
        send_discord_notification(WEBHOOK_URL, msg)
        return

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

    startup_msg = "📡 **Version 14.0 プロ版 監視起動**\n🌐 マクロ・アライメント + シャンデリア出口 + RR≥1.5保証"
    if disabled_info:
        startup_msg += f"\n\n🚫 **エントリー禁止設定:**\n" + "\n".join(disabled_info)

    send_discord_notification(WEBHOOK_URL, startup_msg)
    start_run_time = time.time() # 実行開始時間の記録

    try:
        # スケジューラ遅延対応: MONITOR_START_TIME環境変数で開始待機時刻を制御
        # AMワークフロー: MONITOR_START_TIME='09:30'（08:00 JSTから最大1.5時間待機）
        # PMワークフロー: MONITOR_START_TIME='12:30'（11:30 JSTから最大1時間待機）
        # 2時間遅延が発生した場合でも開始時刻が既に過ぎていれば即座に監視を開始する
        monitor_start_time_str = os.environ.get('MONITOR_START_TIME', MONITORING_LOOP['start_time'])
        monitor_start_time = dt_time.fromisoformat(monitor_start_time_str)
        logger.info(f"監視開始待機時刻: {monitor_start_time_str}（既に過ぎていれば即開始）")

        last_log_minute = -1
        while datetime.now(pytz.timezone('Asia/Tokyo')).time() < monitor_start_time:
            current_jst = datetime.now(pytz.timezone('Asia/Tokyo')).time()
            if current_jst >= monitor_end_time:
                logger.info("待機中に終了時刻に達したため終了します")
                return
            # 10分ごとに残り時間をログ出力
            remaining_sec = (
                datetime.combine(datetime.today(), monitor_start_time) -
                datetime.combine(datetime.today(), current_jst)
            ).seconds
            remaining_min = remaining_sec // 60
            if remaining_min != last_log_minute and remaining_min % 10 == 0:
                logger.info(f"監視開始待機中... 残り約{remaining_min}分 → {monitor_start_time_str}に開始予定")
                last_log_minute = remaining_min
            time.sleep(10)

        threshold_adj = 0.0

        # V3: マクロ指標取得（09:30に1回実行）
        global macro_sentiment
        from utils import fetch_macro_sentiment

        try:
            macro_sentiment = fetch_macro_sentiment()

            # 取得成功の通知
            send_discord_notification(
                WEBHOOK_URL,
                f"📊 **V3マクロ指標取得完了**\n"
                f"VIX: {macro_sentiment.get('vix_value', 18.0):.2f}\n"
                f"SOX変化率: {macro_sentiment.get('sox_chg', 0):+.2f}%\n"
                f"TNX変化率: {macro_sentiment.get('tnx_chg', 0):+.2f}%"
            )
        except Exception as e:
            logger.error(f"マクロ指標取得エラー: {str(e)}")
            # デフォルト値を使用
            macro_sentiment = {
                'sox_chg': 0.0,
                'tnx_chg': 0.0,
                'vix_value': 18.0,
                'vix_chg': 0.0,
                'jpy_chg': 0.0
            }
            send_discord_notification(
                WEBHOOK_URL,
                "⚠️ **マクロ指標取得失敗**\nデフォルト値を使用します\n（V3戦略は一部制限されます）"
            )

        # オープニング分析
        calculate_opening_analysis(tickers)

        # Ver 13.5: ライバル銘柄の全ティッカーリスト収集
        all_rival_tickers = set()
        for rival_list in rivals_map.values():
            all_rival_tickers.update(rival_list)
        all_rival_tickers -= set(tickers)  # 監視銘柄と重複するものを除外

        while True:
            current_time = datetime.now(pytz.timezone('Asia/Tokyo')).time()

            # 最大実行時間チェック（GitHub Actions タイムアウトより先にグレースフル終了）
            elapsed_minutes = (time.time() - start_run_time) / 60
            if elapsed_minutes > max_runtime_minutes:
                send_discord_notification(WEBHOOK_URL, f"⚠️ **最大実行時間（{max_runtime_minutes}分）に達したため正常終了します。**")
                force_close_remaining()
                send_daily_summary()
                break

            if current_time >= monitor_end_time:
                # 監視終了時刻に達した → 残ポジションを強制決済してからサマリー通知
                logger.info(f"監視終了時刻({monitor_end_time_str})に達しました。終了処理を開始します。")
                force_close_remaining()
                send_daily_summary()
                break

            # --- メインデータ取得・シグナル処理 ---
            # データ取得失敗時にmonitorがクラッシュしないようtry/exceptで保護
            try:
                # Ver 13.5: ライバル銘柄データも同時取得
                if all_rival_tickers:
                    fetch_rival_data(list(all_rival_tickers))

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
                            rivals = rivals_map.get(ticker, [])
                            check_new_signal(ticker, df_ind, p['long'], p['short'], threshold_adj, cooldown, 0.0, sector, disabled, rivals)
                    except Exception as e:
                        logger.debug(f"{ticker} 処理エラー: {str(e)}")
                        continue
            except Exception as e:
                logger.error(f"データ取得/処理エラー（次のループで再試行）: {str(e)}")

            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt: 終了処理を実行します")
        force_close_remaining()
        send_daily_summary()
    except Exception as e:
        # 予期せぬ例外でもポジションを確実に決済して記録を残す
        logger.error(f"monitor() 予期せぬ例外: {str(e)}")
        try:
            force_close_remaining()
            send_daily_summary()
        except Exception as e2:
            logger.error(f"終了処理中のエラー: {str(e2)}")


if __name__ == "__main__":
    monitor()
