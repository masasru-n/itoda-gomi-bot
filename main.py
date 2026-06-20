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

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

ADMIN_USER_IDS = [
    uid.strip() for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid.strip()
]
STAFF_USER_IDS = [
    uid.strip() for uid in os.environ.get("STAFF_USER_IDS", "").split(",") if uid.strip()
]
REPORT_KEY = os.environ.get("REPORT_KEY", "")

BASE_DIR = Path(__file__).parent
KNOWLEDGE = (BASE_DIR / "itoda_gomi_knowledge_v2.md").read_text(encoding="utf-8")
SYSTEM_PROMPT_BASE = (BASE_DIR / "system_prompt_v3.txt").read_text(encoding="utf-8")

SYSTEM_PROMPT = f"""{SYSTEM_PROMPT_BASE}

# 知識ベース（糸田町のごみ分別ルール）

{KNOWLEDGE}
"""

app = FastAPI()
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

qa_records: list[dict] = []
feedback_records: list[dict] = []

sessions: dict[str, dict] = {}
SESSION_TIMEOUT_SEC = 600

CLEANUP_KEYWORDS = [
    "片付け", "片づけ", "かたづけ", "処分", "運び出し", "運べない", "運んで",
    "ゴミ屋敷", "ごみ屋敷", "遺品整理", "生前整理", "引っ越し", "引越し",
    "大量", "重い", "一人で", "高齢",
]

TYPE_LABELS = {"t1": "粗大ごみ（家具など）", "t2": "雑多なごみ", "t3": "その他"}
SCALE_LABELS = {"s1": "家具数点", "s2": "1部屋分", "s3": "一軒丸ごと", "s4": "その他"}
TIMING_LABELS = {"d1": "できるだけ早く", "d2": "1週間以内", "d3": "1ヶ月以内", "d4": "その他"}


def now_jst() -> datetime:
    return datetime.now(JST)


def today_str() -> str:
    return now_jst().strftime("%Y-%m-%d")


def is_business_hours(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return 8 <= dt.hour < 17


def prune_old_records() -> None:
    today = today_str()
    qa_records[:] = [r for r in qa_records if r["date"] == today]
    feedback_records[:] = [r for r in feedback_records if r["date"] == today]


def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def get_claude_response(user_message: str) -> str:
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


def feedback_items(answer_id: str) -> list[dict]:
    return [
        {"type": "action", "action": {"type": "postback", "label": "👍 役立った",
            "data": f"action=feedback&rating=good&answer_id={answer_id}", "displayText": "👍 役立った"}},
        {"type": "action", "action": {"type": "postback", "label": "👎 役立たなかった",
            "data": f"action=feedback&rating=bad&answer_id={answer_id}", "displayText": "👎 役立たなかった"}},
    ]


def cleanup_entry_items() -> list[dict]:
    return [
        {"type": "action", "action": {"type": "uri",
            "label": "電話", "uri": "tel:0947260917"}},
        {"type": "action", "action": {"type": "postback",
            "label": "LINE", "data": "action=intake_start", "displayText": "LINEで受付"}},
    ]


def choice_items(action: str, labels: dict) -> list[dict]:
    items = []
    for code, label in labels.items():
        items.append({"type": "action", "action": {"type": "postback",
            "label": label, "data": f"action={action}&value={code}", "displayText": label}})
    return items


async def reply_to_line(reply_token: str, text: str, quick_items: list[dict] | None = None) -> None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    message = {"type": "text", "text": text}
    if quick_items:
        message["quickReply"] = {"items": quick_items}
    payload = {"replyToken": reply_token, "messages": [message]}
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        resp = await http_client.post(LINE_REPLY_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")


async def push_to_line(user_ids: list[str], text: str) -> None:
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
    result = {}
    for pair in data.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key] = value
    return result


def contains_cleanup_keyword(text: str) -> bool:
    return any(kw in text for kw in CLEANUP_KEYWORDS)


def session_expired(sess: dict, now: datetime) -> bool:
    return (now - sess["updated_at"]).total_seconds() > SESSION_TIMEOUT_SEC


# ───────────────────────────────────────────────
# リッチメニュー左タップ（postback: action=usage_guide）で返す使い方ガイド
# ───────────────────────────────────────────────
USAGE_GUIDE = (
    "こんにちは。sil（シル）です。\n"
    "私は糸田町のごみ分別についてのご質問にお答えするAIアシスタントです。\n\n"
    "例えば以下のように質問いただければ私が回答いたします。\n"
    "・「○○は何で出すの？」\n"
    "・「うちの地区の収集日を教えて」\n"
    "・「粗大ごみはどうやって出す？」\n"
    "・「ペットボトルのキャップはどこに入れるの？」\n\n"
    "お気軽にご質問ください。"
)

CLEANUP_GUIDE = (
    "ご自宅からのごみ運び出し・片付けは、糸田清掃が承っております。\n"
    "【対応内容】\n"
    "・ご自宅への訪問集荷\n"
    "・家具・大型品の搬出\n"
    "・焼却場への運搬\n"
    "・生前整理・遺品整理・引越しごみ\n"
    "・高齢等でごみ出しが困難な方の支援\n\n"
    "お見積りは無料です。\n"
    "お電話でのご相談が可能です（平日 8:00〜17:00）\n"
    "📞 0947-26-0917\n\n"
    "LINEでも受け付けております。\n"
    "下のボタンからお選びください。"
)
ASK_CONTACT = (
    "ご依頼を受け付けます。\n"
    "まず、ご連絡先を教えてください。\n\n"
    "・お名前\n・お電話番号\n・ご住所\n\n"
    "を1つのメッセージでお送りください。\n"
    "（中止する場合は「キャンセル」とお送りください）"
)
ASK_TYPE = "ありがとうございます。\nごみの内容をお選びください。"
ASK_SCALE = "規模をお選びください。"
ASK_TIMING = "ご希望の時期をお選びください。"
DURING_INTAKE = (
    "ただいま受付中です。\n"
    "下の選択肢のボタンからお選びください。\n"
    "中止する場合は「キャンセル」とお送りください。"
)
CANCEL_MSG = "受付をキャンセルしました。またのご利用をお待ちしております。"
TIMEOUT_MSG = (
    "一定時間ご返信がなかったため、受付を中断しました。\n"
    "再度ご希望の際は、お申し付けください。"
)
EXPIRED_MSG = "受付の有効期限が切れました。お手数ですが、もう一度お申し付けください。"


def build_staff_notice(sess: dict, when: datetime) -> str:
    ts = when.strftime("%Y-%m-%d %H:%M")
    lines = [
        "【新規片付け依頼】",
        "",
        "■ ご連絡先",
        sess["contact"],
        "",
        "■ ご依頼内容",
        f"ごみの内容: {sess['type']}",
        f"規模: {sess['scale']}",
        f"希望時期: {sess['timing']}",
        "",
        "■ 受付日時",
        ts,
    ]
    if not is_business_hours(when):
        lines.append("")
        lines.append("※ 営業時間外の受付です")
    return "\n".join(lines)


def build_user_complete(sess: dict, when: datetime) -> str:
    lines = [
        "ご依頼を受け付けました。",
        "担当者より折り返しご連絡いたします。",
        "",
        "━━━━━━━━━━━━━",
        "【ご入力内容】",
        f"ご連絡先: {sess['contact']}",
        f"ごみの内容: {sess['type']}",
        f"規模: {sess['scale']}",
        f"希望時期: {sess['timing']}",
        "━━━━━━━━━━━━━",
    ]
    if not is_business_hours(when):
        lines.append("")
        lines.append("※ ただいま営業時間外のため、ご連絡は翌営業日になる場合があります。")
    lines.append("")
    lines.append("糸田清掃 0947-26-0917（平日 8:00〜17:00）")
    return "\n".join(lines)


def build_bad_alert(answer_id: str, when: datetime) -> str:
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
    return (
        f"【👎 bad評価がつきました】{ts}\n"
        "━━━━━━━━━━━━━\n"
        "※ 質問本文を取得できませんでした（再起動直後の可能性）\n"
        f"answer_id: {answer_id}"
    )


def build_daily_report() -> str:
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
    bad_questions = [qa_by_id[aid]["question"] for aid in bad_answer_ids if aid in qa_by_id]
    rated_answer_ids = {f["answer_id"] for f in feedback_records}
    no_rating = sum(1 for r in qa_records if r["answer_id"] not in rated_answer_ids)
    lines = [
        f"【糸田ゴミBot 日次レポート {today}】", "",
        "■ 利用状況",
        f"・質問数: {total_q}件",
        f"・ユニークユーザー: {len(unique_users)}人",
        f"・リピートユーザー: {repeat_users}人", "",
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
    if REPORT_KEY and key != REPORT_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not ADMIN_USER_IDS:
        return JSONResponse(status_code=400, content={"error": "ADMIN_USER_IDS が未設定です"})
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
        now = now_jst()

        if event_type == "message":
            message = event.get("message", {})
            if message.get("type") != "text":
                continue
            user_text = message.get("text", "")
            sess = sessions.get(user_id)

            if sess and session_expired(sess, now):
                sessions.pop(user_id, None)
                await reply_to_line(reply_token, TIMEOUT_MSG)
                continue

            if sess:
                if user_text.strip() in ("キャンセル", "中止", "やめる"):
                    sessions.pop(user_id, None)
                    await reply_to_line(reply_token, CANCEL_MSG)
                    continue
                if sess["step"] == "await_contact":
                    sess["contact"] = user_text.strip()
                    sess["step"] = "await_type"
                    sess["updated_at"] = now
                    await reply_to_line(reply_token, ASK_TYPE, choice_items("intake_type", TYPE_LABELS))
                    continue
                await reply_to_line(reply_token, DURING_INTAKE)
                continue

            if contains_cleanup_keyword(user_text):
                await reply_to_line(reply_token, CLEANUP_GUIDE, cleanup_entry_items())
                continue

            answer = get_claude_response(user_text)
            answer_id = str(uuid.uuid4())
            logger.info(json.dumps({
                "type": "qa_log", "timestamp": now.isoformat(), "answer_id": answer_id,
                "user_id": user_id, "question": user_text, "answer": answer,
            }, ensure_ascii=False))
            prune_old_records()
            qa_records.append({
                "date": now.strftime("%Y-%m-%d"), "answer_id": answer_id,
                "user_id": user_id, "question": user_text, "answer": answer,
            })
            await reply_to_line(reply_token, answer, feedback_items(answer_id))

        elif event_type == "postback":
            pb = parse_postback(event.get("postback", {}).get("data", ""))
            action = pb.get("action", "")

            # ── リッチメニュー：左タップ（使い方ガイド）──
            if action == "usage_guide":
                await reply_to_line(reply_token, USAGE_GUIDE)
                continue

            # ── リッチメニュー：中央タップ（片付けサービス案内）──
            if action == "cleanup_menu":
                await reply_to_line(reply_token, CLEANUP_GUIDE, cleanup_entry_items())
                continue

            if action == "feedback":
                rating = pb.get("rating", "")
                ans_id = pb.get("answer_id", "")
                logger.info(json.dumps({
                    "type": "feedback", "timestamp": now.isoformat(), "answer_id": ans_id,
                    "user_id": user_id, "rating": rating,
                }, ensure_ascii=False))
                prune_old_records()
                feedback_records.append({
                    "date": now.strftime("%Y-%m-%d"), "answer_id": ans_id,
                    "user_id": user_id, "rating": rating,
                })
                if rating == "bad" and ADMIN_USER_IDS:
                    await push_to_line(ADMIN_USER_IDS, build_bad_alert(ans_id, now))
                await reply_to_line(reply_token, "ご評価ありがとうございます。")

            elif action == "intake_start":
                sessions[user_id] = {
                    "step": "await_contact", "contact": None,
                    "type": None, "scale": None, "timing": None, "updated_at": now,
                }
                await reply_to_line(reply_token, ASK_CONTACT)

            elif action in ("intake_type", "intake_scale", "intake_timing"):
                sess = sessions.get(user_id)
                if not sess or session_expired(sess, now):
                    sessions.pop(user_id, None)
                    await reply_to_line(reply_token, EXPIRED_MSG)
                    continue
                if action == "intake_type" and sess["step"] == "await_type":
                    sess["type"] = TYPE_LABELS.get(pb.get("value", ""), "不明")
                    sess["step"] = "await_scale"
                    sess["updated_at"] = now
                    await reply_to_line(reply_token, ASK_SCALE, choice_items("intake_scale", SCALE_LABELS))
                elif action == "intake_scale" and sess["step"] == "await_scale":
                    sess["scale"] = SCALE_LABELS.get(pb.get("value", ""), "不明")
                    sess["step"] = "await_timing"
                    sess["updated_at"] = now
                    await reply_to_line(reply_token, ASK_TIMING, choice_items("intake_timing", TIMING_LABELS))
                elif action == "intake_timing" and sess["step"] == "await_timing":
                    sess["timing"] = TIMING_LABELS.get(pb.get("value", ""), "不明")
                    notice = build_staff_notice(sess, now)
                    logger.info(json.dumps({
                        "type": "intake", "timestamp": now.isoformat(), "user_id": user_id,
                        "contact": sess["contact"], "gomi_type": sess["type"],
                        "scale": sess["scale"], "timing": sess["timing"],
                    }, ensure_ascii=False))
                    if STAFF_USER_IDS:
                        await push_to_line(STAFF_USER_IDS, notice)
                    else:
                        logger.error("STAFF_USER_IDS 未設定のため受付通知を送信できません")
                    await reply_to_line(reply_token, build_user_complete(sess, now))
                    sessions.pop(user_id, None)
                else:
                    await reply_to_line(reply_token, DURING_INTAKE)

    return PlainTextResponse("OK")
