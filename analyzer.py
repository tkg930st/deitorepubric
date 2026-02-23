#!/usr/bin/env python3
"""
analyzer.py - Version 15.11 統合戦略実装
変更点:
- long_disabled / short_disabled フラグを明示的に保存し、Monitorでの誤動作を防止
- 解析対象母数 50銘柄、試行回数 500回 (config準拠)
- 期間名称を Monthly / Weekly に統一
"""
import json
import logging
import time
import os
from datetime import datetime
from typing import List, Dict, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf

from config import (
    LOG_FILE, LOG_LEVEL, LIQUIDITY_THRESHOLD, MIN_PRICE,
    OPTIMIZATION_ITERATIONS, MIN_DATA_POINTS, DATA_FETCH, OUTPUT_CONFIG,
    WEBHOOK_URL, TREND_FILTER
)
from utils import (
    get_jpx_list_with_sector, super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, filter_trading_hours,
    send_discord_notification
)
from backtest_engine import optimize_parameters

CANDIDATE_COUNT = 50

logging.basicConfig(
    filename=LOG_FILE, filemode='a', encoding='utf-8',
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def select_high_volatility_stocks(all_tickers: List[str], count: int = 50) -> List[Dict]:
    msg = f"🔍 スクリーニング開始 (主力候補 {count}銘柄を抽出)"
    print(msg); logger.info(msg)
    candidates = []
    chunk_size = 50
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i+chunk_size]
        try:
            batch = fetch_yfinance_data(chunk, period='1mo', interval='1d')
            for ticker in chunk:
                try:
                    df = super_flatten_columns(batch[ticker] if len(chunk)>1 else batch)
                    if df.empty or len(df) < 15: continue
                    df = calculate_technical_indicators(df)
                    price = df['close'].iloc[-1]
                    if price <= MIN_PRICE: continue
                    avg_val = (df['close'] * df['volume']).mean()
                    if avg_val < LIQUIDITY_THRESHOLD: continue
                    
                    atr_pct = (df['atr_14'].iloc[-1] / price) * 100
                    candidates.append({'t': ticker, 'atr_pct': atr_pct})
                except Exception: continue
        except Exception: continue
    return sorted(candidates, key=lambda x: x['atr_pct'], reverse=True)[:count]

def worker_analyze_ticker(ticker_info: Dict, period: str, hurdle: float) -> Dict:
    ticker = ticker_info['t']
    try:
        data = fetch_yfinance_data([ticker], period=period, interval=DATA_FETCH['analyzer_interval'])
        df = super_flatten_columns(data)
        df = filter_trading_hours(df)
        if len(df) < MIN_DATA_POINTS: return None
        df = calculate_technical_indicators(df)
        
        res_l = optimize_parameters(df, pd.DataFrame(), 'long', OPTIMIZATION_ITERATIONS)
        res_s = optimize_parameters(df, pd.DataFrame(), 'short', OPTIMIZATION_ITERATIONS)
        
        l_prof = res_l['profit']; s_prof = res_s['profit']
        
        # ハードル判定と無効化フラグ
        is_l_valid = l_prof >= hurdle
        is_s_valid = s_prof >= hurdle
        
        valid_l = l_prof if is_l_valid else 0.0
        valid_s = s_prof if s_prof >= hurdle else 0.0
        total = valid_l + valid_s
        
        if total <= 0: return None

        return {
            't': ticker, 'profit': total, 
            'long_profit': valid_l, 'short_profit': valid_s,
            'long_disabled': not is_l_valid, 'short_disabled': not is_s_valid,
            'params': {'long': res_l['params'], 'short': res_s['params']}
        }
    except Exception as e:
        logger.error(f"{ticker} 解析エラー: {e}"); return None

def run_session(elite: List[Dict], period: str, count: int, label: str, hurdle: float) -> List[Dict]:
    msg = f"\n🔬 {label} 解析開始 (ハードル: {hurdle}%)"
    print(msg); logger.info(msg)
    results = []
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = {executor.submit(worker_analyze_ticker, s, period, hurdle): s for s in elite}
        for f in as_completed(futures):
            res = f.result()
            if res:
                res['logic_type'] = label
                results.append(res)
                print(f"   ✅ {res['t']} 完了 (有効利益: {res['profit']:.2f}%)")
    return sorted(results, key=lambda x: x['profit'], reverse=True)[:count]

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return int(obj) if isinstance(obj, np.integer) else float(obj) if isinstance(obj, np.floating) else bool(obj)
        return super(NpEncoder, self).default(obj)

def main():
    start_time = time.time()
    print("\n📊 Version 15.11 統合戦略構築開始")
    sector_df = get_jpx_list_with_sector()
    elite = select_high_volatility_stocks(sector_df['ticker'].tolist(), count=CANDIDATE_COUNT)
    
    # Monthly（1ヶ月/7銘柄/5.0%ハードル）
    long_res = run_session(elite, '1mo', 7, "Monthly", 5.0)
    # Weekly（1週間/3銘柄/3.0%ハードル）
    short_res = run_session(elite, '1wk', 3, "Weekly", 3.0)
    
    combined = long_res + short_res
    if not combined:
        print("❌ 条件を満たす銘柄が見つかりませんでした。"); return

    best_config = {'timestamp': datetime.now().isoformat(), 'version': '15.11', 'details': combined}
    with open(OUTPUT_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(best_config, f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    elapsed = time.time() - start_time
    print(f"\n✅ Version 15.11 構築完了！ ({elapsed/60:.1f}分)")
    send_discord_notification(WEBHOOK_URL, f"✅ **Version 15.11 構築完了**\n選定数: {len(combined)}")

if __name__ == "__main__":
    main()
