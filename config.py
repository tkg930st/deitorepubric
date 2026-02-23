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
LOG_LEVEL = 'DEBUG'

# 銘柄選定基準
LIQUIDITY_THRESHOLD = 5_000_000_000  # 売買代金50億円以上に戻す
MIN_PRICE = 500  # 最低株価
ATR_CHECK_COUNT = 50   # ATRスクリーニング対象銘柄数
TOP_CANDIDATES = 30    # 解析対象の主力銘柄数
FINAL_MONITORING = 10  # 最終監視銘柄数
MIN_SCORE_THRESHOLD = 60.0  # スコア閾値の下限ガード

# バックテスト設定
OPTIMIZATION_ITERATIONS = 500  # パラメータ最適化試行回数
PRECISE_CHECK_COUNT = 20       # 精密検証を行う上位数
MIN_DATA_POINTS = 40  # 必要な最小データポイント数
SLIPPAGE = 0.001  # 往復のスリッページ（0.1%）

# パラメータ探索範囲
PARAM_RANGES = {
    'w_rsi': (10, 35),
    'w_vwap': (10, 35),
    'w_rvol': (10, 35),
    'w_adx': (10, 35),
    'threshold': (30, 75),
    'sl_mul': (0.8, 2.2),
    'tp_mul': (2.0, 5.0)
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

# 当日制限設定
DAILY_COOLDOWN = {
    'enabled': True,
    'cooldown_file': 'daily_cooldown.json',
    'reset_time': '09:00'  # リセット時刻
}

# トレードジャーナル設定
TRADE_JOURNAL = {
    'enabled': True,
    'journal_file': 'trade_journal.csv'
}

# シグナル検出閾値
SIGNAL_THRESHOLDS = {
    'rsi_low': 35,
    'rsi_high': 65,
    'vwap_dev_low': -1.0,
    'vwap_dev_high': 1.0,
    'rvol_threshold': 1.5,
    'adx_threshold': 25,
    'adx_trend_strength': 30,
    'rsi_overbought': 70,
    'rsi_oversold': 30,
    'vwap_dev_max': 2.5,
    'vol_surge_lookback': 10,
    'vol_surge_threshold': 2.0,
}

# 取引時間設定（JST）
TRADING_HOURS = {
    'morning_start': '09:00',
    'morning_end': '11:30',
    'afternoon_start': '12:30',
    'afternoon_end': '15:00',
    'avoid_close_minutes': 10  # 大引け前の除外時間（分）
}

# データ取得設定
DATA_FETCH = {
    'analyzer_period': '1mo',
    'analyzer_interval': '15m',
    'monitor_period': '5d',
    'monitor_interval': '5m',
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
    'loop_interval': 60,
    'use_confirmed_candle': True,
    'am_entry_cutoff': '10:30',
    'pm_entry_cutoff': '14:00',
    'min_rr_ratio': 1.5,
}

# ファンダメンタルズ設定
FUNDAMENTAL_FILTER = {
    'enabled': True,
    'min_roe': 0.10,
    'max_peg': 1.0
}
