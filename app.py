"""Оформление панорам петрографических шлифов и разметка участков детальной съёмки."""
import copy
import os
import sys
from collections import OrderedDict

from PIL import Image
from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont, QFontMetricsF, QIcon, QImage,
                           QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGraphicsEllipseItem,
                               QGraphicsItem, QGraphicsScene, QButtonGroup, QInputDialog, QRadioButton, QHBoxLayout,
                               QGraphicsSimpleTextItem, QGraphicsView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressDialog, QPushButton, QSpinBox, QSplitter, QStyle,
                               QStyleOptionGraphicsItem, QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QGridLayout)

import core

PREVIEW_MAX = 4096   # максимальная сторона предпросмотра, px
PREVIEW_CACHE = 6    # сколько предпросмотров держать в памяти
UNDO_LIMIT = 100
FONT_FAMILY = "Arial"
VIEW_BG = "#1f2329"

STYLE = """
QMainWindow, QDialog { background: #f3f4f6; }
QToolBar { background: #ffffff; border: none; border-bottom: 1px solid #e5e7eb; padding: 6px 10px; spacing: 4px; }
QToolBar::separator { width: 1px; background: #e5e7eb; margin: 6px 8px; }
QToolButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 5px 10px; }
QToolButton:hover { background: #f1f5f9; border-color: #e2e8f0; }
QToolButton:checked { background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; }
QToolButton:disabled { color: #9ca3af; }
QToolButton#primary { background: #2563eb; color: #ffffff; font-weight: 600; }
QToolButton#primary:hover { background: #1d4ed8; }
QToolButton#primary:disabled { background: #bfdbfe; }
QToolButton#tool { border: 1px solid #e5e7eb; background: #ffffff; padding: 4px 2px; }
QToolButton#tool:hover { background: #f8fafc; border-color: #cbd5e1; }
QToolButton#tool:checked { background: #dbeafe; border-color: #3b82f6; color: #1d4ed8; }
QFrame#card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; }
QLabel#header { color: #6b7280; font-size: 8pt; font-weight: 600; }
QLabel#hint { color: #9ca3af; font-size: 8pt; }
QLabel#viewTitle { font-weight: 600; color: #111827; padding: 2px 4px 6px 4px; }
QPushButton { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #f8fafc; border-color: #9ca3af; }
QPushButton:pressed { background: #eef2f7; }
QPushButton:disabled { color: #9ca3af; border-color: #e5e7eb; }
QPushButton#danger { color: #b91c1c; }
QPushButton#danger:disabled { color: #9ca3af; }
QListWidget, QTreeWidget { background: #ffffff; border: none; outline: 0; }
QListWidget::item, QTreeWidget::item { padding: 5px 6px; border-radius: 6px; }
QListWidget::item:hover, QTreeWidget::item:hover { background: #f1f5f9; }
QListWidget::item:selected, QTreeWidget::item:selected { background: #dbeafe; color: #1e3a8a; }
QStatusBar { background: #ffffff; border-top: 1px solid #e5e7eb; color: #4b5563; }
QSplitter::handle { background: transparent; }
QGraphicsView { border: none; border-radius: 6px; }
QToolTip { background: #111827; color: #ffffff; border: none; padding: 4px 8px; }
QMenu { background: #ffffff; border: 1px solid #e5e7eb; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 4px; }
QMenu::item:selected { background: #dbeafe; color: #1e3a8a; }
"""


def make_font(px):
    f = QFont(FONT_FAMILY)
    f.setPixelSize(max(1, round(px)))
    return f


# ---------------------------------------------------------------- загрузка предпросмотров

class _LoaderSignals(QObject):
    done = Signal(str, object, str)  # ключ, QImage или None, текст ошибки


def preview_key(path, warp):
    """Ключ кэша предпросмотра: исходное изображение или исправленное (совмещённое с эталоном)."""
    if not warp:
        return path
    (w, h), t = warp
    return f"{path}|{w}x{h}|" + ",".join(f"{v:.9g}" for v in t)


class _LoadTask(QRunnable):
    def __init__(self, key, path, warp, signals):
        super().__init__()
        self.key, self.path, self.warp, self.signals = key, path, warp, signals

    def run(self):
        try:
            with Image.open(self.path) as im:
                tw, th = im.size
                im.draft("RGB", (PREVIEW_MAX, PREVIEW_MAX))
                img = core.to_rgb(im)
                img.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.Resampling.LANCZOS)
                if self.warp:  # холст эталона в уменьшенном виде; преобразование пересчитано под предпросмотры
                    (w, h), (a, b, c, d, e, f) = self.warp
                    k = min(1.0, PREVIEW_MAX / max(w, h))
                    sx, sy = img.width / tw, img.height / th
                    img = img.transform((max(1, round(w * k)), max(1, round(h * k))), Image.Transform.AFFINE,
                                        (sx * a / k, sx * b / k, sx * c, sy * d / k, sy * e / k, sy * f),
                                        resample=Image.Resampling.BILINEAR, fillcolor=(255, 255, 255))
                data = img.tobytes("raw", "RGB")  # пока файл открыт: небольшое RGB-изображение не копируется
            qimg = QImage(data, img.width, img.height, 3 * img.width, QImage.Format.Format_RGB888).copy()
            self.signals.done.emit(self.key, qimg, "")
        except Exception as e:  # noqa: BLE001 — показать пользователю любую ошибку чтения
            self.signals.done.emit(self.key, None, str(e))


class PreviewLoader(QObject):
    """Фоновая загрузка уменьшенных копий с небольшим кэшем в памяти."""
    loaded = Signal(str)

    def __init__(self):
        super().__init__()
        self.cache = OrderedDict()
        self.errors = {}
        self.queue, self.running = [], set()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(2)
        self.signals = _LoaderSignals()
        self.signals.done.connect(self._on_done)

    def get(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def want(self, items):
        """Задать очередь загрузки [(путь, warp), …] в порядке приоритета, заменив прежнюю."""
        queue = {}
        for path, warp in items:
            key = preview_key(path, warp)
            if key not in self.cache and key not in self.running and key not in self.errors:
                queue.setdefault(key, (path, warp))
        self.queue = list(queue.items())
        self._pump()

    def _pump(self):
        while self.queue and len(self.running) < 2:
            key, (path, warp) = self.queue.pop(0)
            self.running.add(key)
            self.pool.start(_LoadTask(key, path, warp, self.signals))

    def _on_done(self, key, img, err):
        self.running.discard(key)
        if img is not None:
            self.cache[key] = img
            while len(self.cache) > PREVIEW_CACHE:
                self.cache.popitem(last=False)
        else:
            self.errors[key] = err
        self.loaded.emit(key)
        self._pump()


# ---------------------------------------------------------------- графические элементы

class LabelItem(QGraphicsSimpleTextItem):
    """Номер окружности; при перетаскивании сообщает окну, чтобы сдвинуть номер на второй панораме."""
    def __init__(self, text, parent, on_moved):
        super().__init__(text, parent)
        self.on_moved = on_moved

    def itemChange(self, change, value):
        if change == QGraphicsSimpleTextItem.GraphicsItemChange.ItemPositionHasChanged and self.on_moved:
            self.on_moved("label")
        return super().itemChange(change, value)


class CircleItem(QGraphicsEllipseItem):
    def __init__(self, index, obj, side, settings, editable, pair, on_moved):
        r = core.diameter_px(obj["mag"], core.pair_scale(pair, side)) / 2
        settings = core.side_settings(settings, pair, side)
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.index = index
        self.highlight = False
        self.on_moved = None
        color = QColor(core.object_color(obj, settings, pair, side))
        self.setPen(QPen(color, settings["circle_line_px"]))
        self.setBrush(Qt.BrushStyle.NoBrush)  # без заливки; щелчок внутри всё равно выбирает окружность
        self.setPos(QPointF(*obj["pos"][side]))
        self.setZValue(-r)  # мелкие окружности поверх крупных
        self.setData(0, index)

        self.label = LabelItem(str(index + 1), self, None)
        self.label.setFont(make_font(settings["number_font_px"]))
        self.label.setBrush(QBrush(color))
        self.label.setPen(QPen(Qt.PenStyle.NoPen))
        self.label.setPos(QPointF(*core.label_offset(obj, side, settings, pair)))
        self.label.setData(0, index)
        if editable:
            flags = QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable | \
                QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable | \
                QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges
            self.setFlags(flags)
            self.label.setFlags(flags)
            self.on_moved = lambda kind: on_moved(index, kind)
            self.label.on_moved = self.on_moved

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged and self.on_moved:
            self.on_moved("pos")
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        opt = QStyleOptionGraphicsItem(option)
        opt.state = opt.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, widget)
        if self.highlight:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for color, style in ((Qt.GlobalColor.black, Qt.PenStyle.SolidLine),
                                 (Qt.GlobalColor.white, Qt.PenStyle.DashLine)):
                pen = QPen(color, 2, style)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.drawEllipse(self.rect())


class MarkerItem(QGraphicsItem):
    """Точка привязки с номером; размер на экране не зависит от масштаба просмотра."""
    COLOR = QColor("#ff2bd6")

    def __init__(self, text, movable):
        super().__init__()
        self.text = text
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        if movable:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setZValue(2e6)

    def boundingRect(self):
        return QRectF(-13, -30, 50, 44)

    def paint(self, p, option, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), 8, 8)
        for a, b in (((-12, 0), (12, 0)), ((0, -12), (0, 12))):
            path.moveTo(*a)
            path.lineTo(*b)
        font = make_font(18)
        font.setBold(True)
        path.addText(QPointF(12, -10), font, self.text)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#ffffff"), 4))
        p.drawPath(path)
        p.setPen(QPen(self.COLOR, 2))
        p.drawPath(path)


class PanoramaView(QGraphicsView):
    def __init__(self, side, main):
        super().__init__()
        self.side, self.main = side, main
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform |
                            QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor(VIEW_BG))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.scene().selectionChanged.connect(self._selection_changed)
        self.img_size = None          # исходный размер изображения или None
        self.placeholder = []
        self.scale_items = []
        self.circles = {}
        self.ties = {}                # номер точки привязки → MarkerItem
        self.preview_key = None       # ключ предпросмотра, показанного на этой панораме
        self.ghost = None
        self._pan = None
        self._press = None
        self._space = False

    # --- содержимое
    def load(self, size, message):
        self.main.syncing += 1
        self.scene().clear()
        self.main.syncing -= 1
        self.placeholder, self.scale_items, self.circles, self.ghost = [], [], {}, None
        self.ties = {}
        self.img_size = size
        w, h = size or (4632, 3488)
        rect = self.scene().addRect(QRectF(0, 0, w, h), QPen(Qt.PenStyle.NoPen), QBrush(QColor("#2d323a")))
        rect.setZValue(-1e6)
        text = self.scene().addSimpleText(message, make_font(w / 30))
        text.setBrush(QColor("#9ca3af"))
        text.setPos((w - text.boundingRect().width()) / 2, (h - text.boundingRect().height()) / 2)
        text.setZValue(-1e6)
        self.placeholder = [rect, text]
        self.setSceneRect(QRectF(0, 0, w, h))

    def set_pixmap(self, qimg):
        if self.img_size is None:
            return
        for it in self.placeholder:
            self.scene().removeItem(it)
        self.placeholder = []
        item = self.scene().addPixmap(QPixmap.fromImage(qimg))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setScale(self.img_size[0] / qimg.width())
        item.setZValue(-1e6)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def build_scale(self, settings, pair):
        for it in self.scale_items:
            self.scene().removeItem(it)
        self.scale_items = []
        if self.img_size is None:
            return
        w, h = self.img_size
        settings = core.side_settings(settings, pair, self.side)
        sb = core.scale_bar(w, h, settings, core.pair_scale(pair, self.side))
        no_pen = QPen(Qt.PenStyle.NoPen)
        self.scale_items.append(self.scene().addRect(QRectF(0, h, w, sb["field_h"]), no_pen, QBrush(Qt.GlobalColor.white)))
        for x0, y0, x1, y1 in sb["rects"]:
            self.scale_items.append(self.scene().addRect(QRectF(x0, y0, x1 - x0, y1 - y0), no_pen,
                                                         QBrush(Qt.GlobalColor.black)))
        font = make_font(settings["scale_font_px"])
        fm = QFontMetricsF(font)
        text = self.scene().addSimpleText(sb["text"], font)
        text.setPos(sb["text_x"] - fm.horizontalAdvance(sb["text"]) / 2, sb["baseline"] - fm.ascent())
        self.scale_items.append(text)
        for it in self.scale_items:
            it.setZValue(-1e5)
            it.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        margin = w * 0.02
        self.setSceneRect(QRectF(-margin, -margin, w + 2 * margin, h + sb["field_h"] + 2 * margin))

    def build_circles(self, pair, settings, editable, selected):
        self.main.syncing += 1
        for it in self.circles.values():
            self.scene().removeItem(it)
        self.circles = {}
        if self.img_size is not None and pair:
            for i, o in enumerate(pair["objects"]):
                if o["pos"][self.side]:
                    item = CircleItem(i, o, self.side, settings, editable, pair,
                                      lambda idx, kind: self.main.item_moved(self.side, idx, kind))
                    item.highlight = i == selected
                    self.scene().addItem(item)
                    self.circles[i] = item
        self.main.syncing -= 1

    def build_ties(self, pair, visible):
        for it in self.ties.values():
            self.scene().removeItem(it)
        self.ties = {}
        if not visible or not pair or self.img_size is None:
            return
        for n, pt in enumerate(pair["ties"][self.side]):
            if pt:
                item = MarkerItem(str(n + 1), movable=True)
                item.setPos(QPointF(*pt))
                self.scene().addItem(item)
                self.ties[n] = item

    def set_highlight(self, index):
        for i, it in self.circles.items():
            if it.highlight != (i == index):
                it.highlight = i == index
                it.update()

    def fit(self):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def show_area(self, center, size):
        self.fitInView(QRectF(center[0] - size / 2, center[1] - size / 2, size, size),
                       Qt.AspectRatioMode.KeepAspectRatio)

    def update_cursor(self):
        if self._pan is not None:
            shape = Qt.CursorShape.ClosedHandCursor
        elif self._space:
            shape = Qt.CursorShape.OpenHandCursor
        elif self.main.placing:
            shape = Qt.CursorShape.CrossCursor
        else:
            shape = Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(shape)

    # --- призрачная окружность режима размещения
    def update_ghost(self, scene_pos=None):
        pl = self.main.placing
        if not pl or self.img_size is None or scene_pos is None:
            if self.ghost:
                self.ghost.hide()
            return
        r = core.diameter_px(pl["mag"], core.pair_scale(self.main.pair(), self.side)) / 2
        if self.ghost is None:
            self.ghost = QGraphicsEllipseItem()
            self.ghost.setZValue(1e6)
            self.ghost.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene().addItem(self.ghost)
        s = core.side_settings(self.main.project.settings, self.main.pair(), self.side)
        color = QColor(core.object_color(core.new_object(pl["mag"]), s, self.main.pair(), self.side))
        self.ghost.setPen(QPen(color, s["circle_line_px"], Qt.PenStyle.DashLine))
        self.ghost.setBrush(Qt.BrushStyle.NoBrush)
        self.ghost.setRect(-r, -r, 2 * r, 2 * r)
        self.ghost.setPos(scene_pos)
        self.ghost.show()

    # --- события
    def _selection_changed(self):
        if self.main.syncing:
            return
        items = self.scene().selectedItems()
        self.main.set_selection(items[0].data(0) if items else None, source=self.side)

    def wheelEvent(self, e):
        factor = 1.0015 ** e.angleDelta().y()
        current = self.transform().m11()
        if 0.002 < current * factor < 30:
            self.scale(factor, factor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton or (e.button() == Qt.MouseButton.LeftButton and self._space):
            self._pan = e.position()
            self.update_cursor()
            return
        if e.button() == Qt.MouseButton.LeftButton and self.main.placing:
            self._press = e.position()
            return
        if e.button() == Qt.MouseButton.LeftButton and not self.main.marking:
            # «Проверка пар»: точки привязки перетаскиваются, щелчок по пустому месту ставит точку
            if not isinstance(self.itemAt(e.position().toPoint()), MarkerItem):
                self._press = e.position()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(d.y()))
            return
        if self.main.placing:
            self.update_ghost(self.mapToScene(e.position().toPoint()))
            return
        self.main.move_alone = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.update_cursor()
            return
        if self._press is not None and e.button() == Qt.MouseButton.LeftButton:
            moved = (e.position() - self._press).manhattanLength()
            self._press = None
            if moved < 6 and (self.main.placing or not self.main.marking):
                p = self.mapToScene(e.position().toPoint())
                if not (self.img_size and 0 <= p.x() <= self.img_size[0] and 0 <= p.y() <= self.img_size[1]):
                    self.main.statusBar().showMessage("Щёлкните внутри изображения панорамы", 3000)
                elif self.main.placing:
                    self.main.place_at(self.side, p.x(), p.y())
                else:
                    self.main.check_click(self.side, p.x(), p.y())
            return
        super().mouseReleaseEvent(e)
        if e.button() == Qt.MouseButton.LeftButton:
            self.main.sync_positions(self.side)
            self.main.sync_ties()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.update_cursor()
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = False
            self._pan = None
            self.update_cursor()
            return
        super().keyReleaseEvent(e)

    def enterEvent(self, e):
        if not is_text_input(QApplication.focusWidget()):
            self.setFocus()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self.ghost:
            self.ghost.hide()
        super().leaveEvent(e)


def is_text_input(w):
    if isinstance(w, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
        return True
    return isinstance(w, QComboBox) and w.isEditable()


# ---------------------------------------------------------------- диалоги

class SettingsDialog(QDialog):
    def __init__(self, settings, parent):
        super().__init__(parent)
        self.setWindowTitle("Настройки оформления")
        self.s = dict(settings)
        form = QFormLayout(self)
        self.spins = {}
        for key, title, lo, hi, suffix in (
                ("scale_font_px", "Размер шрифта масштабной линейки", 10, 3000, " px"),
                ("scale_line_px", "Толщина линии масштабной линейки", 1, 500, " px"),
                ("number_font_px", "Размер шрифта номеров окружностей", 10, 3000, " px"),
                ("circle_line_px", "Толщина контура окружностей", 1, 500, " px"),
                ("jpeg_quality", "Качество JPEG при экспорте", 50, 100, "")):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setSuffix(suffix)
            sp.setValue(int(self.s[key]))
            form.addRow(title, sp)
            self.spins[key] = sp
        self.color_btns = {}
        for side in core.SIDES:
            b = QPushButton()
            b.clicked.connect(lambda checked=False, sd=side: self._pick_color(sd))
            form.addRow(f"Цвет по умолчанию: {core.SIDE_TITLES[side].lower()}", b)
            self.color_btns[side] = b
        self._show_colors()
        note = QLabel("Размеры указаны в пикселях исходного изображения.")
        note.setObjectName("hint")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _show_colors(self):
        for side, b in self.color_btns.items():
            b.setText(self.s["color_" + side])
            b.setIcon(color_icon(self.s["color_" + side]))

    def _pick_color(self, side):
        c = QColorDialog.getColor(QColor(self.s["color_" + side]), self,
                                  f"Цвет по умолчанию — {core.SIDE_TITLES[side].lower()}")
        if c.isValid():
            self.s["color_" + side] = c.name()
            self._show_colors()

    def result_settings(self):
        for key, sp in self.spins.items():
            self.s[key] = sp.value()
        return self.s


class AssignDialog(QDialog):
    def __init__(self, pair, files, parent):
        super().__init__(parent)
        self.setWindowTitle(f"Назначение изображений — {pair['name']}")
        form = QFormLayout(self)
        self.combos = {}
        for side in core.SIDES:
            cb = QComboBox()
            cb.addItem("— не назначено —", None)
            for f in files:
                cb.addItem(f, f)
            idx = cb.findData(pair[side])
            cb.setCurrentIndex(max(0, idx))
            cb.setMinimumWidth(350)
            form.addRow(core.SIDE_TITLES[side], cb)
            self.combos[side] = cb
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self):
        return {side: cb.currentData() for side, cb in self.combos.items()}


def color_icon(*colors, size=16):
    """Образец цвета; для двух цветов — левая и правая половины (параллельные / скрещённые)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    w = size / len(colors)
    for n, c in enumerate(colors):
        p.fillRect(QRectF(n * w, 0, w, size), QColor(c))
    p.setPen(QPen(QColor("#9ca3af"), 1))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(0, 0, size - 1, size - 1)
    p.end()
    return QIcon(pm)


LEVEL_COLORS = {"green": "#16a34a", "yellow": "#ca8a04", "red": "#dc2626", "none": "#9ca3af"}


def level_icon(level):
    """Кружок-индикатор результата проверки размеров пары."""
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(LEVEL_COLORS[level]))
    p.drawEllipse(QRectF(2, 2, 10, 10))
    p.end()
    return QIcon(pm)


def _icon(draw):
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(p)
    p.end()
    return QIcon(pm)


def mag_icon(mag):
    d = 10 + 48 * core.MAG_DIAMETER_MM[mag] / core.MAG_DIAMETER_MM["2.5"]

    def draw(p):
        p.setPen(QPen(QColor("#ca8a04"), 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(32 - d / 2, 32 - d / 2, d, d))
    return _icon(draw)


def cursor_icon():
    def draw(p):
        p.setPen(QPen(QColor("#1f2937"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(QColor("#ffffff"))
        pts = [(20, 8), (20, 50), (30, 40), (37, 56), (44, 53), (37, 37), (50, 37)]
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
    return _icon(draw)


def card(*widgets, title=None):
    """Белая карточка с необязательным заголовком."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(10, 10, 10, 10)
    lay.setSpacing(8)
    if title:
        h = QLabel(title.upper())
        h.setObjectName("header")
        lay.addWidget(h)
    for w in widgets:
        lay.addWidget(w)
    return frame, lay


# ---------------------------------------------------------------- экспорт в фоне

class ExportWorker(QThread):
    progress = Signal(int, str)
    report = Signal(list, int, bool)

    def __init__(self, pairs, image_dir, settings, out_dir):
        super().__init__()
        self.pairs, self.image_dir, self.settings, self.out_dir = pairs, image_dir, settings, out_dir
        self.cancelled = False

    def run(self):
        errors, done = [], 0
        for i, pair in enumerate(self.pairs):
            if self.cancelled:
                break
            self.progress.emit(i, pair["name"])
            try:
                core.export_pair(pair, self.image_dir, self.settings, self.out_dir)
                done += 1
            except Exception as e:  # noqa: BLE001 — ошибка одного шлифа не прерывает пакет
                errors.append(f"{pair['name']}: {e}")
        self.report.emit(errors, done, self.cancelled)


# ---------------------------------------------------------------- главное окно

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.project = None
        self.cur = -1
        self.sel = None
        self.placing = None    # {"mag": ..., "target": индекс или None}
        self.marking = False
        self.syncing = 0
        self.move_alone = False  # Shift при перетаскивании: двигать только на одной панораме
        self.diag = []           # диагностика размеров по парам
        self.undo, self.redo = {}, {}
        self.worker = None
        self.last_export_dir = ""
        self.loader = PreviewLoader()
        self.loader.loaded.connect(self._preview_loaded)
        self.save_timer = QTimer(self, singleShot=True, interval=2000, timeout=self.save_project)
        self._build_ui()
        self._update_title()

    # --- интерфейс
    def _build_ui(self):
        tb = self.addToolBar("Главная")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.addAction("Новый проект…", self.new_project)
        tb.addAction("Открыть…", self.open_project)
        tb.addAction("Сохранить", self.save_project).setToolTip("Сохранить проект (Ctrl+S)")
        tb.addSeparator()
        group = QActionGroup(self)
        self.act_view = QAction("Проверка пар", self, checkable=True, checked=True)
        self.act_mark = QAction("Разметка", self, checkable=True)
        for a in (self.act_view, self.act_mark):
            group.addAction(a)
            tb.addAction(a)
        self.act_view.triggered.connect(lambda: self.set_marking(False))
        self.act_mark.triggered.connect(lambda: self.set_marking(True))
        tb.addSeparator()
        tb.addAction("Показать целиком", self.fit_views)
        tb.addAction("Настройки", self.edit_settings)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding, spacer.sizePolicy().Policy.Preferred)
        tb.addWidget(spacer)
        self.compress_cb = QCheckBox("Сжимать изображения")
        self.compress_cb.setChecked(True)
        self.compress_cb.toggled.connect(self._compress_toggled)
        tb.addWidget(self.compress_cb)
        tb.addSeparator()
        self.act_exp_cur = tb.addAction("Экспорт текущего", lambda: self.export(False))
        self.act_exp_all = tb.addAction("Экспорт всех", lambda: self.export(True))
        tb.widgetForAction(self.act_exp_all).setObjectName("primary")

        # левая область — список шлифов
        self.pair_list = QListWidget()
        self.pair_list.currentRowChanged.connect(self.select_pair)
        b_swap = QPushButton("Поменять местами")
        b_swap.setToolTip("Поменять местами изображения параллельных и скрещённых николей")
        b_swap.clicked.connect(self.swap_pair)
        b_assign = QPushButton("Назначить файлы…")
        b_assign.clicked.connect(self.assign_files)
        b_del = QPushButton("Удалить из списка…")
        b_del.setObjectName("danger")
        b_del.setToolTip("Убрать пару из проекта; исходные фотографии на диске не удаляются")
        b_del.clicked.connect(self.delete_pair)
        left, lv = card(self.pair_list, b_swap, b_assign, b_del, title="Шлифы")

        # центральная область — две панорамы
        center = QSplitter(Qt.Orientation.Horizontal)
        center.setHandleWidth(8)
        self.views, self.view_titles = {}, {}
        for side in core.SIDES:
            title = QLabel(core.SIDE_TITLES[side])
            self.view_titles[side] = title
            title.setObjectName("viewTitle")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view = PanoramaView(side, self)
            frame, _ = card(title, view)
            center.addWidget(frame)
            self.views[side] = view

        # правая область — инструменты разметки
        tools = QWidget()
        tl = QHBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        self.tool_buttons = {}
        for key, text, icon, tip in (
                [("cursor", "Курсор", cursor_icon(), "Выбор и перемещение окружностей и номеров (Esc)")] +
                [(m, core.mag_label(m), mag_icon(m),
                  f"Размещение окружностей {core.mag_label(m)} — {core.MAG_DIAMETER_MM[m]} мм "
                  f"(клавиша {n + 1})") for n, m in enumerate(core.MAGS)]):
            b = QToolButton()
            b.setObjectName("tool")
            b.setText(text)
            b.setIcon(icon)
            b.setIconSize(QSize(32, 32))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setSizePolicy(b.sizePolicy().Policy.Expanding, b.sizePolicy().Policy.Fixed)
            b.clicked.connect(lambda checked, k=key: self._tool_clicked(k))
            tl.addWidget(b)
            self.tool_buttons[key] = b
        hint = QLabel("Щелчок по панораме ставит окружность. Esc — курсор. Перетаскивание сдвигает "
                      "окружность на обеих панорамах, с Shift — только на одной.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        b_unplaced = QPushButton("Добавить неразмещённый снимок")
        menu = QMenu(b_unplaced)
        for mag in core.MAGS:
            menu.addAction(core.mag_label(mag), lambda m=mag: self.add_unplaced(m))
        b_unplaced.setMenu(menu)
        tools_card, _ = card(tools, hint, b_unplaced, title="Инструменты")

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.itemSelectionChanged.connect(self._tree_selection)
        self.tree.itemDoubleClicked.connect(lambda *_: self.goto_selected())
        self.mag_combo = QComboBox()
        for mag in core.MAGS:
            self.mag_combo.addItem(core.mag_label(mag), mag)
        self.mag_combo.activated.connect(self._mag_changed)
        mag_row = QWidget()
        mr = QHBoxLayout(mag_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(QLabel("Увеличение"))
        mr.addWidget(self.mag_combo, 1)
        btns = QWidget()
        bg = QGridLayout(btns)
        bg.setContentsMargins(0, 0, 0, 0)
        bg.setSpacing(6)
        self.obj_buttons = []
        self.obj_color_btns = {}
        for n, side in enumerate(core.SIDES):
            b = QPushButton(core.SIDE_TITLES[side].split()[0])
            b.setToolTip(f"Цвет выбранной окружности и её номера — {core.SIDE_TITLES[side].lower()}")
            b.clicked.connect(lambda checked=False, sd=side: self.change_color(sd))
            bg.addWidget(b, 0, n)
            self.obj_buttons.append(b)
            self.obj_color_btns[side] = b
        for n, (text, slot, tip) in enumerate((
                ("Указать положение", self.place_selected,
                 "Следующий щелчок по панораме задаст положение выбранного объекта"),
                ("Показать", self.goto_selected, "Показать выбранную окружность на панорамах"),
                ("Удалить", self.delete_selected, "Удалить выбранный объект (Delete)"))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            if n == 0:
                bg.addWidget(b, 1, 0, 1, 2)
            else:
                bg.addWidget(b, 2, n - 1)
            self.obj_buttons.append(b)
        self.obj_buttons[-1].setObjectName("danger")
        color_label = QLabel("Цвет окружности")
        objects_card, _ = card(self.tree, mag_row, color_label, btns, title="Объекты")

        color_row = QWidget()
        cr = QGridLayout(color_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(6)
        self.pair_color_btns = {}
        for n, side in enumerate(core.SIDES):
            b = QPushButton(core.SIDE_TITLES[side].split()[0])
            b.setToolTip(f"Все окружности и номера этого шлифа на панораме «{core.SIDE_TITLES[side]}» — "
                         "выбранным цветом; другие шлифы не меняются")
            b.clicked.connect(lambda checked=False, sd=side: self.set_pair_color(sd))
            cr.addWidget(b, 0, n)
            self.pair_color_btns[side] = b
        self.pair_color_reset = QPushButton("Сбросить цвета шлифа")
        self.pair_color_reset.setToolTip("Вернуть всем окружностям этого шлифа цвета по умолчанию")
        self.pair_color_reset.clicked.connect(self.reset_pair_color)
        cr.addWidget(self.pair_color_reset, 1, 0, 1, 2)
        color_card, _ = card(color_row, title="Цвет всех окружностей шлифа")

        self.right = QWidget()
        rv = QVBoxLayout(self.right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        rv.addWidget(tools_card)
        rv.addWidget(objects_card, 1)
        rv.addWidget(color_card)
        self.right.setVisible(False)
        self.right_check = self._build_check_panel()

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(8)
        split.addWidget(left)
        split.addWidget(center)
        split.addWidget(self.right)
        split.addWidget(self.right_check)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 1200, 320, 320])
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(8, 8, 8, 8)
        ol.addWidget(split)
        self.setCentralWidget(outer)
        self._update_controls()
        self._update_mag_buttons()
        self.update_check_panel()

        for key, fn in (("1", lambda: self.start_placing("2.5")), ("2", lambda: self.start_placing("5")),
                        ("3", lambda: self.start_placing("10")), ("4", lambda: self.start_placing("20")),
                        ("Esc", self.escape), ("Delete", self.delete_selected),
                        ("Ctrl+S", self.save_project), ("Ctrl+Z", self.do_undo), ("Ctrl+Y", self.do_redo)):
            QShortcut(QKeySequence(key), self, activated=lambda f=fn: self._hotkey(f))

    def _build_check_panel(self):
        """Правая панель режима «Проверка пар»: размеры, совмещение по трём точкам, статус."""
        def label():
            lb = QLabel()
            lb.setWordWrap(True)
            lb.setTextFormat(Qt.TextFormat.RichText)
            return lb

        self.diag_label = label()
        diag_card, _ = card(self.diag_label, title="Размеры изображений")

        ref_row = QWidget()
        rr = QHBoxLayout(ref_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addWidget(QLabel("Эталон:"))
        self.ref_radios = {}
        self.ref_group = QButtonGroup(self)
        for side in core.SIDES:
            rb = QRadioButton(core.SIDE_TITLES[side].split()[0])
            rb.setToolTip("Изображение, к геометрии и масштабу которого приводится второе")
            rb.toggled.connect(lambda on, sd=side: on and self.set_ref_side(sd))
            self.ref_group.addButton(rb)
            self.ref_radios[side] = rb
            rr.addWidget(rb)
        rr.addStretch(1)
        self.ref_scale_label = label()
        b_scale = QPushButton("Масштаб эталона")
        m = QMenu(b_scale)
        m.addAction("Реальная ширина эталона, мм…", self.set_ref_width)
        m.addAction("Вернуть 4632 px = 21 мм", self.reset_ref_width)
        b_scale.setMenu(m)
        self.ref_scale_btn = b_scale
        ties_hint = QLabel("Поставьте точки 1, 2, 3 на обоих изображениях в одних и тех же местах шлифа. "
                           "Точки можно перетаскивать.")
        ties_hint.setObjectName("hint")
        ties_hint.setWordWrap(True)
        self.align_label = label()
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        self.align_btn = QPushButton("Совместить")
        self.align_btn.clicked.connect(self.toggle_align)
        self.align_reset_btn = QPushButton("Сбросить")
        self.align_reset_btn.setToolTip("Удалить точки и вернуть исходное отображение")
        self.align_reset_btn.clicked.connect(self.reset_alignment)
        br.addWidget(self.align_btn, 1)
        br.addWidget(self.align_reset_btn)
        align_card, _ = card(ref_row, self.ref_scale_label, b_scale, ties_hint, self.align_label, btn_row,
                             title="Совмещение")

        self.reshoot_cb = QCheckBox("Требуется пересъёмка")
        self.reshoot_cb.setToolTip("Такие пары по умолчанию пропускаются при пакетном экспорте")
        self.reshoot_cb.toggled.connect(self.set_reshoot)
        status_card, _ = card(self.reshoot_cb, title="Статус")

        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(8)
        for c in (diag_card, align_card, status_card):
            pv.addWidget(c)
        pv.addStretch(1)
        return panel

    def _hotkey(self, fn):
        if is_text_input(QApplication.focusWidget()):
            return
        fn()

    def _update_title(self):
        name = os.path.basename(self.project.path) if self.project else "нет проекта"
        self.setWindowTitle(f"Разметка панорам шлифов — {name}")

    def _update_controls(self):
        has = self.project is not None and self.sel is not None
        for b in self.obj_buttons:
            b.setEnabled(has)
        self.mag_combo.setEnabled(has)
        pair = self.pair()
        s = self.project.settings if self.project else core.DEFAULT_SETTINGS
        objs = self.objects()
        for side in core.SIDES:
            pb = self.pair_color_btns[side]
            pb.setEnabled(pair is not None)
            pb.setIcon(color_icon((pair and pair["color"][side]) or s["color_" + side]))
            ob = self.obj_color_btns[side]
            if has and self.sel < len(objs):
                ob.setIcon(color_icon(core.object_color(objs[self.sel], s, pair, side)))
            else:
                ob.setIcon(QIcon())
        self.pair_color_reset.setEnabled(bool(pair) and (any(pair["color"].values()) or
                                                         any(any(o["color"].values()) for o in objs)))
        busy = self.worker is not None
        self.act_exp_cur.setEnabled(self.project is not None and not busy)
        self.act_exp_all.setEnabled(self.project is not None and not busy)

    # --- проект
    def pair(self):
        if self.project and 0 <= self.cur < len(self.project.pairs):
            return self.project.pairs[self.cur]
        return None

    def objects(self):
        p = self.pair()
        return p["objects"] if p else []

    def new_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка с панорамами")
        if not folder:
            return
        try:
            pairs = core.scan_folder(folder)
        except OSError as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось прочитать папку:\n{e}")
            return
        if not pairs:
            QMessageBox.warning(self, "Нет изображений", "В выбранной папке нет файлов PNG/JPG/JPEG.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект как", os.path.join(folder, "проект.json"),
                                              "Проект (*.json)")
        if not path:
            return
        self.save_project()
        project = core.Project(path, folder, pairs)
        try:
            project.save()
        except OSError as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить проект:\n{e}")
            return
        self.set_project(project)
        bad = [p for p in pairs if p["problem"]]
        msg = f"Найдено шлифов: {len(pairs)}."
        if bad:
            lines = "\n".join(f"• {p['name']}: {p['problem']}" for p in bad[:30])
            more = f"\n… и ещё {len(bad) - 30}" if len(bad) > 30 else ""
            QMessageBox.warning(self, "Требуется проверка пар",
                                f"{msg}\nНеполные или неоднозначные пары ({len(bad)}):\n{lines}{more}\n\n"
                                "Отмечены знаком ⚠. Исправьте их кнопкой «Назначить файлы…».")
        else:
            self.statusBar().showMessage(msg, 5000)

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", "Проект (*.json)")
        if not path:
            return
        try:
            project = core.Project.load(path)
        except core.ProjectError as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            return
        self.save_project()
        missing = project.missing_files()
        if missing:
            lines = "\n".join(missing[:15]) + (f"\n… и ещё {len(missing) - 15}" if len(missing) > 15 else "")
            ans = QMessageBox.question(
                self, "Исходные файлы не найдены",
                f"В папке «{project.image_dir}» не найдено файлов: {len(missing)}\n{lines}\n\n"
                "Указать другую папку с изображениями?")
            if ans == QMessageBox.StandardButton.Yes:
                folder = QFileDialog.getExistingDirectory(self, "Папка с панорамами", project.image_dir)
                if folder:
                    project.image_dir = folder
                    still = len(project.missing_files())
                    if still:
                        QMessageBox.information(self, "Файлы не найдены",
                                                f"В новой папке по-прежнему не найдено файлов: {still}.")
                    try:
                        project.save()
                    except OSError:
                        pass
        self.set_project(project)

    def set_project(self, project):
        self.stop_placing()
        self.project = project
        self.undo, self.redo = {}, {}
        self.cur, self.sel = -1, None
        core.clear_size_cache()
        self.loader.errors.clear()
        self.compress_cb.blockSignals(True)
        self.compress_cb.setChecked(bool(project.settings["compress"]))
        self.compress_cb.blockSignals(False)
        self.pair_list.blockSignals(True)
        self.pair_list.clear()
        for i in range(len(project.pairs)):
            self.pair_list.addItem(QListWidgetItem())
        self.pair_list.blockSignals(False)
        self.refresh_diagnostics()
        self._update_title()
        self._update_controls()
        if project.pairs:
            self.pair_list.setCurrentRow(0)

    def _refresh_pair_item(self, i):
        p = self.project.pairs[i]
        missing = [p[s] for s in core.SIDES if p[s] and not os.path.isfile(self.project.image_path(p, s))]
        problem = p["problem"] or ("файл не найден: " + ", ".join(missing) if missing else "")
        item = self.pair_list.item(i)
        item.setText(("⚠ " if problem else "") + p["name"] + (" — пересъёмка" if p["reshoot"] else ""))
        tip = f"Параллельные: {p['parallel'] or '—'}\nСкрещённые: {p['crossed'] or '—'}"
        d = self.diag[i] if i < len(self.diag) else None
        if d:
            item.setIcon(level_icon(d["level"]))
            if d["diff"] is not None:
                tip += f"\nРазличие размеров в паре: {d['diff'] * 100:.1f} %"
            if d["outlier"]:
                tip += "\nРазмер необычен для выборки"
        item.setToolTip(tip + (f"\n\n{problem}" if problem else ""))

    def refresh_diagnostics(self):
        """Диагностика разрешений: внутри пары и относительно медианы всей выборки."""
        if not self.project:
            return
        self.diag = core.size_diagnostics(
            self.project.pairs, lambda p, s: core.image_size(self.project.image_path(p, s)))
        for i in range(len(self.project.pairs)):
            self._refresh_pair_item(i)
        self.update_check_panel()

    def save_project(self):
        self.save_timer.stop()
        if not self.project:
            return
        try:
            self.project.save()
            self.statusBar().showMessage("Проект сохранён", 2000)
        except OSError as e:
            self.statusBar().showMessage(f"Ошибка сохранения проекта: {e}", 10000)

    def changed(self):
        self.save_timer.start()

    # --- выбор шлифа и отображение
    def select_pair(self, i):
        if not self.project or i < 0:
            return
        self.save_project()
        self.escape()
        self.cur, self.sel = i, None
        self.show_pair(fit=True)
        self.rebuild_tree()

    def show_pair(self, fit=False):
        pair = self.pair()
        if not pair:
            return
        info, wanted = [], []
        aligned = core.aligned_side(pair)
        for side, view in self.views.items():
            path = self.project.image_path(pair, side)
            size = core.image_size(path)
            warp = core.warp_params(pair, side) if size else None
            if warp:
                size = warp[0]  # исправленное изображение показывается на холсте эталона
            self.view_titles[side].setText(core.SIDE_TITLES[side] + (" — совмещено с эталоном" if warp else ""))
            if not pair[side]:
                msg = "Изображение не назначено"
            elif not os.path.isfile(path):
                msg = f"Файл не найден:\n{pair[side]}"
            elif size is None:
                msg = f"Не удалось прочитать файл:\n{pair[side]}"
            else:
                msg = "Загрузка…"
            view.load(size, msg)
            view.preview_key = core_key = preview_key(path, warp)
            if size:
                img = self.loader.get(core_key)
                if img is not None:
                    view.set_pixmap(img)
                elif core_key in self.loader.errors:
                    view.load(None, f"Не удалось загрузить:\n{pair[side]}\n{self.loader.errors[core_key]}")
                info.append(f"{core.SIDE_TITLES[side]}: {size[0]}×{size[1]} px, "
                            f"{size[0] * core.pair_scale(pair, side):.2f} мм")
                wanted.append((path, warp))
            view.build_scale(self.project.settings, pair)
            view.build_ties(pair, not self.marking and not aligned)
        self.rebuild_circles()
        self.update_check_panel()
        self.statusBar().showMessage(" · ".join(info))
        if self.cur + 1 < len(self.project.pairs):
            nxt = self.project.pairs[self.cur + 1]
            for s in core.SIDES:
                p = self.project.image_path(nxt, s)
                if p and os.path.isfile(p):
                    wanted.append((p, core.warp_params(nxt, s)))
        self.loader.want(wanted)
        if fit:
            QTimer.singleShot(0, self.fit_views)

    def _preview_loaded(self, key):
        pair = self.pair()
        if not pair:
            return
        for side, view in self.views.items():
            if view.preview_key == key and view.img_size:
                img = self.loader.get(key)
                if img is not None:
                    view.set_pixmap(img)
                else:
                    view.load(None, f"Не удалось загрузить:\n{pair[side]}\n{self.loader.errors.get(key, '')}")
                    view.build_circles(None, self.project.settings, False, None)

    def fit_views(self):
        for v in self.views.values():
            v.fit()

    def refresh_scale(self):
        """Пересчёт линеек и диаметров окружностей после смены калибровки или настроек (центры не меняются)."""
        pair = self.pair()
        if pair:
            for side, v in self.views.items():
                v.build_scale(self.project.settings, pair)
        self.rebuild_circles()
        self.update_check_panel()

    def rebuild_circles(self):
        if not self.project:
            return
        for v in self.views.values():
            v.build_circles(self.pair(), self.project.settings, self.marking, self.sel)

    def rebuild_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        s = self.project.settings if self.project else core.DEFAULT_SETTINGS
        for i, o in enumerate(self.objects()):
            text = f"{i + 1}. {core.mag_label(o['mag'])}"
            if not core.is_placed(o):
                text += " — не размещён"
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setIcon(0, color_icon(*(core.object_color(o, s, self.pair(), sd) for sd in core.SIDES)))
            self.tree.addTopLevelItem(item)
            if i == self.sel:
                item.setSelected(True)
                self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        self._update_selected_controls()

    def _update_selected_controls(self):
        objs = self.objects()
        if self.sel is not None and self.sel < len(objs):
            self.mag_combo.setCurrentIndex(core.MAGS.index(objs[self.sel]["mag"]))
        self._update_controls()

    # --- режимы
    def set_marking(self, on):
        self.marking = on
        (self.act_mark if on else self.act_view).setChecked(True)
        self.right.setVisible(on)
        self.right_check.setVisible(not on)
        if not on:
            self.stop_placing()
        for v in self.views.values():
            v.build_ties(self.pair(), not on and not core.aligned_side(self.pair()))
        self.rebuild_circles()

    def _compress_toggled(self, on):
        if self.project:
            self.project.settings["compress"] = on
            self.changed()

    def edit_settings(self):
        if not self.project:
            return
        dlg = SettingsDialog(self.project.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.project.settings.update(dlg.result_settings())
            self.refresh_scale()
            self.rebuild_tree()
            self.changed()

    # --- выбор объекта
    def set_selection(self, index, source=None):
        objs = self.objects()
        if index is not None and not 0 <= index < len(objs):
            index = None
        self.sel = index
        self.syncing += 1
        for side, v in self.views.items():
            if side != source:
                v.scene().clearSelection()
            v.set_highlight(index)
        self.syncing -= 1
        if source != "tree":
            self.tree.blockSignals(True)
            self.tree.clearSelection()
            if index is not None:
                item = self.tree.topLevelItem(index)
                item.setSelected(True)
                self.tree.setCurrentItem(item)
            self.tree.blockSignals(False)
        self._update_selected_controls()

    def _tree_selection(self):
        items = self.tree.selectedItems()
        self.set_selection(items[0].data(0, Qt.ItemDataRole.UserRole) if items else None, source="tree")

    # --- размещение
    def _tool_clicked(self, key):
        if key == "cursor" or (self.placing and self.placing["mag"] == key and self.placing["target"] is None):
            self.stop_placing()
            self._update_mag_buttons()
        else:
            self.start_placing(key)

    def _can_place(self):
        pair = self.pair()
        if not pair:
            return False
        if any(v.img_size is None for v in self.views.values()):
            self.statusBar().showMessage("Разметка невозможна: нет одного из изображений пары", 5000)
            return False
        return True

    def start_placing(self, mag, target=None):
        if not self.marking:
            self.set_marking(True)
        if not self._can_place():
            self._update_mag_buttons()
            return
        self.placing = {"mag": mag, "target": target}
        self._update_mag_buttons()
        for v in self.views.values():
            v.update_cursor()
        what = f"объекта {target + 1}" if target is not None else "окружностей"
        self.statusBar().showMessage(f"Размещение {what} {core.mag_label(mag)}: щёлкните по панораме. "
                                     "Esc — выход из режима.")

    def stop_placing(self):
        if self.placing is None:
            return
        self.placing = None
        self._update_mag_buttons()
        for v in self.views.values():
            v.update_ghost()
            v.update_cursor()
        self.statusBar().clearMessage()

    def _update_mag_buttons(self):
        for key, b in self.tool_buttons.items():
            if key == "cursor":
                b.setChecked(not self.placing)
            else:
                b.setChecked(bool(self.placing) and self.placing["target"] is None and self.placing["mag"] == key)

    def place_at(self, side, x, y):
        if not self._can_place() or not self.placing:
            return
        sizes = {s: v.img_size for s, v in self.views.items()}
        objs = self.objects()
        target = self.placing["target"]
        self.push_undo()
        if target is not None and target < len(objs):
            obj, idx = objs[target], target
            self.stop_placing()
        else:
            obj = core.new_object(self.placing["mag"])
            objs.append(obj)
            idx = len(objs) - 1
        core.place_object(obj, side, x, y, sizes, self._link_affine(side))
        self.sel = idx
        self.rebuild_circles()
        self.rebuild_tree()
        self.changed()

    def add_unplaced(self, mag):
        if not self.pair():
            return
        self.push_undo()
        self.objects().append(core.new_object(mag))
        self.sel = len(self.objects()) - 1
        self.rebuild_circles()
        self.rebuild_tree()
        self.changed()

    def place_selected(self):
        if self.sel is not None:
            self.start_placing(self.objects()[self.sel]["mag"], target=self.sel)

    def _link_affine(self, side):
        """Перенос положения с панорамы side на вторую: после совмещения координаты общие (None),
        до совмещения — по точкам, если они расставлены."""
        pair = self.pair()
        if core.aligned_side(pair):
            return None
        return core.pair_affine(pair, side, core.other_side(side))

    def item_moved(self, side, index, kind):
        """Во время перетаскивания сдвигает ту же окружность (или номер) на второй панораме.

        Сдвиг переносится пропорционально размерам панорам; с Shift вторая панорама не меняется.
        """
        if self.syncing or not self.project:
            return
        other = core.SIDES[1] if side == core.SIDES[0] else core.SIDES[0]
        src, dst = self.views[side].circles.get(index), self.views[other].circles.get(index)
        if src is None or dst is None:
            return
        o = self.objects()[index]
        alone = self.move_alone
        self.syncing += 1
        sizes = {sd: v.img_size for sd, v in self.views.items()}
        affine = self._link_affine(side)
        if kind == "pos":
            p = src.pos()
            target = o["pos"][other] if alone else core.linked_position(o, side, [p.x(), p.y()], sizes, affine)
            dst.setPos(QPointF(*target))
        else:
            s, pair = self.project.settings, self.pair()
            if alone:
                target = core.label_offset(o, other, s, pair)
            else:
                old, old_other = core.label_offset(o, side, s, pair), core.label_offset(o, other, s, pair)
                lp = src.label.pos()
                dx, dy = core.map_delta(lp.x() - old[0], lp.y() - old[1], side, sizes, affine)
                target = [old_other[0] + dx, old_other[1] + dy]
            dst.label.setPos(QPointF(*target))
        self.syncing -= 1

    def sync_positions(self, side=None):
        """Перенос в проект положений окружностей и номеров (на обеих панорамах) после перетаскивания."""
        if not self.project:
            return
        objs = self.objects()
        s = self.project.settings
        updates = []
        for sd, view in self.views.items():
            for i, item in view.circles.items():
                o = objs[i]
                p, lp = item.pos(), item.label.pos()
                old_p, old_l = o["pos"][sd], core.label_offset(o, sd, s, self.pair())
                new_p = [p.x(), p.y()] if abs(p.x() - old_p[0]) + abs(p.y() - old_p[1]) > 0.01 else None
                new_l = [lp.x(), lp.y()] if abs(lp.x() - old_l[0]) + abs(lp.y() - old_l[1]) > 0.01 else None
                if new_p or new_l:
                    updates.append((o, sd, new_p, new_l))
        if not updates:
            return
        self.push_undo()
        for o, sd, new_p, new_l in updates:
            if new_p:
                o["pos"][sd] = new_p
            if new_l:
                o["label"][sd] = new_l
        self.changed()

    # --- редактирование
    def _mag_changed(self):
        if self.sel is None:
            return
        mag = self.mag_combo.currentData()
        o = self.objects()[self.sel]
        if o["mag"] != mag:
            self.push_undo()
            o["mag"] = mag
            self.rebuild_circles()
            self.rebuild_tree()
            self.changed()

    def change_color(self, side):
        if self.sel is None:
            return
        o = self.objects()[self.sel]
        c = QColorDialog.getColor(QColor(core.object_color(o, self.project.settings, self.pair(), side)), self,
                                  f"Цвет окружности {self.sel + 1} — {core.SIDE_TITLES[side].lower()}")
        if c.isValid():
            self.push_undo()
            o["color"][side] = c.name()
            self.rebuild_circles()
            self.rebuild_tree()
            self.changed()

    def set_pair_color(self, side):
        pair = self.pair()
        if not pair:
            return
        start = QColor(pair["color"][side] or self.project.settings["color_" + side])
        c = QColorDialog.getColor(start, self,
                                  f"Цвет всех окружностей — {pair['name']}, {core.SIDE_TITLES[side].lower()}")
        if c.isValid():
            self._apply_pair_color(c.name(), [side])

    def reset_pair_color(self):
        if self.pair():
            self._apply_pair_color(None, core.SIDES)

    def _apply_pair_color(self, color, sides):
        """Общий цвет шлифа на панорамах sides; индивидуальные цвета там сбрасываются."""
        self.push_undo()
        pair = self.pair()
        for side in sides:
            pair["color"][side] = color
            for o in pair["objects"]:
                o["color"][side] = None
        self.rebuild_circles()
        self.rebuild_tree()
        self.changed()

    def delete_selected(self):
        if self.sel is None or not self.pair():
            return
        self.push_undo()
        del self.objects()[self.sel]
        self.sel = None
        self.stop_placing()
        self.rebuild_circles()
        self.rebuild_tree()
        self.changed()

    def goto_selected(self):
        if self.sel is None:
            return
        o = self.objects()[self.sel]
        if not core.is_placed(o):
            self.statusBar().showMessage("Объект не размещён на панораме", 3000)
            return
        for side, v in self.views.items():
            if o["pos"][side] and v.img_size:
                d = core.diameter_px(o["mag"], core.pair_scale(self.pair(), side))
                v.show_area(o["pos"][side], max(d * 3, 600))

    # --- пары
    def swap_pair(self):
        pair = self.pair()
        if not pair:
            return
        core.swap_sides(pair)
        self._pair_files_changed()

    def assign_files(self):
        pair = self.pair()
        if not pair:
            return
        try:
            files = core.list_images(self.project.image_dir)
        except OSError as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось прочитать папку изображений:\n{e}")
            return
        dlg = AssignDialog(pair, files, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.values()
            core.unalign_pair(pair)  # совмещение относилось к прежним файлам
            pair.update(vals)
            pair["problem"] = "" if all(vals.values()) else "не назначены оба изображения"
            self._pair_files_changed()

    def _pair_files_changed(self):
        self.undo.pop(self.cur, None)
        self.redo.pop(self.cur, None)
        self.stop_placing()
        self.refresh_diagnostics()
        self.show_pair(fit=True)
        self.changed()

    def delete_pair(self):
        pair = self.pair()
        if not pair:
            return
        ans = QMessageBox.question(
            self, "Удаление пары",
            f"Удалить «{pair['name']}» из списка проекта?\n\n"
            "Исходные фотографии на диске не удаляются, но разметка этого шлифа будет удалена из проекта.")
        if ans != QMessageBox.StandardButton.Yes:
            return
        i = self.cur
        self.escape()
        del self.project.pairs[i]
        for stacks in (self.undo, self.redo):  # отмена хранится по номеру пары в списке
            moved = {(k - 1 if k > i else k): v for k, v in stacks.items() if k != i}
            stacks.clear()
            stacks.update(moved)
        self.pair_list.blockSignals(True)
        self.pair_list.takeItem(i)
        self.pair_list.blockSignals(False)
        self.cur, self.sel = -1, None
        self.refresh_diagnostics()
        self.save_project()
        if self.project.pairs:
            n = min(i, len(self.project.pairs) - 1)
            self.pair_list.blockSignals(True)
            self.pair_list.setCurrentRow(n)
            self.pair_list.blockSignals(False)
            self.select_pair(n)
        else:
            for v in self.views.values():
                v.load(None, "Нет шлифов")
            self.rebuild_tree()
            self.update_check_panel()

    # --- проверка пар: эталон, точки, совмещение, статус
    def escape(self):
        self.stop_placing()

    def check_click(self, side, x, y):
        pair = self.pair()
        if not pair:
            return
        if core.aligned_side(pair):
            self.statusBar().showMessage("Чтобы поправить точки, нажмите «Вернуться к исходным»", 4000)
            return
        ties = pair["ties"][side]
        if None not in ties:
            self.statusBar().showMessage("Все три точки на этой панораме уже поставлены — их можно перетащить", 4000)
            return
        ties[ties.index(None)] = [x, y]
        self.views[side].build_ties(pair, True)
        self.update_check_panel()
        self.changed()

    def sync_ties(self):
        pair = self.pair()
        if not pair or self.marking:
            return
        moved = False
        for side, v in self.views.items():
            for n, item in v.ties.items():
                p = [item.pos().x(), item.pos().y()]
                old = pair["ties"][side][n]
                if old and abs(old[0] - p[0]) + abs(old[1] - p[1]) > 0.01:
                    pair["ties"][side][n] = p
                    moved = True
        if moved:
            self.update_check_panel()
            self.changed()

    def _alignment_changed(self):
        """После совмещения/возврата координаты окружностей пересчитаны — прежняя история отмены не подходит."""
        self.undo.pop(self.cur, None)
        self.redo.pop(self.cur, None)
        self.show_pair(fit=True)
        self.changed()

    def toggle_align(self):
        pair = self.pair()
        if not pair:
            return
        if core.aligned_side(pair):
            core.unalign_pair(pair)
        else:
            ref_size = core.image_size(self.project.image_path(pair, pair["ref_side"])) if pair["ref_side"] else None
            if not ref_size or not core.align_pair(pair, ref_size):
                return
        self._alignment_changed()

    def reset_alignment(self):
        pair = self.pair()
        if pair:
            core.unalign_pair(pair)
            pair["ties"] = {s: [None, None, None] for s in core.SIDES}
            self._alignment_changed()

    def set_ref_side(self, side):
        pair = self.pair()
        if pair and pair["ref_side"] != side:
            pair["ref_side"] = side
            self.update_check_panel()
            self.changed()

    def set_ref_width(self):
        pair = self.pair()
        ref = pair and pair["ref_side"]
        size = core.image_size(self.project.image_path(pair, ref)) if ref else None
        if not size:
            return
        mm, ok = QInputDialog.getDouble(
            self, "Масштаб эталона", f"{core.SIDE_TITLES[ref]}: {size[0]} px.\nРеальная ширина изображения, мм:",
            size[0] * core.pair_scale(pair, ref), 0.001, 100000, 3)
        if ok and mm > 0:
            pair["calib"][ref] = {"mm_per_px": mm / size[0], "method": "width", "note": ""}
            self.refresh_scale()
            self.changed()

    def reset_ref_width(self):
        pair = self.pair()
        if pair and pair["ref_side"]:
            pair["calib"][pair["ref_side"]] = None
            self.refresh_scale()
            self.changed()

    def set_reshoot(self, on):
        pair = self.pair()
        if pair and pair["reshoot"] != on:
            pair["reshoot"] = on
            self._refresh_pair_item(self.cur)
            self.changed()

    def update_check_panel(self):
        pair = self.pair()
        d = self.diag[self.cur] if pair and self.cur < len(self.diag) else None
        lines = []
        if d:
            if d["diff"] is not None:
                lines.append(f"<b style='color:{LEVEL_COLORS[d['level']]}'>●</b> Различие внутри пары: "
                             f"<b>{d['diff'] * 100:.1f} %</b>")
            for side in core.SIDES:
                sz, r = d["sizes"][side], d["ratios"][side]
                if not sz:
                    lines.append(f"{core.SIDE_TITLES[side]}: нет изображения")
                    continue
                text = f"{core.SIDE_TITLES[side]}: {sz[0]} × {sz[1]} px"
                if r and (r > core.OUTLIER_RATIO or r < 1 / core.OUTLIER_RATIO):
                    text += f" — <b style='color:{LEVEL_COLORS['yellow']}'>необычно " \
                            f"{'большое' if r > 1 else 'маленькое'} для выборки</b>"
                lines.append(text)
        self.diag_label.setText("<br>".join(lines) or "—")

        aligned = core.aligned_side(pair) if pair else None
        ref = pair["ref_side"] if pair else None
        for side, rb in self.ref_radios.items():
            rb.blockSignals(True)
            self.ref_group.setExclusive(False)
            rb.setChecked(ref == side)
            self.ref_group.setExclusive(True)
            rb.blockSignals(False)
            rb.setEnabled(bool(pair) and not aligned)
        size = core.image_size(self.project.image_path(pair, ref)) if ref else None
        if size:
            mmpp = core.pair_scale(pair, ref)
            known = pair["calib"][ref] is not None
            self.ref_scale_label.setText(
                f"Масштаб эталона: {mmpp:.6f} мм/px<br>Ширина эталона: {size[0] * mmpp:.2f} мм<br>"
                + ("<span style='color:#6b7280'>задан по ширине изображения</span>" if known else
                   f"<span style='color:{LEVEL_COLORS['yellow']}'>не проверен (4632 px = 21 мм)</span>"))
        else:
            self.ref_scale_label.setText("<span style='color:#6b7280'>Выберите эталонное изображение.</span>"
                                         if pair else "")
        self.ref_scale_btn.setEnabled(bool(size))

        if pair:
            counts = " · ".join(f"{core.SIDE_TITLES[s].split()[0]}: {3 - pair['ties'][s].count(None)} из 3"
                                for s in core.SIDES)
            if aligned:
                status = (f"<b style='color:{LEVEL_COLORS['green']}'>Совмещено:</b> "
                          f"{core.SIDE_TITLES[aligned].lower()} приведены к эталону.")
            else:
                problem = core.align_problem(pair)
                complete = all(None not in pair["ties"][s] for s in core.SIDES)
                status = counts + (f"<br><span style='color:{LEVEL_COLORS['red']}'>{problem}</span>"
                                   if problem and complete else "")
            self.align_label.setText(status)
            self.align_btn.setText("Вернуться к исходным" if aligned else "Совместить")
            self.align_btn.setEnabled(bool(aligned) or core.align_problem(pair) is None)
            self.align_reset_btn.setEnabled(bool(aligned) or any(any(pair["ties"][s]) for s in core.SIDES))
        else:
            self.align_label.setText("—")
            self.align_btn.setEnabled(False)
            self.align_reset_btn.setEnabled(False)
        self.reshoot_cb.blockSignals(True)
        self.reshoot_cb.setChecked(bool(pair) and pair["reshoot"])
        self.reshoot_cb.setEnabled(bool(pair))
        self.reshoot_cb.blockSignals(False)

    # --- отмена / повтор
    def _snapshot(self):
        p = self.pair()
        return copy.deepcopy({"objects": p["objects"], "color": p["color"]})

    def push_undo(self):
        st = self.undo.setdefault(self.cur, [])
        st.append(self._snapshot())
        del st[:-UNDO_LIMIT]
        self.redo[self.cur] = []

    def _restore(self, src, dst):
        stack = src.get(self.cur)
        if not stack or not self.pair():
            return
        dst.setdefault(self.cur, []).append(self._snapshot())
        snap = stack.pop()
        self.pair()["objects"] = snap["objects"]
        self.pair()["color"] = snap["color"]
        if self.sel is not None and self.sel >= len(self.objects()):
            self.sel = None
        self.stop_placing()
        self.rebuild_circles()
        self.rebuild_tree()
        self.changed()

    def do_undo(self):
        self._restore(self.undo, self.redo)

    def do_redo(self):
        self._restore(self.redo, self.undo)

    # --- экспорт
    def export(self, all_pairs):
        if not self.project or self.worker:
            return
        pairs = self.project.pairs if all_pairs else ([self.pair()] if self.pair() else [])
        if not pairs:
            return
        reshoot = [p for p in pairs if p["reshoot"]] if all_pairs else []
        if reshoot:
            box = QMessageBox(QMessageBox.Icon.Question, "Экспорт",
                              f"Шлифов с отметкой «Требуется пересъёмка»: {len(reshoot)}. "
                              "По умолчанию они не экспортируются.", parent=self)
            skip = box.addButton("Пропустить их", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Экспортировать все", QMessageBox.ButtonRole.ActionRole)
            cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(skip)
            box.exec()
            if box.clickedButton() == cancel:
                return
            if box.clickedButton() == skip:
                pairs = [p for p in pairs if not p["reshoot"]]
            if not pairs:
                QMessageBox.information(self, "Экспорт", "Нет шлифов для экспорта.")
                return
        out = QFileDialog.getExistingDirectory(self, "Папка для экспорта", self.last_export_dir)
        if not out:
            return
        self.last_export_dir = out
        self.save_project()
        self.worker = ExportWorker(copy.deepcopy(pairs), self.project.image_dir,
                                   dict(self.project.settings), out)
        self.progress = QProgressDialog("Экспорт…", "Отмена", 0, len(pairs), self)
        self.progress.setWindowTitle("Экспорт")
        self.progress.setWindowModality(Qt.WindowModality.NonModal)
        self.progress.setMinimumDuration(0)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.progress.canceled.connect(self._cancel_export)
        self.worker.progress.connect(self._export_progress)
        self.worker.report.connect(lambda e, d, c: self._export_done(e, d, c, len(pairs), out))
        self._update_controls()
        self.progress.show()
        self.worker.start()

    def _cancel_export(self):
        if self.worker:
            self.worker.cancelled = True
            self.progress.setLabelText("Отмена после завершения текущего шлифа…")

    def _export_progress(self, i, name):
        self.progress.setValue(i)
        self.progress.setLabelText(f"Экспорт {i + 1} из {self.progress.maximum()}: {name}")

    def _export_done(self, errors, done, cancelled, total, out):
        self.worker.wait()
        self.worker = None
        self.progress.close()
        self._update_controls()
        text = f"{'Экспорт прерван' if cancelled else 'Экспорт завершён'}.\n" \
               f"Обработано шлифов: {done} из {total}.\nПапка: {out}"
        if errors:
            lines = "\n".join(errors[:20]) + (f"\n… и ещё {len(errors) - 20}" if len(errors) > 20 else "")
            QMessageBox.warning(self, "Экспорт", f"{text}\n\nОшибки ({len(errors)}):\n{lines}")
        else:
            QMessageBox.information(self, "Экспорт", text)

    def closeEvent(self, e):
        if self.worker:
            ans = QMessageBox.question(self, "Экспорт", "Идёт экспорт. Прервать его и закрыть программу?")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.worker.cancelled = True
            self.worker.wait()
        self.save_project()
        self.loader.pool.clear()
        self.loader.pool.waitForDone()
        e.accept()


def apply_style(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#f3f4f6"), (QPalette.ColorRole.Base, "#ffffff"),
                        (QPalette.ColorRole.AlternateBase, "#f9fafb"), (QPalette.ColorRole.Button, "#ffffff"),
                        (QPalette.ColorRole.Text, "#1f2937"), (QPalette.ColorRole.WindowText, "#1f2937"),
                        (QPalette.ColorRole.ButtonText, "#1f2937"), (QPalette.ColorRole.Highlight, "#2563eb"),
                        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, "#111827"),
                        (QPalette.ColorRole.ToolTipText, "#ffffff")):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    font = QFont("Segoe UI")
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(STYLE)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Разметка панорам шлифов")
    apply_style(app)
    w = MainWindow()
    w.resize(1600, 900)
    w.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
