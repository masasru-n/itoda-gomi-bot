import os
import hmac
import hashlib
import base64
import logging
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ナレッジベース・システムプロンプトをファイルから読込
BASE_DIR = Path(__file__).parent
KNOWLEDGE = (BASE_DIR / "itoda_gomi_knowledge_v2.md").read_text(encoding="utf-8")
SYSTEM_PROMPT_BASE = (BASE_DIR / "system_prompt_v3.txt").read_text(encoding="utf-8")

# システムプロンプトにナレッジを埋め込み
SYSTEM_PROMPT = f"""{SYSTEM_PROMPT_BASE}

# 知識ベース（糸田町のごみ分別ルール）

{KNOWLEDGE}
"""

app = FastAPI()
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


def verify_signature(body: bytes, signature: str) -> bool:
    """LINEからのリクエスト署名検証"""
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def get_claude_response(user_message: str) -> str:
    """Claude Haiku 4.5に質問を投げて回答を得る"""
    try:
        message = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return message.content[0].text
    except Exception:
        logger.exception("Claude API error")
        return (
            "申し訳ありません、現在お答えできません。\n"
            "しばらく時間をおいて再度お試しください。\n"
            "📞 税務町民課 環境衛生係 0947-26-1235"
        )


async def reply_to_line(reply_token: str, text: str) -> None:
    """LINE Messaging APIに返信"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        resp = await http_client.post(LINE_REPLY_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")


@app.get("/")
async def root():
    return {"status": "ok", "service": "itoda-gomi-bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    # LINE署名検証
    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    events = data.get("events", [])

    for event in events:
        # テキストメッセージのみ処理
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        user_text = message.get("text", "")
        reply_token = event.get("replyToken", "")
        user_id = event.get("source", {}).get("userId", "unknown")

        logger.info(f"Q from {user_id}: {user_text}")
        answer = get_claude_response(user_text)
        logger.info(f"A: {answer}")

        await reply_to_line(reply_token, answer)

    return PlainTextResponse("OK")
