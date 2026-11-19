"""Рубрики канала, кроме новостей и видео:
  • «Итоги дня» в 22:00 — главные посты дня строчками со ссылками;
  • опрос / викторина раз в неделю (настоящий опрос Telegram).
Оба сначала приходят тебе в личку черновиком с ✅ / ❌ — как и всё остальное.
Настройки — config.yaml → extras.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .dedupe import State
from .llm import CYRILLIC, LLM, LLMBadAnswer, LLMQuotaExhausted
from .util import Http, log, utcnow

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
KIND_EMOJI = {"video": "🎥", "guide": "🗺", "rumor": "🔥", "confirmed": "📰"}

POLL_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["quiz", "regular"]},
        "question": {"type": "string"},
        "option1": {"type": "string"}, "option2": {"type": "string"},
        "option3": {"type": "string"}, "option4": {"type": "string"},
        "correct": {"type": "integer"},
        "explanation": {"type": "string"},
    },
    "required": ["type", "question", "option1", "option2", "option3", "option4", "correct", "explanation"],
    "additionalProperties": False,
}


def _local(cfg: dict) -> datetime:
    return utcnow().astimezone(ZoneInfo(cfg["channel"]["timezone"]))


def _post_link(state: State, msg_id: int) -> str:
    ch = state.channel
    if ch.get("username"):
        return f"https://t.me/{ch['username']}/{msg_id}"
    cid = str(ch.get("id", ""))
    return f"https://t.me/c/{cid.removeprefix('-100')}/{msg_id}" if cid else ""


# ---------- итоги дня ----------
def build_digest(state: State, cfg: dict) -> str | None:
    c = cfg["extras"]["digest"]
    now = _local(cfg)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC")).isoformat()
    today = [p for p in state.published if p["ts"] >= start and p.get("title")]
    if len(today) < int(c.get("min_posts", 3)):
        return None
    best = sorted(today, key=lambda p: -p.get("importance", 0))[: int(c.get("max_items", 6))]
    best.sort(key=lambda p: p["ts"])  # в порядке выхода
    lines = [f"🌙 <b>Итоги дня</b> · {now.day} {MONTHS[now.month - 1]}", ""]
    for p in best:
        title = html.escape(p["title"], quote=False)
        link = _post_link(state, p["msg_id"])
        title = f'<a href="{link}">{title}</a>' if link else title
        lines.append(f"{KIND_EMOJI.get(p.get('kind'), '📰')} {title}")
    lines += ["", c.get("outro", "Что из этого зашло больше всего? 👇")]
    return "\n".join(lines)


# ---------- опрос недели ----------
def _check_poll(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LLMBadAnswer("ответ не объект")
    q = str(data.get("question") or "").strip()
    opts = [str(data.get(f"option{i}") or "").strip() for i in range(1, 5)]
    opts = [o for o in opts if o]
    if not q or len(q) > 250 or not CYRILLIC.search(q):
        raise LLMBadAnswer("вопрос пустой, длинный или не по-русски")
    if len(opts) < 2 or any(len(o) > 90 for o in opts) or len(set(opts)) != len(opts):
        raise LLMBadAnswer("плохие варианты ответа")
    quiz = data.get("type") == "quiz"
    try:
        correct = int(data.get("correct"))
    except (TypeError, ValueError):
        correct = -1
    if quiz and not 0 <= correct < len(opts):
        quiz = False  # непонятно, какой правильный — делаем обычным опросом
    return {"type": "quiz" if quiz else "regular", "question": q, "options": opts,
            "correct": correct if quiz else -1, "explanation": str(data.get("explanation") or "").strip()[:190]}


def build_poll(state: State, cfg: dict, llm: LLM) -> dict | None:
    week_ago = (utcnow() - timedelta(days=7)).isoformat()
    titles = [p["title"] for p in state.published if p["ts"] >= week_ago and p.get("title")]
    titles += [r["title"] for r in state.recent if r.get("status") in ("published", "queued")][-20:]
    user = "Посты и новости канала за неделю:\n" + "\n".join(f"- {t}" for t in titles[-40:] or ["(мало постов)"])
    try:
        data, mid = llm._ask(llm.writer_chain or llm.triage_chain, user, system=cfg["poll_prompt"],
                             schema=POLL_SCHEMA, validator=_check_poll, temperature=0.9)
    except (LLMQuotaExhausted, LLMBadAnswer) as e:
        log(f"⚠️  опрос недели: нейросеть не справилась ({e})")
        return None
    log(f"📊 опрос недели готов [{mid}]: «{data['question'][:70]}»")
    return data


def _poll_preview(p: dict) -> str:
    lines = [f"📊 <b>{'Викторина' if p['type'] == 'quiz' else 'Опрос'} недели</b>", "",
             html.escape(p["question"], quote=False), ""]
    for i, o in enumerate(p["options"]):
        mark = " ✅" if i == p["correct"] else ""
        lines.append(f"{i + 1}) {html.escape(o, quote=False)}{mark}")
    if p["type"] == "quiz" and p.get("explanation"):
        lines += ["", f"💡 {html.escape(p['explanation'], quote=False)}"]
    return "\n".join(lines)


# ---------- черновики и публикация ----------
def _draft(http: Http, state: State, key: str, kind: str, preview: str, payload) -> None:
    from .moderation import _api, admin_id

    markup = {"inline_keyboard": [[{"text": "✅ Опубликовать", "callback_data": f"ok:{key}"},
                                   {"text": "❌ Пропустить", "callback_data": f"no:{key}"}]]}
    try:
        msg = _api(http, "sendMessage", chat_id=admin_id(), text=preview, parse_mode="HTML",
                   disable_web_page_preview=True, reply_markup=markup)
    except Exception as e:
        log(f"⚠️  черновик «{kind}» не отправился ({e})")
        return
    state.extras.append({"key": key, "kind": kind, "payload": payload, "moderation": "pending",
                         "draft_msg_id": msg["message_id"], "queued_at": utcnow().isoformat()})
    state.extras_sent[key] = utcnow().isoformat()
    log(f"📝 черновик «{kind}» тебе в личку")


def send_drafts(http: Http, state: State, cfg: dict, llm: LLM | None) -> None:
    ex = cfg.get("extras", {})
    now = _local(cfg)
    d = ex.get("digest", {})
    key = f"digest-{now:%Y%m%d}"
    if d.get("enabled") and now.hour >= int(d.get("hour", 22)) and key not in state.extras_sent:
        text = build_digest(state, cfg)
        if text:
            _draft(http, state, key, "Итоги дня", text, {"text": text})
        else:
            state.extras_sent[key] = utcnow().isoformat()  # постов мало — сегодня без итогов
            log("🌙 итоги дня: за день вышло мало постов — пропускаю")
    p = ex.get("poll", {})
    key = f"poll-{now:%G-W%V}"
    if (p.get("enabled") and llm is not None and now.isoweekday() == int(p.get("weekday", 6))
            and now.hour >= int(p.get("hour", 18)) and key not in state.extras_sent):
        poll = build_poll(state, cfg, llm)
        state.extras_sent[key] = utcnow().isoformat()  # одна попытка в неделю, без бесконечных повторов
        if poll:
            _draft(http, state, key, "Опрос недели", _poll_preview(poll), poll)


def publish_ready(http: Http, state: State, cfg: dict) -> int:
    from .moderation import _api, channel_id, mark_published
    from .publish import can_post_now

    q = next((q for q in state.extras if q.get("moderation") == "approved"), None)
    if q is None:
        return 0
    can, why = can_post_now(state, cfg, ignore_quiet_hours=True)
    chat = channel_id(state)
    if not can or not chat:
        return 0
    try:
        if q["key"].startswith("digest"):
            msg = _api(http, "sendMessage", chat_id=chat, text=q["payload"]["text"], parse_mode="HTML",
                       disable_web_page_preview=True)
        else:
            p = q["payload"]
            params = {"chat_id": chat, "question": p["question"], "options": [{"text": o} for o in p["options"]],
                      "is_anonymous": True, "type": p["type"]}
            if p["type"] == "quiz":
                params["correct_option_id"] = p["correct"]
                if p.get("explanation"):
                    params["explanation"] = p["explanation"]
            msg = _api(http, "sendPoll", **params)
    except Exception as e:
        q["attempts"] = q.get("attempts", 0) + 1
        log(f"❌ «{q['kind']}» не отправилось в канал ({e}), попытка {q['attempts']}")
        if q["attempts"] >= 3:
            state.extras.remove(q)
        return 0
    state.extras.remove(q)
    state.posts.append(utcnow().isoformat())
    mark_published(http, q)
    log(f"📣 «{q['kind']}» в канале (message_id={msg['message_id']})")
    return 1


def expire(http: Http | None, state: State, cfg: dict) -> None:
    from .moderation import mark_expired

    border = (utcnow() - timedelta(hours=float(cfg.get("extras", {}).get("max_age_hours", 6)))).isoformat()
    for q in [q for q in state.extras if q["queued_at"] < border]:
        if http is not None:
            mark_expired(http, q)
    state.extras = [q for q in state.extras if q["queued_at"] >= border and q.get("moderation") != "rejected"]
