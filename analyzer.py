#!/usr/bin/env python3
"""
analyzer.py - Version 10.0 ハイブリッド版完全対応

主な機能:
1. セクター重複排除（1セクター1銘柄）
2. ライバル5銘柄の確定選定（段階的緩和）
3. ハイブリッド・バックテスト（2段階評価）
   - 第1段階: 高速スクリーニング（200通り）
   - 第2段階: 精密シミュレーション（上位20通り）
4. 15分足データ取得＆トレンドフィルター
5. 規模区分の取得・保存
6. ATRスクリーニングによる銘柄選定
7. 並列処理による高速化
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

from config import (
    LOG_FILE, LOG_LEVEL, LIQUIDITY_THRESHOLD, MIN_PRICE,
    TOP_CANDIDATES, FINAL_MONITORING, OPTIMIZATION_ITERATIONS,
    MIN_DATA_POINTS, PARAM_RANGES, DATA_FETCH, OUTPUT_CONFIG,
    WEBHOOK_URL, PRECISE_CHECK_COUNT, TREND_FILTER, ATR_CHECK_COUNT,
    MIN_SCORE_THRESHOLD
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
    
    Args:
        df: OHLCV データ
        ticker: 銘柄コード
    
    Returns:
        (売買代金平均, 総合スコア)
    """
    if df.empty or len(df) < 5:
        return 0.0, 0.0
    
    try:
        # 最新価格
        latest_price = df['close'].iloc[-1]
        
        # 平均売買代金
        avg_value = (df['close'] * df['volume']).mean()
        
        # ボラティリティスコア（値幅の平均％）
        volatility_score = (((df['high'] - df['low']) / df['close']) * 100).mean()
        
        # 総合スコア = 売買代金 × ボラティリティ
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
    """
    主力銘柄を選定（一括日足スクリーニングによる高速版）
    
    フロー:
    1. 日足データ（3ヶ月分）をチャンクごとに一括取得
    2. 流動性・TOBチェック・ATRボラティリティ計算を同時に実行
    3. ボラティリティ上位銘柄を抽出し、セクター重複を排除して最終選定
    
    Args:
        all_tickers: 全銘柄リスト
        sector_df: セクター情報を含むDataFrame
    
    Returns:
        選定された銘柄情報のリスト（セクター・規模区分付き）
    """
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
            # 日足データ一括取得（高速化のためスクリーニングには日足を使用）
            batch_data = fetch_yfinance_data(
                chunk,
                period='3mo', # ATR計算用に3ヶ月分取得
                interval='1d'
            )
            
            if batch_data.empty:
                continue
            
            for ticker in chunk:
                try:
                    # 個別データの抽出
                    if len(chunk) > 1:
                        if ticker not in batch_data.columns.get_level_values(0):
                            continue
                        ticker_raw = batch_data[ticker].copy()
                    else:
                        ticker_raw = batch_data.copy()
                        
                    # データ正規化（日足のため時間フィルタは不要）
                    df = super_flatten_columns(ticker_raw)
                    
                    if df.empty or len(df) < 20:
                        continue
                        
                    # 鮮度チェック
                    last_date = df.index[-1]
                    days_diff = (datetime.now(last_date.tzinfo) - last_date).days
                    if days_diff > 7:
                        continue
                    
                    # TOB/価格固定チェック
                    if len(df) >= 5:
                        if df['close'].tail(5).std() < 0.0001:
                            continue

                    # 指標計算（ATR）
                    df = calculate_technical_indicators(df)
                    if 'atr_14' not in df.columns or df['atr_14'].isna().all():
                        continue

                    # 最新価格と流動性チェック
                    latest_price = df['close'].iloc[-1]
                    avg_value, _ = calculate_liquidity_score(df, ticker)
                    
                    if latest_price <= MIN_PRICE or avg_value < LIQUIDITY_THRESHOLD:
                        continue
                    
                    # ボラティリティスコア（ATR/価格）計算
                    latest_atr = df['atr_14'].iloc[-1]
                    vol_score = (latest_atr / latest_price) * 100
                    
                    # セクター情報
                    sector_info = sector_df[sector_df['ticker'] == ticker]
                    sector_name = sector_info['sector'].iloc[0] if not sector_info.empty else '不明'
                    size_category = sector_info['size_category'].iloc[0] if not sector_info.empty and 'size_category' in sector_info.columns else ''
                    
                    candidates.append({
                        't': ticker,
                        'price': latest_price,
                        'value': avg_value,
                        'atr': latest_atr,
                        'volatility_score': vol_score,
                        'sector': sector_name,
                        'size_category': size_category
                    })
                except Exception:
                    continue
            
            # レート制限対策
            time.sleep(DATA_FETCH['request_delay'])
            
        except Exception as e:
            logger.warning(f"チャンク処理エラー: {str(e)}")
            continue
    
    # ボラティリティ（ATR/価格）順にソート
    sorted_candidates = sorted(candidates, key=lambda x: x['volatility_score'], reverse=True)
    
    # セクター重複排除：1セクターにつき1銘柄
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
    """
    各監視銘柄のライバル銘柄を5銘柄確定選定（段階的緩和ロジック）
    
    Args:
        target_stocks: 監視対象銘柄のリスト
        all_candidates: 全候補銘柄のリスト（セクター・売買代金情報付き）
        sector_df: 全銘柄のセクター・規模情報
        num_rivals: 選定するライバル数（デフォルト5）
    
    Returns:
        {銘柄コード: [ライバル1, ライバル2, ..., ライバル5], ...}
    """
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
        
        # 同じセクターの全銘柄を抽出（候補リスト外も含む）
        same_sector_all = sector_df[
            (sector_df['sector'] == target_sector) & 
            (sector_df['ticker'] != target_ticker)
        ].copy()
        
        if same_sector_all.empty:
            logger.warning(f"{target_ticker}: 同セクター銘柄が見つかりません")
            print(f"      ⚠️  同セクター銘柄なし → 市場全体から選定")
            
            # セクター内に銘柄がない場合、市場全体から流動性上位を選定
            market_wide = []
            for candidate in all_candidates:
                if candidate['t'] != target_ticker:
                    market_wide.append(candidate)
            
            market_wide_sorted = sorted(market_wide, key=lambda x: x['value'], reverse=True)
            rivals = [c['t'] for c in market_wide_sorted[:num_rivals]]
            
            if len(rivals) < num_rivals:
                # それでも不足する場合、sector_dfから補充
                remaining = num_rivals - len(rivals)
                other_tickers = sector_df[
                    (~sector_df['ticker'].isin(rivals)) & 
                    (sector_df['ticker'] != target_ticker)
                ]['ticker'].tolist()
                rivals.extend(other_tickers[:remaining])
        
        else:
            # STEP 1: 同セクター内で LIQUIDITY_THRESHOLD 以上の銘柄
            high_liquidity = []
            for candidate in all_candidates:
                if (candidate['t'] != target_ticker and 
                    candidate['sector'] == target_sector and 
                    candidate['value'] >= LIQUIDITY_THRESHOLD):
                    high_liquidity.append(candidate)
            
            high_liquidity_sorted = sorted(high_liquidity, key=lambda x: x['value'], reverse=True)
            rivals.extend([c['t'] for c in high_liquidity_sorted[:num_rivals]])
            
            logger.info(f"  STEP1(高流動性): {len(rivals)}銘柄選定")
            
            # STEP 2: 不足分を段階的に緩和して補充
            if len(rivals) < num_rivals:
                threshold_ratios = [0.5, 0.25, 0.0]  # 50%, 25%, 0%（全銘柄）
                
                for ratio in threshold_ratios:
                    if len(rivals) >= num_rivals:
                        break
                    
                    adjusted_threshold = LIQUIDITY_THRESHOLD * ratio
                    logger.info(f"  STEP2(緩和): 閾値={adjusted_threshold/1e9:.1f}億円で再検索")
                    
                    # MIN_PRICE制限を無視して検索
                    relaxed_candidates = []
                    for _, row in same_sector_all.iterrows():
                        ticker = row['ticker']
                        if ticker in rivals:
                            continue
                        
                        # 売買代金を取得（candidatesから）
                        candidate_info = next((c for c in all_candidates if c['t'] == ticker), None)
                        if candidate_info and candidate_info['value'] >= adjusted_threshold:
                            relaxed_candidates.append(candidate_info)
                    
                    relaxed_sorted = sorted(relaxed_candidates, key=lambda x: x['value'], reverse=True)
                    remaining = num_rivals - len(rivals)
                    rivals.extend([c['t'] for c in relaxed_sorted[:remaining]])
                    
                    logger.info(f"    追加: {min(len(relaxed_sorted), remaining)}銘柄")
            
            # STEP 3: それでも不足する場合、同セクター内の全銘柄から補充
            if len(rivals) < num_rivals:
                logger.info(f"  STEP3(全補充): 同セクター全銘柄から補充")
                
                all_same_sector_tickers = same_sector_all['ticker'].tolist()
                for ticker in all_same_sector_tickers:
                    if ticker not in rivals:
                        rivals.append(ticker)
                        if len(rivals) >= num_rivals:
                            break
            
            # STEP 4: まだ不足する場合、市場全体から補充
            if len(rivals) < num_rivals:
                logger.warning(f"  STEP4(市場補充): セクター内不足 → 市場全体から補充")
                print(f"      ⚠️  セクター内不足 → 市場全体から補充")
                
                market_candidates = []
                for candidate in all_candidates:
                    if candidate['t'] not in rivals and candidate['t'] != target_ticker:
                        market_candidates.append(candidate)
                
                market_sorted = sorted(market_candidates, key=lambda x: x['value'], reverse=True)
                remaining = num_rivals - len(rivals)
                rivals.extend([c['t'] for c in market_sorted[:remaining]])
        
        # 最終確認：必ず5銘柄確保
        if len(rivals) < num_rivals:
            logger.error(f"{target_ticker}: ライバル銘柄が{num_rivals}に満たない ({len(rivals)}銘柄)")
            
            # 最後の手段：sector_df全体から無作為に補充
            all_tickers = sector_df['ticker'].tolist()
            for ticker in all_tickers:
                if ticker not in rivals and ticker != target_ticker:
                    rivals.append(ticker)
                    if len(rivals) >= num_rivals:
                        break
        
        # 確定したライバルを5銘柄に制限
        rivals = rivals[:num_rivals]
        rivals_map[target_ticker] = rivals
        
        print(f"      ライバル({len(rivals)}銘柄): {', '.join(rivals)}")
        logger.info(f"{target_ticker} のライバル: {rivals}")
    
    logger.info("ライバル銘柄選定完了")
    return rivals_map


def worker_analyze_ticker(ticker_info: Dict) -> Dict:
    """
    並列処理用ワーカー関数
    
    Args:
        ticker_info: 銘柄情報辞書
    
    Returns:
        最適化結果辞書
    """
    ticker = ticker_info['t']
    sector = ticker_info['sector']
    size_category = ticker_info.get('size_category', '')
    
    result = analyze_ticker(ticker)
    
    if result:
        result['sector'] = sector
        result['size_category'] = size_category
        result['volatility_score'] = ticker_info.get('volatility_score', 0)
    
    return result


def analyze_ticker(ticker: str) -> Dict:
    """
    個別銘柄の最適化（Version 10.0ハイブリッド版）
    
    Args:
        ticker: 銘柄コード
    
    Returns:
        最適化結果
    """
    logger.info(f"{'=' * 60}")
    logger.info(f"銘柄解析開始: {ticker}")
    logger.info(f"{'=' * 60}")
    
    try:
        # データ取得
        print(f"      データ取得中...")
        ticker_data = fetch_yfinance_data(
            [ticker],
            period=DATA_FETCH['analyzer_period'],
            interval=DATA_FETCH['analyzer_interval']
        )
        
        if ticker_data.empty:
            logger.error(f"{ticker}: データ取得失敗")
            return None
        
        # データ正規化
        df = super_flatten_columns(ticker_data)
        df = filter_trading_hours(df)
        
        if df.empty or len(df) < MIN_DATA_POINTS:
            logger.error(f"{ticker}: データ不足 (取得: {len(df)}行)")
            return None
        
        # テクニカル指標計算
        print(f"      テクニカル指標計算中...")
        df = calculate_technical_indicators(df)
        
        # 15分足MA計算（Version 7.0/8.0）
        if TREND_FILTER['enabled']:
            print(f"      15分足MA計算中...")
            df['ma_15m_20'] = calculate_ma_from_higher_timeframe(df, TREND_FILTER['ma_period'])
        
        # 15分足データ用のDataFrame（精密バックテスト用）
        df_15m = df[['ma_15m_20']].copy() if TREND_FILTER['enabled'] else pd.DataFrame()
        
        logger.info(f"{ticker}: データ準備完了 ({len(df)}行)")
        
        # ロング最適化（ハイブリッド）
        print(f"      ロング戦略最適化中（ハイブリッド: {OPTIMIZATION_ITERATIONS}→{PRECISE_CHECK_COUNT}）...")
        result_long = optimize_parameters(
            df, 
            df_15m,
            'long', 
            OPTIMIZATION_ITERATIONS,
            PRECISE_CHECK_COUNT
        )
        
        # ショート最適化（ハイブリッド）
        print(f"      ショート戦略最適化中（ハイブリッド: {OPTIMIZATION_ITERATIONS}→{PRECISE_CHECK_COUNT}）...")
        result_short = optimize_parameters(
            df, 
            df_15m,
            'short', 
            OPTIMIZATION_ITERATIONS,
            PRECISE_CHECK_COUNT
        )
        
        # 結果統合
        long_profit = result_long.get('profit', 0)
        short_profit = result_short.get('profit', 0)
        
        # 5%未満の方向は0%に修正（Ver 11.0）
        long_disabled = False
        short_disabled = False
        
        if long_profit < 5.0:
            logger.info(f"{ticker}: LONG利益 {long_profit:.2f}% < 5% → 0%に修正（エントリー禁止）")
            long_profit = 0.0
            long_disabled = True
        
        if short_profit < 5.0:
            logger.info(f"{ticker}: SHORT利益 {short_profit:.2f}% < 5% → 0%に修正（エントリー禁止）")
            short_profit = 0.0
            short_disabled = True
        
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
            'long_disabled': long_disabled,    # Ver 11.0: LONG禁止フラグ
            'short_disabled': short_disabled,  # Ver 11.0: SHORT禁止フラグ
            'params': {
                'long': result_long.get('params', {}),
                'short': result_short.get('params', {})
            }
        }
        
    except Exception as e:
        logger.error(f"{ticker} 解析エラー: {str(e)}", exc_info=True)
        return None


def main():
    """メイン処理"""
    start_time = time.time()
    
    print("\n" + "=" * 60)
    print("📊 Version 10.0 ハイブリッド版戦略構築システム")
    print("=" * 60)
    
    logger.info("=" * 60)
    logger.info("Version 10.0 ハイブリッド版戦略構築開始")
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
        
        # 3. 各銘柄の最適化（ハイブリッド・バックテスト + 並列処理）
        print("\n🔬 STEP 3: 戦略最適化中（並列処理 + ハイブリッド・バックテスト）...")
        print(f"   対象: {len(elite_stocks)}銘柄")
        print(f"   並列実行: {os.cpu_count()}コア使用")
        print(f"   第1段階: 高速スクリーニング（{OPTIMIZATION_ITERATIONS}通り）")
        print(f"   第2段階: 精密シミュレーション（上位{PRECISE_CHECK_COUNT}通り）")
        
        ticker_results = []
        
        # 並列処理で最適化実行
        with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
            # 全銘柄を並列投入
            futures = {
                executor.submit(worker_analyze_ticker, item): item 
                for item in elite_stocks
            }
            
            # 完了した順に結果を収集
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
                        print(f"   ✅ [{completed}/{len(elite_stocks)}] {ticker} 完了 (期待利益: {profit:.2f}%)")
                    else:
                        print(f"   ❌ [{completed}/{len(elite_stocks)}] {ticker} 失敗")
                
                except Exception as e:
                    logger.error(f"{ticker} 並列処理エラー: {str(e)}")
                    print(f"   ❌ [{completed}/{len(elite_stocks)}] {ticker} エラー: {str(e)}")
        
        # 4. 最終銘柄選定
        if not ticker_results:
            logger.error("解析結果なし")
            print("\n❌ エラー: 有効な解析結果がありませんでした")
            return
        
        print("\n" + "=" * 60)
        print("📋 STEP 4: 最終銘柄選定（期待利益上位5銘柄）")
        print("=" * 60)
        
        # 利益順にソート
        final_top = sorted(
            ticker_results,
            key=lambda x: x['profit'],
            reverse=True
        )[:FINAL_MONITORING]
        
        print(f"\n✅ 全{len(ticker_results)}銘柄の最適化完了")
        print(f"   期待利益上位{len(final_top)}銘柄を最終選定")
        
        # 5. ライバル銘柄選定（5銘柄確定）
        print("\n" + "=" * 60)
        print("📋 STEP 5: ライバル銘柄選定（確定5銘柄）")
        print("=" * 60)
        
        rivals_map = select_rival_stocks(final_top, elite_stocks, sector_df, num_rivals=5)
        
        # 6. 結果表示
        print("\n" + "=" * 60)
        print("📊 最終結果（期待利益順）")
        print("=" * 60)
        
        for idx, result in enumerate(final_top, 1):
            ticker = result['t']
            sector = result['sector']
            size_category = result.get('size_category', '')
            volatility = result.get('volatility_score', 0)
            rivals = rivals_map.get(ticker, [])
            long_disabled = result.get('long_disabled', False)
            short_disabled = result.get('short_disabled', False)
            
            # スコア閾値ガード処理（Ver 10.3）
            result['params']['long']['threshold'] = max(result['params']['long'].get('threshold', 0), MIN_SCORE_THRESHOLD)
            result['params']['short']['threshold'] = max(result['params']['short'].get('threshold', 0), MIN_SCORE_THRESHOLD)
            
            print(f"\n{idx}. {ticker} [{sector}] ({size_category})")
            print(f"   期待利益: {result['profit']:.2f}%")
            print(f"     ├ ロング : {result['long_profit']:.2f}%{' 🚫禁止' if long_disabled else ''} (閾値: {result['params']['long']['threshold']})")
            print(f"     └ ショート: {result['short_profit']:.2f}%{' 🚫禁止' if short_disabled else ''} (閾値: {result['params']['short']['threshold']})")
            print(f"   ボラティリティ: {volatility:.2f}%")
            print(f"   ライバル5銘柄: {', '.join(rivals)}")
        
        # 設定ファイル出力（ライバル情報・規模区分付き）
        best_config = {
            'timestamp': datetime.now().isoformat(),
            'profit': final_top[0]['profit'],
            'params': final_top[0]['params'],
            'top_5': [x['t'] for x in final_top],
            'details': [
                {
                    't': result['t'],
                    'profit': result['profit'],
                    'long_profit': result['long_profit'],
                    'short_profit': result['short_profit'],
                    'long_disabled': result.get('long_disabled', False),   # Ver 11.0
                    'short_disabled': result.get('short_disabled', False),  # Ver 11.0
                    'sector': result['sector'],
                    'size_category': result.get('size_category', ''),
                    'rivals': rivals_map.get(result['t'], []),
                    'params': result['params']
                }
                for result in final_top
            ]
        }
        
        with open(OUTPUT_CONFIG, 'w', encoding='utf-8') as f:
            json.dump(best_config, f, indent=2, ensure_ascii=False)
        
        print(f"\n✅ 設定ファイル保存完了: {OUTPUT_CONFIG}")
        logger.info(f"設定ファイル保存: {OUTPUT_CONFIG}")
        
        # 実行時間
        elapsed = time.time() - start_time
        print(f"\n⏱️  総実行時間: {elapsed:.1f}秒 ({elapsed/60:.1f}分)")
        
        # 完了通知をDiscordに送信
        completion_message = f"""✅ **Version 10.0 戦略構築完了！**

⏱️ 実行時間: {elapsed:.1f}秒 ({elapsed/60:.1f}分)
📅 完了時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} JST
🚀 並列処理: {os.cpu_count()}コア活用

🎯 **選定された監視銘柄 TOP{len(final_top)}（期待利益順）:**
"""
        # 各銘柄の詳細を追加
        for idx, result in enumerate(final_top, 1):
            ticker_name = result['t']
            total_profit = result['profit']
            long_profit = result['long_profit']
            short_profit = result['short_profit']
            long_disabled = result.get('long_disabled', False)
            short_disabled = result.get('short_disabled', False)
            sector = result['sector']
            size_category = result.get('size_category', '')
            volatility = result.get('volatility_score', 0)
            rivals = rivals_map.get(ticker_name, [])
            rivals_str = ', '.join(rivals) if rivals else 'なし'
            
            # 禁止マーク
            long_mark = ' 🚫' if long_disabled else ''
            short_mark = ' 🚫' if short_disabled else ''
            
            completion_message += f"""
{idx}. **{ticker_name}** [{sector}] ({size_category})
   期待利益: {total_profit:.2f}% (L: {long_profit:.1f}%{long_mark} / S: {short_profit:.1f}%{short_mark})
   ボラティリティ: {volatility:.2f}%
   ライバル5銘柄: {rivals_str}"""
        
        completion_message += f"""

📊 **最優秀銘柄: {final_top[0]['t']}**
   期待利益: {final_top[0]['profit']:.2f}%
   ボラティリティ: {final_top[0].get('volatility_score', 0):.2f}%

🆕 **Version 10.0 改良点:**
   • 日足スクリーニングによるデータ取得の高速化
   • 並列処理による高速化（{os.cpu_count()}コア活用）
   • ハイブリッド・バックテスト（実運用99%一致）
   • 期待利益順の最終選定

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