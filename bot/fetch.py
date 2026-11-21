"""Сбор новостей: RSS → записи → фильтр «про GTA 6» → текст статьи."""
from __future__ import annotations

import calendar
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from .util import Http, log, utcnow

TRUST_ORDER = {"official": 0, "high": 1, "medium": 2, "rumor": 3}


@dataclass
class Item:
    title: str
    link: str
    summary: str
    published: str          # ISO-время в UTC
    source: str             # имя источника из config.yaml
    site_name: str          # что писать в «Источник: …»
    trust: str
    text: str = ""          # полный текст статьи (если удалось скачать)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(**{k: d.get(k, "" if k != "extra" else {}) for k in cls.__dataclass_fields__})


# ---------- ссылки ----------
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|mc_|ref$|ref_src$|src$|cmpid$|ocid$|taid$|guccounter$|soc_)", re.I)


def normalize_url(url: str) -> str:
    """Один и тот же материал → одна и та же строка (без utm-меток, www, якорей, хвостового /)."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = p.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


# ---------- текст ----------
def strip_html(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class _ParagraphParser(HTMLParser):
    """Собирает текст из <p>, <h2>, <h3>, <li> — грубо, но без лишних зависимостей."""

    KEEP = {"p", "h2", "h3", "li", "blockquote"}
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.meta: dict[str, str] = {}
        self._buf: list[str] = []
        self._keep_depth = 0
        self._skip_depth = 0
        self.in_article = 0
        self.article_blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:description", "description", "og:site_name", "og:image", "twitter:image") and a.get("content"):
                self.meta.setdefault(key, a["content"])
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "article":
            self.in_article += 1
        if tag in self.KEEP and not self._skip_depth:
            self._keep_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.KEEP and self._keep_depth:
            self._keep_depth -= 1
            if not self._keep_depth:
                block = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                self._buf = []
                if len(block) > 40:
                    self.blocks.append(block)
                    if self.in_article:
                        self.article_blocks.append(block)
        if tag == "article" and self.in_article:
            self.in_article -= 1

    def handle_data(self, data):
        if self._keep_depth and not self._skip_depth:
            self._buf.append(data)


NO_ARTICLE_HOSTS = ("news.google.com", "reddit.com", "rockstargames.com")


def fetch_article_text(http: Http, item: Item, max_chars: int) -> str:
    """Скачать статью и вытащить основной текст. Если не вышло — вернуть текст из RSS."""
    fallback = item.summary
    host = urlsplit(item.link).netloc.lower()
    if any(h in host for h in NO_ARTICLE_HOSTS):
        return fallback[:max_chars]
    try:
        r = http.get(item.link)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return fallback[:max_chars]
        p = _ParagraphParser()
        p.feed(r.text)
        blocks = p.article_blocks if sum(map(len, p.article_blocks)) > 400 else p.blocks
        text = "\n".join(blocks).strip()
        if len(text) < len(fallback):
            text = p.meta.get("og:description") or p.meta.get("description") or fallback
        img = p.meta.get("og:image") or p.meta.get("twitter:image")
        if img and not item.extra.get("image") and img.startswith("http"):
            item.extra["image"] = img  # обложка статьи — для карточки
        return text[:max_chars]
    except Exception as e:
        log(f"   текст статьи не скачался ({type(e).__name__}), беру описание из RSS")
        return fallback[:max_chars]


# ---------- RSS ----------
def _entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def _entry_image(e) -> str:
    """Картинка записи RSS: media:content / media:thumbnail / enclosure / первая <img> в описании."""
    for m in (e.get("media_content") or []):
        if m.get("url") and (m.get("medium") == "image" or "image" in (m.get("type") or "") or not m.get("type")):
            return m["url"]
    for m in (e.get("media_thumbnail") or []):
        if m.get("url"):
            return m["url"]
    for enc in (e.get("enclosures") or []):
        if "image" in (enc.get("type") or "") and enc.get("href"):
            return enc["href"]
    raw = e.get("summary", "") or ""
    if e.get("content"):
        raw += e["content"][0].get("value", "")
    m = re.search(r'<img[^>]+src="(https?://[^"]+)"', raw)
    return m.group(1) if m else ""


def parse_feed(content: bytes, src: dict) -> list[Item]:
    feed = feedparser.parse(content)
    items: list[Item] = []
    for e in feed.entries:
        title = strip_html(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        site_name = src.get("site_name") or src["name"]
        source_info = e.get("source") or {}
        if src.get("strip_title_suffix"):
            # Google News: «Заголовок - Издание»
            publisher = source_info.get("title") or ""
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3].strip()
            elif " - " in title:
                title, _, publisher = title.rpartition(" - ")
            if publisher and not src.get("site_name"):
                site_name = publisher
        summary = strip_html(e.get("summary", "") or "")
        if not summary and e.get("content"):
            summary = strip_html(e["content"][0].get("value", ""))
        when = _entry_time(e) or utcnow()
        image = _entry_image(e)
        items.append(Item(
            title=title, link=link, summary=summary[:3000], published=when.isoformat(),
            source=src["name"], site_name=site_name, trust=src["trust"],
            extra={"image": image} if image else {},
        ))
    return items


def fetch_all(cfg: dict, http: Http) -> tuple[list[Item], list[dict]]:
    """Скачать все включённые источники. Возвращает записи и отчёт по источникам."""
    items: list[Item] = []
    report: list[dict] = []
    for src in cfg["sources"]:
        if src.get("enabled", True) is False:
            report.append({"source": src["name"], "status": "выключен", "entries": 0})
            continue
        try:
            r = http.get(src["url"], headers={"Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.5"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            got = parse_feed(r.content, src)
            if not got:
                raise RuntimeError("0 записей")
            items.extend(got)
            report.append({"source": src["name"], "status": "ok", "entries": len(got)})
        except Exception as e:
            report.append({"source": src["name"], "status": f"ошибка: {type(e).__name__} {e}"[:120], "entries": 0})
            log(f"⚠️  {src['name']}: {type(e).__name__} {e}")
    return items, report


FOREIGN_SCRIPT = re.compile(r"[֐-ۿ฀-๿぀-ヿ㐀-鿿가-힯]")


def keyword_filter(items: list[Item], patterns: list[str], title_only: bool = False) -> list[Item]:
    """Про GTA 6 + заголовок на латинице/кириллице (Google News иногда отдаёт корейские, японские и т.п. версии).
    title_only=True — ключевое слово обязано быть в заголовке (экономит запросы к нейросети:
    отсекает статьи, где GTA 6 упомянута мимоходом в описании)."""
    rx = re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
    return [it for it in items
            if (rx.search(it.title) or (not title_only and rx.search(it.summary)))
            and not FOREIGN_SCRIPT.search(it.title)]


def fresh_only(items: list[Item], max_age_hours: float) -> tuple[list[Item], list[Item]]:
    border = utcnow() - timedelta(hours=max_age_hours)
    fresh, old = [], []
    for it in items:
        (fresh if datetime.fromisoformat(it.published) >= border else old).append(it)
    return fresh, old
