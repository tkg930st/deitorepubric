# deitorepubric (Ver 14.2)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
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

- **AI駆動の最適化 & 監査**: `gemini_reviewer.py` により、トレード結果の分析とパラメータの改善提案、システム稼働の自動監査を実施。
- **高度なテクニカルロジック**: 
  - **セクター・アライメント**: ライバル銘柄との相関に基づくスコアリング。
  - **ダイナミックATR調整**: 市場のボラティリティに応じて利確・損切幅を動的に伸縮。
  - **シャンデリア出口**: TP1達成後、直近の高値/安値に基づいたトレーリングストップで利益を最大化。
- **厳格なリスク管理**:
  - **デイリー・ストップロス**: 1日の通算損益が -3.0% に達した際の緊急停止機能。
  - **RR比強制**: リスク・リワード比が1.5以上になるようシグナルを自動調整。
  - **VIXリスク調整**: 市場全体の恐怖指数(VIX)が高い場合、推奨ロットを自動削減。

## 📁 プロジェクト構造

| ファイル名 | 役割 |
| :--- | :--- |
| `analyzer.py` | 戦略構築・銘柄選定・パラメータ最適化 (v13.5) |
| `monitor.py` | リアルタイム市場監視・シグナル配信 (v14.2) |
| `backtest_engine.py` | バックテスト及びスコアリングのコアロジック |
| `position_manager.py` | 仮想ポジションの状態管理（TP1/TP2、トレーリング） |
| `gemini_reviewer.py` | Gemini APIを用いた運用監査と改善提案 |
| `utils.py` | テクニカル指標計算、データ取得、通知等の共通関数 |
| `config.py` | システム全体の定数・設定管理 |
| `REQUIREMENTS.md` | システム要件定義書 |
| `SYSTEM_DESIGN.md` | 詳細設計・アルゴリズム仕様書 |

## 🛠️ セットアップ & 使用方法

### 必要条件
- Python 3.10以上
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
3. **市場監視の開始 (Monitor)**:
   市場営業日の 09:00〜15:00 に実行します（GitHub Actionsによる自動実行推奨）。
   ```bash
   python monitor.py
   ```

## 📈 ロードマップ
- [ ] セクター別の指数（親子関係）に基づくフィルタリングの実装
- [ ] 複数データソース（公式API等）へのフォールバック対応
- [ ] Webダッシュボードによるトレード結果の可視化

---
*This project is autonomously maintained and improved with the help of Gemini CLI.*
