"""LLM: статья → JSON {type, importance, post, card_title, reason, duplicate, corroborated, ...}.

Всё бесплатно — цепочки моделей у разных провайдеров (config.yaml → llm):
  triage_chain — разбор КАЖДОЙ статьи (много запросов): Groq, потом Gemini Flash-Lite;
  writer_chain — финальный текст только для одобренных новостей (до 12 в сутки): Gemini Flash.
Модель упёрлась в лимит / не отвечает → берём следующую в цепочке. Провайдер без ключа пропускается.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass

import httpx

from .fetch import Item
from .util import log

TYPES = ("confirmed", "rumor", "leak", "skip")
FIELDS = ["type", "importance", "reason", "duplicate", "duplicate_of",
          "corroborated", "corroborated_by", "guide", "card_title", "post"]

# JSON-схема ответа (обычный JSON Schema; для Gemini переводится в его формат)
JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(TYPES)},
        "importance": {"type": "integer"},
        "reason": {"type": "string"},
        "duplicate": {"type": "boolean"},
        "duplicate_of": {"type": "string"},
        "corroborated": {"type": "boolean"},
        "corroborated_by": {"type": "string"},
        "guide": {"type": "boolean"},
        "card_title": {"type": "string"},
        "post": {"type": "string"},
    },
    "required": FIELDS,
    "additionalProperties": False,
}

# Отдельная короткая проверка дублей: «это то же событие, что одна из уже опубликованных новостей?»
SAME_EVENT_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["match", "reason"],
    "additionalProperties": False,
}
SAME_EVENT_PROMPT = (
    "Ты проверяешь дубли в новостном канале. Дана новая новость и пронумерованный список уже "
    "опубликованных. Дубль — это ТО ЖЕ САМОЕ событие или факт (например, «актрису Люсии раскрыли на "
    "номинациях TGA» в разных пересказах разных сайтов), даже если заголовки на разных языках и "
    "акценты чуть отличаются. Не дубль — просто похожая тема с другим событием. "
    "Ответ JSON: match — номер дубля из списка или -1, если дубля нет; reason — одна короткая фраза."
)


# Подпись к видео-клипу (Gemini смотрит само видео)
CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "about_gta6": {"type": "boolean"},
        "interest": {"type": "integer"},
        "spoiler": {"type": "boolean"},
        "nsfw": {"type": "boolean"},
        "repeat": {"type": "boolean"},
        "caption": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["about_gta6", "interest", "spoiler", "nsfw", "repeat", "caption", "reason"],
    "additionalProperties": False,
}


def to_gemini_schema(schema: dict) -> dict:
    """Обычный JSON Schema → формат схемы Gemini (типы заглавными, без additionalProperties)."""
    props = {}
    for k, v in schema["properties"].items():
        props[k] = {"type": v["type"].upper(), **({"enum": v["enum"]} if "enum" in v else {})}
    return {"type": "OBJECT", "properties": props, "required": schema["required"],
            "propertyOrdering": list(schema["properties"])}


EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿〰〽㊗㊙]️?"
)
MAX_EMOJI = 5  # в посте: 1–2 по тексту + голосование реакциями «❤️ — … / 👎 — …»
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
GUIDE_MAX_IMPORTANCE = 6


class LLMQuotaExhausted(Exception):
    """Ни одна модель цепочки не ответила (квота/перегрузка) — дальше в этом запуске не дёргаем."""


class LLMBadAnswer(Exception):
    """Ответ не прошёл валидацию."""


class _Retry(Exception):
    """Временная ошибка (429 в минуту, 5xx) — можно повторить."""

    def __init__(self, msg: str, wait: float | None = None):
        super().__init__(msg)
        self.wait = wait


class _Dead(Exception):
    """Модель на этот запуск больше не трогаем (суточный лимит, нет модели, кривой запрос)."""


@dataclass
class Verdict:
    type: str
    importance: int
    post: str
    card_title: str
    reason: str
    duplicate: bool
    corroborated: bool
    duplicate_of: str = ""
    corroborated_by: str = ""
    guide: bool = False   # рубрика «Гайды и секреты»
    model: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ---------- проверка ответа ----------
def limit_emoji(text: str, max_count: int = 2) -> str:
    """Оставить первые max_count эмодзи, остальные убрать."""
    seen = 0

    def repl(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen <= max_count else ""

    text = EMOJI.sub(repl, text)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"(?m)^[ \t]+", "", text))


def validate(data: dict) -> Verdict:
    if not isinstance(data, dict):
        raise LLMBadAnswer("ответ не объект")
    t = str(data.get("type", "")).strip().lower()
    if t not in TYPES:
        raise LLMBadAnswer(f"неизвестный type={t!r}")
    try:
        imp = int(data.get("importance"))
    except (TypeError, ValueError):
        raise LLMBadAnswer("importance не число")
    if not 0 <= imp <= 10:
        raise LLMBadAnswer(f"importance вне 0..10: {imp}")
    guide = bool(data.get("guide")) and t == "confirmed"
    if guide:
        imp = min(imp, GUIDE_MAX_IMPORTANCE)  # нейросеть завышает гайдам важность — гайды не важнее 6
    post = str(data.get("post") or "").strip()
    card = " ".join(str(data.get("card_title") or "").split()).upper()
    if t in ("confirmed", "rumor"):
        if not post:
            raise LLMBadAnswer("пустой post")
        if not card:
            raise LLMBadAnswer("пустой card_title")
        if len(card) > 70:
            raise LLMBadAnswer("card_title слишком длинный")
        mixed = [w for w in card.split() if LATIN.search(w) and CYRILLIC.search(w)]
        if mixed:  # «LEONIADА» — латиница вперемешку с кириллицей в одном слове
            raise LLMBadAnswer(f"card_title со смесью алфавитов: {mixed}")
        if not CYRILLIC.search(card):
            raise LLMBadAnswer(f"card_title не на русском: {card!r}")
        if len(LATIN.findall(post)) > len(CYRILLIC.findall(post)):  # «Kritiki otdayut 99 ballov…»
            raise LLMBadAnswer("пост транслитом/по-английски, а нужен русский")
        post = " ".join(w for w in post.split(" ") if not w.startswith("#"))  # без хэштегов
        post = limit_emoji(post, MAX_EMOJI)
        if len(post) > 900:
            raise LLMBadAnswer(f"post слишком длинный ({len(post)} симв.)")
    return Verdict(
        type=t, importance=imp, post=post, card_title=card,
        reason=str(data.get("reason") or "").strip(),
        duplicate=bool(data.get("duplicate")), corroborated=bool(data.get("corroborated")),
        duplicate_of=str(data.get("duplicate_of") or "").strip(),
        corroborated_by=str(data.get("corroborated_by") or "").strip(),
        guide=guide,
    )


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def build_user_message(item: Item, recent: list[dict], limit: int) -> str:
    lines = [
        f"Источник: {item.site_name} (лента: {item.source}, уровень доверия: {item.trust})",
        f"Дата публикации (UTC): {item.published[:16].replace('T', ' ')}",
        f"Заголовок: {item.title}",
        f"Ссылка: {item.link}",
        "",
        "Текст статьи:",
        item.text.strip() or "(полный текст недоступен — работай только по заголовку и описанию выше)",
        "",
        "Недавние новости за 48 часов (для полей duplicate и corroborated):",
    ]
    marks = {"published": "[опубликовано]", "queued": "[опубликовано]"}
    shown = recent[-limit:]
    if not shown:
        lines.append("(пусто)")
    for r in shown:
        lines.append(f"- {marks.get(r['status'], '[видели]')} {r['title']} — {r['source']}, доверие: {r['trust']}")
    return "\n".join(lines)


# ---------- провайдеры ----------
class GeminiProvider:
    def __init__(self, key: str, timeout_s: float):
        from google import genai
        from google.genai import types

        self.types = types
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        self.no_thinking: set[str] = set()

    def call(self, spec: dict, system: str, user, temperature: float, schema: dict = JSON_SCHEMA) -> str:
        """user — текст или список частей (например, [видео, текст])."""
        from google.genai import errors

        model = spec["model"]
        kw = {}
        if spec.get("plain"):  # Gemma: без системной инструкции и JSON-режима — всё в одном тексте
            note = f"{system}\n\nОтвет — ТОЛЬКО JSON-объект с полями: {', '.join(schema['properties'])}. Без пояснений."
            contents = [note, *user] if isinstance(user, list) else f"{note}\n\n{user}"
            if spec.get("timeout"):
                kw["http_options"] = self.types.HttpOptions(timeout=int(float(spec["timeout"]) * 1000))
            cfg = self.types.GenerateContentConfig(temperature=temperature, **kw)
            try:
                return self.client.models.generate_content(model=model, contents=contents, config=cfg).text or ""
            except errors.APIError as e:
                code = getattr(e, "code", 0) or 0
                if code == 429 and "PerDay" in str(e):
                    raise _Dead("суточная квота исчерпана")
                if code == 429 or code >= 500:
                    raise _Retry(f"HTTP {code}")
                raise _Dead(f"HTTP {code}: {str(e)[:150]}")
        level = (spec.get("thinking_level") or "").lower()
        if level and model not in self.no_thinking:
            kw["thinking_config"] = self.types.ThinkingConfig(thinking_level=level)
        if spec.get("timeout"):
            kw["http_options"] = self.types.HttpOptions(timeout=int(float(spec["timeout"]) * 1000))
        cfg = self.types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_schema=to_gemini_schema(schema), temperature=temperature, **kw)
        try:
            return self.client.models.generate_content(model=model, contents=user, config=cfg).text or ""
        except errors.APIError as e:
            code, msg = getattr(e, "code", 0) or 0, str(e)
            if code == 400 and "thinking" in msg.lower() and model not in self.no_thinking:
                self.no_thinking.add(model)
                raise _Retry("модель не принимает thinking_level — повторяю без него", wait=0)
            if code == 429 and ("PerDay" in msg or "limit: 0" in msg):
                raise _Dead("суточная квота исчерпана (или модель закрыта на бесплатном тарифе)")
            if code == 404:
                raise _Dead("модель недоступна")
            if code == 429 or code >= 500:
                raise _Retry(f"HTTP {code}")
            raise _Dead(f"HTTP {code}: {msg[:150]}")


class OpenAICompatProvider:
    """Любой сервис с OpenAI-совместимым API: Groq, Mistral, OpenRouter, Cerebras…
    json: schema — строгая JSON-схема; object — просто «верни JSON»; none — только просьба в тексте."""

    def __init__(self, key: str, timeout_s: float, url: str, json_mode: str = "schema"):
        self.url, self.json_mode = url, json_mode
        self.client = httpx.Client(timeout=timeout_s, headers={"Authorization": f"Bearer {key}"})

    def call(self, spec: dict, system: str, user, temperature: float, schema: dict = JSON_SCHEMA) -> str:
        if isinstance(user, list):
            raise _Dead("этот сервис не умеет смотреть видео")
        mode = spec.get("json", self.json_mode)
        if mode != "schema":
            system = f"{system}\n\nОтвет — ТОЛЬКО JSON-объект с полями: {', '.join(schema['properties'])}."
        body = {
            "model": spec["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if mode == "schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "strict": True, "schema": schema}}
        elif mode == "object":
            body["response_format"] = {"type": "json_object"}
        if spec.get("reasoning_effort"):
            body["reasoning_effort"] = spec["reasoning_effort"]
        if spec.get("max_tokens"):
            body["max_tokens"] = int(spec["max_tokens"])
        try:
            r = self.client.post(self.url, json=body)
        except httpx.HTTPError as e:
            raise _Retry(type(e).__name__)
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError, ValueError):
                raise LLMBadAnswer("пустой ответ сервиса")
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        if r.status_code == 429:
            if re.search(r"per day|per-day|daily|\(RPD\)|\(TPD\)|free-models-per-day", msg, re.I):
                raise _Dead("суточный лимит исчерпан")
            wait = r.headers.get("retry-after")
            raise _Retry("HTTP 429 (лимит в минуту)", wait=float(wait) if wait and wait.replace(".", "").isdigit() else None)
        if r.status_code >= 500:
            raise _Retry(f"HTTP {r.status_code}")
        if r.status_code == 400 and "json" in msg.lower() and "valid" in msg.lower():
            raise LLMBadAnswer(f"модель вернула невалидный JSON: {msg[:120]}")
        raise _Dead(f"HTTP {r.status_code}: {msg[:150]}")


DEFAULT_PROVIDERS = {  # если в config.yaml нет раздела llm.providers
    "groq": {"type": "openai", "url": "https://api.groq.com/openai/v1/chat/completions", "key_env": "GROQ_API_KEY"},
    "gemini": {"type": "gemini", "key_env": ["GEMINI_API_KEY"]},
}


def build_providers(cfg: dict, timeout: float) -> tuple[dict, dict]:
    """→ (провайдеры по имени, «семьи»: имя из цепочки → список реальных провайдеров).
    У Gemini может быть несколько ключей (разные проекты Google = отдельные суточные квоты):
    в цепочке пишем provider: gemini, а бот пробует модель сначала на первом ключе, потом на втором."""
    providers, family = {}, {}
    for name, p in (cfg["llm"].get("providers") or DEFAULT_PROVIDERS).items():
        envs = p["key_env"] if isinstance(p["key_env"], list) else [p["key_env"]]
        family[name] = []
        n = 0
        for env in envs:
            key = os.getenv(env, "").strip()
            if not key or any(getattr(x, "_key", None) == key for x in providers.values()):
                continue  # нет ключа или тот же ключ уже подключён
            n += 1
            real = name if n == 1 else f"{name}#{n}"
            if p["type"] == "gemini":
                prov = GeminiProvider(key, timeout)
            else:
                prov = OpenAICompatProvider(key, timeout, p["url"], p.get("json", "schema"))
            prov._key = key
            providers[real] = prov
            family[name].append(real)
    return providers, family


class LLM:
    def __init__(self, cfg: dict):
        c = cfg["llm"]
        self.system_prompt = cfg["system_prompt"]
        self.temperature = float(c.get("temperature", 0.6))
        self.retries = int(cfg.get("http", {}).get("retries", 3))
        self.backoff = float(cfg.get("http", {}).get("backoff_seconds", 2))
        timeout = float(c.get("timeout_seconds", 90))
        self.providers, family = build_providers(cfg, timeout)

        def expand(chain: list[dict]) -> list[dict]:
            return [{**s, "provider": real} for s in chain for real in family.get(s["provider"], [])]

        self.triage_chain = expand(c["triage_chain"])
        self.writer_chain = expand(c.get("writer_chain", []))
        # видео понимает только Gemini
        self.video_chain = [s for s in expand(c.get("video_chain", []))
                            if isinstance(self.providers[s["provider"]], GeminiProvider)]
        log(f"LLM: ключей {len(self.providers)} ({', '.join(self.providers)}); моделей в цепочках: "
            f"разбор {len(self.triage_chain)}, текст {len(self.writer_chain)}, видео {len(self.video_chain)}")
        used = {s["provider"] for s in c["triage_chain"] + c.get("writer_chain", [])}
        missing = sorted(x for x in used if not family.get(x))
        if missing:
            log(f"LLM: нет ключа для {', '.join(missing)} — эти модели пропускаю")
        if not self.triage_chain:
            raise RuntimeError("нет ни одной доступной модели для разбора (проверь GROQ_API_KEY / GEMINI_API_KEY)")
        # модель «отдыхает» до момента time.monotonic(): лимит в минуту — пару минут, перегрузка — 15 мин,
        # суточная квота — 3 ч. Раньше выключали до конца запуска, а запуск идёт 5,5 ч — новости стояли часами.
        self.dead: dict[str, float] = {}
        cd = c.get("cooldown_minutes", {})
        self.cooldown = {"minute": float(cd.get("minute_limit", 2)), "overload": float(cd.get("overload", 15)),
                         "daily": float(cd.get("daily", 180)), "broken": float(cd.get("broken", 360))}

    def _alive(self, mid: str) -> bool:
        return time.monotonic() >= self.dead.get(mid, 0)

    def _bench(self, mid: str, kind: str) -> None:
        mins = self.cooldown[kind]
        self.dead[mid] = time.monotonic() + mins * 60
        log(f"   {mid}: отдыхает {mins:g} мин")

    def writer_alive(self) -> bool:
        return any(self._alive(self._id(s)) for s in self.writer_chain)

    def analyze(self, item: Item, recent: list[dict], recent_limit: int) -> Verdict:
        """Шаг 1: разбор статьи."""
        return self._ask(self.triage_chain, build_user_message(item, recent, recent_limit))

    def rewrite(self, item: Item, recent: list[dict], recent_limit: int) -> Verdict | None:
        """Шаг 2: финальный пост. None — если нет живых моделей/квоты."""
        if not self.writer_alive():
            return None
        try:
            return self._ask(self.writer_chain, build_user_message(item, recent, recent_limit))
        except LLMQuotaExhausted as e:  # кривой ответ (LLMBadAnswer) пробрасываем — такую новость пропускаем
            log(f"   writer: {e} — новость подождёт")
            return None

    def same_event(self, item: Item, published: list[str]) -> tuple[int, str] | None:
        """Это то же событие, что одна из опубликованных новостей? → (номер с нуля, причина) или None.
        Если нейросеть недоступна — None (не мешаем публикации)."""
        if not published:
            return None
        lines = [f"Новая новость: {item.title}", f"Начало текста: {item.text[:600]}", "", "Опубликовано:"]
        lines += [f"{n}. {t}" for n, t in enumerate(published, 1)]

        def check(data: dict) -> dict:
            if not isinstance(data, dict) or not isinstance(data.get("match"), int):
                raise LLMBadAnswer("нет match")
            return data

        try:
            data, _ = self._ask(self.triage_chain, "\n".join(lines), system=SAME_EVENT_PROMPT,
                                schema=SAME_EVENT_SCHEMA, validator=check, temperature=0)
        except (LLMQuotaExhausted, LLMBadAnswer) as e:
            log(f"   проверка дублей недоступна ({e})")
            return None
        n = data["match"]
        return (n - 1, str(data.get("reason", ""))) if 1 <= n <= len(published) else None

    def caption_clip(self, video: bytes | None, meta: str, system: str, check) -> tuple[dict, str, bool]:
        """Подпись к видео. Сначала Gemini смотрит само видео (video_chain); если не вышло —
        текстовая модель пишет по названию и описанию. → (ответ, модель, смотрела_ли_видео)."""
        if video and self.video_chain:
            gem = self.providers[self.video_chain[0]["provider"]].types
            parts = [gem.Part.from_bytes(data=video, mime_type="video/mp4"), meta]
            try:
                data, mid = self._ask(self.video_chain, parts, system=system, schema=CLIP_SCHEMA,
                                      validator=check, temperature=0.8)
                return data, mid, True
            except (LLMQuotaExhausted, LLMBadAnswer) as e:
                log(f"   видео посмотреть не вышло ({e}) — пишу подпись по названию")
        note = ("\n\nВИДЕО ТЫ НЕ ВИДИШЬ — только название и описание. Не придумывай, что происходит "
                "в кадре: пиши осторожно, по названию. interest ставь не выше 6.")
        data, mid = self._ask(self.triage_chain, meta + note, system=system, schema=CLIP_SCHEMA,
                              validator=check, temperature=0.8)
        return data, mid, False

    @staticmethod
    def _id(spec: dict) -> str:
        return f"{spec['provider']}:{spec['model']}"

    def _ask(self, chain: list[dict], user: str, system: str | None = None, schema: dict = JSON_SCHEMA,
             validator=None, temperature: float | None = None):
        """Пройти по цепочке моделей. По умолчанию — разбор статьи → Verdict.
        С validator — произвольный ответ: возвращает (данные, id модели)."""
        bad_answer: str | None = None
        for spec in chain:
            mid = self._id(spec)
            if not self._alive(mid):
                continue
            provider = self.providers[spec["provider"]]
            retries = int(spec.get("retries", self.retries))
            attempt = bad = 0
            while attempt <= retries:
                try:
                    text = provider.call(spec, system or self.system_prompt, user,
                                         self.temperature if temperature is None else temperature, schema)
                    if validator:
                        return validator(parse_json(text)), mid
                    verdict = validate(parse_json(text))
                    verdict.model = mid
                    return verdict
                except _Retry as e:
                    if attempt >= retries:
                        log(f"   {mid}: {e} после {retries} повторов")
                        self._bench(mid, "minute" if "429" in str(e) else "overload")
                        break
                    wait = e.wait if e.wait is not None else self.backoff * 2 ** (attempt + 1) + random.uniform(0, 1)
                    if wait:
                        log(f"   {mid}: {e}, повтор через {wait:.0f} с")
                        time.sleep(min(wait, 60))
                    attempt += 1
                except _Dead as e:
                    log(f"   {mid}: {e} — беру следующую модель")
                    self._bench(mid, "daily" if "суточн" in str(e) else "broken")
                    break
                except (json.JSONDecodeError, LLMBadAnswer) as e:
                    bad += 1
                    if bad <= 1:
                        log(f"   {mid}: плохой ответ ({e}), повторяю")
                        continue
                    bad_answer = str(e)
                    log(f"   {mid}: снова плохой ответ — беру следующую модель")
                    break
                except Exception as e:  # таймаут, обрыв соединения и т.п.
                    log(f"   {mid}: {type(e).__name__} — беру следующую")
                    self._bench(mid, "overload")
                    break
        if bad_answer:
            raise LLMBadAnswer(bad_answer)
        raise LLMQuotaExhausted("ни одна модель не ответила (лимиты/перегрузка)")
