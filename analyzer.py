#!/usr/bin/env python3
"""
analyzer.py - Version 13.5 統合実装

主な機能:
1. セクター重複排除（1セクター1銘柄）
2. ライバル5銘柄の確定選定（段階的緩和）
3. ハイブリッド・バックテスト（2段階評価）
4. 15分足データ取得＆トレンドフィルター
5. 規模区分の取得・保存
6. ATRスクリーニングによる銘柄選定
7. 並列処理による高速化
8. Ver 13.5: Raw Profit保持 & 強力再試行ループ & ライバルデータ供給
"""
import json
import logging
import time
import os
from datetime import datetime
from typing import List, Dict, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed
import random

import numpy as np
import pandas as pd
import yfinance as yf

from config import (
    LOG_FILE, LOG_LEVEL, LIQUIDITY_THRESHOLD, MIN_PRICE,
    TOP_CANDIDATES, FINAL_MONITORING, OPTIMIZATION_ITERATIONS,
    MIN_DATA_POINTS, PARAM_RANGES, DATA_FETCH, OUTPUT_CONFIG,
    WEBHOOK_URL, PRECISE_CHECK_COUNT, TREND_FILTER, ATR_CHECK_COUNT,
    MIN_SCORE_THRESHOLD, RETRY_OPTIMIZATION, SECTOR_ALIGNMENT,
    FUNDAMENTAL_FILTER
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
    """
    流動性スコアを計算（売買代金 × ボラティリティ）
    """
    if df.empty or len(df) < 5:
        return 0.0, 0.0
    
    try:
        latest_price = df['close'].iloc[-1]
        avg_value = (df['close'] * df['volume']).mean()
        volatility_score = (((df['high'] - df['low']) / df['close']) * 100).mean()
        total_score = avg_value * volatility_score
        
        logger.debug(
            f"{ticker}: 価格={latest_price:.0f}, "
            f"売買代金={avg_value/1e9:.1f}億円, "
            f"ボラ={volatility_score:.2f}%, "
            f"スコア={total_score/1e9:.1f}"
        )
        
        return avg_value, total_score
        
    except Exception as e:
        logger.error(f"流動性スコア計算エラー ({ticker}): {str(e)}")
        return 0.0, 0.0


def select_main_stocks(all_tickers: List[str], sector_df: pd.DataFrame) -> List[Dict]:
    """主力銘柄を選定（一括日足スクリーニングによる高速版）"""
    logger.info("=" * 60)
    logger.info("主力銘柄選定フェーズ開始（高速日足スクリーニング）")
    logger.info("=" * 60)
    
    print(f"\n🔍 STEP 2: 全{len(all_tickers)}銘柄から上位{TOP_CANDIDATES}銘柄を高速選定中...")
    print(f" 条件: 株価>{MIN_PRICE}円 & 売買代金>{LIQUIDITY_THRESHOLD/1e8:.1f}億円以上 & セクター重複排除")
    
    candidates = []
    chunk_size = DATA_FETCH.get('chunk_size', 30)
    processed = 0
    
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        processed += len(chunk)
        
        if processed % 90 <= chunk_size:
            print(f"   進捗: {min(processed, len(all_tickers))}/{len(all_tickers)} ({min(processed, len(all_tickers))/len(all_tickers)*100:.1f}%)")
        
        try:
            batch_data = fetch_yfinance_data(chunk, period='3mo', interval='1d')
            
            if batch_data.empty:
                continue
            
            for ticker in chunk:
                try:
                    if len(chunk) > 1:
                        if ticker not in batch_data.columns.get_level_values(0):
                            continue
                        ticker_raw = batch_data[ticker].copy()
                    else:
                        ticker_raw = batch_data.copy()
                        
                    df = super_flatten_columns(ticker_raw)
                    
                    if df.empty or len(df) < 20:
                        continue
                        
                    last_date = df.index[-1]
                    days_diff = (datetime.now(last_date.tzinfo) - last_date).days
                    if days_diff > 7:
                        continue
                    
                    if len(df) >= 5:
                        if df['close'].tail(5).std() < 0.0001:
                            continue

                    df = calculate_technical_indicators(df)
                    if 'atr_14' not in df.columns or df['atr_14'].isna().all():
                        continue

                    latest_price = df['close'].iloc[-1]
                    avg_value, _ = calculate_liquidity_score(df, ticker)
                    
                    if latest_price <= MIN_PRICE or avg_value < LIQUIDITY_THRESHOLD:
                        continue
                    
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
                except Exception:
                    continue
            
            time.sleep(DATA_FETCH['request_delay'])
            
        except Exception as e:
            logger.warning(f"チャンク処理エラー: {str(e)}")
            continue
    
    # ファンダメンタルズ・スクリーニング（プロ手法: ROE & PEGレシオ）
    if FUNDAMENTAL_FILTER.get('enabled', False) and candidates:
        min_roe = FUNDAMENTAL_FILTER.get('min_roe', 0.10)
        max_peg = FUNDAMENTAL_FILTER.get('max_peg', 1.0)
        print(f"\n🔬 ファンダメンタルズ・スクリーニング中... (ROE>={min_roe*100:.0f}%, PEG<={max_peg})")
        filtered_candidates = []
        for idx, candidate in enumerate(candidates):
            ticker = candidate['t']
            try:
                info = yf.Ticker(ticker).info
                roe = info.get('returnOnEquity')
                peg = info.get('pegRatio')

                # データ欠損（None）の場合は厳格に除外せず通過させる
                if roe is not None and roe < min_roe:
                    logger.debug(f"{ticker}: ROE不足 ({roe:.2%} < {min_roe:.0%}) → 除外")
                    continue
                if peg is not None and peg > max_peg:
                    logger.debug(f"{ticker}: PEG超過 ({peg:.2f} > {max_peg}) → 除外")
                    continue

                filtered_candidates.append(candidate)
                logger.debug(f"{ticker}: ファンダメンタルズ通過 (ROE={roe}, PEG={peg})")
            except Exception as e:
                # 取得エラーの場合は除外せず通過
                filtered_candidates.append(candidate)
                logger.debug(f"{ticker}: ファンダメンタルズ取得エラー ({str(e)}) → 通過")

            if idx % 10 == 9:
                time.sleep(1)  # API制限対策

        print(f"   ファンダメンタルズ: {len(candidates)}銘柄 → {len(filtered_candidates)}銘柄")
        candidates = filtered_candidates

    sorted_candidates = sorted(candidates, key=lambda x: x['volatility_score'], reverse=True)
    
    elite = []
    used_sectors = set()
    
    for candidate in sorted_candidates:
        sector = candidate['sector']
        if sector not in used_sectors:
            elite.append(candidate)
            used_sectors.add(sector)
            if len(elite) >= TOP_CANDIDATES:
                break
    
    print(f"\n✅ スクリーニング完了: {len(elite)}銘柄を選定")
    print(f"\n📊 選定銘柄一覧（ボラティリティ順）:")
    for idx, item in enumerate(elite, 1):
        print(f"   {idx:2d}. {item['t']:10s} ボラ={item['volatility_score']:>5.2f}% ATR={item['atr']:>6.1f} 価格={item['price']:>8.0f}円 セクター={item['sector']}")
    
    return elite


def select_rival_stocks(target_stocks: List[Dict], all_candidates: List[Dict], 
                        sector_df: pd.DataFrame, num_rivals: int = 5) -> Dict[str, List[str]]:
    """各監視銘柄のライバル銘柄を5銘柄確定選定（段階的緩和ロジック）"""
    logger.info("=" * 60)
    logger.info(f"ライバル銘柄選定フェーズ開始（確定{num_rivals}銘柄）")
    logger.info("=" * 60)
    
    print(f"\n🔍 各監視銘柄のライバル銘柄を選定中... (確定{num_rivals}銘柄)")
    
    rivals_map = {}
    
    for target in target_stocks:
        target_ticker = target['t']
        target_sector = target['sector']
        
        print(f"\n   📊 {target_ticker} ({target_sector})")
        logger.info(f"ライバル選定開始: {target_ticker} (セクター: {target_sector})")
        
        rivals = []
        
        same_sector_all = sector_df[
            (sector_df['sector'] == target_sector) & 
            (sector_df['ticker'] != target_ticker)
        ].copy()
        
        if same_sector_all.empty:
            logger.warning(f"{target_ticker}: 同セクター銘柄が見つかりません")
            print(f"      ⚠️  同セクター銘柄なし → 市場全体から選定")
            
            market_wide = [c for c in all_candidates if c['t'] != target_ticker]
            market_wide_sorted = sorted(market_wide, key=lambda x: x['value'], reverse=True)
            rivals = [c['t'] for c in market_wide_sorted[:num_rivals]]
            
            if len(rivals) < num_rivals:
                remaining = num_rivals - len(rivals)
                other_tickers = sector_df[
                    (~sector_df['ticker'].isin(rivals)) & 
                    (sector_df['ticker'] != target_ticker)
                ]['ticker'].tolist()
                rivals.extend(other_tickers[:remaining])
        
        else:
            high_liquidity = [c for c in all_candidates 
                            if c['t'] != target_ticker and c['sector'] == target_sector and c['value'] >= LIQUIDITY_THRESHOLD]
            high_liquidity_sorted = sorted(high_liquidity, key=lambda x: x['value'], reverse=True)
            rivals.extend([c['t'] for c in high_liquidity_sorted[:num_rivals]])
            logger.info(f"  STEP1(高流動性): {len(rivals)}銘柄選定")
            
            if len(rivals) < num_rivals:
                threshold_ratios = [0.5, 0.25, 0.0]
                for ratio in threshold_ratios:
                    if len(rivals) >= num_rivals:
                        break
                    adjusted_threshold = LIQUIDITY_THRESHOLD * ratio
                    logger.info(f"  STEP2(緩和): 閾値={adjusted_threshold/1e9:.1f}億円で再検索")
                    
                    relaxed_candidates = []
                    for _, row in same_sector_all.iterrows():
                        ticker = row['ticker']
                        if ticker in rivals:
                            continue
                        candidate_info = next((c for c in all_candidates if c['t'] == ticker), None)
                        if candidate_info and candidate_info['value'] >= adjusted_threshold:
                            relaxed_candidates.append(candidate_info)
                    
                    relaxed_sorted = sorted(relaxed_candidates, key=lambda x: x['value'], reverse=True)
                    remaining = num_rivals - len(rivals)
                    rivals.extend([c['t'] for c in relaxed_sorted[:remaining]])
                    logger.info(f"    追加: {min(len(relaxed_sorted), remaining)}銘柄")
            
            if len(rivals) < num_rivals:
                logger.info(f"  STEP3(全補充): 同セクター全銘柄から補充")
                all_same_sector_tickers = same_sector_all['ticker'].tolist()
                for ticker in all_same_sector_tickers:
                    if ticker not in rivals:
                        rivals.append(ticker)
                        if len(rivals) >= num_rivals:
                            break
            
            if len(rivals) < num_rivals:
                logger.warning(f"  STEP4(市場補充): セクター内不足 → 市場全体から補充")
                print(f"      ⚠️  セクター内不足 → 市場全体から補充")
                market_candidates = [c for c in all_candidates if c['t'] not in rivals and c['t'] != target_ticker]
                market_sorted = sorted(market_candidates, key=lambda x: x['value'], reverse=True)
                remaining = num_rivals - len(rivals)
                rivals.extend([c['t'] for c in market_sorted[:remaining]])
        
        if len(rivals) < num_rivals:
            logger.error(f"{target_ticker}: ライバル銘柄が{num_rivals}に満たない ({len(rivals)}銘柄)")
            all_tickers = sector_df['ticker'].tolist()
            for ticker in all_tickers:
                if ticker not in rivals and ticker != target_ticker:
                    rivals.append(ticker)
                    if len(rivals) >= num_rivals:
                        break
        
        rivals = rivals[:num_rivals]
        rivals_map[target_ticker] = rivals
        
        print(f"      ライバル({len(rivals)}銘柄): {', '.join(rivals)}")
        logger.info(f"{target_ticker} のライバル: {rivals}")
    
    logger.info("ライバル銘柄選定完了")
    return rivals_map


def fetch_rival_5m_data(rivals_map: Dict[str, List[str]]) -> Dict[str, pd.DataFrame]:
    """
    Ver 13.5: ライバル銘柄の5分足データを一括取得
    """
    all_rivals = set()
    for rival_list in rivals_map.values():
        all_rivals.update(rival_list)
    
    all_rivals = list(all_rivals)
    if not all_rivals:
        return {}
    
    print(f"\n📡 ライバル銘柄の5分足データ取得中... ({len(all_rivals)}銘柄)")
    
    rival_dfs = {}
    chunk_size = DATA_FETCH.get('chunk_size', 30)
    
    for i in range(0, len(all_rivals), chunk_size):
        chunk = all_rivals[i:i + chunk_size]
        try:
            raw_data = fetch_yfinance_data(chunk, period=DATA_FETCH['analyzer_period'], interval='5m')
            if raw_data.empty:
                continue
            for ticker in chunk:
                try:
                    if len(chunk) > 1:
                        if ticker not in raw_data.columns.get_level_values(0):
                            continue
                        ticker_raw = raw_data[ticker].copy()
                    else:
                        ticker_raw = raw_data.copy()
                    df = super_flatten_columns(ticker_raw)
                    df = filter_trading_hours(df)
                    if not df.empty and len(df) >= 20:
                        rival_dfs[ticker] = df
                except Exception:
                    continue
            time.sleep(DATA_FETCH['request_delay'])
        except Exception as e:
            logger.warning(f"ライバルデータ取得エラー: {str(e)}")
            continue
    
    print(f"   ✅ ライバルデータ取得完了: {len(rival_dfs)}/{len(all_rivals)}銘柄")
    return rival_dfs


def worker_analyze_ticker(ticker_info: Dict) -> Dict:
    """並列処理用ワーカー関数"""
    ticker = ticker_info['t']
    sector = ticker_info['sector']
    size_category = ticker_info.get('size_category', '')
    rival_dfs = ticker_info.get('rival_dfs', None)  # Ver 13.5
    
    result = analyze_ticker(ticker, rival_dfs=rival_dfs)
    
    if result:
        result['sector'] = sector
        result['size_category'] = size_category
        result['volatility_score'] = ticker_info.get('volatility_score', 0)
    
    return result


def analyze_ticker(ticker: str, rival_dfs: Dict[str, pd.DataFrame] = None) -> Dict:
    """個別銘柄の最適化（Version 13.5: ライバルデータ供給 & Raw Profit保持）"""
    logger.info(f"{'=' * 60}")
    logger.info(f"銘柄解析開始: {ticker}")
    logger.info(f"{'=' * 60}")
    
    try:
        print(f"      データ取得中...")
        ticker_data = fetch_yfinance_data(
            [ticker], period=DATA_FETCH['analyzer_period'], interval=DATA_FETCH['analyzer_interval']
        )
        
        if ticker_data.empty:
            logger.error(f"{ticker}: データ取得失敗")
            return None
        
        df = super_flatten_columns(ticker_data)
        df = filter_trading_hours(df)
        
        if df.empty or len(df) < MIN_DATA_POINTS:
            logger.error(f"{ticker}: データ不足 (取得: {len(df)}行)")
            return None
        
        print(f"      テクニカル指標計算中...")
        df = calculate_technical_indicators(df)
        
        if TREND_FILTER['enabled']:
            print(f"      15分足MA計算中...")
            df['ma_15m_20'] = calculate_ma_from_higher_timeframe(df, TREND_FILTER['ma_period'])
        
        df_15m = df[['ma_15m_20']].copy() if TREND_FILTER['enabled'] else pd.DataFrame()
        
        logger.info(f"{ticker}: データ準備完了 ({len(df)}行)")
        
        # ロング最適化（Ver 13.5: ライバルデータ供給）
        print(f"      ロング戦略最適化中（ハイブリッド: {OPTIMIZATION_ITERATIONS}→{PRECISE_CHECK_COUNT}）...")
        result_long = optimize_parameters(df, df_15m, 'long', OPTIMIZATION_ITERATIONS, PRECISE_CHECK_COUNT, rival_dfs)
        
        # ショート戦略最適化（Ver 13.5: ライバルデータ供給）
        print(f"      ショート戦略最適化中（ハイブリッド: {OPTIMIZATION_ITERATIONS}→{PRECISE_CHECK_COUNT}）...")
        result_short = optimize_parameters(df, df_15m, 'short', OPTIMIZATION_ITERATIONS, PRECISE_CHECK_COUNT, rival_dfs)
        
        # 明示的にfloatへキャスト
        long_profit = float(result_long.get('profit', 0))
        short_profit = float(result_short.get('profit', 0))
        
        # Ver 13.5: Raw Profit（生数値）を保持
        raw_long_profit = long_profit
        raw_short_profit = short_profit
        
        # 5%未満の方向は0%に修正（Ver 11.0）
        # 明示的にboolへキャスト
        long_disabled = bool(long_profit < 5.0)
        short_disabled = bool(short_profit < 5.0)
        
        if long_disabled:
            logger.info(f"{ticker}: LONG利益 {long_profit:.2f}% < 5% → 0%に修正（エントリー禁止）")
            long_profit = 0.0
        
        if short_disabled:
            logger.info(f"{ticker}: SHORT利益 {short_profit:.2f}% < 5% → 0%に修正（エントリー禁止）")
            short_profit = 0.0
        
        total_profit = long_profit + short_profit
        
        logger.info(
            f"{ticker} 最適化完了: "
            f"ロング={long_profit:.2f}%{' (禁止)' if long_disabled else ''}, "
            f"ショート={short_profit:.2f}%{' (禁止)' if short_disabled else ''}, "
            f"総合={total_profit:.2f}%"
        )
        
        return {
            't': ticker,
            'profit': total_profit,
            'long_profit': long_profit,
            'short_profit': short_profit,
            'raw_long_profit': raw_long_profit,    # Ver 13.5: 生数値保持
            'raw_short_profit': raw_short_profit,  # Ver 13.5: 生数値保持
            'long_disabled': long_disabled,
            'short_disabled': short_disabled,
            'params': {
                'long': result_long.get('params', {}),
                'short': result_short.get('params', {})
            }
        }
        
    except Exception as e:
        logger.error(f"{ticker} 解析エラー: {str(e)}", exc_info=True)
        return None


def retry_optimization_loop(ticker_results: List[Dict], elite_stocks: List[Dict],
                             rivals_map: Dict[str, List[str]],
                             all_rival_dfs: Dict[str, pd.DataFrame]) -> List[Dict]:
    """
    Ver 13.5: 強力再試行ループ
    
    利益5%未満の銘柄のうち、生数値(raw profit)が高い上位5銘柄に対し、
    5%を突破するまで最大10回（500回×10）の再最適化を繰り返す。
    """
    if not RETRY_OPTIMIZATION.get('enabled', True):
        return ticker_results
    
    max_retries = RETRY_OPTIMIZATION.get('max_retries', 10)
    retry_top_n = RETRY_OPTIMIZATION.get('retry_top_n', 5)
    target_profit = RETRY_OPTIMIZATION.get('target_profit', 5.0)
    iterations_per_retry = RETRY_OPTIMIZATION.get('iterations_per_retry', 500)
    
    # 利益5%未満で、raw profitが高い銘柄を抽出
    retry_candidates = []
    for result in ticker_results:
        raw_total = result.get('raw_long_profit', 0) + result.get('raw_short_profit', 0)
        if result['profit'] < target_profit * 2 and raw_total > 0:
            retry_candidates.append({
                'result': result,
                'raw_total': raw_total
            })
    
    retry_candidates = sorted(retry_candidates, key=lambda x: x['raw_total'], reverse=True)[:retry_top_n]
    
    if not retry_candidates:
        print(f"\n   再試行対象銘柄なし")
        return ticker_results
    
    print(f"\n🔄 Ver 13.5 強力再試行ループ開始")
    print(f"   対象: {len(retry_candidates)}銘柄, 最大{max_retries}回 × {iterations_per_retry}試行")
    
    # 結果をtickerでインデックス化
    results_by_ticker = {r['t']: r for r in ticker_results}
    
    for cand in retry_candidates:
        result = cand['result']
        ticker = result['t']
        raw_total = cand['raw_total']
        
        print(f"\n   🎯 {ticker} (raw利益: {raw_total:.2f}%)")
        
        # ライバルデータを構築
        rival_dfs_for_ticker = {}
        if ticker in rivals_map:
            for r in rivals_map[ticker]:
                if r in all_rival_dfs:
                    rival_dfs_for_ticker[r] = all_rival_dfs[r]
        
        # 銘柄データを再取得
        try:
            ticker_data = fetch_yfinance_data(
                [ticker], period=DATA_FETCH['analyzer_period'], interval=DATA_FETCH['analyzer_interval']
            )
            if ticker_data.empty:
                continue
            
            df = super_flatten_columns(ticker_data)
            df = filter_trading_hours(df)
            if df.empty or len(df) < MIN_DATA_POINTS:
                continue
            
            df = calculate_technical_indicators(df)
            if TREND_FILTER['enabled']:
                df['ma_15m_20'] = calculate_ma_from_higher_timeframe(df, TREND_FILTER['ma_period'])
            df_15m = df[['ma_15m_20']].copy() if TREND_FILTER['enabled'] else pd.DataFrame()
        except Exception as e:
            logger.error(f"再試行データ取得エラー ({ticker}): {str(e)}")
            continue
        
        best_long_profit = float(result.get('raw_long_profit', 0))
        best_short_profit = float(result.get('raw_short_profit', 0))
        best_long_params = result['params']['long']
        best_short_params = result['params']['short']
        
        for retry_num in range(1, max_retries + 1):
            # LONG再試行
            if best_long_profit < target_profit:
                retry_long = optimize_parameters(
                    df, df_15m, 'long', iterations_per_retry, PRECISE_CHECK_COUNT, rival_dfs_for_ticker
                )
                p_long = float(retry_long.get('profit', 0))
                if p_long > best_long_profit:
                    best_long_profit = p_long
                    best_long_params = retry_long['params']
            
            # SHORT再試行
            if best_short_profit < target_profit:
                retry_short = optimize_parameters(
                    df, df_15m, 'short', iterations_per_retry, PRECISE_CHECK_COUNT, rival_dfs_for_ticker
                )
                p_short = float(retry_short.get('profit', 0))
                if p_short > best_short_profit:
                    best_short_profit = p_short
                    best_short_params = retry_short['params']
            
            current_total = (best_long_profit if best_long_profit >= target_profit else 0) + \
                           (best_short_profit if best_short_profit >= target_profit else 0)
            
            print(f"      [{retry_num}/{max_retries}] L={best_long_profit:.2f}% S={best_short_profit:.2f}% → 合計={current_total:.2f}%")
            
            # 両方5%以上達成したら終了
            if best_long_profit >= target_profit and best_short_profit >= target_profit:
                print(f"      ✅ 5%突破達成！")
                break
            
            # 合計が十分なら終了
            if current_total >= target_profit * 2:
                print(f"      ✅ 合計目標達成！")
                break
        
        # 結果を更新
        # 明示的にboolへキャスト
        long_disabled = bool(best_long_profit < target_profit)
        short_disabled = bool(best_short_profit < target_profit)
        final_long = 0.0 if long_disabled else best_long_profit
        final_short = 0.0 if short_disabled else best_short_profit
        
        updated_result = {
            't': ticker,
            'profit': final_long + final_short,
            'long_profit': final_long,
            'short_profit': final_short,
            'raw_long_profit': best_long_profit,
            'raw_short_profit': best_short_profit,
            'long_disabled': long_disabled,
            'short_disabled': short_disabled,
            'params': {
                'long': best_long_params,
                'short': best_short_params
            },
            'sector': result.get('sector', ''),
            'size_category': result.get('size_category', ''),
            'volatility_score': result.get('volatility_score', 0)
        }
        
        results_by_ticker[ticker] = updated_result
        print(f"      最終: L={final_long:.2f}%{'🚫' if long_disabled else ''} S={final_short:.2f}%{'🚫' if short_disabled else ''} 合計={final_long + final_short:.2f}%")
    
    return list(results_by_ticker.values())


class NpEncoder(json.JSONEncoder):
    """Numpy型をJSON変換するためのカスタムエンコーダ"""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        return super(NpEncoder, self).default(obj)


def main():
    """メイン処理"""
    start_time = time.time()
    
    print("\n" + "=" * 60)
    print("📊 Version 13.5 統合実装 戦略構築システム")
    print("=" * 60)
    
    logger.info("=" * 60)
    logger.info("Version 13.5 統合実装 戦略構築開始")
    logger.info("=" * 60)
    
    try:
        # 1. JPX銘柄リスト取得（セクター・規模区分付き）
        print("\n📋 STEP 1: JPX銘柄リスト取得中...")
        sector_df = get_jpx_list_with_sector()
        all_tickers = sector_df['ticker'].tolist()
        
        print(f"   取得完了: {len(all_tickers)}銘柄")
        logger.info(f"JPX銘柄数: {len(all_tickers)}")
        
        # 2. 主力銘柄選定（高速化版）
        print("\n🎯 STEP 2: 主力銘柄選定（セクター重複排除）")
        elite_stocks = select_main_stocks(all_tickers, sector_df)
        
        if not elite_stocks:
            logger.error("主力銘柄選定失敗")
            print("\n❌ エラー: 条件を満たす銘柄がありませんでした")
            return
        
        # Ver 13.5: ライバル銘柄を先に確定（解析前）
        print("\n" + "=" * 60)
        print("📋 STEP 2.5: ライバル銘柄先行選定（Ver 13.5）")
        print("=" * 60)
        
        rivals_map = select_rival_stocks(elite_stocks, elite_stocks, sector_df, num_rivals=5)
        
        # Ver 13.5: ライバルの5分足データを事前取得
        all_rival_dfs = fetch_rival_5m_data(rivals_map)
        
        # 3. 各銘柄の最適化（ハイブリッド・バックテスト + 並列処理）
        print("\n🔬 STEP 3: 戦略最適化中（並列処理 + ハイブリッド・バックテスト）...")
        print(f"   対象: {len(elite_stocks)}銘柄")
        print(f"   並列実行: {os.cpu_count()}コア使用")
        print(f"   第1段階: 高速スクリーニング（{OPTIMIZATION_ITERATIONS}通り）")
        print(f"   第2段階: 精密シミュレーション（上位{PRECISE_CHECK_COUNT}通り）")
        
        ticker_results = []
        
        # Ver 13.5: 各銘柄にライバルデータを付与
        for item in elite_stocks:
            ticker = item['t']
            rival_tickers = rivals_map.get(ticker, [])
            rival_dfs_for_ticker = {r: all_rival_dfs[r] for r in rival_tickers if r in all_rival_dfs}
            item['rival_dfs'] = rival_dfs_for_ticker if rival_dfs_for_ticker else None
        
        # 並列処理で最適化実行
        with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
            futures = {
                executor.submit(worker_analyze_ticker, item): item 
                for item in elite_stocks
            }
            
            completed = 0
            for future in as_completed(futures):
                completed += 1
                item = futures[future]
                ticker = item['t']
                
                try:
                    result = future.result()
                    if result:
                        ticker_results.append(result)
                        profit = result['profit']
                        raw_l = result.get('raw_long_profit', 0)
                        raw_s = result.get('raw_short_profit', 0)
                        print(f"   ✅ [{completed}/{len(elite_stocks)}] {ticker} 完了 (期待利益: {profit:.2f}%, raw: L={raw_l:.2f}% S={raw_s:.2f}%)")
                    else:
                        print(f"   ❌ [{completed}/{len(elite_stocks)}] {ticker} 失敗")
                
                except Exception as e:
                    logger.error(f"{ticker} 並列処理エラー: {str(e)}")
                    print(f"   ❌ [{completed}/{len(elite_stocks)}] {ticker} エラー: {str(e)}")
        
        # 4. Ver 13.5: 強力再試行ループ
        if not ticker_results:
            logger.error("解析結果なし")
            print("\n❌ エラー: 有効な解析結果がありませんでした")
            return
        
        print("\n" + "=" * 60)
        print("🔄 STEP 3.5: Ver 13.5 強力再試行ループ")
        print("=" * 60)
        
        ticker_results = retry_optimization_loop(ticker_results, elite_stocks, rivals_map, all_rival_dfs)
        
        # 5. 最終銘柄選定
        print("\n" + "=" * 60)
        print("📋 STEP 4: 最終銘柄選定（期待利益上位）")
        print("=" * 60)
        
        # Ver 13.5: raw profitでソート（5%ガード前の生数値を使用）
        for r in ticker_results:
            r['sort_key'] = r.get('raw_long_profit', 0) + r.get('raw_short_profit', 0)
        
        final_top = sorted(
            ticker_results,
            key=lambda x: x['sort_key'],
            reverse=True
        )[:FINAL_MONITORING]
        
        print(f"\n✅ 全{len(ticker_results)}銘柄の最適化完了")
        print(f"   期待利益上位{len(final_top)}銘柄を最終選定（raw profitベースでソート）")
        
        # 6. 結果表示
        print("\n" + "=" * 60)
        print("📊 最終結果（raw利益順）")
        print("=" * 60)
        
        for idx, result in enumerate(final_top, 1):
            ticker = result['t']
            sector = result['sector']
            size_category = result.get('size_category', '')
            volatility = result.get('volatility_score', 0)
            rivals = rivals_map.get(ticker, [])
            long_disabled = result.get('long_disabled', False)
            short_disabled = result.get('short_disabled', False)
            raw_l = result.get('raw_long_profit', 0)
            raw_s = result.get('raw_short_profit', 0)
            
            # スコア閾値ガード処理（Ver 10.3）
            result['params']['long']['threshold'] = max(result['params']['long'].get('threshold', 0), MIN_SCORE_THRESHOLD)
            result['params']['short']['threshold'] = max(result['params']['short'].get('threshold', 0), MIN_SCORE_THRESHOLD)
            
            print(f"\n{idx}. {ticker} [{sector}] ({size_category})")
            print(f"   期待利益: {result['profit']:.2f}% (raw合計: {raw_l + raw_s:.2f}%)")
            print(f"     ├ ロング : {result['long_profit']:.2f}% (raw: {raw_l:.2f}%){' 🚫禁止' if long_disabled else ''} (閾値: {result['params']['long']['threshold']})")
            print(f"     └ ショート: {result['short_profit']:.2f}% (raw: {raw_s:.2f}%){' 🚫禁止' if short_disabled else ''} (閾値: {result['params']['short']['threshold']})")
            print(f"   ボラティリティ: {volatility:.2f}%")
            print(f"   ライバル5銘柄: {', '.join(rivals)}")
        
        # 設定ファイル出力（ライバル情報・規模区分付き）
        best_config = {
            'timestamp': datetime.now().isoformat(),
            'version': '13.5',
            'profit': final_top[0]['profit'],
            'params': final_top[0]['params'],
            'top_5': [x['t'] for x in final_top],
            'details': [
                {
                    't': result['t'],
                    'profit': result['profit'],
                    'long_profit': result['long_profit'],
                    'short_profit': result['short_profit'],
                    'raw_long_profit': result.get('raw_long_profit', 0),
                    'raw_short_profit': result.get('raw_short_profit', 0),
                    'long_disabled': result.get('long_disabled', False),
                    'short_disabled': result.get('short_disabled', False),
                    'sector': result['sector'],
                    'size_category': result.get('size_category', ''),
                    'rivals': rivals_map.get(result['t'], []),
                    'params': result['params']
                }
                for result in final_top
            ]
        }
        
        # json.dump時にカスタムエンコーダーを指定し、numpy型を自動変換するように修正
        with open(OUTPUT_CONFIG, 'w', encoding='utf-8') as f:
            json.dump(best_config, f, indent=2, ensure_ascii=False, cls=NpEncoder)
        
        print(f"\n✅ 設定ファイル保存完了: {OUTPUT_CONFIG}")
        logger.info(f"設定ファイル保存: {OUTPUT_CONFIG}")
        
        # 実行時間
        elapsed = time.time() - start_time
        print(f"\n⏱️  総実行時間: {elapsed:.1f}秒 ({elapsed/60:.1f}分)")
        
        # 完了通知をDiscordに送信
        completion_message = f"""✅ **Version 13.5 戦略構築完了！**

⏱️ 実行時間: {elapsed:.1f}秒 ({elapsed/60:.1f}分)
📅 完了時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} JST
🚀 並列処理: {os.cpu_count()}コア活用

🎯 **選定された監視銘柄 TOP{len(final_top)}（raw利益順）:**
"""
        for idx, result in enumerate(final_top, 1):
            ticker_name = result['t']
            total_profit = result['profit']
            long_profit = result['long_profit']
            short_profit = result['short_profit']
            raw_l = result.get('raw_long_profit', 0)
            raw_s = result.get('raw_short_profit', 0)
            long_disabled = result.get('long_disabled', False)
            short_disabled = result.get('short_disabled', False)
            sector = result['sector']
            size_category = result.get('size_category', '')
            volatility = result.get('volatility_score', 0)
            rivals = rivals_map.get(ticker_name, [])
            rivals_str = ', '.join(rivals) if rivals else 'なし'
            
            long_mark = ' 🚫' if long_disabled else ''
            short_mark = ' 🚫' if short_disabled else ''
            
            completion_message += f"""
{idx}. **{ticker_name}** [{sector}] ({size_category})
   期待利益: {total_profit:.2f}% (raw: {raw_l + raw_s:.2f}%)
   L: {long_profit:.1f}%{long_mark} (raw:{raw_l:.1f}%) / S: {short_profit:.1f}%{short_mark} (raw:{raw_s:.1f}%)
   ボラティリティ: {volatility:.2f}%
   ライバル5銘柄: {rivals_str}"""
        
        completion_message += f"""

📊 **最優秀銘柄: {final_top[0]['t']}**
   期待利益: {final_top[0]['profit']:.2f}%
   ボラティリティ: {final_top[0].get('volatility_score', 0):.2f}%

🆕 **Version 13.5 改良点:**
   • セクター・アライメント (+15点ブースト)
   • 出来高加速 (+10点ブースト)
   • ダイバージェンス・ボーナス (+20点ブースト)
   • TP1後トレーリングTP (ATR×1.0)
   • Raw Profit保持 & 強力再試行ループ
   • ライバル5分足データによるバックテスト検証

🔔 次のステップ:
   `python monitor.py` で監視を開始してください！
"""
        
        send_discord_notification(WEBHOOK_URL, completion_message)
        
        logger.info("処理正常終了")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  ユーザーによる中断")
        logger.warning("ユーザーによる中断")
        
    except Exception as e:
        logger.error(f"処理エラー: {str(e)}", exc_info=True)
        print(f"\n❌ エラー: {str(e)}")
        send_discord_notification(WEBHOOK_URL, f"❌ 戦略構築エラー: {str(e)}")


if __name__ == "__main__":
    main()