# Discord 通知フォーマット定義書 (Ver 15.15基準)

本ドキュメントは、deitorepubric システムが送信するすべての通知の「正解」を定義する。
ロジックの修正やリファクタリング時、本フォーマットの情報量や絵文字、構成を独断で変更することは禁止される。

## 1. 戦略構築完了レポート (analyzer.py)
戦略構築が完了した際に送信される詳細なサマリー。

**テンプレート:**
```
✅ **Version 15.15 統合戦略構築完了！**

⏱️ **実行時間**: {elapsed}秒 ({elapsed_min}分)
📅 **完了時刻**: {finish_time} JST
🚀 **並列処理**: {cpu_count}コア活用

🎯 **選定された監視銘柄 TOP{count}（raw利益順）**:

**{ticker}** [{sector}] ({size_category})
期待利益: {profit}% (raw: {raw_total}%)
L: {long_profit}% {L_status} (raw:{raw_long}%) / S: {short_profit}% {S_status} (raw:{raw_short}%)
ボラティリティ: {atr_pct}%
ライバル5銘柄: {rival_list}
タイプ: {logic_type} (Monthly / Weekly)

📊 **最優秀銘柄**: {best_ticker}
   期待利益: {best_profit}%
   ボラティリティ: {best_atr_pct}%

🆕 **Version 15.15 改良点**:
   • AM/PM 監視セッション連携 (自動ハンドオーバー)
   • 実行ログの軽量化 (1MB以内) & 通信ノイズ遮断
   • 取引記録 (ジャーナル・結果) の整合性ガード
   • 属性別精密フィルター (SOX/N225/TOPIX)
   • デイリー・ストップロス (-3.0%) 安全装置実装

🔔 **次のステップ**:
   python monitor.py で監視を開始してください！
```

## 2. 監視起動時レポート (monitor.py)
監視プログラム起動時の地合いおよび対象銘柄の報告。

**テンプレート:**
```
📡 **Version 15.15 統合戦略監視 ({SESSION_TYPE}) 起動**
━━━━━━━━━━━━━━
🌍 **マクロ地合い情報**:
• VIX: {vix_value} ({vix_chg}%)
• SOX: {sox_chg}%
• JPY: {jpy_chg}%

🎯 **監視対象銘柄**: {ticker_list}
```

## 3. 地合い調整通知 (monitor.py)
ボラティリティ急増等の異常を検知し、パラメータを自動調整した際の通知。

**テンプレート:**
```
ℹ️ **市場ボラティリティ上昇検知 (VIX > 20)**
リスク管理のため以下の調整を自動適用しました：
• エントリー閾値: +5.0 (厳格化)
• 利確幅(TP): ×1.25 (拡大)
• 損切幅(SL): ×1.15 (拡大)
```

## 4. マーケット構造検知通知 (monitor.py)
BOS/CHoCH を検知した際のトレンド状況報告。

**テンプレート:**
```
{emoji} **[STRUCTURE] {ticker}**
検出：{type} ({direction})
状況：{description}
節目価格：¥{price}
```

## 5. 新規シグナル（エントリー）通知 (monitor.py) 🛡️
最も重要なエントリー判断の詳細報告。

**テンプレート:**
```
🛡️ **新規シグナル (Ver 15.15): {SIDE}**
銘柄: {ticker} ({logic_type})
価格: ¥{entry_price}
TP1: ¥{tp1} (ATR×{tp1_mul}) → 50%決済
TP2: トレーリング (ATR×{trailing_mul}幅)
SL: ¥{sl} (ATR×{sl_mul})
スコア: {total_score} (判定閾値: {actual_threshold})
指標: RSI:{rsi}, VWAP:{vwap_dev}
```

## 6. TP1達成通知 (monitor.py / position_manager.py) ✅
第1利確目標達成と、リスク低減の報告。

**テンプレート:**
```
✅ **TP1達成: {ticker}**
🎯 50%利確完了
・価格: ¥{current_price}
・損益: {profit}%
・リスクを半分（建値近辺）に縮小しました
・残り50%はトレーリングTP (ATR×{trailing_mul}) で追従中
```

## 7. 決済（エグジット）通知 (monitor.py) 🛑
ポジションクローズ時の損益報告。

**テンプレート:**
```
🛑 **[EXIT] {ticker}**
理由：{exit_reason}
損益：{profit}% ({logic_type})
決済単価：¥{exit_price}
```

## 8. 本日の最終結果サマリー (monitor.py) 📊
大引け後の全トレード集計報告。

**テンプレート:**
```
📊 **本日の最終結果サマリー**

💰 **総合損益: {total_profit}%**
━━━━━━━━━━━━━━
📅 **Monthly 戦略結果**
• {ticker} ({side}): {profit}% [{reason}]
...

📅 **Weekly 戦略結果**
• {ticker} ({side}): {profit}% [{reason}]
...
```
