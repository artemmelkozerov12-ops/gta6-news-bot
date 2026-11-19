"""Правила публикации, лимиты и отправка в Telegram (sendPhoto)."""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .dedupe import State
from .fetch import Item
from .llm import Verdict
from .util import Http, utcnow

CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото


# ---------- правила ----------
def decide(item: Item, v: Verdict, cfg: dict, corroborated_by_text: bool) -> tuple[bool, str]:
    """Публикуем или нет, и почему. Возвращает (ok, причина)."""
    r = cfg["rules"]
    if v.type in r["never_publish_types"]:
        return False, f"type={v.type} — не публикуем"
    if v.duplicate:
        return False, "нейросеть: эта новость уже была в канале"
    if v.type == "confirmed" and v.importance < r["confirmed_min_importance"]:
        return False, f"confirmed, но importance {v.importance} < {r['confirmed_min_importance']}"
    if v.type == "rumor" and v.importance < r["rumor_min_importance"]:
        return False, f"rumor, но importance {v.importance} < {r['rumor_min_importance']}"
    if item.trust == "rumor" and v.type in r["corroboration_required_for"]:
        if not (v.corroborated or corroborated_by_text):
            return False, "источник уровня rumor без второго подтверждения"
    return True, f"{v.type}, importance {v.importance}"


# ---------- лимиты ----------
def _local(cfg: dict) -> datetime:
    return utcnow().astimezone(ZoneInfo(cfg["channel"]["timezone"]))


def is_quiet_hours(cfg: dict) -> bool:
    start, end = cfg["limits"]["quiet_hours"]
    return start <= _local(cfg).hour < end


def can_post_now(state: State, cfg: dict, ignore_quiet_hours: bool = False) -> tuple[bool, str]:
    lim = cfg["limits"]
    if not ignore_quiet_hours and is_quiet_hours(cfg):
        return False, f"ночь ({lim['quiet_hours'][0]:02d}:00–{lim['quiet_hours'][1]:02d}:00 по Киеву), копим в очередь"
    now = utcnow()
    day = [p for p in state.posts if datetime.fromisoformat(p) > now - timedelta(hours=24)]
    if len(day) >= lim["max_posts_per_day"]:
        return False, f"лимит {lim['max_posts_per_day']} постов за 24 ч"
    if state.posts:
        last = max(datetime.fromisoformat(p) for p in state.posts)
        gap = (now - last).total_seconds() / 60
        if gap < lim["min_minutes_between_posts"]:
            return False, f"с прошлого поста прошло {gap:.0f} мин < {lim['min_minutes_between_posts']}"
    return True, "можно"


# ---------- текст поста ----------
def format_post(post: str) -> str:
    """Экранировать HTML, заголовок (первая строка, после пометки «Слух» — вторая) сделать жирным,
    после заголовка — пустая строка."""
    lines = [l.strip() for l in post.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    h = 1 if lines[0].startswith("🔥") and "лух" in lines[0] and len(lines) > 2 else 0
    out = [html.escape(l, quote=False) for l in lines]
    out[h] = f"<b>{out[h]}</b>"
    head, rest = out[: h + 1], out[h + 1:]
    return "\n".join(head) + ("\n\n" + "\n".join(rest) if rest else "")


def headline(text: str, limit: int = 90) -> str:
    """Первая содержательная строка поста (без пометки «Слух» и эмодзи в начале) — для «Итогов дня»."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if lines and lines[0].startswith("🔥") and "лух" in lines[0] and len(lines) > 1:
        lines = lines[1:]
    line = lines[0] if lines else ""
    line = re.sub(r"^[^\wА-Яа-яЁё«\"(]+", "", line).strip()
    line = re.sub(r"^Гайд:\s*", "", line)
    line = line[:1].upper() + line[1:]
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def build_caption(item: Item, v: Verdict, reserve: int = 0) -> str:
    """reserve — сколько символов подписи уже занято (шапка черновика)."""
    body = format_post(v.post)
    source = f'Источник: <a href="{html.escape(item.link, quote=True)}">{html.escape(item.site_name, quote=False)}</a>'
    caption = f"{body}\n\n{source}"
    if len(caption) > CAPTION_LIMIT - reserve:  # подстраховка: режем текст по предложению
        room = CAPTION_LIMIT - reserve - len(source) - 3
        cut = body[:room]
        cut = cut[: max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? ")) + 1] or cut[: room - 1] + "…"
        caption = f"{cut}\n\n{source}"
    return caption


# ---------- Telegram ----------
class TelegramError(Exception):
    pass


def send_photo(http: Http, photo_png: bytes, caption: str, chat_id: str = "") -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    if not token or not chat_id:
        raise TelegramError("Нет TELEGRAM_BOT_TOKEN или канала (добавь бота в канал админом)")
    r = http.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("card.png", photo_png, "image/png")},
    )
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(f"HTTP {r.status_code}, не JSON")
    if not data.get("ok"):
        raise TelegramError(f"HTTP {r.status_code}: {data.get('description', '')}")
    return int(data["result"]["message_id"])
