# システム設計書：deitorepubric (Ver 15.15)

## 1. システムアーキテクチャ
本システムは、解析（Analyzer）と監視（Monitor）の2フェーズで構成され、GitHub Actions上での24時間365日（稼働は市場日のみ）の自律運用を実現する。

### 1.1 コンポーネント構成
* **Analyzer**: `analyzer.py`, `backtest_engine.py` (Monthly/Weekly 独立解析)
* **Monitor**: `monitor.py`, `position_manager.py` (実機執行)
* **Session Handler**: `positions.json` を介した AM/PM 連携。

### 1.2 AM/PM ハンドオーバー設計
前場(AM)と後場(PM)の円滑な移行のため、以下のメカニズムを採用する：
1. **PM起動シグナル**: PMセッション起動時、`positions.json` の `pm_active` フラグを `true` に設定し、GitHubへプッシュ。
2. **AM自動終了**: 11:30以降、AMセッションは `pm_active` フラグを検知すると、後場へポジションを託して安全に終了する。
3. **状態同期**: ポジション情報は常に `positions.json` で一元管理され、セッションを跨いで引き継がれる。

## 2. データ設計
### 2.1 戦略設定 (`best_config.json`)
* 各銘柄に対し、`logic_type` を紐付けてパラメータを保持。

### 2.2 取引データの永続性 (厳格ガード)
システムの透明性と解析精度維持のため、以下のデータの出力・整合性を最優先事項とする：
1. **Trade Journal (`trade_journal.csv`)**: エントリー時の詳細（RSI, VWAP, RVOL, ADX, Score）を記録。
2. **Trade Results (`trade_results.csv`)**: エグジット結果をヘッダー順序（ticker, side, entry_price, entry_time, exit_price, exit_time, exit_reason, tp1_hit, tp1_profit, final_profit, total_profit, logic_type）に厳密に準拠して記録。
* **禁止事項**: リファクタリングや最適化の過程で、これらの記録ロジックを削除・バイパスすることを一切禁止する。

## 3. ログ出力設計 (サイズと価値の最適化)
* **1MB制限**: 1日あたりの `execution_log.txt` を 1MB 以内（推奨 500KB 以下）に保つ。
* **ノイズ遮断**: `yfinance`, `urllib3` 等の通信デバッグログを抑制（WARNING以上）。
* **ニアミス記録**: エントリー閾値に届かなかったが「閾値 - 10」以上のスコアを出した銘柄を、指標と共にログ出力する。

## 4. アルゴリズム・テクニカル指標の定義 (厳格遵守)
システムの精度維持のため、以下の計算式を唯一の正解とする：

1. **RSI (Relative Strength Index)**
   - $RS = \frac{EMA(Gain, 14)}{EMA(Loss, 14)}$
   - $RSI = 100 - \frac{100}{1 + RS}$
   - ※ $EMA$ は $Wilder's Smoothing$ (平滑化定数 $\alpha = 1/14$) を使用。

2. **ADX (Average Directional Index)**
   - $TR = \max(High - Low, |High - Close_{prev}|, |Low - Close_{prev}|)$
   - $+DM = \text{if } (High - High_{prev} > Low_{prev} - Low) \text{ and } (High - High_{prev} > 0) \text{ then } (High - High_{prev}) \text{ else } 0$
   - $-DM = \text{if } (Low_{prev} - Low > High - High_{prev}) \text{ and } (Low_{prev} - Low > 0) \text{ then } (Low_{prev} - Low) \text{ else } 0$
   - $DI = 100 \times \frac{EMA(DM, 14)}{EMA(TR, 14)}$
   - $DX = 100 \times \frac{|+DI - -DI|}{+DI + -DI}$
   - $ADX = EMA(DX, 14)$

3. **VWAP (Volume Weighted Average Price)**
   - $VWAP = \frac{\sum (TypicalPrice \times Volume)}{\sum Volume}$
   - ※ $\sum$ は当日のセッション開始時（09:00）からの累積とし、日跨ぎの合算は禁止。
   - $TypicalPrice = \frac{High + Low + Close}{3}$

4. **ATR (Average True Range)**
   - $ATR = EMA(TR, 14)$
   - ※ $EMA$ は $\alpha = 1/14$ を使用。

## 5. リスク管理設計
* **TP2トレーリングストップ**: TP1到達後、直近の高値（買い）/安値（売り）からATRの一定倍率（trailing_atr_multiplier）を引いた価格で逆指値を動的に更新する。
* **ボラティリティ比例型損切り**: ATRに基づく動的SL。
* **重複エントリー防止**: 当日既に損切り/利確が完了した銘柄への再エントリーをブロック。
* **デイリー・ストップロス**: 1日の損益合計 -3.0% で緊急停止。
