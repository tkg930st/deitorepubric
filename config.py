"""
システム設定ファイル
環境変数や調整可能なパラメータを一元管理
"""
import os
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

# Discord Webhook設定
WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

# ログ設定
LOG_FILE = 'execution_log.txt'
LOG_LEVEL = 'INFO'

# 銘柄選定基準
LIQUIDITY_THRESHOLD = 5_000_000_000  # 売買代金50億円以上に戻す
MIN_PRICE = 500  # 最低株価
MIN_SCORE_THRESHOLD = 60.0  # スコア閾値の下限ガード

# バックテスト設定
OPTIMIZATION_ITERATIONS = 500  # パラメータ最適化試行回数
MIN_DATA_POINTS = 40  # 必要な最小データポイント数
SLIPPAGE = 0.003  # 往復のスリッページ（0.3%）

# パラメータ探索範囲
PARAM_RANGES = {
    'w_rsi': (0, 100),
    'w_vwap': (0, 100),
    'w_rvol': (0, 100),
    'w_adx': (0, 100),
    'threshold': (10, 200),
    'sl_mul': (0.3, 4.0),
    'tp_mul': (1.0, 10.0)
}

# トレンドフィルター設定
TREND_FILTER = {
    'enabled': True,
    'ma_period': 20,  # 15分足20MA
    'timeframe': '15m'
}

# ダイバージェンス設定
DIVERGENCE = {
    'enabled': True,
    'lookback': 25,  # 検知期間
    'rsi_threshold': 5.0,  # RSI差分閾値
    'price_threshold': 0.5  # 価格変化率閾値（%）
}

# トレードジャーナル設定
TRADE_JOURNAL = {
    'enabled': True,
    'journal_file': 'trade_journal.csv'
}

# シグナル検出閾値 (SYSTEM_DESIGN §4 多段階スコアリング定義に準拠)
SIGNAL_THRESHOLDS = {
    # RSI 段階的閾値 (買い: oversold/moderate, 売り: overbought/moderate)
    'rsi_oversold': 30,         # 買い強シグナル (×1.5倍)
    'rsi_buy_moderate': 45,     # 買い中シグナル (×1.0倍)
    'rsi_overbought': 70,       # 売り強シグナル (×1.5倍)
    'rsi_sell_moderate': 55,    # 売り中シグナル (×1.0倍)
    # RSI/VWAPフィルタ (エントリー除外閾値)
    'rsi_filter_long_max': 75,  # LONG時 RSI上限
    'rsi_filter_short_min': 25, # SHORT時 RSI下限
    'vwap_filter_max': 3.0,     # VWAP乖離フィルタ上限
    # VWAP乖離 段階的閾値
    'vwap_dev_strong': 1.5,     # 強シグナル (×1.5倍)
    'vwap_dev_moderate': 0.3,   # 中シグナル (×1.0倍)
    # RVOL (出来高倍率) 段階的閾値
    'rvol_strong': 2.5,         # 強シグナル (×2.0倍)
    'rvol_moderate': 1.5,       # 中シグナル (×1.0倍)
    'rvol_weak': 1.1,           # 弱シグナル (×0.5倍)
    'rvol_lookback': 5,         # RVOL計算のローリング期間
    # ADX (トレンド強度) 段階的閾値
    'adx_strong': 35,           # 強トレンド (×1.5倍)
    'adx_moderate': 20,         # 中トレンド (×1.0倍)
}

# 解析・バックテスト目標利益 (%)
TARGET_PROFIT = {
    'Monthly': 5.0,
    'Weekly': 3.0
}

# 取引時間設定 (JST)
TRADING_HOURS = {
    'morning_start': '09:30',
    'morning_end': '11:30',
    'afternoon_start': '12:30',
    'afternoon_end': '15:00',
    'avoid_close_minutes': 5,
    'start': '09:30',
    'end': '15:10',
    'am_cutoff': '11:30',
    'pm_cutoff': '14:30',
    'force_close': '14:55'
}

# データ取得設定
DATA_FETCH = {
    'analyzer_period': '1mo',
    'analyzer_interval': '15m',
    'monitor_period': '5d',
    'monitor_interval': '1m',
    'chunk_size': 30,
    'request_delay': 2.0,
    'max_retries': 3,
    'retry_delay': 2
}

# ポジション管理設定
POSITION_MANAGEMENT = {
    'positions_file': 'positions.json',
    'trade_results_file': 'trade_results.csv',
    'tp1_multiplier': 1.5,
    'tp2_multiplier': 3.0,
    'tp1_exit_ratio': 0.5,
    'breakeven_enabled': True,
    'trailing_ma_period': 15,
    'trailing_atr_multiplier': 1.0,
    'chandelier_lookback': 5,
    'chandelier_atr_multiplier': 2.5,
}

# セクター・アライメント設定
SECTOR_ALIGNMENT = {
    'enabled': True,
    'min_aligned_rivals': 2,
    'alignment_score': 15,
    'volume_accel_score': 10,
    'divergence_bonus_score': 20,
    'volume_accel_rvol_threshold': 1.2,
}

# 再試行ループ設定
RETRY_OPTIMIZATION = {
    'enabled': True,
    'max_retries': 10,
    'retry_top_n': 5,
    'target_profit': 5.0,
    'iterations_per_retry': 500,
}

# リスク管理設定
RISK_MANAGEMENT = {
    'daily_stop_loss_pct': -3.0,
    'check_interval_loops': 1,
    'min_sl_multiplier': 0.7,        # SLの下限ガード (ATR倍率)
    'sentiment_brake_threshold': -0.3, # 地合いブレーキ発動閾値
    'sentiment_brake_penalty': 15.0,  # ブレーキ時の閾値上乗せ点数
    'macro_update_interval_sec': 3600 # マクロ指標の更新間隔
}

# 出力ファイル
OUTPUT_CONFIG = 'best_config.json'

# 地合い判定設定
MARKET_SENTIMENT = {
    'nikkei_ticker': '^N225',
    'topix_etf_ticker': '1306.T',
    'judgment_time': '09:15',
    'check_period_start': '09:05',
    'check_period_end': '09:15',
    'large_cap_categories': ['TOPIX Core30', 'TOPIX Large70'],
    'positive_adjustment': -5,
    'negative_adjustment': 5,
    'neutral_threshold': 0.0,
    'macro_tickers': {
        'SOX': '^SOX',
        'TNX': '^TNX',
        'VIX': '^VIX',
        'JPY': 'JPY=X'
    }
}

# 監視ループ設定
MONITORING_LOOP = {
    'start_time': '09:30',
    'judgment_time': '09:15',
    'end_time': '15:00',
    'loop_interval': 30,
    'use_confirmed_candle': True,
    'am_entry_cutoff': '11:30',
    'pm_entry_cutoff': '14:30',
}
