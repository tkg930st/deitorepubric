"""
gemini_reviewer.py - Gemini AIによる自動戦略レビュー・改善提案ツール

目的:
  REQUIREMENTS.md / SYSTEM_DESIGN.md を正として、実装コードとの乖離を検出し、
  バックテスト結果に基づいた戦略改善の具体的提案を生成する。

セキュリティ方針:
  - 全ソースコード送信を許容（戦略改善には analyzer.py / monitor.py の全文が不可欠）
  - APIキー等は環境変数化を徹底（コード内にハードコードしない）
  - CSVデータは直近N件にフィルタリングして送信量を抑制
  - best_config.json は最適化パラメータとして送信（戦略評価に必須）
"""
import os
import sys
from google import genai
from pathlib import Path

# ─── 設定 ─────────────────────────────────────────
MAX_TOTAL_CHARS = 400_000        # プロンプト全体の文字数上限（Gemini 2.0 Flash対応）
CSV_MAX_ROWS = 50                # CSVファイルは直近N行のみ送信
LOG_MAX_LINES = 200              # ログファイルは直近N行のみ送信

# 送信対象のソースコード（戦略改善に必要なファイル）
SOURCE_FILES = [
    'analyzer.py',
    'backtest_engine.py',
    'monitor.py',
    'position_manager.py',
    'utils.py',
    'config.py',
]

# 設計ドキュメント（レビューの基準）
DOC_FILES = [
    'REQUIREMENTS.md',
    'SYSTEM_DESIGN.md',
]

# データファイル（直近データのみフィルタリングして送信）
DATA_FILES = [
    'best_config.json',          # 最適化パラメータ（戦略評価に必須）
    'trade_results.csv',         # 取引結果（直近N件のみ）
    'trade_journal.csv',         # エントリー記録（直近N件のみ）
]

# ログファイル（直近N行のみ）
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


def read_log_tail(path: str, max_lines: int) -> str:
    """ログファイルの直近N行を返す。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        total = len(lines)
        result = "".join(tail)
        if total > max_lines:
            result = f"# ({total}行中、直近{max_lines}行を表示)\n" + result
        return result
    except Exception as e:
        print(f"[WARN] {path} の読み込みに失敗: {e}", file=sys.stderr)
        return ""


def collect_all_content() -> str:
    """レビュー対象のコンテンツを収集し、1つの文字列にまとめる。"""
    sections = []

    # 1. 設計ドキュメント（レビュー基準として最優先）
    for filepath in DOC_FILES:
        content = read_file_safe(filepath)
        if content:
            sections.append(f"=== {filepath} (設計ドキュメント) ===\n{content}")

    # 2. ソースコード（戦略改善に必須）
    for filepath in SOURCE_FILES:
        content = read_file_safe(filepath)
        if content:
            sections.append(f"=== {filepath} (ソースコード) ===\n{content}")

    # 3. ワークフロー設定
    workflow_dir = Path(".github/workflows")
    if workflow_dir.exists():
        for yml_file in sorted(workflow_dir.glob("*.yml")):
            content = read_file_safe(str(yml_file))
            if content:
                sections.append(f"=== {yml_file} (ワークフロー) ===\n{content}")

    # 4. データファイル（フィルタリング済み）
    for filepath in DATA_FILES:
        if not os.path.exists(filepath):
            continue
        if filepath.endswith('.csv'):
            content = read_csv_tail(filepath, CSV_MAX_ROWS)
        else:
            content = read_file_safe(filepath)
        if content:
            sections.append(f"=== {filepath} (データ) ===\n{content}")

    # 5. ログファイル（直近のみ）
    for filepath in LOG_FILES:
        content = read_log_tail(filepath, LOG_MAX_LINES)
        if content:
            sections.append(f"=== {filepath} (ログ・直近{LOG_MAX_LINES}行) ===\n{content}")

    combined = "\n\n".join(sections)

    # 文字数上限チェック
    if len(combined) > MAX_TOTAL_CHARS:
        print(f"[INFO] コンテンツが{len(combined)}文字 → {MAX_TOTAL_CHARS}文字に切り詰めます",
              file=sys.stderr)
        combined = combined[:MAX_TOTAL_CHARS] + "\n\n... (文字数上限により切り詰め)"

    return combined


def build_prompt(content: str) -> str:
    """レビュー用のプロンプトを構築する。"""
    return f"""あなたはデイトレード・自動売買システムの専門家およびシニアエンジニアです。

## あなたの役割
以下のソースコード、設計ドキュメント、実行ログ、および取引結果を分析し、
「戦略の自動改善」を主目的とした健康診断レポートを作成してください。

## 評価基準
REQUIREMENTS.md と SYSTEM_DESIGN.md を「正」とし、実装コードがこれらの仕様を
満たしているかを検証してください。乖離がある箇所は具体的に指摘してください。

## 分析対象データ
{content}

## 依頼事項（以下の構造で回答してください）

### 1. 設計書との乖離チェック
REQUIREMENTS.md / SYSTEM_DESIGN.md の各要件に対し、実装が正しく対応しているか。
未実装や仕様と異なる挙動がある場合は具体的に指摘すること。

### 2. 実行状況の要約
execution_log.txt や trade_results.csv から読み取れるエラー、異常な挙動、
パフォーマンスの傾向を要約すること。

### 3. 戦略改善の提案（バックテスト観点）
best_config.json のパラメータや analyzer.py / backtest_engine.py のロジックに対し、
損益改善が見込める具体的な修正案を提示すること。

### 4. コード品質・運用面の改善提案
バグの温床、エラーハンドリングの不備、パフォーマンス改善の余地など。

日本語で、簡潔かつ建設的なフィードバックをお願いします。
"""


def main():
    # API キーの存在チェック
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: GEMINI_API_KEY 環境変数が設定されていません。", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("[INFO] レビュー対象ファイルを収集中...", file=sys.stderr)
    content = collect_all_content()
    print(f"[INFO] 収集完了: {len(content):,}文字", file=sys.stderr)

    prompt = build_prompt(content)

    try:
        print("[INFO] Gemini API にリクエスト送信中...", file=sys.stderr)
        response = client.models.generate_content(
            model="gemini-3.1-pro-preview",
            contents=prompt
        )
        # 分析結果を stdout に出力（workflow で review_result.txt にリダイレクト）
        print(response.text)
    except Exception as e:
        print(f"Gemini API エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
