#!/usr/bin/env python3
"""
test_analyzer.py - Version 15.15 高速検証 (ロジック同期版)
同期ルール：analyzer.py と同一の解析ロジックを保持。テスト時はDiscord通知を行わない。
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
    LOG_FILE, LOG_LEVEL, OPTIMIZATION_ITERATIONS, MIN_DATA_POINTS,
    DATA_FETCH, WEBHOOK_URL, TREND_FILTER, MIN_PRICE, LIQUIDITY_THRESHOLD,
    TARGET_PROFIT
)
from utils import (
    get_jpx_list_with_sector, super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, filter_trading_hours
)
from backtest_engine import optimize_parameters
from analyzer import get_rival_tickers

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

def worker_analyze_ticker(ticker_info: Dict, period: str, hurdle: float) -> Dict:
    ticker = ticker_info['t']
    try:
        data = fetch_yfinance_data([ticker], period=period, interval=DATA_FETCH['analyzer_interval'])
        df = super_flatten_columns(data)
        df = filter_trading_hours(df)
        if len(df) < MIN_DATA_POINTS: return None
        df = calculate_technical_indicators(df)
        
        # Ver 15.15 ロジック同期
        res_l = optimize_parameters(df, pd.DataFrame(), 'long', OPTIMIZATION_ITERATIONS)
        res_s = optimize_parameters(df, pd.DataFrame(), 'short', OPTIMIZATION_ITERATIONS)
        
        l_prof = res_l['profit']; s_prof = res_s['profit']
        is_l_valid = l_prof >= hurdle
        is_s_valid = s_prof >= hurdle
        valid_l = l_prof if is_l_valid else 0.0
        valid_s = s_prof if is_s_valid else 0.0
        total = valid_l + valid_s
        
        if total <= 0: return None

        return {
            't': ticker, 'profit': total, 
            'long_profit': valid_l, 'short_profit': valid_s,
            'raw_long_profit': l_prof, 'raw_short_profit': s_prof,
            'long_disabled': not is_l_valid, 'short_disabled': not is_s_valid,
            'atr_pct': ticker_info['atr_pct'],
            'sector': ticker_info['sector'], 'size_category': ticker_info['size_category'],
            'rivals': ticker_info['rivals'],
            'params': {'long': res_l['params'], 'short': res_s['params']}
        }
    except Exception as e:
        logger.error(f"{ticker} 解析エラー: {e}"); return None

def run_test_session(elite: List[Dict], period: str, count: int, label: str, hurdle: float):
    print(f"\n🔬 {label} 解析中 (ハードル: {hurdle}%)")
    results = []
    max_p = -999.0
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = {executor.submit(worker_analyze_ticker, s, period, hurdle): s for s in elite}
        for f in as_completed(futures):
            res = f.result()
            if res:
                res['logic_type'] = label
                results.append(res)
                max_p = max(max_p, res['profit'])
                print(f"   ✅ {res['t']} 完了 (有効利益: {res['profit']:.2f}%)")
    
    final_res = sorted(results, key=lambda x: x['profit'], reverse=True)[:count]
    if not final_res:
        print(f"   ⚠️ ハードル達成銘柄なし (最高利益: {max_p:.2f}%)")
    return final_res

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return int(obj) if isinstance(obj, np.integer) else float(obj) if isinstance(obj, np.floating) else bool(obj)
        return super(NpEncoder, self).default(obj)

def main():
    if os.path.exists(TEST_LOG):
        try:
            os.remove(TEST_LOG)
        except Exception:
            with open(TEST_LOG, 'w'): pass

    start_time = time.time()
    print("\n🚀 [TEST MODE] Ver 15.15 高速検証 (ロジック同期版)")
    
    sector_df = get_jpx_list_with_sector()
    
    # テスト対象銘柄のスクリーニング
    print(f"🔍 テスト用スクリーニング開始 ({len(TEST_TICKERS)} 銘柄対象)")
    elite = []
    batch = fetch_yfinance_data(TEST_TICKERS, period='1mo', interval='1d')
    for ticker in TEST_TICKERS:
        try:
            df = super_flatten_columns(batch[ticker] if len(TEST_TICKERS)>1 else batch)
            if df.empty or len(df) < 15: continue
            df = calculate_technical_indicators(df)
            price = df['close'].iloc[-1]
            if price <= MIN_PRICE: continue
            avg_val = (df['close'] * df['volume']).mean()
            if avg_val < LIQUIDITY_THRESHOLD: continue
            
            atr_pct = (df['atr_14'].iloc[-1] / price) * 100
            s_info = sector_df[sector_df['ticker'] == ticker]
            sector = s_info['sector'].iloc[0] if not s_info.empty else '不明'
            size = s_info['size_category'].iloc[0] if not s_info.empty else '-'
            
            elite.append({
                't': ticker, 'atr_pct': atr_pct, 
                'sector': sector, 'size_category': size,
                'rivals': get_rival_tickers(ticker, sector, sector_df)
            })
        except Exception: continue
    
    elite = sorted(elite, key=lambda x: x['atr_pct'], reverse=True)
    print(f"   -> フィルタ通過: {len(elite)} / {len(TEST_TICKERS)}")

    # Monthly / Weekly 解析 (固定ハードル遵守)
    long_res = run_test_session(elite, '1mo', 7, "Monthly", TARGET_PROFIT['Monthly'])
    short_res = run_test_session(elite, '1wk', 3, "Weekly", TARGET_PROFIT['Weekly'])
    
    combined = long_res + short_res
    output_file = "test_best_config.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({'timestamp': datetime.now().isoformat(), 'version': '15.15-TEST', 'details': combined}, 
                  f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    elapsed = time.time() - start_time
    print(f"\n✅ テスト完了！ ({elapsed/60:.1f}分)")
    if combined: print(f"選定数: {len(combined)} / 10")
    else: print("❌ 銘柄が選定されませんでした。")

if __name__ == "__main__":
    main()
