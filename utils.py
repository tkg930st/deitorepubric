"""
共通ユーティリティ関数
データ処理、エラーハンドリング、時間管理などの共通機能
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
from config import DATA_FETCH, TRADING_HOURS

# ロガー設定
logger = logging.getLogger(__name__)


def retry_on_error(max_retries: int = DATA_FETCH['max_retries'], 
                   delay: float = DATA_FETCH['retry_delay']):
    """
    エラー時のリトライデコレータ
    
    Args:
        max_retries: 最大リトライ回数
        delay: リトライ間隔（秒）
    """
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
                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_retries}): {str(e)}"
                        )
                        time.sleep(delay * (attempt + 1))  # 指数バックオフ
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_retries} attempts: {str(e)}"
                        )
            raise last_exception
        return wrapper
    return decorator


@retry_on_error()
def get_jpx_list() -> List[str]:
    """
    JPX（日本取引所グループ）から全銘柄リストを取得
    
    Returns:
        銘柄コードリスト（例: ['7203.T', '9984.T']）
    """
    try:
        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
        logger.info(f"JPX銘柄リストを取得中: {url}")
        
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        
        df = pd.read_excel(io.BytesIO(res.content), engine='xlrd')
        target_markets = ['プライム（内国株式）']
        
        codes = df[df['市場・商品区分'].isin(target_markets)]['コード'].tolist()
        tickers = [f"{str(code)}.T" for code in codes]
        
        logger.info(f"取得完了: {len(tickers)}銘柄")
        return tickers
        
    except Exception as e:
        logger.error(f"JPX銘柄リスト取得エラー: {str(e)}")
        raise


@retry_on_error()
def get_jpx_list_with_sector() -> pd.DataFrame:
    """
    JPXから銘柄リストとセクター情報、規模区分を取得
    
    Returns:
        銘柄コード、セクター、規模区分を含むDataFrame
    """
    try:
        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
        logger.info(f"JPX銘柄リスト（セクター・規模区分付き）を取得中: {url}")
        
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        
        df = pd.read_excel(io.BytesIO(res.content), engine='xlrd')
        target_markets = ['プライム（内国株式）', 'スタンダード（内国株式）', 'グロース（内国株式）']

        # 必要な列を抽出
        df_filtered = df[df['市場・商品区分'].isin(target_markets)].copy()
        df_filtered['ticker'] = df_filtered['コード'].apply(lambda x: f"{str(x)}.T")

        # セクター情報を取得（33業種区分）
        sector_column = '33業種区分' if '33業種区分' in df_filtered.columns else '業種'

        # 規模区分を取得
        size_column = '規模区分' if '規模区分' in df_filtered.columns else None

        # 超大型株を除外（値動きが重くデイトレに不向きなため）
        if size_column:
            exclude_sizes = ['TOPIX Core30', 'TOPIX Large70']
            before_count = len(df_filtered)
            df_filtered = df_filtered[~df_filtered[size_column].isin(exclude_sizes)].copy()
            logger.info(f"超大型株除外: {before_count}銘柄 → {len(df_filtered)}銘柄 (Core30/Large70を除外)")

        if size_column:
            result = df_filtered[['ticker', sector_column, size_column]].rename(
                columns={sector_column: 'sector', size_column: 'size_category'}
            )
        else:
            result = df_filtered[['ticker', sector_column]].rename(
                columns={sector_column: 'sector'}
            )
            result['size_category'] = ''  # 規模区分がない場合は空文字
        
        logger.info(f"取得完了: {len(result)}銘柄（セクター・規模区分付き）")
        return result
        
    except Exception as e:
        logger.error(f"JPX銘柄リスト（セクター・規模区分）取得エラー: {str(e)}")
        raise


def super_flatten_columns(df: pd.DataFrame, ticker: str = "") -> pd.DataFrame:
    """
    MultiIndexカラムを完全にフラット化し、JST時間に変換
    
    Args:
        df: 元のDataFrame
        ticker: 銘柄コード（ログ用）
    
    Returns:
        正規化されたDataFrame
    """
    if df is None or df.empty:
        return pd.DataFrame()
    
    # タイムゾーン変換（UTC → JST）
    try:
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
        else:
            df.index = df.index.tz_convert('Asia/Tokyo')
    except Exception as e:
        logger.warning(f"タイムゾーン変換エラー ({ticker}): {str(e)}")
    
    # MultiIndex対応
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(map(str, col)).lower().strip() for col in df.columns]
    else:
        df.columns = [str(c).lower().strip() for c in df.columns]
    
    # カラム名の正規化マッピング
    mapping = {
        'adj close': 'close',
        'close': 'close',
        'high': 'high',
        'low': 'low',
        'open': 'open',
        'vol': 'volume',
        'volume': 'volume'
    }
    
    final_rename = {}
    for key, target in mapping.items():
        for actual in df.columns:
            if key in actual and target not in final_rename.values():
                final_rename[actual] = target
                break
    
    df = df.rename(columns=final_rename)
    
    # 必要なカラムのみ抽出
    keep = ['open', 'high', 'low', 'close', 'volume']
    existing = [c for c in keep if c in df.columns]
    
    result = df[existing].copy()
    
    # 欠損値削除
    result = result.dropna()
    
    return result


def is_trading_hours(dt: datetime) -> bool:
    """
    取引時間内かどうかを判定（昼休み・大引け直前を除外）
    
    Args:
        dt: 判定する日時（JSTタイムゾーン想定）
    
    Returns:
        取引可能時間ならTrue
    """
    # 土日を除外
    if dt.weekday() >= 5:
        return False
    
    # JSTに変換
    if dt.tzinfo is None:
        dt = pytz.timezone('Asia/Tokyo').localize(dt)
    else:
        dt = dt.astimezone(pytz.timezone('Asia/Tokyo'))
    
    current_time = dt.time()
    
    # 午前セッション
    morning_start = dt_time.fromisoformat(TRADING_HOURS['morning_start'])
    morning_end = dt_time.fromisoformat(TRADING_HOURS['morning_end'])
    
    # 午後セッション（大引け直前を除外）
    afternoon_start = dt_time.fromisoformat(TRADING_HOURS['afternoon_start'])
    afternoon_end_str = TRADING_HOURS['afternoon_end']
    afternoon_end = dt_time.fromisoformat(afternoon_end_str)
    
    # 大引け前N分を除外（時間計算を修正）
    avoid_minutes = TRADING_HOURS['avoid_close_minutes']
    
    # 分の引き算を正しく処理
    close_hour = afternoon_end.hour
    close_minute = afternoon_end.minute - avoid_minutes
    
    # 分が負になる場合、時間を1時間前に
    if close_minute < 0:
        close_hour -= 1
        close_minute += 60
    
    close_avoid_time = dt_time(close_hour, close_minute)
    
    # 判定
    in_morning = morning_start <= current_time <= morning_end
    in_afternoon = afternoon_start <= current_time <= close_avoid_time
    
    return in_morning or in_afternoon


def filter_trading_hours(df: pd.DataFrame) -> pd.DataFrame:
    """
    取引時間外のデータをフィルタリング
    
    Args:
        df: 元のDataFrame（DateTimeIndexを持つ）
    
    Returns:
        フィルタリング後のDataFrame
    """
    if df.empty:
        return df
    
    mask = df.index.to_series().apply(is_trading_hours)
    filtered = df[mask].copy()
    
    logger.info(f"取引時間フィルタ: {len(df)}行 → {len(filtered)}行")
    return filtered


@retry_on_error()
def fetch_yfinance_data(tickers: List[str], period: str, interval: str,
                        group_by: str = 'ticker') -> pd.DataFrame:
    """
    yfinanceからデータを取得（リトライ機能付き）
    
    Args:
        tickers: 銘柄コードリスト
        period: 取得期間
        interval: 時間足
        group_by: グルーピング方法
    
    Returns:
        取得したDataFrame
    """
    logger.info(f"データ取得開始: {len(tickers)}銘柄, period={period}, interval={interval}")
    
    data = yf.download(
        tickers,
        period=period,
        interval=interval,
        group_by=group_by,
        auto_adjust=True,
        progress=False,
        threads=True  # 並列ダウンロード
    )
    
    logger.info(f"データ取得完了: shape={data.shape}")
    return data


def send_discord_notification(webhook_url: str, message: str) -> bool:
    """
    Discord Webhookで通知を送信
    
    Args:
        webhook_url: Webhook URL
        message: 送信メッセージ
    
    Returns:
        成功ならTrue
    """
    try:
        response = requests.post(
            webhook_url,
            json={"content": message},
            timeout=10
        )
        response.raise_for_status()
        logger.info("Discord通知送信成功")
        return True
        
    except Exception as e:
        logger.error(f"Discord通知送信エラー: {str(e)}")
        return False


def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    テクニカル指標を計算（RSI, ADX, ATR, VWAP, RVOL）
    
    Args:
        df: OHLCV データ
    
    Returns:
        指標を追加したDataFrame
    """
    if df.empty or len(df) < 14:
        logger.warning("データ不足のため指標計算をスキップ")
        return df
    
    df = df.copy()
    
    try:
        # pandas_taを明示的にインポート
        import pandas_ta as ta
        
        # RSI
        rsi = ta.rsi(df['close'], length=14)
        if rsi is not None:
            df['rsi_14'] = rsi
        
        # ADX
        adx_result = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_result is not None and not adx_result.empty:
            df['adx_14'] = adx_result['ADX_14']
        
        # ATR
        atr = ta.atr(df['high'], df['low'], df['close'], length=14)
        if atr is not None:
            df['atr_14'] = atr
        
        # VWAP乖離率
        vwap = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        if vwap is not None and not vwap.empty:
            df['vwap_dev'] = ((df['close'] - vwap) / vwap) * 100
        else:
            df['vwap_dev'] = 0
        
        # 相対出来高（RVOL）
        df['rvol'] = df['volume'] / df['volume'].rolling(window=5).mean()
        
        # 欠損値を0で埋める
        df = df.fillna(0)
        
        # カラム名を小文字化
        df.columns = [str(c).lower() for c in df.columns]

        # 一目均衡表（プロ手法: 雲によるトレンドコンフルエンス確認）
        # ※NaN値は意味を持つため fillna(0) の後に配置し、NaNを保持する
        tenkan_sen = (df['high'].rolling(window=9).max() + df['low'].rolling(window=9).min()) / 2
        kijun_sen = (df['high'].rolling(window=26).max() + df['low'].rolling(window=26).min()) / 2
        senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
        senkou_span_b = ((df['high'].rolling(window=52).max() + df['low'].rolling(window=52).min()) / 2).shift(26)
        df['tenkan_sen'] = tenkan_sen
        df['kijun_sen'] = kijun_sen
        df['senkou_span_a'] = senkou_span_a
        df['senkou_span_b'] = senkou_span_b

        logger.debug(f"テクニカル指標計算完了: {df.columns.tolist()}")
        
    except Exception as e:
        logger.error(f"テクニカル指標計算エラー: {str(e)}")
    
    return df


def safe_get(row: pd.Series, key: str, default: Any = 0) -> Any:
    """
    安全にSeriesから値を取得
    
    Args:
        row: pandas Series
        key: キー
        default: デフォルト値
    
    Returns:
        値またはデフォルト値
    """
    try:
        return row.get(key, default)
    except:
        return default


def check_divergence(df: pd.DataFrame, lookback: int = 25) -> Dict[str, bool]:
    """
    ダイバージェンス検知（Version 9.0）
    
    Args:
        df: OHLCV + テクニカル指標を含むDataFrame
        lookback: 検知期間
    
    Returns:
        {'bullish': bool, 'bearish': bool, 'reverse_bullish': bool, 'reverse_bearish': bool}
    """
    from config import DIVERGENCE
    
    result = {
        'bullish': False,          # 強気ダイバージェンス（買い）
        'bearish': False,          # 弱気ダイバージェンス（売り）
        'reverse_bullish': False,  # 逆行強気（保有中警告）
        'reverse_bearish': False   # 逆行弱気（保有中警告）
    }
    
    if not DIVERGENCE['enabled']:
        return result
    
    if df.empty or len(df) < lookback + 5:
        return result
    
    try:
        # 直近lookback期間のデータ
        recent = df.iloc[-lookback:].copy()
        
        if 'rsi_14' not in recent.columns or 'close' not in recent.columns:
            return result
        
        # 価格とRSIの始点・終点
        price_start = recent['close'].iloc[0]
        price_end = recent['close'].iloc[-1]
        rsi_start = recent['rsi_14'].iloc[0]
        rsi_end = recent['rsi_14'].iloc[-1]
        
        # NaNチェック
        if pd.isna(price_start) or pd.isna(price_end) or pd.isna(rsi_start) or pd.isna(rsi_end):
            return result
        
        # 変化率計算
        price_change_pct = ((price_end / price_start) - 1) * 100
        rsi_change = rsi_end - rsi_start
        
        rsi_threshold = DIVERGENCE['rsi_threshold']
        price_threshold = DIVERGENCE['price_threshold']
        
        # 強気ダイバージェンス（価格↓、RSI↑）
        if price_change_pct < -price_threshold and rsi_change > rsi_threshold:
            result['bullish'] = True
            logger.debug(f"強気ダイバージェンス検出: 価格{price_change_pct:.2f}%, RSI{rsi_change:+.1f}")
        
        # 弱気ダイバージェンス（価格↑、RSI↓）
        if price_change_pct > price_threshold and rsi_change < -rsi_threshold:
            result['bearish'] = True
            logger.debug(f"弱気ダイバージェンス検出: 価格{price_change_pct:.2f}%, RSI{rsi_change:+.1f}")
        
        # 逆行強気（価格↑、RSI↓） ← 保有中の警告用
        if price_change_pct > price_threshold and rsi_change < -rsi_threshold:
            result['reverse_bearish'] = True
        
        # 逆行弱気（価格↓、RSI↑） ← 保有中の警告用
        if price_change_pct < -price_threshold and rsi_change > rsi_threshold:
            result['reverse_bullish'] = True
    
    except Exception as e:
        logger.error(f"ダイバージェンス検知エラー: {str(e)}")
    
    return result


def check_trend_filter(current_price: float, ma15_value: float, side: str) -> bool:
    """
    トレンドフィルター判定（Version 7.0/8.0）
    
    Args:
        current_price: 現在価格
        ma15_value: 15分足20MAの値
        side: 'LONG' または 'SHORT'
    
    Returns:
        トレンド条件を満たすか
    """
    from config import TREND_FILTER
    
    if not TREND_FILTER['enabled']:
        return True  # 無効の場合は常にTrue
    
    if pd.isna(ma15_value) or ma15_value == 0:
        return True  # MAが取得できない場合は許可
    
    if side == 'LONG':
        # ロング: 価格がMAより上
        return current_price > ma15_value
    else:  # SHORT
        # ショート: 価格がMAより下
        return current_price < ma15_value


def detect_market_structure(df: pd.DataFrame, lookback: int = 5) -> Dict:
    """
    BOS (Break of Structure) および CHoCH (Change of Character) を検知する
    
    Args:
        df: 株価データフレーム (5m足推奨)
        lookback: スイングハイ・ローを判定する左右のバー数
        
    Returns:
        検知結果辞書 {'type': 'BOS'|'CHoCH'|None, 'direction': 'LONG'|'SHORT', 'price': float}
    """
    if len(df) < lookback * 3:
        return {'type': None, 'direction': None, 'price': 0.0}
    
    try:
        # スイングハイ・ローの特定
        df = df.copy()
        
        # 局所的な高値・安値を抽出
        df['is_high'] = (df['high'] == df['high'].rolling(window=lookback*2+1, center=True).max())
        df['is_low'] = (df['low'] == df['low'].rolling(window=lookback*2+1, center=True).min())
        
        highs = df[df['is_high']]['high']
        lows = df[df['is_low']]['low']
        
        if len(highs) < 2 or len(lows) < 2:
            return {'type': None, 'direction': None, 'price': 0.0}
            
        last_high = highs.iloc[-1]
        prev_high = highs.iloc[-2]
        last_low = lows.iloc[-1]
        prev_low = lows.iloc[-2]
        
        current_price = df['close'].iloc[-1]
        prev_price = df['close'].iloc[-2]
        
        # 検知ロジック
        # 1. CHoCH (Change of Character - トレンド転換)
        # 上昇トレンド中の押し安値(prev_low)を下抜ける
        if prev_price >= last_low > current_price and last_low > prev_low:
             return {'type': 'CHoCH', 'direction': 'SHORT', 'price': last_low}
        # 下落トレンド中の戻り高値(prev_high)を上抜ける
        if prev_price <= last_high < current_price and last_high < prev_high:
             return {'type': 'CHoCH', 'direction': 'LONG', 'price': last_high}
             
        # 2. BOS (Break of Structure - トレンド継続)
        # 直近の高値(last_high)を上抜ける (上昇トレンド継続)
        if prev_price <= last_high < current_price:
            return {'type': 'BOS', 'direction': 'LONG', 'price': last_high}
        # 直近の安値(last_low)を下抜ける (下落トレンド継続)
        if prev_price >= last_low > current_price:
            return {'type': 'BOS', 'direction': 'SHORT', 'price': last_low}
            
        return {'type': None, 'direction': None, 'price': 0.0}
        
    except Exception as e:
        logger.error(f"Market Structure Detection Error: {str(e)}")
        return {'type': None, 'direction': None, 'price': 0.0}


def calculate_ma_from_higher_timeframe(df_5m: pd.DataFrame, ma_period: int = 20) -> pd.Series:
    """
    5分足データから15分足相当のMAを計算
    
    Args:
        df_5m: 5分足DataFrame
        ma_period: MA期間
    
    Returns:
        15分足20MAに相当するSeries
    """
    try:
        # 5分足を15分足にリサンプリング
        df_15m = df_5m.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        # 20MA計算
        ma_15m = df_15m['close'].rolling(window=ma_period).mean()
        
        # 5分足のインデックスに合わせて補間（ffill）
        ma_15m_reindexed = ma_15m.reindex(df_5m.index, method='ffill')
        
        return ma_15m_reindexed
    
    except Exception as e:
        logger.error(f"15分足MA計算エラー: {str(e)}")
        return pd.Series(index=df_5m.index, dtype=float)


def fetch_macro_sentiment() -> Dict[str, float]:
    """
    V3: マクロ指標取得（前日比変化率 + VIX現在値）
    
    米国市場の前営業日のデータを取得し、変化率を計算します。
    エラー時はデフォルト値を返します。
    
    Returns:
        {
            'sox_chg': float,      # SOX指数の前日比変化率（%）
            'tnx_chg': float,      # TNX（米10年債利回り）の前日比変化率（%）
            'vix_value': float,    # VIXの現在値
            'vix_chg': float,      # VIXの前日比変化率（%）
            'jpy_chg': float       # JPY=Xの前日比変化率（%）
        }
    """
    import warnings
    warnings.filterwarnings('ignore')
    
    # デフォルト値（取得失敗時）
    result = {
        'sox_chg': 0.0,
        'tnx_chg': 0.0,
        'vix_value': 18.0,  # VIXの典型的な値
        'vix_chg': 0.0,
        'jpy_chg': 0.0
    }
    
    # ティッカーマップ（複数の代替シンボル）
    ticker_alternatives = {
        'SOX': ['^SOX', 'SOXX', 'SMH'],      # SOX指数、iShares半導体ETF、VanEck半導体ETF
        'TNX': ['^TNX', '^TYX', 'IEF'],       # 米10年債、30年債、iShares債券ETF
        'VIX': ['^VIX', 'VIXY'],              # 恐怖指数、VIX先物ETF
        'JPY': ['JPY=X', 'JPYUSD=X', 'FXY']   # ドル円、円ETF
    }
    
    try:
        logger.info("=" * 60)
        logger.info("マクロ指標取得開始")
        logger.info("=" * 60)
        
        for key, tickers in ticker_alternatives.items():
            success = False
            
            for ticker in tickers:
                try:
                    logger.info(f"試行: {key} <- {ticker}")
                    
                    # yfinanceでデータ取得
                    try:
                        ticker_obj = yf.Ticker(ticker)
                        hist = ticker_obj.history(period='1mo', interval='1d')
                    except Exception as fetch_err:
                        logger.warning(f"  Ticker取得失敗: {str(fetch_err)}")
                        continue
                    
                    # データ検証
                    if hist is None or hist.empty:
                        logger.warning(f"  データなし")
                        continue
                    
                    if len(hist) < 2:
                        logger.warning(f"  データ不足: {len(hist)}行")
                        continue
                    
                    # Close価格を取得
                    try:
                        close_prices = hist['Close'].dropna()
                        
                        if len(close_prices) < 2:
                            logger.warning(f"  Close価格不足")
                            continue
                        
                        # 最新2日分
                        latest = float(close_prices.iloc[-1])
                        previous = float(close_prices.iloc[-2])
                        
                        # 変化率計算
                        if previous > 0:
                            change = ((latest / previous) - 1) * 100
                        else:
                            change = 0.0
                        
                    except (KeyError, IndexError, ValueError) as e:
                        logger.warning(f"  価格計算エラー: {str(e)}")
                        continue
                    
                    # 結果格納
                    if key == 'SOX':
                        result['sox_chg'] = round(change, 2)
                        logger.info(f"✅ SOX: {change:+.2f}% (from {ticker})")
                        success = True
                        
                    elif key == 'TNX':
                        result['tnx_chg'] = round(change, 2)
                        logger.info(f"✅ TNX: {change:+.2f}% (from {ticker})")
                        success = True
                        
                    elif key == 'VIX':
                        result['vix_value'] = round(latest, 2)
                        result['vix_chg'] = round(change, 2)
                        logger.info(f"✅ VIX: {latest:.2f} ({change:+.2f}%) (from {ticker})")
                        success = True
                        
                    elif key == 'JPY':
                        result['jpy_chg'] = round(change, 2)
                        logger.info(f"✅ JPY: {change:+.2f}% (from {ticker})")
                        success = True
                    
                    # 成功したらループを抜ける
                    if success:
                        break
                
                except Exception as ticker_err:
                    logger.warning(f"  {ticker} エラー: {str(ticker_err)}")
                    continue
                
                finally:
                    # レート制限対策
                    time.sleep(0.5)
            
            if not success:
                logger.warning(f"⚠️ {key}: すべてのティッカーで取得失敗（デフォルト値使用）")
        
        logger.info("=" * 60)
        logger.info("マクロ指標取得完了")
        logger.info(f"VIX={result['vix_value']:.2f}, SOX={result['sox_chg']:+.2f}%, TNX={result['tnx_chg']:+.2f}%")
        logger.info("=" * 60)
        
        return result
    
    except Exception as e:
        logger.error(f"マクロ指標取得全体エラー: {str(e)}")
        logger.info("デフォルト値を返します")
        return result
