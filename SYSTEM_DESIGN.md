# システム設計書：deitorepubric (Ver 15.12)

## 1. システムアーキテクチャ
本システムは、解析（Analyzer）と監視（Monitor）の2フェーズで構成され、ローカル環境を起点に運用される。

### 1.1 コンポーネント構成
* **Analyzer**: `analyzer.py`, `backtest_engine.py` (Monthly/Weekly 独立解析)
* **Monitor**: `monitor.py`, `position_manager.py` (実機執行)
* **Test Tool**: `test_analyzer.py` (高速検証用)

## 2. データ設計
### 2.1 戦略設定 (`best_config.json`)
* 各銘柄に対し、`logic_type` (Monthly または Weekly) を紐付けてパラメータを保持。

## 3. アルゴリズム詳細設計
### 3.1 攻撃型トレンドフォロー最適化
1. **2段階進化型最適化**: 広域探索と局所進化を組み合わせたパラメータ特定。
2. **評価関数 (Fitness Score)**: リスク（SL幅）に対する収益効率を最大化。
3. **動的戦略構築**: 買い(LONG)と売り(SHORT)のパラメータを独立して構築し、期待利益未達のサイドは自動的に `disabled` 化。

## 4. リスク管理設計
* **ボラティリティ比例型損切り**: ATRに基づく動的SL。
* **デイリー・ストップロス**: 1日の損益合計 -3.0% で緊急停止。
