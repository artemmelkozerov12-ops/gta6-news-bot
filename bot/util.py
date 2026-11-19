"""Общие мелочи: конфиг, .env, логи без секретов, HTTP с повторами."""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY",
                "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY",
                "GEMINI_API_KEY_2", "MISTRAL_API_KEY", "OPENROUTER_API_KEY")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Подхватить .env для локального запуска. Уже заданные переменные не трогаем."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def redact(text: str) -> str:
    """Вырезать значения секретов из любой строки перед выводом."""
    text = str(text)
    for name in SECRET_NAMES:
        value = os.getenv(name)
        if value and len(value) >= 6:
            text = text.replace(value, "***")
    return text


def log(*parts) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    msg = redact(" ".join(str(p) for p in parts))
    try:
        print(f"[{stamp}] {msg}", flush=True)
    except UnicodeEncodeError:  # старые консоли Windows
        print(f"[{stamp}] {msg}".encode(sys.stdout.encoding or "utf-8", "replace").decode(), flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RETRY_STATUS = {429, 500, 502, 503, 504}


class Http:
    """httpx.Client + повторы с экспоненциальной паузой на 429/5xx и сетевых ошибках."""

    def __init__(self, cfg: dict):
        h = cfg.get("http", {})
        self.retries = int(h.get("retries", 3))
        self.backoff = float(h.get("backoff_seconds", 2))
        self.client = httpx.Client(
            timeout=float(h.get("timeout_seconds", 20)),
            follow_redirects=True,
            headers={
                "User-Agent": h.get("user_agent", "Mozilla/5.0"),
                "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            },
        )

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.client.request(method, url, **kw)
                if r.status_code not in RETRY_STATUS:
                    return r
                wait = self._retry_after(r) or self.backoff * 2**attempt
                last_exc = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                wait = self.backoff * 2**attempt
                last_exc = e
            if attempt < self.retries:
                time.sleep(min(wait, 60) + random.uniform(0, 0.5))
        assert last_exc is not None
        raise last_exc

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)

    @staticmethod
    def _retry_after(r: httpx.Response) -> float | None:
        value = r.headers.get("retry-after")
        if value and value.isdigit():
            return float(value)
        # Telegram кладёт паузу в тело: {"parameters": {"retry_after": 5}}
        try:
            return float(r.json()["parameters"]["retry_after"])
        except Exception:
            return None
