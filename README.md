# 糸田町ごみ分別Bot（検証用）

糸田町のごみ分別に関する住民の質問に、LINE上でAIが自動応答する検証用システム。

## 概要

- 糸田町公式「ごみ分別表（令和6年12月1日施行）」と「Q&A」をナレッジベースとして使用
- Claude Haiku 4.5 がナレッジを参照して回答
- LINE公式アカウントの Messaging API 経由で住民とやり取り

## 構成

- `main.py`: FastAPIアプリ本体（LINE Webhook受信、Claude API呼び出し、返信処理）
- `itoda_gomi_knowledge_v2.md`: ナレッジベース
- `system_prompt_v3.txt`: 回答フォーマット定義

## 環境変数

| 変数名 | 内容 |
|---|---|
| `LINE_CHANNEL_SECRET` | LINE Developers > チャネル基本設定 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers > Messaging API設定（長期トークン） |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ で発行 |

## デプロイ

Railway を使用。GitHub リポジトリと連携してプッシュで自動デプロイ。

## ローカル動作確認

```bash
pip install -r requirements.txt
export LINE_CHANNEL_SECRET=xxx
export LINE_CHANNEL_ACCESS_TOKEN=xxx
export ANTHROPIC_API_KEY=xxx
uvicorn main:app --reload
```

## 注意事項

- 検証用システム。AIによる回答であり正確性は保証されない
- 最終確認は糸田町役場 税務町民課 環境衛生係（0947-26-1235）へ
