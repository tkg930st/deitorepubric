#!/usr/bin/env python3
"""
analyzer.py - Version 15.12 統合戦略実装
主な機能:
1. 2段階進化型最適化 (Phase 1: 広域 / Phase 2: 局所進化)
2. 詳細なDiscordレポート通知の完全復元 (Ver 13.5形式 + ライバル銘柄表示)
3. ボラティリティ比例型SL & フィルター個別最適化
"""
import json
import logging
import time
import os
import multiprocessing
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

def get_rival_tickers(ticker: str, sector: str, sector_df: pd.DataFrame, count: int = 5) -> List[str]:
    """同一セクターからライバル銘柄を抽出"""
    rivals = sector_df[sector_df['sector'] == sector]['ticker'].tolist()
    rivals = [r for r in rivals if r != ticker]
    return rivals[:count]

def select_high_volatility_stocks(all_tickers: List[str], sector_df: pd.DataFrame, count: int = 50) -> List[Dict]:
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
                    s_info = sector_df[sector_df['ticker'] == ticker]
                    sector = s_info['sector'].iloc[0] if not s_info.empty else '不明'
                    size = s_info['size_category'].iloc[0] if not s_info.empty else '-'
                    
                    candidates.append({
                        't': ticker, 'atr_pct': atr_pct, 
                        'sector': sector, 'size_category': size,
                        'rivals': get_rival_tickers(ticker, sector, sector_df)
                    })
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
    print("\n📊 Version 15.12 統合戦略構築開始")
    
    sector_df = get_jpx_list_with_sector()
    all_tickers = sector_df['ticker'].tolist()
    elite = select_high_volatility_stocks(all_tickers, sector_df, count=CANDIDATE_COUNT)
    
    # Monthly / Weekly 解析
    long_res = run_session(elite, '1mo', 7, "Monthly", 5.0)
    short_res = run_session(elite, '1wk', 3, "Weekly", 3.0)
    
    combined = long_res + short_res
    if not combined:
        print("❌ 条件を満たす銘柄が見つかりませんでした。"); return

    best_config = {'timestamp': datetime.now().isoformat(), 'version': '15.12', 'details': combined}
    with open(OUTPUT_CONFIG, 'w', encoding='utf-8') as f:
        json.dump(best_config, f, indent=2, ensure_ascii=False, cls=NpEncoder)
    
    elapsed = time.time() - start_time
    
    finish_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"✅ **Version 15.12 統合戦略構築完了！**\n\n"
    msg += f"⏱️ **実行時間**: {elapsed:.1f}秒 ({elapsed/60:.1f}分)\n"
    msg += f"📅 **完了時刻**: {finish_time} JST\n"
    msg += f"🚀 **並列処理**: {os.cpu_count()}コア活用\n\n"
    msg += f"🎯 **選定された監視銘柄 TOP{len(combined)}（raw利益順）**:\n\n"
    
    for r in combined:
        l_status = "🚫" if r['long_disabled'] else "✅"
        s_status = "🚫" if r['short_disabled'] else "✅"
        raw_total = r['raw_long_profit'] + r['raw_short_profit']
        msg += f"**{r['t']}** [{r['sector']}] ({r['size_category']})\n"
        msg += f"期待利益: {r['profit']:.2f}% (raw: {raw_total:.2f}%)\n"
        msg += f"L: {r['long_profit']:.1f}% {l_status} (raw:{r['raw_long_profit']:.1f}%) / S: {r['short_profit']:.1f}% {s_status} (raw:{r['raw_short_profit']:.1f}%)\n"
        msg += f"ボラティリティ: {r['atr_pct']:.2f}%\n"
        msg += f"ライバル5銘柄: {', '.join(r['rivals'])}\n"
        msg += f"タイプ: {r['logic_type']}\n\n"

    msg += f"📊 **最優秀銘柄**: {combined[0]['t']}\n"
    msg += f"   期待利益: {combined[0]['profit']:.2f}%\n"
    msg += f"   ボラティリティ: {combined[0]['atr_pct']:.2f}%\n\n"
    
    msg += f"🆕 **Version 15.12 改良点**:\n"
    msg += f"   • 2段階進化型最適化 (Phase 1: 広域 / Phase 2: 局所進化)\n"
    msg += f"   • Fitness Ver 2 (リスク対効果・RR効率評価)\n"
    msg += f"   • ボラティリティ比例型動的損切り (ATR基準)\n"
    msg += f"   • フィルターON/OFF個別最適化 (RSI/VWAP)\n"
    msg += f"   • 月次(Monthly) / 週次(Weekly) 独立戦略構築\n\n"
    
    msg += f"🔔 **次のステップ**:\n"
    msg += f"   python monitor.py で監視を開始してください！"
    
    send_discord_notification(WEBHOOK_URL, msg)
    print(f"\n✅ Version 15.12 構築完了！ ({elapsed/60:.1f}分)")

if __name__ == "__main__":
    main()
