"""Модерация через бота: черновик → тебе в личку с кнопками → в канал только одобренное.

Сервера нет. Каждый запуск в GitHub Actions после сбора новостей ещё несколько минут «слушает»
Telegram (getUpdates с ожиданием) — поэтому нажатие кнопки срабатывает почти сразу.

Кнопки:  ✅ Опубликовать — в канал сразу (если с прошлого поста прошло меньше 40 мин — чуть позже)
         ❌ Пропустить   — убрать из очереди

Канал бот находит сам: как только его добавят в канал администратором, Telegram присылает
событие my_chat_member — бот запоминает канал (state.channel) и пишет тебе об этом.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .card import card_png
from .dedupe import State, url_key
from .fetch import TRUST_ORDER, Item
from .llm import Verdict
from .publish import build_caption, is_quiet_hours
from .util import Http, log, utcnow

UPDATE_TYPES = ["callback_query", "message", "my_chat_member"]
BUTTONS = {"ok": "approved", "now": "approved", "no": "rejected"}  # "now" — от старых черновиков
STATUS_TEXT = {
    "approved": "✅ Одобрено — публикую",
    "rejected": "❌ Пропущено",
    "published": "📣 Опубликовано в канале",
    "expired": "⌛ Устарело, не публикуем",
}


def admin_id() -> str:
    return os.getenv("TELEGRAM_ADMIN_ID", "").strip()


def enabled(cfg: dict) -> bool:
    """Модерация включена в config.yaml (по умолчанию — да)."""
    return bool(cfg.get("moderation", {}).get("enabled", True))


def channel_id(state: State) -> str:
    """Куда публиковать: канал, найденный ботом сам, иначе TELEGRAM_CHANNEL_ID."""
    if state.channel.get("id"):
        return str(state.channel["id"])
    return os.getenv("TELEGRAM_CHANNEL_ID", "").strip()


def _api(http: Http, method: str, files=None, **params) -> dict | list | bool:
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    kw = {"timeout": float(params["timeout"]) + 15} if method == "getUpdates" and params.get("timeout") else {}
    if files:
        kw = {"timeout": 180.0}  # видео до 50 МБ грузится дольше обычного запроса
    r = http.post(url, data=params, files=files, **kw) if files else http.post(url, json=params, **kw)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"{method}: HTTP {r.status_code}, не JSON")
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', r.status_code)}")
    return data["result"]


def notify(http: Http, text: str) -> None:
    """Короткое сообщение тебе в личку."""
    if not admin_id():
        return
    try:
        _api(http, "sendMessage", chat_id=admin_id(), text=text, parse_mode="HTML",
             disable_web_page_preview=True)
    except Exception as e:
        log(f"   не смог написать в личку: {e}")


def _set_status(http: Http, q: dict, status: str) -> None:
    """Пометить черновик в личке: вместо кнопок — строка со статусом."""
    q["moderation"] = status
    if q.get("draft_msg_id"):
        try:
            _api(http, "editMessageReplyMarkup", chat_id=admin_id(), message_id=q["draft_msg_id"],
                 reply_markup={"inline_keyboard": [[{"text": STATUS_TEXT[status], "callback_data": "noop"}]]})
        except Exception as e:
            log(f"   не смог обновить кнопки черновика: {e}")


# ---------- 1. события Telegram ----------
def poll_updates(http: Http, state: State, wait: int = 0) -> list[dict]:
    """Забрать нажатия кнопок, /start, добавление бота в канал. wait — сколько секунд ждать новых
    событий (long polling). Возвращает отклонённые записи очереди."""
    try:
        updates = _api(http, "getUpdates", offset=state.tg_offset + 1, timeout=wait,
                       allowed_updates=UPDATE_TYPES)
    except Exception as e:
        log(f"⚠️  Telegram: не получил события ({e})")
        return []
    me = admin_id()
    for u in updates:
        state.tg_offset = max(state.tg_offset, u["update_id"])
        if "my_chat_member" in u:
            _on_membership(http, state, u["my_chat_member"])
        elif "message" in u:
            _on_message(http, state, u["message"], me)
        elif "callback_query" in u:
            _on_button(http, state, u["callback_query"], me)
    rejected = [q for q in state.queue if q.get("moderation") == "rejected"]
    state.queue = [q for q in state.queue if q.get("moderation") != "rejected"]
    state.clips = [q for q in state.clips if q.get("moderation") != "rejected"]
    return rejected


def _on_membership(http: Http, state: State, ev: dict) -> None:
    chat = ev.get("chat", {})
    if chat.get("type") != "channel":
        return
    new = ev.get("new_chat_member", {})
    title = html.escape(chat.get("title") or "")
    link = f"@{chat['username']}" if chat.get("username") else "приватный канал"
    if new.get("status") == "administrator" and new.get("can_post_messages"):
        state.channel = {"id": chat["id"], "title": chat.get("title"), "username": chat.get("username")}
        log(f"📡 подключён канал «{chat.get('title')}» ({link}, id {chat['id']})")
        notify(http, f"📡 Подключил канал <b>{title}</b> ({link}). Одобренные посты пойдут туда.")
    elif new.get("status") == "administrator":
        notify(http, f"⚠️ Я админ в канале <b>{title}</b>, но без права «Публикация сообщений». Включи его.")
    elif str(chat.get("id")) == str(state.channel.get("id")) and new.get("status") in ("left", "kicked", "member"):
        state.channel = {}
        log(f"📡 бота убрали из канала «{chat.get('title')}»")
        notify(http, f"⚠️ Меня убрали из админов канала <b>{title}</b> — публиковать некуда.")


def _on_message(http: Http, state: State, msg: dict, me: str) -> None:
    chat = msg.get("chat", {})
    text = (msg.get("text") or "").strip()
    if chat.get("type") != "private":
        return
    if text.startswith("/start"):
        reply = ("Это бот канала «GTA 6 | Горячие новости».\n"
                 f"Твой chat id: <code>{chat['id']}</code>")
        if str(chat["id"]) == me:
            reply += "\nТы модератор — сюда будут приходить черновики постов."
    elif text.startswith("/channel") and str(chat["id"]) == me:
        c = state.channel
        reply = (f"Канал: <b>{html.escape(c.get('title') or '')}</b> (id {c.get('id')})" if c.get("id") else
                 "Канал не подключён. Добавь меня в канал администратором с правом «Публикация сообщений».")
    else:
        return
    try:
        _api(http, "sendMessage", chat_id=chat["id"], text=reply, parse_mode="HTML")
    except Exception:
        pass


def _on_button(http: Http, state: State, cb: dict, me: str) -> None:
    action, _, key = (cb.get("data") or "").partition(":")
    answer = ""
    if str(cb.get("from", {}).get("id")) != me:
        answer = "Это не твоя кнопка 🙂"
    elif action in BUTTONS:
        q = next((q for q in state.queue if url_key(q["item"]["link"]) == key), None)
        if q is None:  # видео-черновик
            q = next((q for q in state.clips if url_key(q["clip"]["url"]) == key), None)
        if q is None:
            answer = "Этого поста уже нет в очереди"
        else:
            status = BUTTONS[action]
            _set_status(http, q, status)
            answer = STATUS_TEXT[status]
            title = (q.get("item") or q.get("clip"))["title"]
            log(f"🧑‍⚖️ модерация: {STATUS_TEXT[status]} — «{title[:70]}»")
    try:
        _api(http, "answerCallbackQuery", callback_query_id=cb["id"], text=answer)
    except Exception:
        pass


# ---------- 2. новые черновики ----------
def drafts_today(state: State) -> int:
    day_ago = (utcnow() - timedelta(hours=24)).isoformat()
    return sum(1 for t in state.drafts if t >= day_ago)


def _local_now(cfg: dict) -> datetime:
    return utcnow().astimezone(ZoneInfo(cfg["channel"]["timezone"]))


def current_slot(cfg: dict) -> dict | None:
    """Слот расписания (утро/обед/вечер), в котором мы сейчас, по времени канала."""
    hour = _local_now(cfg).hour
    for slot in cfg["moderation"].get("slots", []):
        if slot["start"] <= hour < slot["end"]:
            return slot
    return None


def _fresh(q: dict, hours: float) -> bool:
    return datetime.fromisoformat(q["item"]["published"]) >= utcnow() - timedelta(hours=hours)


def _rank(q: dict):
    return (-q["verdict"]["importance"], TRUST_ORDER.get(q["item"]["trust"], 9), q["item"]["published"])


def send_drafts(http: Http, state: State, cfg: dict) -> None:
    """Расписание черновиков:
    • срочное (важность ≥ 9 или официальное от Rockstar/Take-Two ≥ 8) — сразу, но не больше N в сутки;
    • в каждом слоте (утро, обед, вечер) — одна лучшая свежая новость.
    Ночью не шлём ничего."""
    m = cfg["moderation"]
    if is_quiet_hours(cfg) or drafts_today(state) >= int(m.get("max_drafts_per_day", 6)):
        return
    waiting = [q for q in state.queue if not q.get("moderation")]

    # 1) срочное
    u = m.get("urgent", {})
    if len(state.urgent) < int(u.get("per_day", 2)):
        hot = [q for q in waiting if _fresh(q, float(u.get("max_age_hours", 3))) and (
            q["verdict"]["importance"] >= int(u.get("min_importance", 9))
            or (q["item"]["trust"] == "official" and q["verdict"]["importance"] >= int(u.get("official_min_importance", 8))))]
        if hot:
            q = min(hot, key=_rank)
            if _send(http, state, cfg, q, "🚨 <b>СРОЧНО</b>"):
                state.urgent.append(utcnow().isoformat())
                waiting.remove(q)

    # 2) по ритму: одна лучшая свежая новость раз в every_minutes (в часы hours)
    sched = m.get("schedule")
    if sched:
        start, end = sched["hours"]
        if not start <= _local_now(cfg).hour < end:
            return
        last = max(state.drafts) if state.drafts else ""
        if last and utcnow() - datetime.fromisoformat(last) < timedelta(minutes=float(sched["every_minutes"])):
            return
        best = [q for q in waiting if _fresh(q, float(m.get("max_age_hours", 8)))
                and q["verdict"]["importance"] >= int(m.get("min_importance", 7))]
        if best:
            _send(http, state, cfg, min(best, key=_rank), "📰 <b>Новость</b>")
        return

    # 2) слот расписания (старый режим, если schedule не задан)
    slot = current_slot(cfg)
    if not slot:
        return
    key = f"{_local_now(cfg):%Y-%m-%d}:{slot['name']}"
    if key in state.slots_sent:
        return
    best = [q for q in waiting if _fresh(q, float(m.get("max_age_hours", 8)))
            and q["verdict"]["importance"] >= int(m.get("min_importance", 7))]
    if best and _send(http, state, cfg, min(best, key=_rank), f"{slot.get('emoji', '📰')} <b>{slot['name'].capitalize()}</b>"):
        state.slots_sent[key] = utcnow().isoformat()


def _send(http: Http, state: State, cfg: dict, q: dict, label: str) -> bool:
    it, v = Item.from_dict(q["item"]), Verdict.from_dict(q["verdict"])
    key = url_key(it.link)
    age_min = int((utcnow() - datetime.fromisoformat(it.published)).total_seconds() // 60)
    age = f"{age_min} мин назад" if age_min < 120 else f"{age_min // 60} ч назад"
    head = (f"{label} · {'слух' if v.type == 'rumor' else 'подтверждено'} · "
            f"{v.importance}/10 · {html.escape(it.source)} · {age}\n\n")
    caption = head + build_caption(it, v, reserve=len(head))
    markup = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"ok:{key}"},
        {"text": "❌ Пропустить", "callback_data": f"no:{key}"},
    ]]}
    try:
        png = card_png(v.card_title, v.type, cfg, seed=int(key, 16) % 10_000, trust=it.trust)
        msg = _api(http, "sendPhoto", files={"photo": ("card.png", png, "image/png")},
                   chat_id=admin_id(), caption=caption, parse_mode="HTML",
                   reply_markup=json.dumps(markup))
    except Exception as e:
        log(f"⚠️  черновик не отправился ({e})")
        return False
    q["moderation"], q["draft_msg_id"] = "pending", msg["message_id"]
    state.drafts.append(utcnow().isoformat())
    log(f"📝 черновик тебе в личку [{re.sub('<[^>]+>', '', label)}]: «{it.title[:70]}» ({v.importance}/10, {age})")
    return True


def mark_published(http: Http, q: dict) -> None:
    _set_status(http, q, "published")


def mark_expired(http: Http, q: dict) -> None:
    if q.get("moderation") == "pending":
        _set_status(http, q, "expired")
