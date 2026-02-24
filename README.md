# deitorepubric (Ver 15.15)

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![AI Powered](https://img.shields.io/badge/AI-Gemini%20CLI-orange.svg)](https://github.com/google/gemini-cli)

日本株市場を対象とした、マルチ銘柄対応・自律型デイトレード・シグナルエンジン。
Gemini CLI（AIエージェント）を開発パートナーとし、バックテストによる最適化とリアルタイム監視を統合したシステムです。

## 🚀 システム概要

本システムは、以下の2つの主要フェーズで構成されています。

1. **Analyzerフェーズ (`analyzer.py`)**: 
   市場全体から流動性とボラティリティの高い銘柄をスクリーニングし、並列処理を用いたハイブリッド・バックテストによって、各銘柄に最適なトレードパラメータを自動算出します。
2. **Monitorフェーズ (`monitor.py`)**: 
   算出されたパラメータに基づき、リアルタイムで市場を監視。RSI、VWAP、出来高急増、セクター相関などを組み合わせた高度なフィルターを通過した際に、Discordを通じて売買シグナルを配信します。

## ✨ 主な機能

- **AM/PM セッション連携 (New)**: GitHub Actions上での前場(AM)と後場(PM)の連携を自動化。PM起動時にAMが安全に終了するハンドオーバー機能を備え、Git競合を完全に回避します。
- **データ永続性ガード (New)**: 取引詳細（`trade_journal.csv`）および決済結果（`trade_results.csv`）の自動保存。いかなるリファクタリング時もこれらの記録ロジックを維持することを最重要要件としています。
- **インテリジェント・ロギング (New)**: 1日あたりのログサイズを1MB未満に抑制しつつ、エントリー閾値に迫った「惜しいシグナル」を主要指標と共に記録。後からの戦略分析を容易にします。
- **高度なテクニカルロジック**: 
  - **セクター・アライメント**: ライバル銘柄との相関に基づくスコアリング。
  - **ダイナミックATR調整**: 市場のボラティリティに応じて利確・損切幅を動的に伸縮。
- **厳格なリスク管理**:
  - **デイリー・ストップロス**: 1日の通算損益が -3.0% に達した際の緊急停止機能。
  - **重複エントリー防止**: 損切りされた銘柄への同日中の再エントリーを自動ブロック。

## 📁 プロジェクト構造

| ファイル名 | 役割 |
| :--- | :--- |
| `analyzer.py` | 戦略構築・銘柄選定・パラメータ最適化 (v15.6) |
| `monitor.py` | リアルタイム市場監視・シグナル配信 (v15.15) |
| `backtest_engine.py` | バックテスト及びスコアリングのコアロジック |
| `position_manager.py` | 仮想ポジションの状態管理（TP1/TP2、連携フラグ） |
| `gemini_reviewer.py` | Gemini APIを用いた運用監査と改善提案 |
| `utils.py` | テクニカル指標計算、データ取得、通知等の共通関数 |
| `config.py` | システム全体の定数・設定管理 (Log Level: INFO) |
| `REQUIREMENTS.md` | システム要件定義書 (データ整合性要件を定義) |
| `SYSTEM_DESIGN.md` | 詳細設計・アルゴリズム仕様書 (連携アーキテクチャ定義) |

## 🛠️ セットアップ & 使用方法

### 必要条件
- Python 3.12以上 (CPython 3.12.12 推奨)
- yfinance, pandas, pandas-ta などのライブラリ（`requirements.txt` 参照）
- Discord Webhook URL（通知用）

### 手順
1. **依存関係のインストール**:
   ```bash
   pip install -r requirements.txt
   ```
2. **戦略の構築 (Analyzer)**:
   市場が閉まっている時間帯に実行し、翌日の監視銘柄を決定します。
   ```bash
   python analyzer.py
   ```
3. **市場監視の自動実行 (GitHub Actions)**:
   - **AMセッション**: 08:00 起動 → 09:30 監視開始 → 後場起動で終了。
   - **PMセッション**: 10:30 起動 → 12:30 監視開始 → 15:10 終了。

---
*This project is autonomously maintained and improved with the help of Gemini CLI.*
