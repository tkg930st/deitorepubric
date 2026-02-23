#!/usr/bin/env python3
"""
test_analyzer.py - 開発・検証用高速エディション (Ver 15.10 ロジック)
同期ルール：analyzer.py と同一の解析ロジックを保持。
"""
import json
import logging
import time
import os
from datetime import datetime
from typing import List, Dict
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

from config import (
    LOG_FILE, LOG_LEVEL, OPTIMIZATION_ITERATIONS, DATA_FETCH,
    WEBHOOK_URL, TREND_FILTER
)
from utils import (
    super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, filter_trading_hours,
    send_discord_notification
)
from backtest_engine import optimize_parameters

# 検証用銘柄
TEST_TICKERS = [
    "6920.T", "8035.T", "6857.T", "6146.T", "7735.T",
    "9984.T", "6758.T", "6501.T", "6723.T", "6701.T",
    "9101.T", "9104.T", "9107.T",
    "7203.T", "7267.T", "7261.T",
    "8306.T", "8316.T", "8411.T", "8766.T",
    "8001.T", "8031.T", "8058.T",
    "4063.T", "4519.T", "4568.T", "4901.T",
    "5401.T", "5406.T", "5713.T",
    "6098.T", "4661.T", "9020.T", "9201.T",
    "1570.T", "1357.T", "1458.T", "1459.T",
    "7011.T", "7012.T", "6301.T", "6367.T",
    "3064.T", "3103.T", "5838.T", "5101.T", "7014.T",
    "186A.T", "285A.T"
]

TEST_LOG = "test_execution_log.txt"

logging.basicConfig(
    filename=TEST_LOG, filemode='a', encoding='utf-8',
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def worker_analyze_ticker(ticker: str, period: str, hurdle: float) -> Dict:
    try:
        data = fetch_yfinance_data([ticker], period=period, interval=DATA_FETCH['analyzer_interval'])
        df = super_flatten_columns(data)
        df = filter_trading_hours(df)
        if len(df) < 20: return None
        df = calculate_technical_indicators(df)
        df_15m = pd.DataFrame()
        
        # Ver 15.10 ロジック (500回試行)
        res_l = optimize_parameters(df, df_15m, 'long', OPTIMIZATION_ITERATIONS)
        res_s = optimize_parameters(df, df_15m, 'short', OPTIMIZATION_ITERATIONS)
        
        l_prof = res_l['profit']; s_prof = res_s['profit']
        valid_l = l_prof if l_prof >= hurdle else 0.0
        valid_s = s_prof if s_prof >= hurdle else 0.0
        total = valid_l + valid_s
        
        if total <= 0: return None

        return {
            't': ticker, 'profit': total, 'long_profit': valid_l, 'short_profit': valid_s,
            'params': {'long': res_l['params'], 'short': res_s['params']}
        }
    except Exception: return None

def run_test_session(tickers: List[str], period: str, count: int, label: str, hurdle: float):
    print(f"\n🔬 {label} 解析中 (ハードル: {hurdle}%)")
    results = []
    with ProcessPoolExecutor() as executor:
        futures = {executor.submit(worker_analyze_ticker, t, period, hurdle): t for t in tickers}
        for f in as_completed(futures):
            res = f.result()
            if res:
                res['logic_type'] = label
                results.append(res)
                print(f"   ✅ {res['t']} 完了 (期待値: {res['profit']:.2f}%)")
    return sorted(results, key=lambda x: x['profit'], reverse=True)[:count]

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return int(obj) if isinstance(obj, np.integer) else float(obj) if isinstance(obj, np.floating) else bool(obj)
        return super(NpEncoder, self).default(obj)

def main():
    if os.path.exists(TEST_LOG):
        try: os.remove(TEST_LOG)
        except: 
            with open(TEST_LOG, 'w'): pass

    start_time = time.time()
    print("\n🚀 [TEST MODE] Ver 15.10 高速検証 (詳細数値ログ対応)")
    
    long_res = run_test_session(TEST_TICKERS, '1mo', 7, "Long(1Month)", 5.0)
    short_res = run_test_session(TEST_TICKERS, '1wk', 3, "Short(1Week)", 3.0)
    
    combined = long_res + short_res
    output_file = "test_best_config.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({'timestamp': datetime.now().isoformat(), 'version': '15.10-TEST', 'details': combined}, 
                  f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    elapsed = time.time() - start_time
    print(f"\n✅ テスト完了！ ({elapsed/60:.1f}分)")
    if combined: print(f"選定数: {len(combined)} / 10")
    else: print("❌ 銘柄が選定されませんでした。")

if __name__ == "__main__":
    main()
