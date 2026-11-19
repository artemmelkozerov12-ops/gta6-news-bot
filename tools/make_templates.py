"""Генератор фонов-заставок для карточек: настоящее фото ночного Майами (прототип Вайс-Сити)
+ цветокоррекция под тип новости + объёмная монета-значок, плашка, неоновая сетка.

Запуск:  python tools/make_templates.py            → assets/templates/{confirmed,rumor,official}.png
         python tools/make_templates.py --preview  → ещё и out/templates_preview.png с примером заголовка

Фото — Unsplash (бесплатная лицензия, см. assets/bg/SOURCES.md). Заголовок на карточку пишет бот (bot/card.py),
поэтому низ кадра оставляем тёмным и пустым.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
W, H = 1280, 720
S = 3  # суперсэмплинг: рисуем в 3 раза крупнее и уменьшаем — края гладкие
HORIZON = 455  # где у фото примерно линия воды (после кадрирования)

THEMES = {
    "confirmed": dict(
        photo="miami_teal.jpg", y_offset=40, zoom=1.0,
        duo=("020C10", "0E8F7A", "D8FFF4"), duo_mix=0.45,
        glow_a="00E5A0", glow_b="1E6BFF", grid="00E5C8",
        face=("A6FFD6", "26D985", "0A8A4C"), side="05613A", rim="13A866", icon="check",
        badge=("2BE07F", "119A52"), badge_side="0A5F33", label="ПОДТВЕРЖДЕНО"),
    "rumor": dict(
        photo="miami_sunset.jpg", y_offset=150, zoom=1.0,
        duo=("12040F", "C2386E", "FFD9A8"), duo_mix=0.5,
        glow_a="FF7A00", glow_b="FF2E88", grid="FF3D7F",
        face=("FFDCA0", "FF8A1E", "D9480F"), side="8A2E00", rim="C9500F", icon="q",
        badge=("FF9A3C", "E8590C"), badge_side="8A3200", label="СЛУХ"),
    "official": dict(
        photo="miami_brickell.jpg", y_offset=400, zoom=1.45,
        duo=("0A0703", "A07814", "FFF2C6"), duo_mix=0.55,
        glow_a="FFC83D", glow_b="6C4CFF", grid="FFD166",
        face=("FFF6CC", "F5C542", "B07A06"), side="6B4A00", rim="A97A0A", icon="star",
        badge=("FFD95A", "D49B09"), badge_side="6E4E02", label="ОФИЦИАЛЬНО"),
}


def rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def font(size: int, variation: str = "Black") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Variable.ttf"), size)
    try:
        f.set_variation_by_name(variation)
    except Exception:
        pass
    return f


def lin_gradient(size, colors, angle_deg=135) -> Image.Image:
    """Линейный градиент по нескольким цветам под углом."""
    w, h = size
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    small = Image.new("RGB", (64, 64))
    px = small.load()
    stops = [rgb(c) for c in colors]
    for y in range(64):
        for x in range(64):
            t = ((x / 63 - 0.5) * dx + (y / 63 - 0.5) * dy) / (abs(dx) * 0.5 + abs(dy) * 0.5) * 0.5 + 0.5
            t = min(1, max(0, t)) * (len(stops) - 1)
            i = min(int(t), len(stops) - 2)
            f = t - i
            px[x, y] = tuple(int(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f) for k in range(3))
    return small.resize((w, h), Image.BICUBIC)


def radial_glow(size, center, radius, color, alpha) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius * 0.8, cx + radius, cy + radius * 0.8), fill=rgb(color) + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.45))


# ---------- фон: фото + цветокоррекция ----------
def background(th: dict) -> Image.Image:
    photo = Image.open(ROOT / "assets/bg" / th["photo"]).convert("RGB")
    scale = W / photo.width * th["zoom"]
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    x0 = (photo.width - W) // 2
    img = photo.crop((x0, th["y_offset"], x0 + W, th["y_offset"] + H))
    # «дуотон»: яркость фото → градиент цветов темы, смешиваем с оригиналом (реализм + единый стиль)
    lum = ImageOps.grayscale(img)
    lum = ImageOps.autocontrast(lum, cutoff=1)
    dark, mid, light = (rgb(c) for c in th["duo"])
    luts = []
    for k in range(3):
        lut = []
        for v in range(256):
            t = v / 255
            if t < 0.5:
                lut.append(int(dark[k] + (mid[k] - dark[k]) * t * 2))
            else:
                lut.append(int(mid[k] + (light[k] - mid[k]) * (t - 0.5) * 2))
        luts.append(lut)
    duo = Image.merge("RGB", [lum.point(luts[k]) for k in range(3)])
    img = Image.blend(img, duo, th["duo_mix"])
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.25)
    img = img.convert("RGBA")
    # цветные свечения (как неон над городом)
    for glow in (radial_glow((W, H), (950, 120), 420, th["glow_a"], 120),
                 radial_glow((W, H), (80, 40), 380, th["glow_b"], 100)):
        img = Image.alpha_composite(img, glow)
    return img


def neon_grid(color: str) -> Image.Image:
    """Неоновая сетка на воде: линии сходятся к точке на горизонте, к горизонту — тают."""
    layer = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = rgb(color)
    vx, vy = W * S // 2, (HORIZON - 160) * S
    top, bottom = HORIZON * S, H * S
    for i in range(-30, 31):
        bx = vx + i * 140 * S
        x_top = vx + (bx - vx) * (top - vy) / (bottom - vy)
        d.line((x_top, top, bx, bottom), fill=c + (255,), width=2 * S)
    for k in range(1, 13):
        y = top + (bottom - top) * (k / 12) ** 2.1
        d.line((0, y, W * S, y), fill=c + (255,), width=2 * S)
    layer = layer.resize((W, H), Image.LANCZOS)
    fade = Image.new("L", (1, H))
    for y in range(H):
        fade.putpixel((0, y), 0 if y < HORIZON else int(150 * min(1, (y - HORIZON) / 200)))
    alpha = ImageChops.multiply(layer.getchannel("A"), fade.resize((W, H)))
    layer.putalpha(alpha)
    glow = layer.filter(ImageFilter.GaussianBlur(4))
    return Image.alpha_composite(glow, layer)


# ---------- объёмная монета ----------
def icon_layer(kind: str, size: int) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = size / 100
    if kind == "check":
        d.line([(24 * s, 52 * s), (43 * s, 70 * s), (77 * s, 32 * s)], fill="white", width=int(13 * s), joint="curve")
        for x, y in ((24, 52), (77, 32)):
            r = 6.5 * s
            d.ellipse((x * s - r, y * s - r, x * s + r, y * s + r), fill="white")
    elif kind == "star":
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            r = 40 if i % 2 == 0 else 17
            pts.append((50 * s + r * s * math.cos(a), 52 * s + r * s * math.sin(a)))
        d.polygon(pts, fill="white")
    else:
        f = font(int(78 * s))
        d.text((50 * s, 50 * s), "?", font=f, fill="white", anchor="mm")
    return im


def coin(d: int, th: dict, ry: float, rz: float, thick: float) -> Image.Image:
    """Монета диаметром d, повёрнута вокруг вертикали на ry градусов (видно ребро), наклон rz."""
    D = d * S
    sx = max(0.2, math.cos(math.radians(ry)))
    fw = int(D * sx)
    side_dx = math.sin(math.radians(ry)) * thick * S  # куда «уходит» толщина
    pad = int(abs(side_dx) + D * 0.15)
    cw, ch = fw + 2 * pad, D + 2 * pad
    im = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    fx, fy = pad, pad
    steps = max(40, int(abs(side_dx)))  # ребро: плотная стопка эллипсов — гладкий цилиндр
    side, rim = rgb(th["side"]), rgb(th["rim"])
    for i in range(steps, 0, -1):
        off = side_dx * i / steps
        t = i / steps
        base = tuple(int(side[k] + (rim[k] - side[k]) * (1 - t)) for k in range(3))  # к лицу светлее
        col = tuple(int(c * (0.7 + 0.3 * (1 - t))) for c in base)
        dr.ellipse((fx + off, fy + off * 0.15, fx + off + fw, fy + off * 0.15 + D), fill=col + (255,))
    # лицевая сторона: градиент по маске эллипса
    mask = Image.new("L", (fw, D), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, fw - 1, D - 1), fill=255)
    face = lin_gradient((fw, D), th["face"], 125).convert("RGBA")
    face.putalpha(mask)
    fdraw = ImageDraw.Draw(face)
    lw = max(2, D // 70)
    fdraw.ellipse((lw // 2, lw // 2, fw - lw, D - lw), outline=(255, 255, 255, 150), width=lw)
    inset = 0.1
    fdraw.ellipse((fw * inset, D * inset, fw * (1 - inset), D * (1 - inset)), outline=(255, 255, 255, 80), width=max(1, lw // 2))
    # блик
    shine = Image.new("RGBA", (fw, D), (0, 0, 0, 0))
    ImageDraw.Draw(shine).ellipse((fw * 0.12, D * 0.06, fw * 0.72, D * 0.34), fill=(255, 255, 255, 110))
    shine = shine.filter(ImageFilter.GaussianBlur(D * 0.03))
    shine.putalpha(ImageChops.multiply(shine.getchannel("A"), mask))
    face = Image.alpha_composite(face, shine)
    # значок: сжимаем по горизонтали так же, как монету, + тень (будто выпуклый)
    ic = icon_layer(th["icon"], int(D * 0.62)).resize((int(D * 0.62 * sx), int(D * 0.62)), Image.LANCZOS)
    shadow = Image.new("RGBA", ic.size, (0, 0, 0, 0))
    shadow.putalpha(ic.getchannel("A").point(lambda v: v * 0.45))
    ix, iy = (fw - ic.width) // 2, (D - ic.height) // 2
    face.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(D * 0.012)), (ix + int(D * 0.012), iy + int(D * 0.02)))
    face.alpha_composite(ic, (ix, iy))
    im.alpha_composite(face, (fx, fy))
    im = im.rotate(rz, resample=Image.BICUBIC, expand=True)
    return im.resize((max(1, im.width // S), max(1, im.height // S)), Image.LANCZOS)


def place(base: Image.Image, layer: Image.Image, cx: int, cy: int, blur: float = 0, shadow: bool = True) -> None:
    if shadow:  # мягкая тень снизу
        sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        sh.putalpha(layer.getchannel("A").point(lambda v: v * 0.55))
        sh = sh.filter(ImageFilter.GaussianBlur(max(4, layer.width // 18)))
        base.alpha_composite(sh, (cx - layer.width // 2 + layer.width // 25, cy - layer.height // 2 + layer.height // 10))
    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(layer, (cx - layer.width // 2, cy - layer.height // 2))


# ---------- объёмная плашка ----------
def perspective_coeffs(src, dst):
    """Коэффициенты для Image.transform(PERSPECTIVE): точки dst → src."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    n = 8  # метод Гаусса для системы 8×8 (без numpy)
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[piv], b[c], b[piv] = a[piv], a[c], b[piv], b[c]
        for r in range(n):
            if r != c and a[r][c]:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
                b[r] -= f * b[c]
    return [b[i] / a[i][i] for i in range(n)]


def badge(th: dict) -> Image.Image:
    f = font(34 * S)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = probe.textlength(th["label"], font=f) + 2 * S * len(th["label"])
    w, h, depth = int(tw + 64 * S), 76 * S, 14 * S
    im = Image.new("RGBA", (w + 4 * S, h + depth + 4 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(depth, 0, -S):  # толщина плашки
        d.rounded_rectangle((0, i, w, h + i), radius=22 * S, fill=rgb(th["badge_side"]) + (255,))
    face = lin_gradient((w, h), th["badge"], 90).convert("RGBA")
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=22 * S, fill=255)
    face.putalpha(m)
    fd = ImageDraw.Draw(face)
    fd.rounded_rectangle((S, S, w - 2 * S, h - 2 * S), radius=21 * S, outline=(255, 255, 255, 140), width=2 * S)
    hl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle((6 * S, 4 * S, w - 6 * S, h * 0.45), radius=18 * S, fill=(255, 255, 255, 45))
    face = Image.alpha_composite(face, hl)
    x = 32 * S
    fd = ImageDraw.Draw(face)
    for dx, dy, col in ((2 * S, 3 * S, (0, 0, 0, 90)), (0, 0, (255, 255, 255, 255))):
        cx = x + dx
        for ch in th["label"]:
            fd.text((cx, h / 2 + dy), ch, font=f, fill=col, anchor="lm")
            cx += probe.textlength(ch, font=f) + 2 * S
    im.alpha_composite(face, (0, 0))
    # лёгкая перспектива: правый край дальше (как поворот на ~20°)
    try:
        iw, ih = im.size
        k = 0.1
        dst = [(0, 0), (iw, ih * k * 0.6), (iw, ih * (1 - k * 0.4)), (0, ih)]
        src = [(0, 0), (iw, 0), (iw, ih), (0, ih)]
        im = im.transform(im.size, Image.PERSPECTIVE, perspective_coeffs(src, dst), Image.BICUBIC)
    except Exception:
        pass
    return im.resize((im.width // S, im.height // S), Image.LANCZOS)


# ---------- сборка ----------
def build(key: str) -> Image.Image:
    th = THEMES[key]
    img = background(th)
    img = Image.alpha_composite(img, neon_grid(th["grid"]))
    # дальние монетки — размытые (глубина резкости), ближняя большая — резкая
    for (cx, cy, d, ry, rz, blur) in ((600, 92, 50, -40, 12, 2.2), (500, 330, 38, 35, -18, 2.8),
                                      (150, 238, 72, 30, -10, 0.8), (1185, 470, 66, -45, 16, 0)):
        place(img, coin(d, th, ry, rz, d * 0.22), cx, cy, blur)
    big = coin(330, th, -32, -6, 64)
    img = Image.alpha_composite(img, radial_glow((W, H), (965, 215), 230, th["glow_a"], 110))
    place(img, big, 965, 215)
    b = badge(th)
    place(img, b, 48 + b.width // 2, 40 + b.height // 2)
    # тёмный низ под заголовок
    shade = Image.new("L", (1, H))
    for y in range(H):
        t = y / H
        a = 0 if t < 0.5 else (0.72 * (t - 0.5) / 0.28 if t < 0.78 else 0.72 + 0.22 * (t - 0.78) / 0.22)
        shade.putpixel((0, y), int(255 * min(0.94, a)))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    dark.putalpha(shade.resize((W, H)))
    img = Image.alpha_composite(img, dark)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((64, 664, 174, 670), radius=3, fill=rgb(th["grid"]) + (255,))
    d.text((1216, 668), "@gta6_hotnews", font=font(26, "Bold"), fill=(255, 255, 255, 205), anchor="rm")
    return img.convert("RGB")


def main() -> None:
    out_dir = ROOT / "assets/templates"
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in THEMES:
        build(key).save(out_dir / f"{key}.png", optimize=True)
        print(f"готово: assets/templates/{key}.png")
    if "--preview" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from bot.card import card_png
        from bot.util import load_config
        import io
        cfg = load_config()
        demo = [("confirmed", "high", "GTA 6 забрала 8 номинаций на TGA"),
                ("rumor", "rumor", "Инсайдер: GTA 6 выйдет на ПК уже весной"),
                ("confirmed", "official", "Rockstar выпустила первый патч для GTA 6")]
        sheet = Image.new("RGB", (W, H * 3))
        for i, (kind, trust, title) in enumerate(demo):
            sheet.paste(Image.open(io.BytesIO(card_png(title, kind, cfg, trust=trust))), (0, H * i))
        (ROOT / "out").mkdir(exist_ok=True)
        sheet.save(ROOT / "out/templates_preview.png")
        print("превью: out/templates_preview.png")


if __name__ == "__main__":
    main()
