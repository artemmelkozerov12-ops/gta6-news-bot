"""Память бота (state.json) и дедупликация: по URL, по хэшу заголовка, по смыслу."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from .fetch import Item, normalize_url
from .util import ROOT, utcnow

STATE_PATH = ROOT / "state.json"

STOP_WORDS = set("""
a an the of to in on for and or is are was with at by from as it its this that be new now
gta grand theft auto vi 6 rockstar games game report reportedly says said
в на и с по о об для за из к от до что как это его её их не но а
гта игра игры рокстар новый новая новые
""".split())


def url_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def norm_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def title_key(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]


def similarity(a: str, b: str) -> float:
    """Схожесть заголовков 0..1: максимум из совпадения слов (Жаккар) и посимвольного сравнения."""
    na, nb = norm_title(a), norm_title(b)
    ta = {w for w in na.split() if w not in STOP_WORDS and len(w) > 2}
    tb = {w for w in nb.split() if w not in STOP_WORDS and len(w) > 2}
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    sm = SequenceMatcher(None, na, nb, autojunk=False)
    if sm.real_quick_ratio() <= jac or sm.quick_ratio() <= jac:  # дешёвая верхняя оценка — точный расчёт не нужен
        return jac
    return max(jac, sm.ratio())


class State:
    """
    seen:    {url_key: iso}            — какие ссылки уже обработаны
    titles:  {title_key: iso}          — какие заголовки уже обработаны
    recent:  [{title, source, trust, status, ts, card_title}]  — последние 48 ч, для смысловых дублей
    queue:   [{item, result, queued_at}]  — одобрено, ждёт своего часа (ночь/лимиты)
    posts:   [iso]                     — когда публиковали (для лимитов)
    """

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.seen: dict[str, str] = data.get("seen", {})
        self.titles: dict[str, str] = data.get("titles", {})
        self.recent: list[dict] = data.get("recent", [])
        self.queue: list[dict] = data.get("queue", [])
        self.posts: list[str] = data.get("posts", [])
        self.tg_offset: int = int(data.get("tg_offset", 0))  # последнее обработанное обновление Telegram
        self.channel: dict = data.get("channel", {})          # канал, куда бота добавили админом
        self.drafts: list[str] = data.get("drafts", [])       # когда слали черновики (лимит в сутки)
        self.slots_sent: dict = data.get("slots_sent", {})    # {"2026-11-19:утро": iso} — слот закрыт
        self.urgent: list[str] = data.get("urgent", [])       # когда слали срочные черновики
        self.clips: list[dict] = data.get("clips", [])        # видео-черновики: ждут ✅ / одобрены
        self.clip_drafts: list[str] = data.get("clip_drafts", [])  # когда слали видео-черновики
        # какие клипы уже смотрели/слали (автор, запись стрима, секунда момента) — чтобы не было повторов
        self.clip_history: list[dict] = data.get("clip_history", [])

    # ----- файл -----
    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "State":
        if path.exists() and path.stat().st_size:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        return cls()

    def save(self, path: Path = STATE_PATH) -> None:
        data = {"seen": self.seen, "titles": self.titles, "recent": self.recent,
                "queue": self.queue, "posts": self.posts, "tg_offset": self.tg_offset,
                "channel": self.channel, "drafts": self.drafts,
                "slots_sent": self.slots_sent, "urgent": self.urgent,
                "clips": self.clips, "clip_drafts": self.clip_drafts, "clip_history": self.clip_history}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def prune(self, cfg: dict) -> None:
        d = cfg["dedupe"]
        old = (utcnow() - timedelta(days=d["forget_after_days"])).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= old}
        self.titles = {k: v for k, v in self.titles.items() if v >= old}
        window = (utcnow() - timedelta(hours=d["window_hours"])).isoformat()
        self.recent = [r for r in self.recent if r["ts"] >= window]
        day_ago = (utcnow() - timedelta(days=2)).isoformat()
        self.posts = [p for p in self.posts if p >= day_ago]
        self.drafts = [p for p in self.drafts if p >= day_ago]
        self.urgent = [p for p in self.urgent if p >= day_ago]
        self.clip_drafts = [p for p in self.clip_drafts if p >= day_ago]
        self.clip_history = [h for h in self.clip_history if h["ts"] >= day_ago]
        self.slots_sent = {k: v for k, v in self.slots_sent.items() if v >= day_ago}

    # ----- дубли -----
    def is_seen(self, item: Item) -> bool:
        return url_key(item.link) in self.seen or title_key(item.title) in self.titles

    def mark_seen(self, item: Item) -> None:
        now = utcnow().isoformat()
        self.seen[url_key(item.link)] = now
        self.titles[title_key(item.title)] = now

    def remember(self, item: Item, status: str, card_title: str = "") -> None:
        self.recent.append({
            "title": item.title, "source": item.source, "trust": item.trust,
            "status": status, "ts": utcnow().isoformat(), "card_title": card_title,
        })

    def similar_published(self, item: Item, threshold: float) -> dict | None:
        """Похожая новость уже опубликована или стоит в очереди за последние 48 ч?"""
        for r in self.recent:
            if r["status"] in ("published", "queued") and similarity(item.title, r["title"]) >= threshold:
                return r
        return None

    def corroborating(self, item: Item, threshold: float) -> dict | None:
        """Есть ли та же новость от источника уровня official/high/medium (другого)?"""
        for r in self.recent:
            if (r["trust"] != "rumor" and r["source"] != item.source
                    and similarity(item.title, r["title"]) >= threshold):
                return r
        return None


def dedupe_batch(items: list[Item], threshold: float) -> tuple[list[Item], list[tuple[Item, Item]]]:
    """Внутри одной пачки оставить по одной записи на событие (лучшую по доверию)."""
    from .fetch import TRUST_ORDER

    ordered = sorted(items, key=lambda i: (TRUST_ORDER.get(i.trust, 9), -datetime.fromisoformat(i.published).timestamp()))
    kept: list[Item] = []
    dropped: list[tuple[Item, Item]] = []
    keys: set[str] = set()
    for it in ordered:
        k = url_key(it.link)
        if k in keys:
            continue
        twin = next((x for x in kept if similarity(x.title, it.title) >= threshold), None)
        if twin:
            dropped.append((it, twin))
            continue
        keys.add(k)
        kept.append(it)
    return kept, dropped
