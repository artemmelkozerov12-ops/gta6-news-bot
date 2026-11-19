"""Карточка 1280x720 к посту: фон, затемнение, крупный заголовок, плашка, подпись канала."""
from __future__ import annotations

import io
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .util import ROOT

BADGES = {
    "confirmed": ("ПОДТВЕРЖДЕНО", (22, 163, 74), "check"),
    "rumor": ("СЛУХ", (234, 112, 12), "flame"),
}


# ---------- фон ----------
def _gradient_background(w: int, h: int, seed: int) -> Image.Image:
    """Ночной город: сине-чёрный градиент, красное неоновое свечение, силуэты домов."""
    rnd = random.Random(seed)
    top, bottom = (10, 16, 40), (2, 3, 10)
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    img = grad.resize((w, h))

    # красное и синее свечение
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    cx = rnd.randint(int(w * 0.55), int(w * 0.85))
    g.ellipse((cx - 420, h * 0.15, cx + 420, h * 0.95), fill=(200, 20, 50))
    g.ellipse((-200, -150, 500, 350), fill=(20, 60, 160))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.blend(img, Image.composite(glow, img, Image.new("L", (w, h), 255)), 0.55)

    # силуэты небоскрёбов с окнами
    city = ImageDraw.Draw(img)
    x = -20
    while x < w:
        bw = rnd.randint(50, 130)
        bh = rnd.randint(int(h * 0.2), int(h * 0.62))
        city.rectangle((x, h - bh, x + bw, h), fill=(5, 7, 16))
        for wy in range(h - bh + 14, h - 10, 22):
            for wx in range(x + 10, x + bw - 10, 18):
                if rnd.random() < 0.18:
                    color = rnd.choice([(255, 60, 90), (90, 140, 255), (255, 200, 120)])
                    city.rectangle((wx, wy, wx + 6, wy + 9), fill=tuple(int(c * 0.55) for c in color))
        x += bw + rnd.randint(2, 14)
    # неоновая линия горизонта
    line = Image.new("RGB", (w, h), (0, 0, 0))
    ImageDraw.Draw(line).rectangle((0, int(h * 0.42), w, int(h * 0.42) + 3), fill=(255, 30, 70))
    img = Image.blend(img, Image.composite(line.filter(ImageFilter.GaussianBlur(6)), img,
                                           Image.new("L", (w, h), 255)), 0.25)
    return img


def _background(cfg: dict, seed: int) -> Image.Image:
    w, h = cfg["card"]["width"], cfg["card"]["height"]
    path = ROOT / cfg["card"].get("background", "assets/bg.png")
    if path.exists():
        return ImageOps.fit(Image.open(path).convert("RGB"), (w, h), Image.LANCZOS)
    return _gradient_background(w, h, seed)


def _bottom_shade(img: Image.Image) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (1, h))
    for y in range(h):
        t = max(0.0, (y - h * 0.25) / (h * 0.75))
        mask.putpixel((0, y), int(235 * t ** 1.3))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(black, img, mask.resize((w, h)))


# ---------- текст ----------
def _font(path: str, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(ROOT / path), size)
    if variation:
        try:
            f.set_variation_by_name(variation)
        except Exception:
            pass
    return f


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    # «GTA 6» не разрываем между строками: склеиваем неразрывным пробелом
    text = re.sub(r"\b(GTA|ГТА)\s+(6|VI)\b", "\\1\u00a0\\2", " ".join(text.split()))
    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_title(draw, text: str, cfg: dict, max_width: int):
    c = cfg["card"]
    for size in range(c["title_max_size"], c["title_min_size"] - 1, -4):
        font = _font(c["title_font"], size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= c["title_max_lines"] and all(draw.textlength(l, font=font) <= max_width for l in lines):
            return font, lines
    font = _font(c["title_font"], c["title_min_size"])
    lines = _wrap(draw, text, font, max_width)[: c["title_max_lines"]]
    return font, lines


# ---------- значки ----------
def _icon(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, s: int) -> None:
    if kind == "check":
        draw.line([(x + s * 0.12, y + s * 0.55), (x + s * 0.4, y + s * 0.82), (x + s * 0.9, y + s * 0.2)],
                  fill="white", width=max(4, s // 7), joint="curve")
    else:  # пламя
        draw.polygon([(x + s * 0.5, y), (x + s * 0.88, y + s * 0.55), (x + s * 0.78, y + s * 0.9),
                      (x + s * 0.5, y + s), (x + s * 0.22, y + s * 0.9), (x + s * 0.12, y + s * 0.6),
                      (x + s * 0.3, y + s * 0.35), (x + s * 0.38, y + s * 0.55)], fill="white")
        draw.ellipse((x + s * 0.36, y + s * 0.6, x + s * 0.64, y + s * 0.92), fill=(234, 112, 12))


def _badge(img: Image.Image, kind: str, cfg: dict) -> None:
    label, color, icon = BADGES.get(kind, BADGES["confirmed"])
    font = _font(cfg["card"]["ui_font"], 30, "ExtraBold")
    draw = ImageDraw.Draw(img)
    pad_x, h, icon_s = 22, 60, 30
    tw = int(draw.textlength(label, font=font))
    bw = pad_x * 2 + icon_s + 12 + tw
    x, y = 48, 44
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x, y + 6, x + bw, y + h + 6), 14, fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    draw.rounded_rectangle((x, y, x + bw, y + h), 14, fill=color + (255,))
    _icon(draw, icon, x + pad_x, y + (h - icon_s) // 2, icon_s)
    draw.text((x + pad_x + icon_s + 12, y + h // 2), label, font=font, fill="white", anchor="lm")


# ---------- сборка ----------
def render_card(card_title: str, kind: str, cfg: dict, seed: int = 0) -> Image.Image:
    c = cfg["card"]
    w, h = c["width"], c["height"]
    img = _bottom_shade(_background(cfg, seed)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    margin = 64

    font, lines = _fit_title(draw, card_title.upper(), cfg, w - margin * 2)
    line_h = int(font.size * 1.08)
    block_h = line_h * len(lines)
    y0 = h - 110 - block_h

    # красный акцент слева от заголовка
    draw.rectangle((margin - 26, y0 + 8, margin - 16, y0 + block_h - 8), fill=(230, 30, 60, 255))
    # мягкая тень под текстом
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for i, line in enumerate(lines):
        sd.text((margin + 3, y0 + i * line_h + 5), line, font=font, fill=(0, 0, 0, 200))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((margin, y0 + i * line_h), line, font=font, fill=(255, 255, 255, 255))

    _badge(img, kind, cfg)

    handle_font = _font(c["ui_font"], 26, "SemiBold")
    handle = cfg["channel"]["handle"]
    draw.text((w - margin, h - 46), handle, font=handle_font, fill=(255, 255, 255, 190), anchor="rs")
    draw.rectangle((margin, h - 62, margin + 90, h - 58), fill=(230, 30, 60, 255))
    return img.convert("RGB")


def template_name(kind: str, trust: str = "") -> str:
    """Какой шаблон из Figma: официальное от Rockstar/Take-Two, слух или подтверждённое."""
    if kind == "rumor":
        return "rumor"
    if kind == "guide":
        return "guide"
    return "official" if trust == "official" else "confirmed"


def render_from_template(card_title: str, path: Path, cfg: dict) -> Image.Image:
    """Фон-шаблон из Figma (плашка, подпись канала уже на нём) + заголовок в зоне из макета."""
    t = cfg["card"]["template"]
    img = Image.open(path).convert("RGBA")
    if img.size != (cfg["card"]["width"], cfg["card"]["height"]):
        img = ImageOps.fit(img, (cfg["card"]["width"], cfg["card"]["height"]), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    x, bottom, width = t["title_x"], t["title_bottom"], t["title_width"]
    text = card_title.upper()
    font, lines = None, []
    for size in range(t["title_max_size"], t["title_min_size"] - 1, -2):
        font = _font(t["title_font"], size, t.get("title_variation"))
        lines = _wrap(draw, text, font, width)
        if len(lines) <= t["title_max_lines"] and all(draw.textlength(l, font=font) <= width for l in lines):
            break
    lines = lines[: t["title_max_lines"]]
    line_h = int(font.size * t.get("line_height", 1.08))
    y0 = bottom - line_h * len(lines)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for i, line in enumerate(lines):
        sd.text((x, y0 + i * line_h + 4), line, font=font, fill=(0, 0, 0, 160))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((x, y0 + i * line_h), line, font=font, fill=(255, 255, 255, 255))
    return img.convert("RGB")


def card_png(card_title: str, kind: str, cfg: dict, seed: int = 0, trust: str = "") -> bytes:
    tpl = cfg["card"].get("template")
    path = ROOT / tpl["dir"] / f"{template_name(kind, trust)}.png" if tpl else None
    img = render_from_template(card_title, path, cfg) if path and path.exists() else render_card(card_title, kind, cfg, seed)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def save_card(card_title: str, kind: str, cfg: dict, path: Path, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(card_png(card_title, kind, cfg, seed))
    return path
