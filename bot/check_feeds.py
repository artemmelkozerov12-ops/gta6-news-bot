"""Проверка источников из config.yaml: отвечает ли (200), есть ли записи, сколько про GTA 6.

Запуск:  python -m bot.check_feeds          — только включённые
         python -m bot.check_feeds --all    — включая выключенные (enabled: false)
"""
from __future__ import annotations

import sys
import time

from .fetch import keyword_filter, parse_feed
from .util import Http, load_config, log


def main() -> int:
    cfg = load_config()
    cfg["http"]["retries"] = 1
    http = Http(cfg)
    check_all = "--all" in sys.argv
    rows, broken = [], 0
    for src in cfg["sources"]:
        enabled = src.get("enabled", True) is not False
        if not enabled and not check_all:
            rows.append((src["name"], src["trust"], "выключен", "-", "-"))
            continue
        try:
            r = http.get(src["url"])
            items = parse_feed(r.content, src) if r.status_code == 200 else []
            gta = len(keyword_filter(items, cfg["keywords"]))
            status = "OK" if r.status_code == 200 and items else f"НЕ РАБОТАЕТ (HTTP {r.status_code}, записей {len(items)})"
            rows.append((src["name"], src["trust"], status, str(len(items)), str(gta)))
        except Exception as e:
            status = f"НЕ РАБОТАЕТ ({type(e).__name__})"
            rows.append((src["name"], src["trust"], status, "0", "0"))
        if rows[-1][2] != "OK":
            broken += 1
        if "news.google.com" in src["url"]:
            time.sleep(2)  # Google News отвечает 429 на частые запросы
    print(f"\n{'Источник':20} {'trust':9} {'записей':>8} {'про GTA':>8}  статус")
    print("-" * 75)
    for name, trust, status, n, gta in rows:
        print(f"{name:20} {trust:9} {n:>8} {gta:>8}  {status}")
    log(f"Проверено: {len(rows)}, проблемных: {broken}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
