"""
gemini_reviewer.py - Gemini AIによる自動戦略レビュー・改善提案ツール

目的:
  REQUIREMENTS.md / SYSTEM_DESIGN.md を正として、実行ログや結果データと照合し、
  運用監査およびパラメータの具体的なチューニング提案を自動生成する。

セキュリティ方針:
  - ソースコード全文の送信は行わず、結果データとドキュメントのみを送信（トークン節約とプライバシー保護）。
  - APIキー等は環境変数化を徹底（コード内にハードコードしない）。
  - CSVデータは直近N件にフィルタリングして送信量を抑制。
  - ログデータは実行当日（JST）の差分のみを抽出して送信。
"""
import os
import sys
import time
from datetime import datetime
import pytz
from google import genai
from pathlib import Path

# ─── 設定 ─────────────────────────────────────────
MAX_TOTAL_CHARS = 400_000        # プロンプト全体の文字数上限（Gemini 2.0 Flash対応）
CSV_MAX_ROWS = 50                # CSVファイルは直近N行のみ送信
MAX_RETRIES = 3                  # APIリトライ回数
RETRY_DELAY_BASE = 10            # リトライ待機時間のベース（秒）

# 設計ドキュメント（運用監査の基準）
DOC_FILES = [
    'REQUIREMENTS.md',
    'SYSTEM_DESIGN.md',
]

# データファイル（直近データのみフィルタリングして送信）
DATA_FILES = [
    'best_config.json',          # 現在の最適化パラメータ
    'trade_results.csv',         # 取引結果（直近N件のみ）
    'trade_journal.csv',         # エントリー記録（直近N件のみ）
]

# ログファイル
LOG_FILES = [
    'execution_log.txt',
]

# 除外パターン
EXCLUDE_PATTERNS = ['.env', 'credentials', '__pycache__', 'venv', '.git']


def read_file_safe(path: str) -> str:
    """ファイルを安全に読み込む。存在しない場合は空文字を返す。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] {path} の読み込みに失敗: {e}", file=sys.stderr)
        return ""


def read_csv_tail(path: str, max_rows: int) -> str:
    """CSVファイルの先頭行（ヘッダー）+ 直近N行を返す。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) <= 1:
            return "".join(lines)  # ヘッダーのみ or 空
        header = lines[0]
        data_lines = lines[1:]
        tail = data_lines[-max_rows:] if len(data_lines) > max_rows else data_lines
        total = len(data_lines)
        result = header + "".join(tail)
        if total > max_rows:
            result = f"# ({total}件中、直近{max_rows}件を表示)\n" + result
        return result
    except Exception as e:
        print(f"[WARN] {path} の読み込みに失敗: {e}", file=sys.stderr)
        return ""


def read_today_log(path: str) -> str:
    """ログファイルから本日の日付(JST)で始まる行とその後のスタックトレース等を抽出する。"""
    try:
        jst = pytz.timezone('Asia/Tokyo')
        today_str = datetime.now(jst).strftime('%Y-%m-%d')
        
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        today_lines = []
        capturing = False
        for line in lines:
            # ログの行が当日の日付で始まるかチェック
            if line.startswith(today_str):
                capturing = True
            # 別の日付の行が来たらキャプチャを一旦止める（スタックトレース等のインデント行は継続）
            elif len(line) >= 10 and line[:10].count('-') == 2:
                if not line.startswith(today_str):
                    capturing = False
            
            if capturing:
                today_lines.append(line)
        
        if not today_lines:
            return f"# 本日({today_str})のログは見つかりませんでした。\n"
        
        result = "".join(today_lines)
        return f"# ({today_str} のログを抽出)\n" + result
    except Exception as e:
        print(f"[WARN] {path} の読み込みに失敗: {e}", file=sys.stderr)
        return ""


def collect_all_content() -> str:
    """レビュー対象のコンテンツを収集し、1つの文字列にまとめる。"""
    sections = []

    # 1. 設計ドキュメント（運用監査の基準）
    for filepath in DOC_FILES:
        content = read_file_safe(filepath)
        if content:
            sections.append(f"=== {filepath} (設計ドキュメント) ===\n{content}")

    # 2. データファイル（設定値とパフォーマンス結果）
    for filepath in DATA_FILES:
        if not os.path.exists(filepath):
            continue
        if filepath.endswith('.csv'):
            content = read_csv_tail(filepath, CSV_MAX_ROWS)
        else:
            content = read_file_safe(filepath)
        if content:
            sections.append(f"=== {filepath} (データ) ===\n{content}")

    # 3. ログファイル（当日実行分のみ）
    for filepath in LOG_FILES:
        content = read_today_log(filepath)
        if content:
            sections.append(f"=== {filepath} (本日分のログ) ===\n{content}")

    combined = "\n\n".join(sections)

    # 文字数上限チェック
    if len(combined) > MAX_TOTAL_CHARS:
        print(f"[INFO] コンテンツが{len(combined)}文字 → {MAX_TOTAL_CHARS}文字に切り詰めます",
              file=sys.stderr)
        combined = combined[:MAX_TOTAL_CHARS] + "\n\n... (文字数上限により切り詰め)"

    return combined


def build_prompt(content: str) -> str:
    """レビュー用のプロンプトを構築する。"""
    return f"""あなたは日本株に特化した自動売買システムの専門家、およびクオンツ・データアナリストです。

## あなたの役割
提供された設計ドキュメント、直近の取引結果、実行ログ、および現在のパラメータ設定を分析し、
「運用監査」と「損益（期待値）の最大化」を目的とした具体的な改善レポートを作成してください。
※コアとなるソースコード（ロジック）の大幅な書き換えではなく、パラメータのチューニングによる改善を最優先としてください。

## 分析対象データ
{content}

## 依頼事項（以下の構造で回答してください）

### 1. 運用監査（ドキュメントとの齟齬・エラーチェック）
`execution_log.txt` と設計ドキュメント（`REQUIREMENTS.md`, `SYSTEM_DESIGN.md`）を照らし合わせ、
システムが定義通りに稼働しているか（禁止事項の違反や異常挙動がないか）を確認してください。
また、発生しているエラーや警告、意図しない挙動（約定タイムアウト、データ取得失敗、不自然な強制決済など）を抽出し、その原因と対処法を提示してください。

### 2. 取引パフォーマンスと傾向分析
`trade_results.csv` および `trade_journal.csv` から、以下の観点で「勝ちパターン」と「負けパターン」の傾向を統計的に分析してください。
- 利益が出やすい、または損失が出やすい時間帯（寄り付き直後、前場後半、後場など）
- パフォーマンスが良い/悪いセクターや銘柄群
- エントリー時の各指標（RSI過熱感、VWAP乖離率、ボラティリティなど）と勝敗の相関関係

### 3. 損益改善のためのパラメータ・チューニング提案
上記の傾向分析に基づき、現在の `best_config.json` の設定値をどのように変更すれば期待値が向上するか、具体的な数値を挙げて提案してください。
（提案例：「負けトレードの多くがVWAP乖離率2.0%以上で発生しているため、`vwap_dev_max` を 2.5 から 2.0 に下げることを推奨します」「トレンド相場において利確が早すぎる傾向があるため、TPのATR倍率を広げることを検討してください」など）

### 4. プロの視点からのリスク管理アドバイス
ボラティリティの変動、地合いの急変、またはシステムの「癖」に対して、リスクを低減し生き残るための専門的なアドバイスを簡潔にまとめてください。

日本語で、感情を排したデータに基づく客観的かつ建設的なフィードバックをお願いします。
"""


def main():
    # API キーの存在チェック
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: GEMINI_API_KEY 環境変数が設定されていません。", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("[INFO] レビュー対象データを収集中...", file=sys.stderr)
    content = collect_all_content()
    print(f"[INFO] 収集完了: {len(content):,}文字", file=sys.stderr)

    prompt = build_prompt(content)

    for attempt in range(MAX_RETRIES):
        try:
            print(f"[INFO] Gemini API にリクエスト送信中... (試行 {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
            response = client.models.generate_content(
                model="gemini-3.1-pro-preview",
                contents=prompt
            )
            # 分析結果を stdout に出力（workflow で review_result.txt にリダイレクト）
            print(response.text)
            return  # 成功したら終了
        except Exception as e:
            if "429" in str(e) and attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY_BASE * (2 ** attempt)
                print(f"[WARN] レート制限(429)を検知。{wait_time}秒後に再試行します: {e}", file=sys.stderr)
                time.sleep(wait_time)
            else:
                print(f"Gemini API エラー: {e}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
