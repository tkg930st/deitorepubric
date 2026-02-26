# システム設計書：deitorepubric (Ver 15.15)

## 1. システムアーキテクチャ
本システムは、解析（Analyzer）と監視（Monitor）の2フェーズで構成され、GitHub Actions上での24時間365日（稼働は市場日のみ）の自律運用を実現する。

### 1.1 コンポーネント構成
* **Analyzer**: `analyzer.py`, `backtest_engine.py` (Monthly/Weekly 独立解析)
* **Monitor**: `monitor.py`, `position_manager.py` (実機執行)
    * **ハイブリッド判定**: 1分足でエントリートリガーを引きつつ、5分足リサンプルデータでテクニカル指標を計算する。
    * **高頻度監視**: 30秒間隔のメインループで動作し、リアルタイム性を確保する。
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
   - $-DM = \text{if } (Low_{prev} - Low > High - High_{prev} ) \text{ and } (Low_{prev} - Low > 0) \text{ then } (Low_{prev} - Low) \text{ else } 0$
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

5. **多段階スコアリングロジック (Multi-tiered Scoring)**
   - 指標の強さに応じた段階的な加点により、最適化の精度を向上させる。
   - **RSI**: 境界値（買い 30/45, 売り 70/55）に基づき 1.0倍〜1.5倍の重みを加算。
   - **VWAP乖離**: 乖離率（0.3% / 1.5%）に基づき段階的に加算。
   - **Rvol**: 出来高倍率（1.1 / 1.5 / 2.5）に基づき 0.5倍〜2.0倍の重みを加算。
   - **ADX**: トレンド強度（20 / 35）に基づき 1.0倍〜1.5倍の重みを加算。

## 5. リスク管理設計
* **戦略最適化エンジン (GA)**:
    - 探索範囲を大幅に拡張（重み 0-100, 閾値上限 200）し、ボーナス加点を考慮した広域探索を行う。
    - 1世代あたりの多様性を確保するため、フィルタ設定（RSI/VWAP）の有無も進化の対象に含める。
* **スリッページ適用**: 往復合計 **0.3%** を全シミュレーションおよび実機判定のコストとして算入。
* **TP2トレーリングストップ**: TP1到達後、直近の高値（買い）/安値（売り）からATRの一定倍率（trailing_atr_multiplier）を引いた価格で逆指値を動的に更新する。
* **ボラティリティ比例型損切り**: ATRに基づく動的SL。
* **重複エントリー防止**: 当日既に損切り/利確が完了した銘柄への再エントリーをブロック。
* **デイリー・ストップロス**: 1日の損益合計 -3.0% で緊急停止。

## 6. 今後の拡張予定 (Future Roadmap)
1. **マクロ・インデックス同期型バックテストの実装**
   - SOX指数、為替（JPY）、日経平均等の日次騰落データを `analyzer.py` のデータ取得時に同期。
   - `backtest_engine.py` 内で、これらのマクロデータに基づいたスコア加算・フィルタリングをシミュレーションに組み込み、実機（Monitor）との乖離をゼロにする。
2. **セクター・アライメントのバックテスト統合**
   - 銘柄単体の動きではなく、業種別指数のモメンタムをバックテストの評価軸に採用。

## 7. 品質保証とテスト体制
本システムの信頼性維持のため、以下のテスト体系を構築し、開発・保守の全工程で運用する。

### 7.1 統合テスト (`test_monitor.py`)
Monitorモジュールの全主要パスを仮想環境で検証する。主な検証項目は以下の通り：
1. **Config整合性**: `config.py` の定数定義と型の垂直同期チェック。
2. **セッション環境**: AM/PM別の時刻設定および動作フラグの動的切替検証。
3. **データ・計算精度**: `yfinance` 1分足取得、5分足リサンプル、およびテクニカル指標（RSI/ADX/VWAP/ATR）の計算ロジック検証。
4. **リアルタイムトリガー**: 最新1分足価格によるエントリー判定の正確性。
5. **永続化・通知**: ポジション保存、ジャーナル記録、決済結果出力（各ファイルへのmock書き込み）、およびDiscord通知のモック検証。
6. **セッション間連携**: PMセッション起動フラグの検知とAMセッションの安全な終了（ハンドオーバー）のシミュレーション。

### 7.2 解析ロジック同期テスト (`test_analyzer.py`)
`analyzer.py` の解析アルゴリズム修正時、`test_analyzer.py` を用いて小規模データセットでの即時検証を行い、本番環境へのデプロイ前にロジックの不整合を排除する。
