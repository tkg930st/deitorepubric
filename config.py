"""
システム設定ファイル
環境変数や調整可能なパラメータを一元管理
"""
import os
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

# Discord Webhook設定
WEBHOOK_URL = os.getenv(
    'DISCORD_WEBHOOK_URL',
    'https://discordapp.com/api/webhooks/1461740489693986947/TBycLmvriQtEKM_jVJ8KjoZEwBUn4EqCaD4NMGxZAT25f26IvVc0gDBM91qdjQvWdbqY'
)

# ログ設定
LOG_FILE = 'execution_log.txt'
LOG_LEVEL = 'INFO'

# 銘柄選定基準
LIQUIDITY_THRESHOLD = 5_000_000_000  # 売買代金50億円以上
MIN_PRICE = 500  # 最低株価
ATR_CHECK_COUNT = 50   # ATRスクリーニング対象銘柄数
TOP_CANDIDATES = 15    # 解析対象の主力銘柄数（最適化実行数）
FINAL_MONITORING = 8   # 最終監視銘柄数（Ver 11.0: 5→8に拡張）
MIN_SCORE_THRESHOLD = 60.0  # スコア閾値の下限ガード（Ver 10.3）

# バックテスト設定
OPTIMIZATION_ITERATIONS = 500  # パラメータ最適化試行回数
PRECISE_CHECK_COUNT = 20       # 精密検証を行う上位数（Version 10.0）
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
    'tp_mul': (1.2, 4.0)
}

# トレンドフィルター設定（Version 7.0/8.0）
TREND_FILTER = {
    'enabled': True,
    'ma_period': 20,  # 15分足20MA
    'timeframe': '15m'
}

# ダイバージェンス設定（Version 9.0）
DIVERGENCE = {
    'enabled': True,
    'lookback': 25,  # 検知期間
    'rsi_threshold': 5.0,  # RSI差分閾値
    'price_threshold': 0.5  # 価格変化率閾値（%）
}

# 当日制限設定（Version 9.0）
DAILY_COOLDOWN = {
    'enabled': True,
    'cooldown_file': 'daily_cooldown.json',
    'reset_time': '09:00'  # リセット時刻
}

# トレードジャーナル設定（Version 8.0）
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
    'adx_trend_strength': 30  # トレーリング開始判断基準（Ver 10.3）
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
    'analyzer_period': '1mo',  # 最適化期間を1ヶ月に変更
    'analyzer_interval': '15m',
    'monitor_period': '5d',
    'monitor_interval': '5m',
    'chunk_size': 30,  # 一度に取得する銘柄数
    'request_delay': 2.0,  # リクエスト間隔（秒）
    'max_retries': 3,  # 最大リトライ回数
    'retry_delay': 2  # リトライ間隔（秒）
}

# ポジション管理設定
POSITION_MANAGEMENT = {
    'positions_file': 'positions.json',  # ポジション管理ファイル
    'trade_results_file': 'trade_results.csv',  # 取引結果ファイル
    'tp1_multiplier': 1.5,  # TP1のATR倍率
    'tp2_multiplier': 3.0,  # TP2のATR倍率（Ver13.5: トレーリング初期値として使用）
    'tp1_exit_ratio': 0.5,  # TP1での決済比率（50%）
    'breakeven_enabled': True,  # TP1後に建値へ移動（Ver 12.0で有効化）
    'trailing_ma_period': 15,    # トレーリング停止判定用MA（Ver 10.3）
    'trailing_atr_multiplier': 1.0,  # Ver 13.5: TP1後のトレーリングストップ幅（ATR×1.0）
}

# Ver 13.5: セクター・アライメント設定
SECTOR_ALIGNMENT = {
    'enabled': True,
    'min_aligned_rivals': 2,     # アライメント判定に必要な最低一致数
    'alignment_score': 15,       # セクター・アライメント加点
    'volume_accel_score': 10,    # 出来高加速加点
    'divergence_bonus_score': 20, # ダイバージェンス・ボーナス加点
    'volume_accel_rvol_threshold': 1.2,  # 出来高加速判定のRVOL閾値
}

# Ver 13.5: 再試行ループ設定
RETRY_OPTIMIZATION = {
    'enabled': True,
    'max_retries': 10,           # 最大再試行回数
    'retry_top_n': 5,            # 再試行対象の上位N銘柄
    'target_profit': 5.0,        # 突破目標利益（%）
    'iterations_per_retry': 500, # 1回あたりの最適化試行数
}

# 出力ファイル
OUTPUT_CONFIG = 'best_config.json'

# 地合い判定設定
MARKET_SENTIMENT = {
    'nikkei_ticker': '^N225',  # 日経平均のティッカー
    'topix_etf_ticker': '1306.T',  # TOPIX ETF のティッカー
    'judgment_time': '09:15',  # 地合い判定時刻
    'check_period_start': '09:05',  # チェック開始時刻（09:05に変更）
    'check_period_end': '09:15',  # チェック終了時刻
    
    # 規模区分判定（Core30/Large70用）
    'large_cap_categories': ['TOPIX Core30', 'TOPIX Large70'],
    
    # スコア補正係数
    'positive_adjustment': -5,  # プラス地合い時の閾値調整（甘くする）
    'negative_adjustment': 5,   # マイナス地合い時の閾値調整（厳しくする）
    'neutral_threshold': 0.0,   # ±この範囲内は中立とみなす（%）
    
    # V3: マクロ指標（Ver 11.0）
    'macro_tickers': {
        'SOX': '^SOX',    # 半導体指数
        'TNX': '^TNX',    # 米10年債利回り
        'VIX': '^VIX',    # 恐怖指数
        'JPY': 'JPY=X'    # 為替（ドル円）
    }
}

# 監視ループ設定
MONITORING_LOOP = {
    'start_time': '09:30',      # 監視開始時刻
    'judgment_time': '09:15',   # 地合い判定時刻
    'end_time': '15:00',        # 監視終了時刻
    'loop_interval': 60,        # ループ間隔（秒）
    'use_confirmed_candle': True,  # 確定足を使用するか
}