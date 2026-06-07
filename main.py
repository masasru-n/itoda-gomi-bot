import os
import hmac
import hashlib
import base64
import logging
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import httpx
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# 環境変数
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# 日次レポート・bad即時通知のPush送信先（カンマ区切りで複数可）
ADMIN_USER_IDS = [
    uid.strip() for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid.strip()
]
# /daily-report の簡易保護キー（未設定なら保護なし）
REPORT_KEY = os.environ.get("REPORT_KEY", "")

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
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# === インメモリ集計用ストア（当日分のみ保持。再起動でリセット） ===
qa_records: list[dict] = []
feedback_records: list[dict] = []


def today_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def prune_old_records() -> None:
    """当日(JST)以外のレコードを破棄してメモリ肥大を防ぐ"""
    today = today_str()
    qa_records[:] = [r for r in qa_records if r["date"] == today]
    feedback_records[:] = [r for r in feedback_records if r["date"] == today]


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
            "📞 糸田清掃 0947-26-0917（平日 8:00〜17:00）"
        )


async def reply_to_line(reply_token: str, text: str, answer_id: str | None = None) -> None:
    """LINE Messaging APIに返信。answer_idがあるとき👍/👎のQuick Replyを付与"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    message = {"type": "text", "text": text}

    if answer_id:
        message["quickReply"] = {
            "items": [
                {
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": "👍 役立った",
                        "data": f"action=feedback&rating=good&answer_id={answer_id}",
                        "displayText": "👍 役立った",
                    },
                },
                {
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": "👎 役立たなかった",
                        "data": f"action=feedback&rating=bad&answer_id={answer_id}",
                        "displayText": "👎 役立たなかった",
                    },
                },
            ]
        }

    payload = {
        "replyToken": reply_token,
        "messages": [message],
    }
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        resp = await http_client.post(LINE_REPLY_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")


async def push_to_line(user_ids: list[str], text: str) -> None:
    """指定ユーザーへPush送信"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        for uid in user_ids:
            payload = {"to": uid, "messages": [{"type": "text", "text": text}]}
            resp = await http_client.post(LINE_PUSH_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error(f"LINE push failed ({uid}): {resp.status_code} {resp.text}")


def parse_postback(data: str) -> dict:
    """'action=feedback&rating=good&answer_id=xxx' 形式を辞書に変換"""
    result = {}
    for pair in data.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key] = value
    return result


def build_bad_alert(answer_id: str, when: datetime) -> str:
    """bad評価された質問・回答をインメモリから引いて即時通知文を生成"""
    qa = next((r for r in qa_records if r["answer_id"] == answer_id), None)
    ts = when.strftime("%Y-%m-%d %H:%M")
    if qa:
        return (
            f"【👎 bad評価がつきました】{ts}\n"
            "━━━━━━━━━━━━━\n"
            f"Q: {qa['question']}\n"
            "━━━━━━━━━━━━━\n"
            f"A:\n{qa['answer']}"
        )
    # 再起動等でqa_recordsに無い場合は最低限の情報のみ
    return (
        f"【👎 bad評価がつきました】{ts}\n"
        "━━━━━━━━━━━━━\n"
        f"※ 質問本文を取得できませんでした（再起動直後の可能性）\n"
        f"answer_id: {answer_id}"
    )


def build_daily_report() -> str:
    """当日分のインメモリ集計から日次レポート本文を生成"""
    prune_old_records()
    today = today_str()

    total_q = len(qa_records)
    unique_users = {r["user_id"] for r in qa_records}
    user_counts: dict[str, int] = {}
    for r in qa_records:
        user_counts[r["user_id"]] = user_counts.get(r["user_id"], 0) + 1
    repeat_users = sum(1 for c in user_counts.values() if c >= 2)

    good = sum(1 for f in feedback_records if f["rating"] == "good")
    bad = sum(1 for f in feedback_records if f["rating"] == "bad")
    rated = good + bad
    rate_pct = round(rated / total_q * 100, 1) if total_q else 0.0
    bad_pct = round(bad / total_q * 100, 1) if total_q else 0.0

    bad_answer_ids = {f["answer_id"] for f in feedback_records if f["rating"] == "bad"}
    qa_by_id = {r["answer_id"]: r for r in qa_records}
    bad_questions = [
        qa_by_id[aid]["question"] for aid in bad_answer_ids if aid in qa_by_id
    ]

    rated_answer_ids = {f["answer_id"] for f in feedback_records}
    no_rating = sum(1 for r in qa_records if r["answer_id"] not in rated_answer_ids)

    lines = [
        f"【糸田ゴミBot 日次レポート {today}】",
        "",
        "■ 利用状況",
        f"・質問数: {total_q}件",
        f"・ユニークユーザー: {len(unique_users)}人",
        f"・リピートユーザー: {repeat_users}人",
        "",
        "■ フィードバック",
        f"・👍 good: {good}件",
        f"・👎 bad: {bad}件",
        f"・評価率: {rate_pct}% ({rated}/{total_q})",
        f"・bad率: {bad_pct}%",
    ]

    if bad_questions:
        lines.append("")
        lines.append("■ bad評価された質問")
        for i, q in enumerate(bad_questions, 1):
            lines.append(f"{i}. {q}")

    lines.append("")
    lines.append(f"■ 評価なしで終わった質問数: {no_rating}件")

    return "\n".join(lines)


@app.get("/")
async def root():
    return {"status": "ok", "service": "itoda-gomi-bot"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/daily-report")
async def daily_report(key: str = ""):
    """当日分を集計して管理者へPush。REPORT_KEY設定時はkey一致が必要"""
    if REPORT_KEY and key != REPORT_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not ADMIN_USER_IDS:
        return JSONResponse(
            status_code=400,
            content={"error": "ADMIN_USER_IDS が未設定です"},
        )
    report = build_daily_report()
    await push_to_line(ADMIN_USER_IDS, report)
    return {"status": "sent", "recipients": len(ADMIN_USER_IDS)}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")

    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    events = data.get("events", [])

    for event in events:
        event_type = event.get("type")
        reply_token = event.get("replyToken", "")
        user_id = event.get("source", {}).get("userId", "unknown")

        if event_type == "message":
            message = event.get("message", {})
            if message.get("type") != "text":
                continue

            user_text = message.get("text", "")
            answer = get_claude_response(user_text)
            answer_id = str(uuid.uuid4())
            now = datetime.now(JST)

            logger.info(json.dumps({
                "type": "qa_log",
                "timestamp": now.isoformat(),
                "answer_id": answer_id,
                "user_id": user_id,
                "question": user_text,
                "answer": answer,
            }, ensure_ascii=False))

            prune_old_records()
            qa_records.append({
                "date": now.strftime("%Y-%m-%d"),
                "answer_id": answer_id,
                "user_id": user_id,
                "question": user_text,
                "answer": answer,
            })

            await reply_to_line(reply_token, answer, answer_id=answer_id)

        elif event_type == "postback":
            pb = parse_postback(event.get("postback", {}).get("data", ""))
            if pb.get("action") == "feedback":
                now = datetime.now(JST)
                rating = pb.get("rating", "")
                ans_id = pb.get("answer_id", "")

                logger.info(json.dumps({
                    "type": "feedback",
                    "timestamp": now.isoformat(),
                    "answer_id": ans_id,
                    "user_id": user_id,
                    "rating": rating,
                }, ensure_ascii=False))

                prune_old_records()
                feedback_records.append({
                    "date": now.strftime("%Y-%m-%d"),
                    "answer_id": ans_id,
                    "user_id": user_id,
                    "rating": rating,
                })

                # bad評価は管理者へ即時Push（取りこぼし防止）
                if rating == "bad" and ADMIN_USER_IDS:
                    alert = build_bad_alert(ans_id, now)
                    await push_to_line(ADMIN_USER_IDS, alert)

                await reply_to_line(reply_token, "ご評価ありがとうございます。")

    return PlainTextResponse("OK")
