import os
from google import genai
from pathlib import Path

# Geminiの設定（最新のgoogle-genaiライブラリを使用）
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

def collect_files():
    """分析対象のファイルを読み集める"""
    # 読み込みたい拡張子
    extensions = ['.py', '.md', '.json', '.csv', '.yml']
    content = []
    
    for path in Path(".").rglob("*"):
        if path.suffix in extensions and "venv" not in str(path) and ".git" not in str(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content.append(f"--- File: {path} ---\n{f.read()}\n")
            except Exception as e:
                print(f"Error reading {path}: {e}")
                
    return "\n".join(content)

def main():
    all_code = collect_files()
    
    prompt = f"""
あなたはデイトレード・自動売買システムの専門家およびシニアエンジニアです。
以下のソースコード、設計ドキュメント、実行ログ、およびトレード結果をすべて読み込み、
今日の「システムの健康診断」を行ってください。

【分析対象のデータ】
{all_code}

【依頼事項】
1. 実行状況の要約（エラーの有無や異常な挙動がないか）
2. ロジックの評価（現在の相場状況や設計思想に対して、改善の余地はあるか）
3. 具体的な改善提案（コードの修正案や、次に追加すべき機能など）

日本語で、簡潔かつ建設的なフィードバックをお願いします。
"""

    # 最新のAPI形式で生成
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt
    )
    
    # 分析結果を出力
    print(response.text)

if __name__ == "__main__":
    main()
