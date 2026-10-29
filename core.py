"""Логика без GUI: распознавание пар, масштаб, проект (JSON) и экспорт (Pillow)."""
import copy
import io
import json
import math
import os
import re
import unicodedata

from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None  # панорамы бывают очень большими

# Калибровка: 4632 пикселя = 21 мм
MM_PER_PX = 21 / 4632

MAGS = ["2.5", "5", "10", "20"]
MAG_DIAMETER_MM = {"2.5": 7.58, "5": 4.59, "10": 2.18, "20": 1.09}

SIDES = ("parallel", "crossed")
SIDE_TITLES = {"parallel": "Параллельные николи", "crossed": "Скрещённые николи"}
SIDE_FILE_SUFFIX = {"parallel": "параллельные", "crossed": "скрещенные"}

IMAGE_EXT = {".png", ".jpg", ".jpeg"}
JPEG_MAX_SIDE = 65500
TARGET_FILE_BYTES = 20 * 1024 * 1024
MIN_AUTO_QUALITY = 85

DEFAULT_SETTINGS = {
    "scale_font_px": 200,     # размер шрифта подписи линейки, px исходного изображения
    "scale_line_px": 20,      # толщина линии линейки
    "number_font_px": 200,    # размер шрифта номеров окружностей
    "circle_line_px": 12,     # толщина контура окружностей
    "default_color": "#ffff00",
    "fill_opacity": 20,       # непрозрачность заливки окружностей, %
    "jpeg_quality": 93,
    "compress": True,
}


def mag_label(mag):
    return f"{mag}×"


def diameter_px(mag):
    return MAG_DIAMETER_MM[mag] / MM_PER_PX


# ---------------------------------------------------------------- пары файлов

_CROSSED_RE = re.compile(r"^(.*\S)\s+[сСcC]$")  # кириллическая или латинская «с»
_PREFIX_RE = re.compile(r"^\d+\.\s+")


def natural_key(s):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def list_images(folder):
    files = []
    for f in os.listdir(folder):
        if os.path.splitext(f)[1].lower() in IMAGE_EXT and os.path.isfile(os.path.join(folder, f)):
            files.append(f)
    return sorted(files, key=natural_key)


def split_name(filename):
    """Возвращает (базовое имя, скрещённые ли николи)."""
    stem = unicodedata.normalize("NFC", os.path.splitext(filename)[0]).strip()
    m = _CROSSED_RE.match(stem)
    if m:
        return m.group(1).strip(), True
    return stem, False


def display_name(base):
    return _PREFIX_RE.sub("", base).strip() or base


def new_pair(name, parallel, crossed, problem=""):
    return {"name": name, "parallel": parallel, "crossed": crossed, "problem": problem, "objects": []}


def scan_folder(folder):
    groups = {}
    for f in list_images(folder):
        base, crossed = split_name(f)
        g = groups.setdefault(base, {"parallel": [], "crossed": []})
        g["crossed" if crossed else "parallel"].append(f)

    pairs, used = [], set()
    for base in sorted(groups, key=natural_key):
        g = groups[base]
        name = display_name(base)
        unique, n = name, 2
        while unique.casefold() in used:
            unique = f"{name} ({n})"
            n += 1
        used.add(unique.casefold())
        problems = []
        for side in SIDES:
            if not g[side]:
                problems.append(f"нет изображения: {SIDE_TITLES[side].lower()}")
            elif len(g[side]) > 1:
                problems.append(f"несколько файлов ({SIDE_TITLES[side].lower()}): " + ", ".join(g[side]))
        pairs.append(new_pair(unique, (g["parallel"] or [None])[0], (g["crossed"] or [None])[0],
                              "; ".join(problems)))
    return pairs


# ---------------------------------------------------------------- объекты

def new_object(mag):
    return {"mag": mag, "color": None,
            "pos": {"parallel": None, "crossed": None},
            "label": {"parallel": None, "crossed": None}}


def is_placed(obj):
    return any(obj["pos"][s] for s in SIDES)


def object_color(obj, settings):
    return obj["color"] or settings["default_color"]


def place_object(obj, side, x, y, sizes):
    """Ставит центр на одной панораме, на второй — по относительным координатам."""
    w, h = sizes[side]
    rx, ry = x / w, y / h
    for s in SIDES:
        sw, sh = sizes[s]
        obj["pos"][s] = [x, y] if s == side else [rx * sw, ry * sh]


def label_offset(obj, side, settings):
    """Смещение левого верхнего угла номера относительно центра окружности."""
    off = obj["label"][side]
    if off:
        return off
    d = diameter_px(obj["mag"]) / 2 * 0.7071 + settings["circle_line_px"]
    return [d, d]


def swap_sides(pair):
    pair["parallel"], pair["crossed"] = pair["crossed"], pair["parallel"]
    for o in pair["objects"]:
        for key in ("pos", "label"):
            o[key]["parallel"], o[key]["crossed"] = o[key]["crossed"], o[key]["parallel"]


def drawing_order(objects):
    """Крупные окружности снизу, мелкие сверху (как в предпросмотре)."""
    return sorted(range(len(objects)), key=lambda i: -MAG_DIAMETER_MM[objects[i]["mag"]])


# ---------------------------------------------------------------- масштабная линейка

def choose_scale_mm(width_mm):
    half, target = width_mm / 2, width_mm * 0.3
    cands = [5 * k for k in range(1, int(half // 5) + 2) if 5 * k < half]
    if not cands:
        cands = [v for v in (2, 1) if v < half] or [1]
    return min(cands, key=lambda v: (abs(v - target), v))


def scale_field_height(settings):
    f, t = settings["scale_font_px"], settings["scale_line_px"]
    return math.ceil(0.5 * f + 0.95 * f + 0.3 * f + max(3 * t, 0.4 * f) + 0.5 * f)


def scale_bar(img_w, img_h, settings):
    """Геометрия линейки в координатах исходного изображения (белое поле начинается с y = img_h)."""
    f, t = settings["scale_font_px"], settings["scale_line_px"]
    mm = choose_scale_mm(img_w * MM_PER_PX)
    length = mm / MM_PER_PX
    tick = max(3 * t, 0.4 * f)
    baseline = img_h + 0.5 * f + 0.95 * f
    tick_top = baseline + 0.3 * f
    tick_bottom = tick_top + tick
    bar_y = (tick_top + tick_bottom) / 2
    x0 = max(f, 2 * t)
    x1 = x0 + length
    rects = [
        (x0 - t / 2, bar_y - t / 2, x1 + t / 2, bar_y + t / 2),
        (x0 - t / 2, tick_top, x0 + t / 2, tick_bottom),
        (x1 - t / 2, tick_top, x1 + t / 2, tick_bottom),
    ]
    return {"mm": mm, "text": f"{mm} мм", "rects": rects, "text_x": (x0 + x1) / 2,
            "baseline": baseline, "field_h": scale_field_height(settings)}


# ---------------------------------------------------------------- проект

class ProjectError(Exception):
    pass


class Project:
    def __init__(self, path, image_dir, pairs, settings=None):
        self.path = path
        self.image_dir = image_dir
        self.pairs = pairs
        self.settings = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})

    def image_path(self, pair, side):
        return os.path.join(self.image_dir, pair[side]) if pair[side] else None

    def missing_files(self):
        missing = []
        for p in self.pairs:
            for s in SIDES:
                path = self.image_path(p, s)
                if path and not os.path.isfile(path):
                    missing.append(p[s])
        return missing

    def to_dict(self):
        return {"format": "panorama_marking", "version": 1, "image_dir": self.image_dir,
                "settings": self.settings, "pairs": self.pairs}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            pairs = []
            for p in data["pairs"]:
                pair = new_pair(p["name"], p.get("parallel"), p.get("crossed"), p.get("problem", ""))
                for o in p.get("objects", []):
                    obj = new_object(o["mag"] if o.get("mag") in MAGS else "5")
                    obj["color"] = o.get("color")
                    for key in ("pos", "label"):
                        for s in SIDES:
                            v = (o.get(key) or {}).get(s)
                            obj[key][s] = [float(v[0]), float(v[1])] if v else None
                    pair["objects"].append(obj)
                pairs.append(pair)
            return cls(path, data["image_dir"], pairs, data.get("settings"))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            raise ProjectError(f"Не удалось прочитать проект «{path}»: {e}") from e


_size_cache = {}


def image_size(path):
    """Размер изображения по заголовку файла (без полной загрузки) или None."""
    if not path:
        return None
    if path not in _size_cache:
        try:
            with Image.open(path) as im:
                _size_cache[path] = im.size
        except (OSError, ValueError):
            return None
    return _size_cache[path]


def clear_size_cache():
    _size_cache.clear()


# ---------------------------------------------------------------- экспорт

def to_rgb(img):
    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        img = img.point(lambda v: v / 256).convert("L")
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "La", "RGBa"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img


_font_cache = {}


def load_font(px):
    px = max(1, int(round(px)))
    if px not in _font_cache:
        font = None
        for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                font = ImageFont.truetype(name, px)
                break
            except OSError:
                continue
        _font_cache[px] = font or ImageFont.load_default(px)
    return _font_cache[px]


def hex_to_rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def draw_scale(draw, sb, ox, oy, settings):
    for x0, y0, x1, y1 in sb["rects"]:
        draw.rectangle([round(ox + x0), round(oy + y0), round(ox + x1) - 1, round(oy + y1) - 1], fill="black")
    draw.text((ox + sb["text_x"], oy + sb["baseline"]), sb["text"], fill="black",
              font=load_font(settings["scale_font_px"]), anchor="ms")


def draw_circle(canvas, cx, cy, r, rgb, settings):
    """Сглаженная полупрозрачная окружность (рисуется с суперсэмплингом по фрагменту)."""
    line = settings["circle_line_px"]
    outer = r + line / 2
    x0 = max(0, math.floor(cx - outer) - 2)
    y0 = max(0, math.floor(cy - outer) - 2)
    x1 = min(canvas.width, math.ceil(cx + outer) + 2)
    y1 = min(canvas.height, math.ceil(cy + outer) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    size = max(x1 - x0, y1 - y0)
    ss = 4 if size * 4 <= 4000 else (2 if size * 2 <= 6000 else 1)
    ov = Image.new("RGBA", ((x1 - x0) * ss, (y1 - y0) * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    c = ((cx - x0) * ss, (cy - y0) * ss)
    alpha = round(255 * settings["fill_opacity"] / 100)
    if alpha:
        d.ellipse([c[0] - r * ss, c[1] - r * ss, c[0] + r * ss, c[1] + r * ss], fill=rgb + (alpha,))
    R = outer * ss
    d.ellipse([c[0] - R, c[1] - R, c[0] + R, c[1] + R], outline=rgb + (255,), width=max(1, round(line * ss)))
    if ss > 1:
        ov = ov.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    region = canvas.crop((x0, y0, x1, y1)).convert("RGBA")
    region.alpha_composite(ov)
    canvas.paste(region.convert("RGB"), (x0, y0))


def render_panorama(path, settings):
    """Исходная панорама + белое поле с линейкой. Возвращает (изображение, исходный размер)."""
    with Image.open(path) as im:
        im.load()
        src = to_rgb(im)
    w, h = src.size
    sb = scale_bar(w, h, settings)
    out = Image.new("RGB", (w, h + sb["field_h"]), "white")
    out.paste(src, (0, 0))
    del src
    draw_scale(ImageDraw.Draw(out), sb, 0, 0, settings)
    return out, (w, h)


def save_jpeg(img, path, settings):
    if max(img.size) > JPEG_MAX_SIDE:
        raise ValueError(f"размер {img.width}×{img.height} превышает предел формата JPEG ({JPEG_MAX_SIDE} px)")
    if not settings["compress"]:
        img.save(path, "JPEG", quality=100, subsampling=0, optimize=True)
        return
    q = int(settings["jpeg_quality"])
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= TARGET_FILE_BYTES or q <= MIN_AUTO_QUALITY:
            break
        q = max(MIN_AUTO_QUALITY, q - 4)
    with open(path, "wb") as fh:
        fh.write(buf.getbuffer())


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name or "шлиф"


def export_pair(pair, image_dir, settings, out_root):
    for side in SIDES:
        if not pair[side]:
            raise ValueError(f"не назначено изображение: {SIDE_TITLES[side].lower()}")
        if not os.path.isfile(os.path.join(image_dir, pair[side])):
            raise ValueError(f"файл не найден: {pair[side]}")
    name = safe_filename(pair["name"])
    folder = os.path.join(out_root, name)
    os.makedirs(folder, exist_ok=True)

    imgs, sizes = {}, {}
    for side in SIDES:
        img, sizes[side] = render_panorama(os.path.join(image_dir, pair[side]), settings)
        save_jpeg(img, os.path.join(folder, f"{name} {SIDE_FILE_SUFFIX[side]}.jpg"), settings)
        imgs[side] = img

    f = settings["scale_font_px"]
    margin, gap, title_h = round(f), round(2 * f), round(1.8 * f)
    wp, wx = imgs["parallel"].width, imgs["crossed"].width
    W = margin + wp + gap + wx + margin
    H = margin + title_h + max(imgs["parallel"].height, imgs["crossed"].height) + margin
    if max(W, H) > JPEG_MAX_SIDE:
        raise ValueError(f"схема {W}×{H} превышает предел формата JPEG ({JPEG_MAX_SIDE} px)")
    canvas = Image.new("RGB", (W, H), "white")
    offs = {"parallel": (margin, margin + title_h), "crossed": (margin + wp + gap, margin + title_h)}
    for side in SIDES:
        canvas.paste(imgs[side], offs[side])
        imgs[side] = None
    imgs.clear()

    draw = ImageDraw.Draw(canvas)
    title_font = load_font(f)
    num_font = load_font(settings["number_font_px"])
    for side in SIDES:
        ox, oy = offs[side]
        draw.text((ox + sizes[side][0] / 2, oy - 0.5 * f), SIDE_TITLES[side], fill="black",
                  font=title_font, anchor="ms")
        for i in drawing_order(pair["objects"]):
            o = pair["objects"][i]
            pos = o["pos"][side]
            if not pos:
                continue
            rgb = hex_to_rgb(object_color(o, settings))
            cx, cy = ox + pos[0], oy + pos[1]
            draw_circle(canvas, cx, cy, diameter_px(o["mag"]) / 2, rgb, settings)
            dx, dy = label_offset(o, side, settings)
            draw.text((cx + dx, cy + dy), str(i + 1), fill=rgb, font=num_font, anchor="la")
    save_jpeg(canvas, os.path.join(folder, f"{name} схема.jpg"), settings)


def clone(obj):
    return copy.deepcopy(obj)
