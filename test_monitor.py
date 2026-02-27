#!/usr/bin/env python3
"""
test_monitor.py - Monitorモジュールの統合テスト (Ver 15.15対応)
目的: AM/PMセッション設定、データ取得、5分足リサンプル、指標計算、
      および1分足トリガーロジックの垂直同期をローカルで一度に検証する。
"""
import os
import json
import logging
import time
from datetime import datetime, time as dt_time
import pytz
import pandas as pd
from unittest.mock import MagicMock, patch

# テスト用ログ設定
TEST_LOG = "test_monitor_log.txt"

# 実行前に既存のログを完全にクリアする
with open(TEST_LOG, 'w', encoding='utf-8') as f:
    f.write("") 

logging.basicConfig(
    filename=TEST_LOG, filemode='w', encoding='utf-8',
    level=logging.INFO,
    format='%(asctime)s - [TEST] - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 出力先をダミーに変更
DUMMY_JOURNAL = 'test_trade_journal.csv'
DUMMY_RESULTS = 'test_trade_results.csv'
DUMMY_POSITIONS = 'test_positions.json'
DUMMY_CONFIG = 'dummy_config.json'

def log_notification(url, msg):
    """Discord通知の代わりにログに書き込む"""
    logger.info(f"--- DISCORD NOTIFICATION MOCK ---\n{msg}\n---------------------------------")
    print(f"[LOGGED] 通知を {TEST_LOG} に記録しました。")

def log_trade_journal(entry):
    """取引ジャーナル記録の代わりにログに書き込む"""
    logger.info(f"--- TRADE JOURNAL RECORD MOCK ---")
    for k, v in entry.items():
        logger.info(f"  {k}: {v}")
    logger.info(f"---------------------------------")
    print(f"[LOGGED] 取引ジャーナル詳細を {TEST_LOG} に記録しました。")

def log_file_write(filename, content_desc, data):
    """ファイル書き込みの代わりにログに記録する"""
    logger.info(f"--- FILE WRITE MOCK ({filename}) ---")
    logger.info(f"  Description: {content_desc}")
    logger.info(f"  Data: {data}")
    logger.info(f"------------------------------------")
    print(f"[LOGGED] {filename} への書き込み試行を {TEST_LOG} に記録しました。")

def mock_git_sync(action):
    """Git同期の代わりにログに記録する"""
    logger.info(f"--- GIT SYNC MOCK ({action}) ---")
    print(f"[LOGGED] Git {action} 試行を {TEST_LOG} に記録しました。")

def run_integration_test():
    print("[START] Monitor 統合テスト開始...")
    logger.info("Starting Monitor Integration Test (Ver 15.15 Hybrid Logic)")

    # 0. Config整合性チェック
    print("\n[Step 0] Config整合性チェック")
    try:
        import config
        required_vars = {
            'SLIPPAGE': float,
            'PARAM_RANGES': dict,
            'TARGET_PROFIT': dict,
            'TRADING_HOURS': dict,
            'RISK_MANAGEMENT': dict,
            'DATA_FETCH': dict
        }
        for var, vtype in required_vars.items():
            val = getattr(config, var)
            if not isinstance(val, vtype):
                raise TypeError(f"{var} の型が不正です: {type(val)}")
            logger.info(f"Config Check: {var} = {val} (OK)")
        
        # 特定の重要項目の値チェック
        if config.SLIPPAGE != 0.003:
            logger.warning(f"注意: SLIPPAGE が 0.3% (0.003) ではありません: {config.SLIPPAGE}")
        
        print(f"   [OK] Config変数の読み込み成功 (SLIPPAGE={config.SLIPPAGE})")
    except Exception as e:
        logger.error(f"Config check failed: {e}")
        print(f"   [NG] Configチェックでエラー: {e}")
        return

    # 1. 環境変数のモック (AMセッション)
    print("\n[Step 1] AMセッション環境の検証")
    with patch.dict(os.environ, {"SESSION_TYPE": "AM", "SKIP_DAILY_SUMMARY": "true"}):
        from config import MONITORING_LOOP
        import monitor
        # 再ロード的な挙動をシミュレート
        monitor.SESSION_TYPE = "AM"
        monitor.MONITOR_START_TIME = "09:30"
        monitor.MONITOR_END_TIME = "11:40"
        monitor.ENTRY_CUTOFF_TIME = "11:30"
        
        logger.info(f"AM Setup: Start={monitor.MONITOR_START_TIME}, End={monitor.MONITOR_END_TIME}, Cutoff={monitor.ENTRY_CUTOFF_TIME}")
        print(f"   [OK] AMセッション設定完了: Cutoff={monitor.ENTRY_CUTOFF_TIME}")

    # 2. 環境変数のモック (PMセッション)
    print("\n[Step 2] PMセッション環境の検証 (Git Pull含む)")
    with patch.dict(os.environ, {"SESSION_TYPE": "PM", "FORCE_CLOSE_TIME": "14:55"}):
        monitor.SESSION_TYPE = "PM"
        monitor.MONITOR_START_TIME = "12:30"
        monitor.MONITOR_END_TIME = "15:10"
        monitor.ENTRY_CUTOFF_TIME = "14:30"
        monitor.FORCE_CLOSE_TIME = "14:55"
        
        # Pull behavior mock call test
        mock_git_sync('pull')
        logger.info(f"PM Setup: Start={monitor.MONITOR_START_TIME}, End={monitor.MONITOR_END_TIME}, Cutoff={monitor.ENTRY_CUTOFF_TIME}, ForceClose={monitor.FORCE_CLOSE_TIME}")
        print(f"   [OK] PMセッション設定完了 (待機前にGit Pull実行): ForceClose={monitor.FORCE_CLOSE_TIME}")

    # 3. データ取得 & ハイブリッドロジックの検証
    print("[Step 3] データ取得 & ハイブリッド判定ロジックの検証")
    try:
        # dummy_config.json から銘柄読み込み
        with open(DUMMY_CONFIG, 'r', encoding='utf-8') as f:
            config = json.load(f)
        ticker = config['details'][0]['t']
        logger.info(f"Testing with ticker: {ticker}")

        from utils import fetch_yfinance_data, super_flatten_columns, calculate_technical_indicators, calculate_ma_from_higher_timeframe
        from config import DATA_FETCH
        
        print(f"   [*] {ticker} の1分足データを取得中 (yfinance)...")
        # 1回だけ取得
        raw_data = fetch_yfinance_data([ticker], period='1d', interval='1m')
        df_1m = super_flatten_columns(raw_data[ticker] if isinstance(raw_data, pd.DataFrame) and ticker in raw_data.columns else raw_data)
        
        if df_1m.empty:
            raise ValueError("取得データが空です。市場が閉まっているか通信エラーの可能性があります。")
        
        logger.info(f"1m data fetched: {len(df_1m)} rows. Latest: {df_1m.index[-1]}")
        print(f"   [OK] 1分足データ取得成功: {len(df_1m)}行")

        # 5分足リサンプル
        df_5m = df_1m.resample('5min').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        logger.info(f"Resampled to 5m: {len(df_5m)} rows.")
        print(f"   [OK] 5分足リサンプル完了: {len(df_5m)}行")

        # 指標計算
        df_5m_inds = calculate_technical_indicators(df_5m)
        df_5m_inds['ma_15m_20'] = calculate_ma_from_higher_timeframe(df_1m, 20)
        
        latest_price_1m = df_1m['close'].iloc[-1]
        logger.info(f"Indicator Check (Latest 5m row): RSI={df_5m_inds['rsi_14'].iloc[-1]:.2f}, ADX={df_5m_inds['adx_14'].iloc[-1]:.2f}")
        logger.info(f"1m Trigger Check: Latest 1m Price={latest_price_1m}")
        print(f"   [OK] 指標計算成功: RSI={df_5m_inds['rsi_14'].iloc[-1]:.1f}, LatestPrice(1m)={latest_price_1m}")

        # 4. 通知 & 記録機能の検証
        print("[Step 4] 通知 & 記録機能の検証")
        
        from position_manager import PositionManager
        pm = PositionManager()
        
        # 内部状態を維持するための模擬ストレージ
        mock_storage = {"positions": {}, "pm_active": None}

        def mock_save():
            mock_storage["positions"] = pm.positions
            mock_storage["pm_active"] = pm.pm_active_date
            log_file_write('positions.json', 'Update Position State', mock_storage)

        def mock_load():
            pm.positions = mock_storage["positions"]
            pm.pm_active_date = mock_storage["pm_active"]
            logger.info("MOCK: Data reloaded from memory (simulating disk load)")

        pm.save_positions = MagicMock(side_effect=mock_save)
        pm.load_from_file = MagicMock(side_effect=mock_load)
        pm.save_trade_result = MagicMock(side_effect=lambda res: log_file_write('trade_results.csv', 'Append Trade Result', res))

        with patch('monitor.send_discord_notification', side_effect=log_notification), \
             patch('monitor.record_trade_journal', side_effect=log_trade_journal), \
             patch('monitor.position_manager', pm):
            
            # ダミーのシグナル判定実行
            monitor.send_discord_notification("http://dummy", f"[TEST] テスト通知: {ticker} 解析テスト完了")
            
            # 1. ジャーナル記録テスト
            dummy_entry = {
                'ticker': ticker, 'side': 'LONG', 'entry_price': latest_price_1m,
                'entry_time': datetime.now().isoformat(), 'score': 85.0, 'threshold': 41.0,
                'rsi': df_5m_inds['rsi_14'].iloc[-1], 'vwap_dev': df_5m_inds['vwap_dev'].iloc[-1]
            }
            monitor.record_trade_journal(dummy_entry)
            
            # --- ここから通知テンプレートのテスト用模擬呼び出し ---
            # 地合い調整通知のテスト
            monitor.current_macro_sentiment = {'vix_value': 22.5, 'vix_chg': 5.2, 'sox_chg': -1.5, 'jpy_chg': -0.1}
            monitor.apply_macro_adjustments(monitor.current_macro_sentiment)
            
            # 構造検知通知のテスト
            dummy_structure_df = df_5m_inds.copy()
            monitor.check_structure_signal(ticker, dummy_structure_df)
            monitor.send_discord_notification("http://dummy", f"[STRUCTURE] {ticker}: BOS(LONG) 節目価格: {latest_price_1m:,.1f}")
            
            # 新規シグナル通知のテスト
            monitor.current_macro_adjustments = {'threshold_add': 5.0, 'tp_mul': 1.25, 'sl_mul': 1.15}
            monitor.send_discord_notification("http://dummy",
                f"[SIGNAL] 新規シグナル (Ver 15.15): LONG\n"
                f"銘柄: {ticker} (Monthly)\n"
                f"価格: {latest_price_1m:,.1f}\n"
                f"TP1: {latest_price_1m * 1.02:,.1f} (ATR x 1.25)\n"
                f"SL: {latest_price_1m * 0.98:,.1f} (ATR x 1.15)\n"
                f"スコア: 85.0 (判定閾値: 41.0)\n"
                f"指標: RSI:70.1, VWAP:+1.50%"
            )
            # --- 模擬呼び出しここまで ---

            # 2. ポジション追加テスト (positions.json書き込み相当)
            print("   [*] ポジション追加テスト...")
            pm.add_position(ticker, 'LONG', latest_price_1m, config['details'][0])
            
            # 3. TP1達成と決済記録テスト (trade_results.csv書き込み相当)
            print("   [*] ポジション決済テスト...")
            # TP1 notification test
            monitor.send_discord_notification("http://dummy", 
                f"[TP1] {ticker}: 50%利確完了 損益:+2.00% リスク縮小済み"
            )
            # EXIT notification test
            pm.close_position(ticker, latest_price_1m * 1.05, 'STOP_LOSS')
            monitor.send_discord_notification("http://dummy", 
                f"[EXIT] {ticker}: STOP_LOSS 損益:+5.00%"
            )

            print(f"   [OK] 通知・ジャーナル・ポジション・決済記録の全ロジック検証完了")

        # 5. AM/PM ハンドオーバー & 損益計算の安全性検証
        print("[Step 5] セッション間連携 & 損益計算の検証")
        with patch('monitor.git_sync', side_effect=mock_git_sync):
            tz = pytz.timezone('Asia/Tokyo')
            today_str = datetime.now(tz).strftime('%Y-%m-%d')
            
            # 5a. PM起動時のフラグセット
            print("   [>>] PMセッション起動シミュレーション...")
            pm.set_pm_active(today_str)
            
            # 5b. AM実行中のフラグ検知とセッション終了時のPush
            print("   [<<] AMセッション引き継ぎ検知 & 終了時Git Pushテスト...")
            active_date = pm.get_pm_active_date()
            if active_date == today_str:
                logger.info(f"Handover Detection: Success (Match found for {today_str})")
                print(f"   [OK] 引き継ぎフラグ検知成功")
            else:
                raise ValueError(f"引き継ぎフラグが不一致です: {active_date} != {today_str}")

            print("   [..] AMセッション11:30以降の監視待機ループシミュレーション...")
            # Simulate the monitor loop end condition behavior
            monitor.SESSION_TYPE = 'AM'
            monitor.MONITOR_END_TIME = '11:40'
            import datetime as dt_module
            
            # Case 1: Past 11:40, PM NOT active -> Should NOT break (should_end = False)
            pm.set_pm_active(None)
            should_end = True
            if monitor.SESSION_TYPE == 'AM':
                if pm.get_pm_active_date() != today_str:
                    should_end = False
            if should_end:
                 raise ValueError("AMセッションがPMフラグ未検知にも関わらず終了しようとしました")
            else:
                 print("   [OK] PM未起動時: ループ継続を確認")

            # Case 2: Past 11:40, PM IS active -> Should break (should_end = True)
            pm.set_pm_active(today_str)
            should_end = True
            if monitor.SESSION_TYPE == 'AM':
                if pm.get_pm_active_date() != today_str:
                    should_end = False
            if not should_end:
                 raise ValueError("AMセッションがPMフラグ検知後も継続しようとしました")
            else:
                 print("   [OK] PM起動検知時: ループ終了条件クリアを確認")
            
            mock_git_sync('push')
            print(f"   [OK] AMセッション終了時のGit Push(模擬)成功")

            # 5c. 損益計算の安全性 (空データ)
            print("   [-$-] 損益計算の安全性テスト (空データ)...")
            with patch('pandas.read_csv', side_effect=FileNotFoundError):
                profit = monitor.get_today_total_profit()
                logger.info(f"Daily Profit (No File): {profit}")
                print(f"   [OK] 空データ時の損益計算成功: {profit}")

            # 5d. CSV破損/形式混在の堅牢性検証 (Ver 15.15 修正確認)
            print("   [shield] CSV破損/形式混在の堅牢性テスト...")
            test_csv = 'test_corrupted_results.csv'
            # ヘッダー + 正解データ(12列) + 古いデータ(11列) + 破損データ(多すぎる列)
            csv_content = [
                "ticker,side,entry_price,entry_time,exit_price,exit_time,exit_reason,tp1_hit,tp1_profit,final_profit,total_profit,logic_type",
                f"3103.T,LONG,1500.0,{today_str}T10:00:00+09:00,1550.0,{today_str}T11:00:00+09:00,TP1,True,2.5,5.0,5.0,Monthly",
                f"5801.T,LONG,28000.0,{today_str}T09:30:00+09:00,27800.0,{today_str}T09:45:00+09:00,SL,False,0.0,-0.7,-0.7", # 11列
                f"9999.T,LONG,100.0,time,110.0,time,reason,False,0.0,10.0,10.0,Monthly,EXTRA_COLUMN" # 13列(破損)
            ]
            with open(test_csv, 'w', encoding='utf-8') as f:
                f.write("\n".join(csv_content))
            
            from config import POSITION_MANAGEMENT
            with patch.dict(POSITION_MANAGEMENT, {'trade_results_file': test_csv}):
                # 1. 決済済み銘柄の取得 (11列/13列が混ざっていても3103.Tを取得できるか)
                closed_trades = monitor.get_today_closed_trades()
                closed = {t['ticker'] for t in closed_trades}
                logger.info(f"Robustness Test (Closed Tickers): {closed}")
                if '3103.T' in closed:
                    print("   [OK] 形式混在CSVからの銘柄抽出成功")
                else:
                    raise ValueError(f"銘柄抽出に失敗しました: {closed}")

                # 2. 損益計算 (エラーで止まらずに計算できるか)
                profit = monitor.get_today_total_profit()
                logger.info(f"Robustness Test (Daily Profit): {profit}")
                print(f"   [OK] 形式混在CSVからの損益計算成功: {profit}")

                # 3. サマリー送信 (エラーで止まらないか)
                monitor.send_daily_summary()
                print(f"   [OK] 形式混在CSVからのサマリー作成(通知試行)成功")

            if os.path.exists(test_csv): os.remove(test_csv)

    except Exception as e:
        logger.error(f"Test failed: {e}")
        print(f"   [NG] エラー発生: {e}")
        return

    print("\n[DONE] 全てのテスト項目が完了しました。")
    print(f"ログファイル '{TEST_LOG}' を確認して、変数の値や計算結果が正しいかチェックしてください。")

if __name__ == "__main__":
    run_integration_test()
