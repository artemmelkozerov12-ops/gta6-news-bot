"""Видео-посты в стиле «человек показывает прикольный момент из игры».

Цепочка:
  Twitch (клипы по игре GTA VI) + YouTube (короткие ролики по запросам)
  → самые «залетающие» (просмотры за час) → скачать (yt-dlp)
  → Gemini СМОТРИТ видео и пишет короткую живую подпись (или говорит «скучно / не про GTA 6»)
  → тебе в личку видео с кнопками ✅ / ❌ → ✅ → в канал (тем же файлом, без повторного скачивания).

Ключи (бесплатные): TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, YOUTUBE_API_KEY.
Нет ключа — этот источник просто пропускается. Все числа — в config.yaml → clips.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .dedupe import State, url_key
from .llm import CYRILLIC, LLM, LLMBadAnswer, LLMQuotaExhausted, limit_emoji
from .util import Http, log, utcnow

CAPTION_LIMIT = 1024


@dataclass
class Clip:
    source: str        # twitch / youtube
    url: str
    title: str
    author: str        # стример / канал
    views: int
    duration: float    # секунды
    created: str       # ISO UTC
    lang: str = ""
    description: str = ""
    vod_id: str = ""       # Twitch: запись стрима, из которой нарезан клип
    vod_offset: float = -1 # Twitch: секунда в этой записи (у нарезок одного момента почти одинаковая)

    @property
    def key(self) -> str:
        return url_key(self.url)

    def hours_old(self) -> float:
        return max(0.0, (utcnow() - datetime.fromisoformat(self.created)).total_seconds() / 3600)

    def heat(self) -> float:
        """«Как быстро залетает»: просмотры в час (свежие клипы не проигрывают старым)."""
        return self.views / (self.hours_old() + 1.0)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> str:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()


# ---------- 1. Twitch ----------
def fetch_twitch(http: Http, cfg: dict) -> list[Clip]:
    c = cfg["clips"]["twitch"]
    cid, secret = os.getenv("TWITCH_CLIENT_ID", "").strip(), os.getenv("TWITCH_CLIENT_SECRET", "").strip()
    if not c.get("enabled", True) or not cid or not secret:
        return []
    r = http.post("https://id.twitch.tv/oauth2/token",
                  data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    if r.status_code != 200:
        raise RuntimeError(f"Twitch: не дал токен (HTTP {r.status_code}) — проверь TWITCH_CLIENT_ID/SECRET")
    h = {"Client-Id": cid, "Authorization": f"Bearer {r.json()['access_token']}"}
    games = http.get("https://api.twitch.tv/helix/games", params={"name": c["game"]}, headers=h).json().get("data", [])
    if not games:
        raise RuntimeError(f"Twitch: игра «{c['game']}» не найдена")
    now = utcnow()
    params = {"game_id": games[0]["id"], "first": 100,
              "started_at": _iso(now - timedelta(hours=cfg["clips"]["hours_back"])), "ended_at": _iso(now)}
    data = http.get("https://api.twitch.tv/helix/clips", params=params, headers=h).json().get("data", [])
    langs = set(c.get("languages") or [])
    out = [Clip("twitch", x["url"], x["title"], x["broadcaster_name"], int(x["view_count"]),
                float(x["duration"]), _parse_time(x["created_at"]), x.get("language", ""),
                vod_id=str(x.get("video_id") or ""),
                vod_offset=float(x["vod_offset"]) if x.get("vod_offset") is not None else -1)
           for x in data if not langs or x.get("language") in langs]
    return [x for x in out if x.views >= int(c.get("min_views", 0))]


# ---------- 2. YouTube ----------
def _iso_duration(s: str) -> float:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, h, mi, se = (int(g or 0) for g in m.groups())
    return float(((d * 24 + h) * 60 + mi) * 60 + se)


def fetch_youtube(http: Http, cfg: dict) -> list[Clip]:
    """Поиск стоит 100 «единиц» из 10 000 бесплатных в сутки → запросов в config мало и хватает с запасом."""
    c = cfg["clips"]["youtube"]
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not c.get("enabled", True) or not key:
        return []
    since = _iso(utcnow() - timedelta(hours=cfg["clips"]["hours_back"]))
    ids: list[str] = []
    for q in c["queries"]:
        r = http.get("https://www.googleapis.com/youtube/v3/search", params={
            "key": key, "part": "snippet", "q": q, "type": "video", "order": "viewCount",
            "publishedAfter": since, "videoDuration": "short", "maxResults": int(c.get("max_results", 25)),
            **({"relevanceLanguage": c["language"]} if c.get("language") else {})})
        if r.status_code != 200:
            raise RuntimeError(f"YouTube: HTTP {r.status_code} {r.text[:150]}")
        ids += [x["id"]["videoId"] for x in r.json().get("items", []) if x["id"]["videoId"] not in ids]
    if not ids:
        return []
    out: list[Clip] = []
    for i in range(0, len(ids), 50):  # подробности: 1 единица за пачку до 50 видео
        r = http.get("https://www.googleapis.com/youtube/v3/videos", params={
            "key": key, "part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50])})
        for x in r.json().get("items", []):
            sn = x["snippet"]
            out.append(Clip("youtube", f"https://www.youtube.com/shorts/{x['id']}", sn["title"], sn["channelTitle"],
                            int(x.get("statistics", {}).get("viewCount", 0)),
                            _iso_duration(x["contentDetails"].get("duration", "")), _parse_time(sn["publishedAt"]),
                            sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "",
                            (sn.get("description") or "")[:500]))
    return [x for x in out if x.views >= int(c.get("min_views", 0))]


def collect(http: Http, cfg: dict, state: State) -> list[Clip]:
    """Все новые клипы, лучшие сверху. Один стример — один клип (на один момент делают по 5 клипов)."""
    found: list[Clip] = []
    for name, fn in (("Twitch", fetch_twitch), ("YouTube", fetch_youtube)):
        try:
            got = fn(http, cfg)
            log(f"🎥 {name}: {len(got)} клипов")
            found += got
        except Exception as e:
            log(f"⚠️  {name}: {e}")
    c = cfg["clips"]
    fresh = [x for x in found if x.key not in state.seen and x.duration <= float(c["max_duration"])]
    fresh.sort(key=lambda x: x.heat(), reverse=True)
    picked: list[Clip] = []
    skipped = 0
    for x in fresh:
        why = repeat_reason(x, state.clip_history + [_history_entry(p) for p in picked], cfg)
        if why:
            skipped += 1
            continue
        picked.append(x)
    if skipped:
        log(f"🎥 отброшено повторов (тот же момент / тот же стример / похожее название): {skipped}")
    return picked


def _history_entry(clip: Clip, drafted: bool = True) -> dict:
    return {"author": f"{clip.source}:{clip.author.lower()}", "vod": clip.vod_id, "offset": clip.vod_offset,
            "title": clip.title, "drafted": drafted, "ts": utcnow().isoformat()}


def repeat_reason(clip: Clip, history: list[dict], cfg: dict) -> str:
    """Это повтор? Один момент стрима нарезают десятки зрителей (10 с, 30 с, минута) — берём один раз."""
    from .dedupe import similarity

    c = cfg["clips"]
    author = f"{clip.source}:{clip.author.lower()}"
    cooldown = (utcnow() - timedelta(hours=float(c.get("author_cooldown_hours", 12)))).isoformat()
    window = float(c.get("moment_window_seconds", 180))
    for h in history:
        if h["author"] == author and h.get("drafted") and h["ts"] >= cooldown:
            return "этот стример уже был недавно"
        if clip.vod_id and h.get("vod") == clip.vod_id and clip.vod_offset >= 0 and h.get("offset", -1) >= 0                 and abs(clip.vod_offset - h["offset"]) <= window:
            return "тот же момент стрима"
        if h["author"] == author and similarity(clip.title, h["title"]) >= 0.6:
            return "похожее название у того же автора"
        if similarity(clip.title, h["title"]) >= 0.85 and len(clip.title) > 15:
            return "почти то же название"
    return ""


# ---------- 3. скачать ----------
def _ffmpeg() -> str:
    """ffmpeg склеивает видео и звук. В GitHub Actions он есть; на компьютере — из пакета imageio-ffmpeg."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def download(clip: Clip, folder: Path, max_mb: float) -> tuple[Path, dict] | None:
    """mp4 до 720p и до max_mb (лимит Telegram для бота — 50 МБ). None — не вышло."""
    import yt_dlp

    ffmpeg = _ffmpeg()
    # H.264 (avc1) — играется в Telegram на любом телефоне. Видео и звук YouTube склеивает ffmpeg.
    fmt = ("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
           "b[height<=720][ext=mp4]/b[ext=mp4]") if ffmpeg else "b[height<=720][ext=mp4]/b[ext=mp4]"
    opts = {"outtmpl": str(folder / "%(id)s.%(ext)s"), "format": fmt, "merge_output_format": "mp4",
            **({"ffmpeg_location": ffmpeg} if ffmpeg else {}),
            "max_filesize": int(max_mb * 1024 * 1024), "quiet": True, "no_warnings": True,
            "noplaylist": True, "socket_timeout": 30, "retries": 2, "noprogress": True}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(clip.url, download=True)
            path = Path(y.prepare_filename(info)).with_suffix(".mp4")
    except Exception as e:
        log(f"   ⚠️  не скачалось ({str(e).splitlines()[0][:150]})")
        return None
    if not path.exists() or path.stat().st_size > max_mb * 1024 * 1024:
        log("   ⚠️  файла нет или он больше лимита")
        return None
    meta = {"width": info.get("width"), "height": info.get("height"), "duration": int(info.get("duration") or clip.duration)}
    return path, meta


# ---------- 4. подпись ----------
def _meta_text(clip: Clip) -> str:
    where = "клип со стрима на Twitch" if clip.source == "twitch" else "ролик на YouTube"
    lines = [f"Это {where}.", f"Название: {clip.title}", f"Автор: {clip.author}",
             f"Просмотров: {clip.views}, длина: {clip.duration:.0f} с, вышел {clip.hours_old():.0f} ч назад"]
    if clip.description:
        lines.append(f"Описание: {clip.description}")
    return "\n".join(lines)


def _check(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LLMBadAnswer("ответ не объект")
    try:
        data["interest"] = int(data.get("interest"))
    except (TypeError, ValueError):
        raise LLMBadAnswer("interest не число")
    cap = str(data.get("caption") or "").strip()
    cap = " ".join(w for w in cap.split(" ") if not w.startswith("#"))
    cap = limit_emoji(cap, 5)
    if data.get("about_gta6") and data["interest"] >= 1:
        if not cap or not CYRILLIC.search(cap):
            raise LLMBadAnswer("подпись пустая или не на русском")
        if len(cap) > 700:
            raise LLMBadAnswer(f"подпись длинная ({len(cap)})")
    data["caption"] = cap
    return data


def format_caption(clip: Clip, text: str, reserve: int = 0) -> str:
    """Подпись в канал: текст + кто снял (уважаем авторов клипов)."""
    where = "Twitch" if clip.source == "twitch" else "YouTube"
    credit = f'🎥 <a href="{html.escape(clip.url, quote=True)}">{html.escape(clip.author)} · {where}</a>'
    body = html.escape(text.strip(), quote=False)
    room = CAPTION_LIMIT - reserve - len(credit) - 2
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{body}\n\n{credit}"


# ---------- 5. расписание видео-черновиков ----------
def _local(cfg: dict) -> datetime:
    return utcnow().astimezone(ZoneInfo(cfg["channel"]["timezone"]))


def time_for_clip(state: State, cfg: dict) -> tuple[bool, str]:
    c = cfg["clips"]
    start, end = c["hours"]
    if not start <= _local(cfg).hour < end:
        return False, f"видео шлём с {start}:00 до {end}:00"
    day_ago = (utcnow() - timedelta(hours=24)).isoformat()
    sent = [t for t in state.clip_drafts if t >= day_ago]
    if len(sent) >= int(c["per_day"]):
        return False, f"уже {len(sent)} видео за сутки"
    if sent and (utcnow() - datetime.fromisoformat(max(sent))).total_seconds() < float(c["min_gap_minutes"]) * 60:
        return False, f"с прошлого видео меньше {c['min_gap_minutes']} мин"
    return True, ""


# ---------- 6. подготовить: скачать + подпись ----------
def prepare(clip: Clip, llm: LLM, cfg: dict, state: State, folder: Path) -> dict | None:
    """Скачать и подписать один клип. None — не подошёл (скучно, не про GTA 6, не скачался)."""
    c = cfg["clips"]
    state.seen[clip.key] = utcnow().isoformat()  # второй раз не смотрим, что бы ни решили
    state.clip_history.append(_history_entry(clip, drafted=False))  # и этот момент тоже больше не берём
    log(f"🎬 [{clip.source}] {clip.author}: «{clip.title[:70]}» ({clip.views} просм., {clip.duration:.0f} с)")
    got = download(clip, folder, float(c["max_file_mb"]))
    if not got:
        return None
    path, meta = got
    video = path.read_bytes()
    try:
        data, model, watched = llm.caption_clip(
            video if len(video) <= float(c.get("inline_video_mb", 19)) * 1024 * 1024 else None,
            _meta_text(clip), cfg["clip_prompt"], _check)
    except (LLMQuotaExhausted, LLMBadAnswer) as e:
        log(f"   ⏭  нейросеть не подписала ({e})")
        return None
    log(f"   → интерес {data['interest']}/10, {'про GTA 6' if data['about_gta6'] else 'НЕ про GTA 6'}"
        f"{', спойлер' if data['spoiler'] else ''}{', 18+' if data['nsfw'] else ''} [{model}"
        f"{'' if watched else ', видео не смотрела'}]: {data['reason'][:100]}")
    if not data["about_gta6"] or data["interest"] < int(c["min_interest"]):
        return None
    if c.get("require_watched", True) and not watched:
        log("   ⏭  нейросеть не смогла посмотреть видео — без этого не шлём (require_watched)")
        return None
    return {"clip": clip.to_dict(), "caption": data["caption"], "interest": data["interest"],
            "spoiler": bool(data["spoiler"]), "nsfw": bool(data["nsfw"]), "watched": watched,
            "model": model, "path": str(path), "media": meta, "queued_at": utcnow().isoformat()}


# ---------- 7. Telegram ----------
def _send_video(http: Http, chat_id, video, caption: str, q: dict, markup: dict | None = None) -> dict:
    """video — байты файла или file_id уже загруженного в Telegram видео."""
    from .moderation import _api

    params: dict = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "supports_streaming": True}
    if q.get("spoiler") or q.get("nsfw"):
        params["has_spoiler"] = True  # видео размыто, пока не нажмёшь
    for k in ("width", "height", "duration"):
        if q.get("media", {}).get(k):
            params[k] = int(q["media"][k])
    if markup:
        params["reply_markup"] = markup
    if isinstance(video, bytes):  # загрузка файла: поля формы — строки
        form = {k: json.dumps(v) if isinstance(v, (dict, bool)) else str(v) for k, v in params.items()}
        return _api(http, "sendVideo", files={"video": ("clip.mp4", video, "video/mp4")}, **form)
    return _api(http, "sendVideo", video=video, **params)  # file_id — обычный JSON-запрос


def _file_id(msg: dict) -> str:
    for k in ("video", "animation", "document"):
        if msg.get(k):
            return msg[k]["file_id"]
    return ""


def send_draft(http: Http, state: State, cfg: dict, q: dict) -> bool:
    from .moderation import admin_id

    clip = Clip.from_dict(q["clip"])
    views = f"{clip.views / 1000:.1f}K" if clip.views >= 1000 else str(clip.views)
    head = (f"🎥 <b>Видео</b> · {'Twitch' if clip.source == 'twitch' else 'YouTube'} · {views} просм. · "
            f"{clip.hours_old():.0f} ч назад · {q['interest']}/10"
            f"{'' if q['watched'] else ' · ⚠️ нейросеть не смотрела видео'}\n\n")
    markup = {"inline_keyboard": [[{"text": "✅ Опубликовать", "callback_data": f"ok:{clip.key}"},
                                   {"text": "❌ Пропустить", "callback_data": f"no:{clip.key}"}]]}
    try:
        msg = _send_video(http, admin_id(), Path(q["path"]).read_bytes(),
                          head + format_caption(clip, q["caption"], reserve=len(head)), q, markup)
    except Exception as e:
        log(f"⚠️  видео-черновик не отправился ({e})")
        return False
    q.pop("path", None)
    q["file_id"], q["moderation"], q["draft_msg_id"] = _file_id(msg), "pending", msg["message_id"]
    state.clips.append(q)
    state.clip_drafts.append(utcnow().isoformat())
    state.clip_history.append(_history_entry(clip, drafted=True))  # автора — на перерыв
    log(f"📝 видео тебе в личку: «{clip.title[:70]}» ({q['interest']}/10)")
    return True


def send_drafts(http: Http, state: State, cfg: dict, llm: LLM | None, pool: list[Clip]) -> None:
    """Пора — берём лучший клип из найденных, готовим и шлём. Не подошёл — следующий (до N попыток)."""
    if not cfg.get("clips", {}).get("enabled") or llm is None or not pool:
        return
    ok, _ = time_for_clip(state, cfg)
    if not ok:
        return
    with tempfile.TemporaryDirectory() as tmp:
        for _ in range(int(cfg["clips"].get("tries_per_draft", 3))):
            # пока клип лежал в списке, мог уйти черновик того же стримера/момента — проверяем заново
            while pool and repeat_reason(pool[0], state.clip_history, cfg):
                pool.pop(0)
            if not pool:
                return
            q = prepare(pool.pop(0), llm, cfg, state, Path(tmp))
            if q and send_draft(http, state, cfg, q):
                return


def publish_ready(http: Http, state: State, cfg: dict) -> int:
    """Одобренные видео → в канал. Пауза между постами общая с новостями."""
    from .moderation import channel_id, mark_published
    from .publish import can_post_now

    q = next((q for q in state.clips if q.get("moderation") == "approved"), None)
    if q is None:
        return 0
    can, why = can_post_now(state, cfg, ignore_quiet_hours=True)
    chat = channel_id(state)
    if not can or not chat:
        if not q.get("waiting_logged"):
            log(f"⏳ одобренное видео подождёт: {why or 'канал не подключён'}")
            q["waiting_logged"] = True
        return 0
    clip = Clip.from_dict(q["clip"])
    try:
        msg = _send_video(http, chat, q["file_id"], format_caption(clip, q["caption"]), q)
    except Exception as e:
        q["attempts"] = q.get("attempts", 0) + 1
        log(f"❌ видео не отправилось в канал ({e}), попытка {q['attempts']}")
        if q["attempts"] >= 3:
            state.clips.remove(q)
        return 0
    state.clips.remove(q)
    state.posts.append(utcnow().isoformat())
    mark_published(http, q)
    log(f"📣 видео в канале (message_id={msg['message_id']}): «{clip.title[:70]}»")
    return 1


def expire(http: Http | None, state: State, cfg: dict) -> None:
    from .moderation import mark_expired

    border = (utcnow() - timedelta(hours=float(cfg["clips"]["queue_max_age_hours"]))).isoformat()
    for q in [q for q in state.clips if q["queued_at"] < border]:
        log(f"🗑  видео устарело: «{q['clip']['title'][:70]}»")
        if http is not None:
            mark_expired(http, q)
    state.clips = [q for q in state.clips if q["queued_at"] >= border and q.get("moderation") != "rejected"]
