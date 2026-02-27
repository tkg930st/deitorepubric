"""
共通ユーティリティ関数 - Ver 15.3 (pandas-ta 依存排除版)
データ処理、テクニカル指標計算、時間管理などの共通機能
"""
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import logging
import time
import io
from datetime import datetime, time as dt_time
import pytz
from typing import List, Optional, Dict, Any
from functools import wraps
from config import DATA_FETCH, TRADING_HOURS, SIGNAL_THRESHOLDS

# ロガー設定
logger = logging.getLogger(__name__)


def retry_on_error(max_retries: int = DATA_FETCH['max_retries'], 
                   delay: float = DATA_FETCH['retry_delay']):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {str(e)}")
                        time.sleep(delay * (attempt + 1))
                    else:
                        logger.error(f"{func.__name__} failed after {max_retries} attempts: {str(e)}")
            raise last_exception
        return wrapper
    return decorator


@retry_on_error()
def get_jpx_list_with_sector() -> pd.DataFrame:
    try:
        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        df = pd.read_excel(io.BytesIO(res.content), engine='xlrd')
        target_markets = ['プライム（内国株式）', 'スタンダード（内国株式）', 'グロース（内国株式）']
        df_filtered = df[df['市場・商品区分'].isin(target_markets)].copy()
        df_filtered['ticker'] = df_filtered['コード'].apply(lambda x: f"{str(x)}.T")
        sector_column = '33業種区分' if '33業種区分' in df_filtered.columns else '業種'
        size_column = '規模区分' if '規模区分' in df_filtered.columns else None
        if size_column:
            exclude_sizes = ['TOPIX Core30', 'TOPIX Large70']
            df_filtered = df_filtered[~df_filtered[size_column].isin(exclude_sizes)].copy()
            result = df_filtered[['ticker', sector_column, size_column]].rename(
                columns={sector_column: 'sector', size_column: 'size_category'}
            )
        else:
            result = df_filtered[['ticker', sector_column]].rename(columns={sector_column: 'sector'})
            result['size_category'] = ''
        return result
    except Exception as e:
        logger.error(f"JPX銘柄リスト取得エラー: {str(e)}")
        raise


def super_flatten_columns(df: pd.DataFrame, ticker: str = "") -> pd.DataFrame:
    if df is None or df.empty: return pd.DataFrame()
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
        else:
            df.index = df.index.tz_convert('Asia/Tokyo')
    except Exception: pass
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(map(str, col)).lower().strip() for col in df.columns]
    else:
        df.columns = [str(c).lower().strip() for c in df.columns]
    mapping = {'adj close': 'close', 'close': 'close', 'high': 'high', 'low': 'low', 'open': 'open', 'volume': 'volume'}
    final_rename = {}
    for key, target in mapping.items():
        for actual in df.columns:
            if key in actual and target not in final_rename.values():
                final_rename[actual] = target
                break
    df = df.rename(columns=final_rename)
    keep = ['open', 'high', 'low', 'close', 'volume']
    existing = [c for c in keep if c in df.columns]
    return df[existing].copy().dropna()


def is_trading_hours(dt: datetime) -> bool:
    if dt.weekday() >= 5: return False
    if dt.tzinfo is None: dt = pytz.timezone('Asia/Tokyo').localize(dt)
    else: dt = dt.astimezone(pytz.timezone('Asia/Tokyo'))
    current_time = dt.time()
    morning_start = dt_time.fromisoformat(TRADING_HOURS['morning_start'])
    morning_end = dt_time.fromisoformat(TRADING_HOURS['morning_end'])
    afternoon_start = dt_time.fromisoformat(TRADING_HOURS['afternoon_start'])
    afternoon_end = dt_time.fromisoformat(TRADING_HOURS['afternoon_end'])
    avoid_minutes = TRADING_HOURS['avoid_close_minutes']
    close_avoid_time = (datetime.combine(datetime.today(), afternoon_end) - pd.Timedelta(minutes=avoid_minutes)).time()
    return (morning_start <= current_time <= morning_end) or (afternoon_start <= current_time <= close_avoid_time)


def filter_trading_hours(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df
    mask = df.index.to_series().apply(is_trading_hours)
    return df[mask].copy()


@retry_on_error()
def fetch_yfinance_data(tickers: List[str], period: str, interval: str, group_by: str = 'ticker') -> pd.DataFrame:
    return yf.download(tickers, period=period, interval=interval, group_by=group_by, auto_adjust=True, progress=False, threads=True)


def send_discord_notification(webhook_url: str, message: str) -> bool:
    if not webhook_url: return False
    try:
        # Discordの文字数制限(2000文字)に対応するための分割送信
        MAX_LEN = 1900 
        if len(message) <= MAX_LEN:
            res = requests.post(webhook_url, json={"content": message}, timeout=10)
            res.raise_for_status()
        else:
            # 行単位で分割
            lines = message.split('\n')
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > MAX_LEN:
                    res = requests.post(webhook_url, json={"content": current_chunk}, timeout=10)
                    res.raise_for_status()
                    time.sleep(1.0) # レート制限回避
                    current_chunk = line + '\n'
                else:
                    current_chunk += line + '\n'
            if current_chunk:
                res = requests.post(webhook_url, json={"content": current_chunk}, timeout=10)
                res.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Discord通知送信エラー: {str(e)}")
        return False


def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    テクニカル指標を計算 (RSI, ATR, VWAP, ADX, 一目均衡表)
    """
    if df.empty or len(df) < 20: return df
    df = df.copy()
    try:
        # RSI (Wilder方式: EMAを使用)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        # alpha=1/14 は Wilder's Smoothing (RMA) に相当
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['rsi_14'] = 100 - (100 / (1 + rs))
        
        # ATR (Wilder's EMA: α=1/14)
        high_low = df['high'] - df['low']
        high_cp = np.abs(df['high'] - df['close'].shift())
        low_cp = np.abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
        df['atr_14'] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        
        # VWAP (日次リセット版)
        v = df['volume']
        tp = (df['high'] + df['low'] + df['close']) / 3
        df['date_group'] = df.index.date
        cumsum_pv = (tp * v).groupby(df['date_group']).cumsum()
        cumsum_vol = v.groupby(df['date_group']).cumsum()
        df['vwap'] = cumsum_pv / (cumsum_vol + 1e-10) # ゼロ除算回避
        df['vwap_dev'] = ((df['close'] - df['vwap']) / (df['vwap'] + 1e-10)) * 100
        df.drop(columns=['date_group'], inplace=True)
        
        # ADX (Wilder方式: ATR_EMAと同じスムージングを使用)
        up_move = df['high'].diff()
        down_move = -df['low'].diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

        # DI = EMA(DM, 14) / ATR(EMA版)  ※ATRは既にEMAで計算済み
        smoothed_plus_dm = pd.Series(plus_dm, index=df.index).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        smoothed_minus_dm = pd.Series(minus_dm, index=df.index).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        plus_di = 100 * (smoothed_plus_dm / (df['atr_14'] + 1e-10))
        minus_di = 100 * (smoothed_minus_dm / (df['atr_14'] + 1e-10))
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        df['adx_14'] = dx.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        
        # RVOL (ローリング期間は SIGNAL_THRESHOLDS から参照)
        rvol_lookback = SIGNAL_THRESHOLDS.get('rvol_lookback', 5)
        df['rvol'] = df['volume'] / df['volume'].rolling(window=rvol_lookback).mean()
        
        # 一目均衡表
        tenkan_sen = (df['high'].rolling(window=9).max() + df['low'].rolling(window=9).min()) / 2
        kijun_sen = (df['high'].rolling(window=26).max() + df['low'].rolling(window=26).min()) / 2
        df['tenkan_sen'] = tenkan_sen
        df['kijun_sen'] = kijun_sen
        df['senkou_span_a'] = ((tenkan_sen + kijun_sen) / 2).shift(26)
        df['senkou_span_b'] = ((df['high'].rolling(window=52).max() + df['low'].rolling(window=52).min()) / 2).shift(26)

        df = df.fillna(0)
        df.columns = [str(c).lower() for c in df.columns]
    except Exception as e:
        logger.error(f"テクニカル指標計算エラー: {str(e)}")
    return df


def safe_get(row: pd.Series, key: str, default: Any = 0) -> Any:
    try: return row.get(key, default)
    except Exception: return default


def check_divergence(df: pd.DataFrame, lookback: int = 25) -> Dict[str, bool]:
    from config import DIVERGENCE
    result = {'bullish': False, 'bearish': False, 'reverse_bullish': False, 'reverse_bearish': False}
    if not DIVERGENCE['enabled']: return result
    if df.empty or len(df) < lookback + 5: return result
    try:
        recent = df.iloc[-lookback:].copy()
        if 'rsi_14' not in recent.columns or 'close' not in recent.columns: return result
        half = len(recent) // 2
        # 前半・後半の安値/高値を比較して極値ベースの判定を行う
        first_half = recent.iloc[:half]
        second_half = recent.iloc[half:]
        if first_half.empty or second_half.empty: return result

        # 価格とRSIの安値・高値
        p_low1, p_low2 = first_half['close'].min(), second_half['close'].min()
        p_high1, p_high2 = first_half['close'].max(), second_half['close'].max()
        r_at_p_low1 = first_half.loc[first_half['close'].idxmin(), 'rsi_14']
        r_at_p_low2 = second_half.loc[second_half['close'].idxmin(), 'rsi_14']
        r_at_p_high1 = first_half.loc[first_half['close'].idxmax(), 'rsi_14']
        r_at_p_high2 = second_half.loc[second_half['close'].idxmax(), 'rsi_14']

        if any(pd.isna(v) for v in [r_at_p_low1, r_at_p_low2, r_at_p_high1, r_at_p_high2]): return result

        rsi_t, prc_t = DIVERGENCE['rsi_threshold'], DIVERGENCE['price_threshold']

        p_low_chg = ((p_low2 / p_low1) - 1) * 100
        p_high_chg = ((p_high2 / p_high1) - 1) * 100
        r_low_chg = r_at_p_low2 - r_at_p_low1
        r_high_chg = r_at_p_high2 - r_at_p_high1

        # 通常の強気: 価格安値切り下げ、RSI安値切り上げ
        if p_low_chg < -prc_t and r_low_chg > rsi_t: result['bullish'] = True
        # 通常の弱気: 価格高値切り上げ、RSI高値切り下げ
        if p_high_chg > prc_t and r_high_chg < -rsi_t: result['bearish'] = True
        # 隠れた(リバース)強気: 価格安値切り上げ、RSI安値切り下げ
        if p_low_chg > prc_t and r_low_chg < -rsi_t: result['reverse_bullish'] = True
        # 隠れた(リバース)弱気: 価格高値切り下げ、RSI高値切り上げ
        if p_high_chg < -prc_t and r_high_chg > rsi_t: result['reverse_bearish'] = True
    except Exception: pass
    return result


def check_trend_filter(current_price: float, ma15_value: float, side: str) -> bool:
    if pd.isna(ma15_value) or ma15_value == 0: return False  # Block entries if missing data
    return current_price > ma15_value if side == 'LONG' else current_price < ma15_value


def detect_market_structure(df: pd.DataFrame, lookback: int = 5) -> Dict:
    if len(df) < lookback * 3: return {'type': None, 'direction': None, 'price': 0.0}
    try:
        df = df.copy()
        df['is_high'] = (df['high'] == df['high'].rolling(window=lookback*2+1, center=True).max())
        df['is_low'] = (df['low'] == df['low'].rolling(window=lookback*2+1, center=True).min())
        highs = df[df['is_high']]['high']
        lows = df[df['is_low']]['low']
        if len(highs) < 2 or len(lows) < 2: return {'type': None, 'direction': None, 'price': 0.0}
        last_high, prev_high = highs.iloc[-1], highs.iloc[-2]
        last_low, prev_low = lows.iloc[-1], lows.iloc[-2]
        current_price, prev_price = df['close'].iloc[-1], df['close'].iloc[-2]
        if prev_price >= last_low > current_price and last_low > prev_low:
             return {'type': 'CHoCH', 'direction': 'SHORT', 'price': last_low}
        if prev_price <= last_high < current_price and last_high < prev_high:
             return {'type': 'CHoCH', 'direction': 'LONG', 'price': last_high}
        if prev_price <= last_high < current_price: return {'type': 'BOS', 'direction': 'LONG', 'price': last_high}
        if prev_price >= last_low > current_price: return {'type': 'BOS', 'direction': 'SHORT', 'price': last_low}
        return {'type': None, 'direction': None, 'price': 0.0}
    except Exception: return {'type': None, 'direction': None, 'price': 0.0}


def calculate_ma_from_higher_timeframe(df_1m: pd.DataFrame, ma_period: int = 20) -> pd.Series:
    """
    1分足データから15分足の移動平均(M15)を計算し、1分足のインデックスに合わせる
    """
    try:
        # 1. 1m足を15m足にリサンプリング
        df_15m = df_1m.resample('15min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
        # 2. 15m足上でMAを計算
        ma_15m = df_15m['close'].rolling(window=ma_period).mean()
        # 3. 1m足のインデックスに合わせて前値補完 (ffill)
        return ma_15m.reindex(df_1m.index, method='ffill')
    except Exception as e:
        logger.error(f"MA calculation error: {e}")
        return pd.Series(index=df_1m.index, dtype=float)


def fetch_macro_sentiment() -> Dict[str, float]:
    result = {'sox_chg': 0.0, 'tnx_chg': 0.0, 'vix_value': 18.0, 'vix_chg': 0.0, 'jpy_chg': 0.0, 'topix_chg': 0.0, 'market_sentiment': 0.0}
    ticker_alternatives = {'SOX': ['^SOX', 'SOXX'], 'TNX': ['^TNX', 'IEF'], 'VIX': ['^VIX'], 'JPY': ['JPY=X'], 'N225': ['^N225'], 'TOPX': ['^TOPX', '1306.T']}
    
    score = 0.0
    for key, tickers in ticker_alternatives.items():
        for ticker in tickers:
            try:
                hist = yf.Ticker(ticker).history(period='5d', interval='1d')
                if len(hist) >= 2:
                    latest, previous = float(hist['Close'].iloc[-1]), float(hist['Close'].iloc[-2])
                    change = ((latest / previous) - 1) * 100
                    if key == 'VIX': 
                        result['vix_value'] = round(latest, 2)
                        # VIXによるスコアリング
                        if latest > 25: score -= 0.4
                        elif latest > 20: score -= 0.2
                        elif latest < 15: score += 0.2
                    elif key == 'SOX':
                        if change > 1.5: score += 0.2
                        elif change < -1.5: score -= 0.2
                    elif key == 'N225':
                        if change > 1.0: score += 0.2
                        elif change < -1.0: score -= 0.2
                    elif key == 'TOPX':
                        if change > 0.8: score += 0.2
                        elif change < -0.8: score -= 0.2
                    
                    if key != 'VIX' and key != 'N225' and key != 'TOPX':
                        result[key.lower() + '_chg'] = round(change, 2)
                    elif key == 'VIX':
                        result['vix_chg'] = round(change, 2)
                    elif key == 'TOPX':
                        result['topix_chg'] = round(change, 2)
                    break
            except Exception: continue
            
    result['market_sentiment'] = round(np.clip(score, -1.0, 1.0), 2)
    return result
