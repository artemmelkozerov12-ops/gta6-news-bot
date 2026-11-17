"""Один запуск бота: собрать → отфильтровать → Gemini → очередь → (опубликовать).

Запуск:  python -m bot.main
Режимы:  DRY_RUN=1          — ничего не публикует и не сохраняет state.json, всё пишет в out/
         MAX_LLM_CALLS=N    — переопределить llm.max_calls_per_run (для тестов)
         MAX_ITEM_AGE_HOURS — переопределить limits.max_item_age_hours (для тестов)
"""
from __future__ import annotations

import html
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

from . import moderation
from .card import card_png
from .dedupe import State, dedupe_batch, similarity, url_key
from .fetch import TRUST_ORDER, Item, fetch_all, fetch_article_text, fresh_only, keyword_filter
from .llm import LLM, LLMBadAnswer, LLMQuotaExhausted, Verdict
from .publish import TelegramError, build_caption, can_post_now, decide, send_photo
from .util import ROOT, Http, env_flag, load_config, load_dotenv, log, utcnow

OUT = ROOT / "out"


def short(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def check_llm_flags(v: Verdict, it: Item, shown: list[dict]) -> None:
    """Страховка от фантазий модели: «дубль» и «подтверждено» засчитываем, только если
    названная моделью новость действительно есть в показанном ей списке с нужной пометкой."""
    def find(title: str) -> dict | None:
        title = title.strip().lstrip("-").strip()
        for mark in ("[опубликовано]", "[видели]"):
            title = title.replace(mark, "").strip()
        title = title.split(" — ")[0].strip()
        if len(title) < 8:
            return None
        return next((r for r in shown if similarity(title, r["title"]) >= 0.85), None)

    if v.duplicate:
        r = find(v.duplicate_of)
        v.duplicate = bool(r and r["status"] in ("published", "queued"))
    if v.corroborated:
        r = find(v.corroborated_by)
        v.corroborated = bool(r and r["trust"] != "rumor" and r["source"] != it.source)


def queue_sort_key(q: dict):
    return (TRUST_ORDER.get(q["item"]["trust"], 9), -q["verdict"]["importance"], q["queued_at"])


def run() -> int:
    load_dotenv()
    cfg = load_config()
    dry = env_flag("DRY_RUN")
    if os.getenv("MAX_ITEM_AGE_HOURS"):
        cfg["limits"]["max_item_age_hours"] = float(os.environ["MAX_ITEM_AGE_HOURS"])
    max_calls = int(os.getenv("MAX_LLM_CALLS") or cfg["llm"]["max_calls_per_run"])
    thr = float(cfg["dedupe"]["title_similarity"])

    log(f"=== {cfg['channel']['name']} — запуск {'(DRY_RUN: ничего не публикую)' if dry else ''}")
    state = State.load()
    state.prune(cfg)
    http = Http(cfg)
    decisions: list[dict] = []  # для отчёта в dry-run

    # 1. Сбор
    items, report = fetch_all(cfg, http)
    ok_sources = sum(1 for r in report if r["status"] == "ok")
    log(f"Источники: {ok_sources} ok из {len(report)}, записей всего: {len(items)}")
    relevant = keyword_filter(items, cfg["keywords"], cfg.get("keywords_title_only", False))
    fresh, old = fresh_only(relevant, cfg["limits"]["max_item_age_hours"])
    new = [i for i in fresh if not i.link or not state.is_seen(i)]
    log(f"Про GTA 6: {len(relevant)} | свежих (≤{cfg['limits']['max_item_age_hours']:g} ч): {len(fresh)} | новых: {len(new)}")

    # 2. Дубли внутри пачки (одна и та же новость с разных сайтов)
    kept, dropped = dedupe_batch(new, thr)
    for it, twin in dropped:
        log(f"⏭  дубль в пачке: «{short(it.title)}» ({it.source}) ≈ «{short(twin.title, 50)}» ({twin.source})")
        state.mark_seen(it)
        state.remember(it, "dup")

    # 3. Уже было в канале за 48 ч?
    candidates: list[Item] = []
    for it in kept:
        twin = state.similar_published(it, thr)
        if twin:
            log(f"⏭  уже было: «{short(it.title)}» ≈ «{short(twin['title'], 50)}»")
            state.mark_seen(it)
            state.remember(it, "dup")
            continue
        candidates.append(it)
    candidates.sort(key=lambda i: i.published, reverse=True)  # сначала самые свежие

    # 4. Нейросеть
    processed = 0
    if candidates:
        try:
            gemini = LLM(cfg)
        except Exception as e:
            log(f"❌ LLM не настроен: {e}")
            gemini = None
        for idx, it in enumerate(candidates):
            if gemini is None:
                break
            if processed >= max_calls:
                log(f"⏸  ещё {len(candidates) - idx} новостей отложено до следующего запуска (лимит {max_calls} на запуск)")
                break
            log(f"🔎 [{it.trust}] {it.source}: «{short(it.title, 100)}»")
            it.text = fetch_article_text(http, it, cfg["llm"]["article_max_chars"])
            try:
                v = gemini.analyze(it, state.recent, cfg["llm"]["recent_titles_in_prompt"])
            except LLMQuotaExhausted as e:
                log(f"⛔ {e}. Остальное разберём в следующий запуск.")
                break
            except LLMBadAnswer as e:
                processed += 1
                log(f"   ⏭  кривой ответ нейросети ({e}) — пропуск")
                state.mark_seen(it)
                state.remember(it, "error")
                continue
            except Exception as e:
                processed += 1
                log(f"   ⚠️  ошибка нейросети: {type(e).__name__}: {e}")
                state.mark_seen(it)
                state.remember(it, "error")
                continue
            processed += 1
            shown = state.recent[-cfg["llm"]["recent_titles_in_prompt"]:]
            check_llm_flags(v, it, shown)
            corr = state.corroborating(it, thr) is not None
            ok, why = decide(it, v, cfg, corr)
            state.mark_seen(it)
            log(f"   → {v.type}, важность {v.importance} [{v.model}]: {short(v.reason, 120)}")
            if v.duplicate:
                log(f"   дубль чего: «{short(v.duplicate_of, 90)}»")
            if ok:
                # Отдельная проверка «то же событие уже опубликовано/в очереди?» (узкий вопрос — надёжнее)
                published = [r["title"] for r in state.recent if r["status"] in ("published", "queued")]
                same = gemini.same_event(it, published[-30:])
                if same:
                    ok, why = False, f"дубль: «{short(published[-30:][same[0]], 70)}» ({short(same[1], 60)})"
            if ok:
                # Шаг 2: финальный текст умной моделью (если есть квота)
                final = gemini.rewrite(it, state.recent, cfg["llm"]["recent_titles_in_prompt"])
                if final is None:
                    log("   ✍️  финальный текст: квоты нет, оставляю черновик шага 1")
                elif final.type in ("leak", "skip"):  # умная модель строже — верим ей
                    ok, why = False, f"writer-модель: type={final.type} ({short(final.reason, 80)})"
                elif v.type == "rumor" and final.type == "confirmed":
                    log(f"   ✍️  writer считает это подтверждённым, но оставляю «слух» и черновик (осторожность)")
                else:
                    if final.type == "rumor" and v.type == "confirmed":  # writer видит слух — помечаем строже
                        v.type, v.importance = "rumor", final.importance
                        ok, why = decide(it, v, cfg, corr)
                    v.post, v.card_title, v.model = final.post, final.card_title, f"{v.model} → {final.model}"
                    log(f"   ✍️  финальный текст: {final.model}")
            if ok:
                state.queue.append({"item": it.to_dict(), "verdict": v.to_dict(), "queued_at": utcnow().isoformat()})
                state.remember(it, "queued", v.card_title)
                log(f"   ✅ в очередь ({why})")
            else:
                state.remember(it, "skipped")
                log(f"   ⏭  пропуск: {why}")
            decisions.append({"item": it, "verdict": v, "ok": ok, "why": why})

    # 5. Очередь: выкинуть протухшее, отсортировать
    moderated = moderation.enabled(cfg)
    if moderated and not dry and not moderation.admin_id():
        log("❌ Модерация включена, но нет TELEGRAM_ADMIN_ID — ничего не публикую (см. README)")
        state.save()
        return 1
    expire_queue(state, cfg, http if moderated and not dry else None)
    log(f"Очередь: {len(state.queue)}")

    if dry:
        can, why = can_post_now(state, cfg)
        write_dry_run(cfg, state, decisions, report, can, why)
        log("DRY_RUN: state.json не сохраняю.")
        return 0

    if not moderated:
        log("Модерация выключена в config.yaml — публикую без подтверждения")
        posted = publish_ready(http, state, cfg, moderated=False)
        state.save()
        log(f"Готово. Разобрано нейросетью: {processed}, опубликовано: {posted}, в очереди: {len(state.queue)}")
        return 0

    # 6. Модерация: события Telegram → новые черновики → публикация одобренного.
    # Дальше несколько минут «слушаем» Telegram, чтобы кнопка срабатывала почти сразу.
    listen = float(os.getenv("LISTEN_MINUTES") or cfg["moderation"].get("listen_minutes", 0))
    deadline = time.monotonic() + listen * 60
    posted, wait = 0, 0
    if listen:
        log(f"👂 слушаю кнопки {listen:g} мин")
    while True:
        for q in moderation.poll_updates(http, state, wait=wait):
            set_recent_status(state, q["item"]["title"], "rejected")
        moderation.send_drafts(http, state, cfg)
        posted += publish_ready(http, state, cfg, moderated=True)
        state.save()  # сохраняем после каждого шага: запуск могут оборвать
        left = deadline - time.monotonic()
        if left <= 5:
            break
        wait = int(min(50, left))
    waiting = sum(1 for q in state.queue if q.get("moderation") == "pending")
    log(f"Готово. Разобрано нейросетью: {processed}, опубликовано: {posted}, "
        f"ждут твоего решения: {waiting}, черновиков за сутки: {moderation.drafts_today(state)}")
    return 0


def expire_queue(state: State, cfg: dict, http) -> None:
    border = (utcnow() - timedelta(hours=cfg["limits"]["queue_max_age_hours"])).isoformat()
    for q in [q for q in state.queue if q["queued_at"] < border]:
        log(f"🗑  из очереди (старше {cfg['limits']['queue_max_age_hours']} ч): «{short(q['item']['title'])}»")
        if http is not None:
            moderation.mark_expired(http, q)
    state.queue = sorted([q for q in state.queue if q["queued_at"] >= border], key=queue_sort_key)


def publish_ready(http: Http, state: State, cfg: dict, moderated: bool) -> int:
    """Опубликовать в канал готовое: при модерации — только одобренное тобой."""
    posted = 0
    while posted < cfg["limits"]["max_posts_per_run"]:
        q = next((q for q in state.queue if not moderated or q.get("moderation") == "approved"), None)
        if q is None:
            return posted
        if moderated:  # ты нажал кнопку — значит, ты не спишь: ночной запрет не нужен, только пауза
            can, why = can_post_now(state, cfg, ignore_quiet_hours=True)
        else:
            can, why = can_post_now(state, cfg)
        if not can:
            if not q.get("waiting_logged"):
                log(f"⏳ одобренный пост подождёт: {why}")
                q["waiting_logged"] = True
            return posted
        chat = moderation.channel_id(state)
        if not chat:
            if not q.get("no_channel_notified"):
                moderation.notify(http, "⚠️ Не знаю, куда публиковать: добавь меня в канал администратором "
                                        "с правом «Публикация сообщений» — пост выйдет сразу после этого.")
                q["no_channel_notified"] = True
            return posted
        it, v = Item.from_dict(q["item"]), Verdict.from_dict(q["verdict"])
        try:
            png = card_png(v.card_title, v.type, cfg, seed=int(url_key(it.link), 16) % 10_000, trust=it.trust)
            msg_id = send_photo(http, png, build_caption(it, v), chat_id=chat)
        except Exception as e:
            err = str(e).lower()
            if "chat not found" in err or "not enough rights" in err or "not a member" in err:
                # Канал недоступен — пост НЕ выкидываем. Канал не забываем: сразу после добавления бота
                # Telegram пару секунд может отвечать «not a member» — просто повторим позже.
                q["channel_fails"] = q.get("channel_fails", 0) + 1
                log(f"❌ канал «{chat}» недоступен ({e}) — пост ждёт, попытка {q['channel_fails']}")
                if q["channel_fails"] == 3:
                    moderation.notify(http, f"⚠️ Не могу опубликовать: Telegram ответил «{html.escape(str(e))}». "
                                            "Проверь, что я админ канала с правом «Публикация сообщений» — "
                                            "пост выйдет сразу после этого.")
                return posted
            q["attempts"] = q.get("attempts", 0) + 1
            log(f"❌ не отправилось в канал ({type(e).__name__}: {e}), попытка {q['attempts']}")
            if q["attempts"] >= 3:
                log("   убираю из очереди после 3 неудач")
                state.queue.remove(q)
            return posted
        state.queue.remove(q)
        state.posts.append(utcnow().isoformat())
        set_recent_status(state, it.title, "published")
        if moderated:
            moderation.mark_published(http, q)
        posted += 1
        log(f"📣 опубликовано в канал (message_id={msg_id}): «{short(it.title)}»")
    return posted


def set_recent_status(state: State, title: str, status: str) -> None:
    for r in state.recent:
        if r["title"] == title and r["status"] in ("queued", "published"):
            r["status"] = status


def write_dry_run(cfg: dict, state: State, decisions: list[dict], report: list[dict], can: bool, why: str) -> None:
    """Сложить карточки, тексты и отчёт в out/, плюс preview.html для просмотра в браузере."""
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "rejected").mkdir(parents=True)

    cards: list[dict] = []
    for n, q in enumerate(state.queue, 1):
        it, v = Item.from_dict(q["item"]), Verdict.from_dict(q["verdict"])
        cards.append(_dump(cfg, OUT, f"{n:02d}", it, v, "в очереди"))
    rej = 0
    for d in decisions:
        if not d["ok"] and d["verdict"].post and d["verdict"].type in ("confirmed", "rumor"):
            rej += 1
            cards.append(_dump(cfg, OUT / "rejected", f"r{rej:02d}", d["item"], d["verdict"], "отклонено: " + d["why"]))

    lines = ["# Отчёт dry-run", "", f"Время: {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Публикация сейчас: {'да, ушёл бы пост №01' if can and state.queue else 'нет — ' + (why if not can else 'очередь пуста')}", "",
             "## Источники", "", "| Источник | Статус | Записей |", "|---|---|---|"]
    lines += [f"| {r['source']} | {r['status']} | {r['entries']} |" for r in report]
    lines += ["", "## Решения по новостям", "", "| Решение | Тип | Важн. | Источник | Заголовок | Причина |", "|---|---|---|---|---|---|"]
    for d in decisions:
        v, it = d["verdict"], d["item"]
        lines.append(f"| {'✅' if d['ok'] else '⏭'} | {v.type} | {v.importance} | {it.source} | {short(it.title, 70)} | {d['why']} / {short(v.reason, 90)} |")
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")

    blocks = []
    for c in cards:
        blocks.append(
            f'<div class="post"><div class="tag">{html.escape(c["label"])}</div>'
            f'<img src="{c["img"]}"><div class="cap">{c["caption"].replace(chr(10), "<br>")}</div></div>')
    (OUT / "preview.html").write_text(PREVIEW_HTML.replace("{{POSTS}}", "\n".join(blocks) or "<p>Пусто</p>"), encoding="utf-8")
    log(f"DRY_RUN: в out/ {len(state.queue)} постов из очереди и {rej} отклонённых; открой out/preview.html")


def _dump(cfg: dict, folder, prefix: str, it: Item, v: Verdict, label: str) -> dict:
    png = card_png(v.card_title, v.type, cfg, seed=int(url_key(it.link), 16) % 10_000, trust=it.trust)
    (folder / f"{prefix}.png").write_bytes(png)
    caption = build_caption(it, v)
    meta = {"type": v.type, "importance": v.importance, "reason": v.reason, "card_title": v.card_title,
            "source": it.source, "trust": it.trust, "title": it.title, "link": it.link, "model": v.model}
    (folder / f"{prefix}.txt").write_text(caption + "\n\n---\n" + json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    rel = (folder / f"{prefix}.png").relative_to(OUT).as_posix()
    return {"img": rel, "caption": caption, "label": f"{label} · {v.type} · {v.importance}/10 · {it.source}"}


PREVIEW_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dry-run preview</title>
<style>body{background:#0e1621;color:#e9eef3;font:15px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px}
.post{max-width:520px;margin:0 auto 28px;background:#182533;border-radius:14px;overflow:hidden}
.post img{width:100%;display:block}.cap{padding:12px 14px 14px;white-space:normal}
.cap a{color:#6ab3f3}.tag{font-size:12px;color:#8aa0b4;padding:8px 14px;background:#121d29}</style></head>
<body>{{POSTS}}</body></html>"""


if __name__ == "__main__":
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        sys.exit(130)
