#!/usr/bin/env python3
"""
analyzer.py - Version 15.0 統合戦略実装
主な機能:
1. 長期（1ヶ月）7銘柄 ＆ 短期（1週間）3銘柄の統合選定
2. セクター重複排除（1セクター1銘柄）
3. ハイブリッド・バックテスト（2段階評価）
4. 並列処理による高速化
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
    TOP_CANDIDATES, OPTIMIZATION_ITERATIONS,
    MIN_DATA_POINTS, DATA_FETCH, OUTPUT_CONFIG,
    WEBHOOK_URL, PRECISE_CHECK_COUNT, TREND_FILTER,
    MIN_SCORE_THRESHOLD, RETRY_OPTIMIZATION, FUNDAMENTAL_FILTER
)
from utils import (
    get_jpx_list_with_sector, super_flatten_columns, fetch_yfinance_data,
    calculate_technical_indicators, filter_trading_hours,
    send_discord_notification, calculate_ma_from_higher_timeframe
)
from backtest_engine import optimize_parameters

# ロギング設定
logging.basicConfig(
    filename=LOG_FILE,
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def calculate_liquidity_score(df: pd.DataFrame, ticker: str) -> Tuple[float, float]:
    if df.empty or len(df) < 5: return 0.0, 0.0
    try:
        avg_value = (df['close'] * df['volume']).mean()
        volatility_score = (((df['high'] - df['low']) / df['close']) * 100).mean()
        return avg_value, avg_value * volatility_score
    except Exception: return 0.0, 0.0

def select_main_stocks(all_tickers: List[str], sector_df: pd.DataFrame, count: int = 10) -> List[Dict]:
    candidates = []
    chunk_size = DATA_FETCH.get('chunk_size', 30)
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        try:
            batch_data = fetch_yfinance_data(chunk, period='1mo', interval='1d')
            if batch_data.empty: continue
            for ticker in chunk:
                try:
                    if len(chunk) > 1:
                        if ticker not in batch_data.columns.get_level_values(0): continue
                        ticker_raw = batch_data[ticker].copy()
                    else: ticker_raw = batch_data.copy()
                    df = super_flatten_columns(ticker_raw)
                    if df.empty or len(df) < 20: continue
                    df = calculate_technical_indicators(df)
                    latest_price = df['close'].iloc[-1]
                    avg_value, _ = calculate_liquidity_score(df, ticker)
                    if latest_price <= MIN_PRICE or avg_value < LIQUIDITY_THRESHOLD: continue
                    latest_atr = df['atr_14'].iloc[-1]
                    vol_score = (latest_atr / latest_price) * 100
                    sector_info = sector_df[sector_df['ticker'] == ticker]
                    sector_name = sector_info['sector'].iloc[0] if not sector_info.empty else '不明'
                    size_category = sector_info['size_category'].iloc[0] if not sector_info.empty and 'size_category' in sector_info.columns else ''
                    candidates.append({
                        't': ticker, 'price': latest_price, 'value': avg_value,
                        'atr': latest_atr, 'volatility_score': vol_score,
                        'sector': sector_name, 'size_category': size_category
                    })
                except Exception: continue
            time.sleep(0.5)
        except Exception: continue

    sorted_candidates = sorted(candidates, key=lambda x: x['volatility_score'], reverse=True)
    elite = []
    used_sectors = set()
    for candidate in sorted_candidates:
        if candidate['sector'] not in used_sectors:
            elite.append(candidate)
            used_sectors.add(candidate['sector'])
            if len(elite) >= count * 3: break # 多めに確保
    return elite

def worker_analyze_ticker(ticker_info: Dict, period: str) -> Dict:
    ticker = ticker_info['t']
    try:
        ticker_data = fetch_yfinance_data([ticker], period=period, interval=DATA_FETCH['analyzer_interval'])
        if ticker_data.empty: return None
        df = super_flatten_columns(ticker_data)
        df = filter_trading_hours(df)
        if len(df) < MIN_DATA_POINTS: return None
        df = calculate_technical_indicators(df)
        if TREND_FILTER['enabled']:\
            df['ma_15m_20'] = calculate_ma_from_higher_timeframe(df, TREND_FILTER['ma_period'])
        df_15m = df[['ma_15m_20']].copy() if TREND_FILTER['enabled'] else pd.DataFrame()
        result_long = optimize_parameters(df, df_15m, 'long', OPTIMIZATION_ITERATIONS, PRECISE_CHECK_COUNT)
        result_short = optimize_parameters(df, df_15m, 'short', OPTIMIZATION_ITERATIONS, PRECISE_CHECK_COUNT)
        
        long_profit = float(result_long.get('profit', 0))
        short_profit = float(result_short.get('profit', 0))
        total_profit = (long_profit if long_profit >= 5.0 else 0) + (short_profit if short_profit >= 5.0 else 0)

        if total_profit <= 0: return None

        return {
            't': ticker, 'profit': total_profit,
            'long_profit': long_profit, 'short_profit': short_profit,
            'long_disabled': long_profit < 5.0, 'short_disabled': short_profit < 5.0,
            'sector': ticker_info['sector'], 'size_category': ticker_info.get('size_category', ''),
            'params': {'long': result_long['params'], 'short': result_short['params']}
        }
    except Exception: return None

def run_analysis_session(elite_stocks: List[Dict], period: str, count: int, label: str) -> List[Dict]:
    print(f"\n🔬 {label} 解析開始 (期間: {period}, 目標: {count}銘柄)")
    results = []
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = {executor.submit(worker_analyze_ticker, s, period): s for s in elite_stocks}
        for future in as_completed(futures):
            res = future.result()
            if res:
                res['logic_type'] = label
                results.append(res)
                print(f"   ✅ {res['t']} 完了 (利益: {res['profit']:.2f}%)")
    return sorted(results, key=lambda x: x['profit'], reverse=True)[:count]

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            if isinstance(obj, np.integer): return int(obj)
            if isinstance(obj, np.floating): return float(obj)
            return bool(obj)
        return super(NpEncoder, self).default(obj)

def main():
    start_time = time.time()
    print("\n📊 Version 15.0 統合戦略 戦略構築開始")
    sector_df = get_jpx_list_with_sector()
    all_tickers = sector_df['ticker'].tolist()
    elite_candidates = select_main_stocks(all_tickers, sector_df, count=15)
    
    # 長期（1ヶ月/7銘柄）
    long_term_results = run_analysis_session(elite_candidates, '1mo', 7, "Long(1Month)")
    # 短期（1週間/3銘柄）
    short_term_results = run_analysis_session(elite_candidates, '1wk', 3, "Short(1Week)")
    
    combined_results = long_term_results + short_term_results
    if not combined_results:
        print("\n❌ 銘柄が選定されませんでした")
        return

    best_config = {
        'timestamp': datetime.now().isoformat(),
        'version': '15.0',
        'details': combined_results
    }
    
    with open(OUTPUT_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(best_config, f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    elapsed = time.time() - start_time
    msg = f"✅ **Version 15.0 統合戦略構築完了！**\n⏱️ 実行時間: {elapsed/60:.1f}分\n\n"
    for r in combined_results:
        msg += f"• **{r['t']}** | {r['logic_type']} | 期待: {r['profit']:.2f}%\n"
    
    send_discord_notification(WEBHOOK_URL, msg)
    print(f"\n✅ 設定ファイル保存完了: {OUTPUT_CONFIG}")

if __name__ == "__main__":
    main()
