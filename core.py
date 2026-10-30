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
    "color_parallel": "#000000",  # цвет окружностей по умолчанию на параллельных николях
    "color_crossed": "#ffffff",   # … и на скрещённых
    "jpeg_quality": 93,
    "compress": True,
}


def mag_label(mag):
    return f"{mag}×"


def diameter_px(mag, mm_per_px=MM_PER_PX):
    return MAG_DIAMETER_MM[mag] / mm_per_px


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


def strip_prefix(base):
    """Имя без порядкового номера (так называли пары старые версии программы)."""
    return _PREFIX_RE.sub("", base).strip() or base


def new_pair(name, parallel, crossed, problem=""):
    return {"name": name, "parallel": parallel, "crossed": crossed, "problem": problem,
            "color": {"parallel": None, "crossed": None},
            "calib": {"parallel": None, "crossed": None},   # {"mm_per_px", "method", "note"} или None
            "ref_side": None,                                # изображение с правильным масштабом
            "ties": {"parallel": [None, None, None], "crossed": [None, None, None]},
            "align": None,  # совмещение: {"ref_side", "matrix" (исправляемое → эталон), "ref_size"}
            "reshoot": False,
            "objects": []}


def scan_folder(folder):
    groups = {}
    for f in list_images(folder):
        base, crossed = split_name(f)
        g = groups.setdefault(base, {"parallel": [], "crossed": []})
        g["crossed" if crossed else "parallel"].append(f)

    pairs, used = [], set()
    for base in sorted(groups, key=natural_key):
        g = groups[base]
        name = base  # порядковый номер из имени файла сохраняется: «16. Г-170»
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
    return {"mag": mag, "color": {"parallel": None, "crossed": None},
            "pos": {"parallel": None, "crossed": None},
            "label": {"parallel": None, "crossed": None}}


def is_placed(obj):
    return any(obj["pos"][s] for s in SIDES)


def object_color(obj, settings, pair, side):
    """Цвет объекта на панораме side: индивидуальный → общий цвет шлифа → цвет по умолчанию."""
    pair_color = pair["color"][side] if pair else None
    return obj["color"][side] or pair_color or settings["color_" + side]


def side_colors(value):
    """Цвета по сторонам из проекта (старые проекты хранили один цвет строкой)."""
    if isinstance(value, dict):
        return {s: value.get(s) for s in SIDES}
    return {s: value for s in SIDES}


def other_side(side):
    return SIDES[1] if side == SIDES[0] else SIDES[0]


def place_object(obj, side, x, y, sizes, affine=None):
    """Ставит центр на одной панораме, на второй — по привязке (affine: side → другая)
    или, если привязки нет, по относительным координатам."""
    other = other_side(side)
    obj["pos"][side] = [x, y]
    if affine:
        obj["pos"][other] = apply_affine(affine, x, y)
    else:
        obj["pos"][other] = [x / sizes[side][0] * sizes[other][0], y / sizes[side][1] * sizes[other][1]]


def map_delta(dx, dy, side, sizes, affine=None):
    """Сдвиг на панораме side → сдвиг на второй (линейная часть привязки или отношение размеров)."""
    if affine:
        a, b, _, d, e, _ = affine
        return [a * dx + b * dy, d * dx + e * dy]
    other = other_side(side)
    return [dx * sizes[other][0] / sizes[side][0], dy * sizes[other][1] / sizes[side][1]]


def linked_position(obj, side, new, sizes, affine=None):
    """Положение на второй панораме после перемещения на side: тот же сдвиг, перенесённый на неё
    (индивидуальная поправка между панорамами сохраняется)."""
    old, old_other = obj["pos"][side], obj["pos"][other_side(side)]
    dx, dy = map_delta(new[0] - old[0], new[1] - old[1], side, sizes, affine)
    return [old_other[0] + dx, old_other[1] + dy]


def label_offset(obj, side, settings, pair=None):
    """Смещение левого верхнего угла номера относительно центра окружности."""
    off = obj["label"][side]
    if off:
        return off
    line = side_settings(settings, pair, side)["circle_line_px"]
    d = diameter_px(obj["mag"], pair_scale(pair, side)) / 2 * 0.7071 + line
    return [d, d]


def swap_sides(pair):
    unalign_pair(pair)  # совмещение относится к прежнему назначению изображений
    pair["parallel"], pair["crossed"] = pair["crossed"], pair["parallel"]
    for o in pair["objects"]:
        for key in ("pos", "label"):
            o[key]["parallel"], o[key]["crossed"] = o[key]["crossed"], o[key]["parallel"]
    for key in ("calib", "ties"):  # калибровка и точки привязки относятся к самим изображениям
        pair[key]["parallel"], pair[key]["crossed"] = pair[key]["crossed"], pair[key]["parallel"]
    if pair["ref_side"]:
        pair["ref_side"] = other_side(pair["ref_side"])


# ---------------------------------------------------------------- масштаб и привязка

CALIB_METHODS = {
    "width": "по ширине изображения",
    "two_points": "по двум точкам",
    "ties": "по точкам привязки",
    "ties_approx": "по точкам привязки — ПРИБЛИЗИТЕЛЬНО",
}


def aligned_side(pair):
    """Исправленное (приведённое к эталону) изображение пары или None, если пара не совмещена."""
    a = pair.get("align") if pair else None
    return other_side(a["ref_side"]) if a else None


def pair_scale(pair, side):
    """мм/пиксель для панорамы side; по умолчанию — общая калибровка 4632 px = 21 мм.
    После совмещения исправленное изображение имеет масштаб эталона."""
    if pair and aligned_side(pair) == side:
        side = pair["align"]["ref_side"]
    c = pair["calib"][side] if pair else None
    return c["mm_per_px"] if c else MM_PER_PX


PX_SETTINGS = ("scale_font_px", "scale_line_px", "number_font_px", "circle_line_px")


def side_settings(settings, pair, side):
    """Настройки для панорамы side: размеры в px заданы для калибровки 4632 px = 21 мм и пересчитываются
    под масштаб изображения, чтобы подписи и линии имели одинаковый физический размер."""
    k = MM_PER_PX / pair_scale(pair, side)
    if k == 1:
        return settings
    out = dict(settings)
    for key in PX_SETTINGS:
        out[key] = max(1.0, settings[key] * k)
    return out


def calib_text(pair, side):
    c = pair["calib"][side]
    if not c:
        return "по умолчанию (4632 px = 21 мм)"
    text = CALIB_METHODS.get(c["method"], c["method"])
    return f"{text} ({c['note']})" if c.get("note") else text


def _solve3(m, r):
    """Решение системы 3×3 по правилу Крамера."""
    def det(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) -
                a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0]) +
                a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    d = det(m)
    out = []
    for col in range(3):
        mc = [row[:] for row in m]
        for i in range(3):
            mc[i][col] = r[i]
        out.append(det(mc) / d)
    return out


def _degenerate(pts):
    """Точки почти на одной прямой (или совпадают)."""
    (x1, y1), (x2, y2), (x3, y3) = pts
    area2 = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1))
    span = max(math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2]), math.dist(pts[0], pts[2]))
    return span == 0 or area2 < 0.02 * span * span


def pair_affine(pair, src, dst):
    """Аффинное преобразование по трём парам точек: (x, y) на src → (a·x + b·y + c, d·x + e·y + f) на dst.
    None, если точки не расставлены полностью или лежат почти на одной прямой."""
    ps, pd = pair["ties"][src], pair["ties"][dst]
    if None in ps or None in pd or _degenerate(ps) or _degenerate(pd):
        return None
    m = [[x, y, 1.0] for x, y in ps]
    a, b, c = _solve3(m, [p[0] for p in pd])
    d, e, f = _solve3(m, [p[1] for p in pd])
    return (a, b, c, d, e, f)


def apply_affine(t, x, y):
    a, b, c, d, e, f = t
    return [a * x + b * y + c, d * x + e * y + f]


def invert_affine(t):
    a, b, c, d, e, f = t
    det = a * e - b * d
    ia, ib, id_, ie = e / det, -b / det, -d / det, a / det
    return (ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f))


def _convert_objects(pair, side, t):
    """Перевод центров (и ручных смещений номеров) окружностей панорамы side преобразованием t."""
    a, b, _, d, e, _ = t
    for o in pair["objects"]:
        if o["pos"][side]:
            o["pos"][side] = apply_affine(t, *o["pos"][side])
        off = o["label"][side]
        if off:
            o["label"][side] = [a * off[0] + b * off[1], d * off[0] + e * off[1]]


def align_problem(pair):
    """Почему пару нельзя совместить (текст) или None."""
    ref = pair["ref_side"]
    if not ref:
        return "Выберите эталонное изображение."
    if any(None in pair["ties"][s] for s in SIDES):
        return "Поставьте по три точки на обоих изображениях."
    t = pair_affine(pair, other_side(ref), ref)
    if not t:
        return "Точки лежат почти на одной прямой — расставьте их шире."
    if t[0] * t[4] - t[1] * t[3] <= 0:
        return "Точки на изображениях расставлены в разном порядке."
    return None


def align_pair(pair, ref_size):
    """Совмещение: исправляемое изображение приводится к координатам и холсту эталона (ref_size).
    Окружности исправляемой панорамы переводятся в координаты эталона."""
    if align_problem(pair):
        return False
    unalign_pair(pair)
    ref = pair["ref_side"]
    t = pair_affine(pair, other_side(ref), ref)
    _convert_objects(pair, other_side(ref), t)
    pair["align"] = {"ref_side": ref, "matrix": list(t), "ref_size": list(ref_size)}
    return True


def unalign_pair(pair):
    """Возврат к исходному отображению: окружности исправляемой панорамы — снова в её исходных координатах."""
    a = pair.get("align")
    if a:
        _convert_objects(pair, other_side(a["ref_side"]), invert_affine(a["matrix"]))
        pair["align"] = None


def warp_params(pair, side):
    """(размер холста, преобразование «холст эталона → исходное изображение») для исправленной панорамы."""
    if aligned_side(pair) != side:
        return None
    a = pair["align"]
    return tuple(a["ref_size"]), invert_affine(a["matrix"])


# ---------------------------------------------------------------- проверка размеров

PAIR_DIFF_OK, PAIR_DIFF_WARN = 0.05, 0.15
OUTLIER_RATIO = 1.5  # линейный размер отличается от медианы выборки более чем в 1.5 раза


def size_diff(a, b):
    """Наибольшее относительное различие ширины или высоты двух изображений."""
    return max(abs(a[0] - b[0]) / max(a[0], b[0]), abs(a[1] - b[1]) / max(a[1], b[1]))


def size_diagnostics(pairs, size_of):
    """Диагностика разрешений: для каждой пары level ∈ green/yellow/red/none, различие внутри пары
    и отношение линейного размера каждого изображения к медиане всей выборки."""
    sizes = [{s: size_of(p, s) for s in SIDES} for p in pairs]
    linear = sorted(math.sqrt(w * h) for sz in sizes for v in sz.values() if v for w, h in [v])
    median = 0.0
    if linear:
        n = len(linear)
        median = linear[n // 2] if n % 2 else (linear[n // 2 - 1] + linear[n // 2]) / 2
    out = []
    for sz in sizes:
        ratios = {s: (math.sqrt(sz[s][0] * sz[s][1]) / median if sz[s] and median else None) for s in SIDES}
        outlier = any(r and (r > OUTLIER_RATIO or r < 1 / OUTLIER_RATIO) for r in ratios.values())
        diff = size_diff(sz["parallel"], sz["crossed"]) if all(sz.values()) else None
        if diff is None:
            level = "none"
        elif diff > PAIR_DIFF_WARN:
            level = "red"
        elif diff > PAIR_DIFF_OK or outlier:
            level = "yellow"
        else:
            level = "green"
        out.append({"level": level, "diff": diff, "ratios": ratios, "outlier": outlier,
                    "sizes": sz, "median": median})
    return out


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
    return math.ceil(0.9 * f + 0.15 * f + max(2.5 * t, 0.3 * f) + 0.2 * f)


def scale_bar(img_w, img_h, settings, mm_per_px=MM_PER_PX):
    """Геометрия линейки в координатах исходного изображения (белое поле начинается с y = img_h).

    Линейка прижата к правому краю, подпись над ней; поле по высоте минимальное.
    """
    f, t = settings["scale_font_px"], settings["scale_line_px"]
    mm = choose_scale_mm(img_w * mm_per_px)
    length = mm / mm_per_px
    tick = max(2.5 * t, 0.3 * f)
    baseline = img_h + 0.9 * f
    tick_top = baseline + 0.15 * f
    tick_bottom = tick_top + tick
    bar_y = (tick_top + tick_bottom) / 2
    x1 = img_w - t / 2 - 5  # правый край штриха на 5 px левее правого края изображения
    x0 = x1 - length
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


def _restore_number(name, filename):
    """Старые проекты хранили имя без порядкового номера («Г-170»); возвращаем «16. Г-170» из имени файла."""
    if filename:
        base = split_name(filename)[0]
        if base != name and strip_prefix(base) == name:
            return base
    return name


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
                pair = new_pair(_restore_number(p["name"], p.get("parallel") or p.get("crossed")),
                                p.get("parallel"), p.get("crossed"), p.get("problem", ""))
                pair["color"] = side_colors(p.get("color"))
                for s in SIDES:
                    c = (p.get("calib") or {}).get(s)
                    if c and float(c.get("mm_per_px", 0)) > 0:
                        pair["calib"][s] = {"mm_per_px": float(c["mm_per_px"]), "method": str(c.get("method", "")),
                                            "note": str(c.get("note", ""))}
                    ties = (p.get("ties") or {}).get(s) or []
                    pair["ties"][s] = [[float(t[0]), float(t[1])] if t else None
                                       for t in (list(ties) + [None, None, None])[:3]]
                pair["ref_side"] = p.get("ref_side") if p.get("ref_side") in SIDES else None
                a = p.get("align")
                if a and a.get("ref_side") in SIDES and len(a.get("matrix") or []) == 6:
                    pair["align"] = {"ref_side": a["ref_side"], "matrix": [float(v) for v in a["matrix"]],
                                     "ref_size": [int(v) for v in a["ref_size"]]}
                pair["reshoot"] = bool(p.get("reshoot", False))
                for o in p.get("objects", []):
                    obj = new_object(o["mag"] if o.get("mag") in MAGS else "5")
                    obj["color"] = side_colors(o.get("color"))
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
    """Сглаженный контур окружности без заливки (суперсэмплинг по фрагменту)."""
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
    R = outer * ss
    d.ellipse([c[0] - R, c[1] - R, c[0] + R, c[1] + R], outline=rgb + (255,), width=max(1, round(line * ss)))
    if ss > 1:
        ov = ov.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    region = canvas.crop((x0, y0, x1, y1)).convert("RGBA")
    region.alpha_composite(ov)
    canvas.paste(region.convert("RGB"), (x0, y0))


def render_panorama(path, settings, mm_per_px=MM_PER_PX, warp=None):
    """Панорама + белое поле с линейкой. Возвращает (изображение, размер панорамы).
    warp = (размер холста эталона, преобразование холст → исходное): исправленное (совмещённое) изображение."""
    with Image.open(path) as im:
        im.load()
        src = to_rgb(im)
    if warp:
        size, t = warp
        src = src.transform(size, Image.Transform.AFFINE, t, resample=Image.Resampling.BICUBIC,
                            fillcolor=(255, 255, 255))
    w, h = src.size
    sb = scale_bar(w, h, settings, mm_per_px)
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
        img, sizes[side] = render_panorama(os.path.join(image_dir, pair[side]),
                                           side_settings(settings, pair, side), pair_scale(pair, side),
                                           warp_params(pair, side))
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
    for side in SIDES:
        ox, oy = offs[side]
        ss = side_settings(settings, pair, side)
        num_font = load_font(ss["number_font_px"])
        draw.text((ox + sizes[side][0] / 2, oy - 0.5 * f), SIDE_TITLES[side], fill="black",
                  font=title_font, anchor="ms")
        for i in drawing_order(pair["objects"]):
            o = pair["objects"][i]
            pos = o["pos"][side]
            if not pos:
                continue
            rgb = hex_to_rgb(object_color(o, settings, pair, side))
            cx, cy = ox + pos[0], oy + pos[1]
            draw_circle(canvas, cx, cy, diameter_px(o["mag"], pair_scale(pair, side)) / 2, rgb, ss)
            dx, dy = label_offset(o, side, settings, pair)
            draw.text((cx + dx, cy + dy), str(i + 1), fill=rgb, font=num_font, anchor="la")
    save_jpeg(canvas, os.path.join(folder, f"{name} схема.jpg"), settings)


def clone(obj):
    return copy.deepcopy(obj)
