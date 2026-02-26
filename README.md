# deitorepubric (Ver 15.15+)

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![AI Powered](https://img.shields.io/badge/AI-Gemini%20CLI-orange.svg)](https://github.com/google/gemini-cli)

日本株市場を対象とした、高精度フィルタリング・自律型デイトレードエンジン。
2026-02-26の暴落相場における全敗データを糧に、市場指数（N225/TOPIX/SOX）と銘柄属性を連動させた「鉄壁の防御ロジック」を搭載した最終安定版です。

## 🚀 システム概要

本システムは、**「実データに基づく自己最適化」**と**「市場環境に応じた精密執行」**を垂直同期させた2フェーズ構成です。

1. **Analyzerフェーズ (`analyzer.py`)**: 
   市場指数データをバックテストに同期。地合いが悪い時期の無謀なエントリーを学習段階で排除し、真に期待値の高いパラメータを自己学習します。
2. **Monitorフェーズ (`monitor.py`)**: 
   銘柄の特性（大型・ハイテク・小型）に合わせ、関連指数の騰落をリアルタイム監視。スコアが合格点でも環境が整わなければ執行を物理的に遮断する「多重フィルター」を搭載しています。

## ✨ 主な新機能 (Ver 15.15+)

- **属性別・精密フィルター (New)**: 
  - **主力株**: 日経平均・TOPIX連動型ブレーキ。
  - **ハイテク株**: SOX指数連動型ブレーキ。
  - **小型株**: 地合い感度を調整し、個別の勢いを優先。
- **市場連動型バックテスト (New)**: Analyzerが過去の「地合い」を考慮して最適化を実行。理論値と実戦値の乖離を極限まで解消しました。
- **デイリー・リテスト (`daily_retest.py`) (New)**: 当日の1分足実データを用いて、ロジック変更の有効性を100%の精度で検証。数値で証明された改善のみを反映する体制を構築。
- **利益保護トレーリングストップ (New)**: TP1到達後の損切り引き上げと、高値追従型のトレーリング決済により、利益を確実に確保。
- **リスク管理の強化**:
  - **SL下限ガード**: どんな設定でも 0.7×ATR より近い損切りを置かない安全装置。
  - **当日再エントリー禁止**: 損切りされた銘柄への同日中の再エントリー（リベンジトレード）を完全遮断。
- **データ堅牢化**: CSV破損への自動耐性と、yfinance通信のキャッシュ機構（API制限対策）を実装。

## 📁 プロジェクト構造

| ファイル名 | 役割 |
| :--- | :--- |
| `analyzer.py` | 市場連動型パラメータ最適化 |
| `monitor.py` | 属性別精密フィルターによる市場監視 |
| `backtest_engine.py` | 垂直同期バックテスト・エンジン |
| `daily_retest.py` | 当日実データを用いたロジック検証ツール |
| `position_manager.py` | 仮想ポジション及び取引結果の永続化管理 |
| `utils.py` | 指標計算・地合い判定（N225/TOPIX/SOX/VIX） |
| `config.py` | システム全体の定数・判定基準の一元管理 |
| `SYSTEM_DESIGN.md` | 詳細設計書 (最新の防御ロジック仕様を定義) |

## 🛠️ セットアップ & 使用方法

### 手順
1. **依存関係のインストール**:
   ```bash
   pip install -r requirements.txt
   ```
2. **戦略の構築 (Analyzer)**:
   ```bash
   python analyzer.py
   ```
3. **当日データの検証 (Retest)**:
   ```bash
   python daily_retest.py 2026-02-26
   ```
4. **市場監視の実行 (Monitor)**:
   ```bash
   python monitor.py
   ```

---
*This project is autonomously maintained and improved with the help of Gemini CLI based on real market data analysis.*
