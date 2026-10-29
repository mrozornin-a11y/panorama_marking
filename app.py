"""Оформление панорам петрографических шлифов и разметка участков детальной съёмки."""
import copy
import os
import sys
from collections import OrderedDict

from PIL import Image
from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont, QFontMetricsF, QIcon, QImage,
                           QKeySequence, QPainter, QPalette, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGraphicsEllipseItem,
                               QGraphicsScene, QHBoxLayout,
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
    done = Signal(str, object, str)  # путь, QImage или None, текст ошибки


class _LoadTask(QRunnable):
    def __init__(self, path, signals):
        super().__init__()
        self.path, self.signals = path, signals

    def run(self):
        try:
            with Image.open(self.path) as im:
                im.draft("RGB", (PREVIEW_MAX, PREVIEW_MAX))
                img = core.to_rgb(im)
                img.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.Resampling.LANCZOS)
            data = img.tobytes("raw", "RGB")
            qimg = QImage(data, img.width, img.height, 3 * img.width, QImage.Format.Format_RGB888).copy()
            self.signals.done.emit(self.path, qimg, "")
        except Exception as e:  # noqa: BLE001 — показать пользователю любую ошибку чтения
            self.signals.done.emit(self.path, None, str(e))


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

    def get(self, path):
        if path in self.cache:
            self.cache.move_to_end(path)
            return self.cache[path]
        return None

    def want(self, paths):
        """Задать очередь загрузки (в порядке приоритета), заменив прежнюю."""
        self.queue = [p for p in dict.fromkeys(paths)
                      if p and p not in self.cache and p not in self.running and p not in self.errors]
        self._pump()

    def _pump(self):
        while self.queue and len(self.running) < 2:
            p = self.queue.pop(0)
            self.running.add(p)
            self.pool.start(_LoadTask(p, self.signals))

    def _on_done(self, path, img, err):
        self.running.discard(path)
        if img is not None:
            self.cache[path] = img
            while len(self.cache) > PREVIEW_CACHE:
                self.cache.popitem(last=False)
        else:
            self.errors[path] = err
        self.loaded.emit(path)
        self._pump()


# ---------------------------------------------------------------- графические элементы

class CircleItem(QGraphicsEllipseItem):
    def __init__(self, index, obj, side, settings, editable, pair):
        r = core.diameter_px(obj["mag"]) / 2
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.index = index
        self.highlight = False
        color = QColor(core.object_color(obj, settings, pair))
        self.setPen(QPen(color, settings["circle_line_px"]))
        self.setBrush(Qt.BrushStyle.NoBrush)  # без заливки; щелчок внутри всё равно выбирает окружность
        self.setPos(QPointF(*obj["pos"][side]))
        self.setZValue(-r)  # мелкие окружности поверх крупных
        self.setData(0, index)

        self.label = QGraphicsSimpleTextItem(str(index + 1), self)
        self.label.setFont(make_font(settings["number_font_px"]))
        self.label.setBrush(QBrush(color))
        self.label.setPen(QPen(Qt.PenStyle.NoPen))
        self.label.setPos(QPointF(*core.label_offset(obj, side, settings)))
        self.label.setData(0, index)
        if editable:
            flags = QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable | \
                QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable
            self.setFlags(flags)
            self.label.setFlags(flags)

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

    def build_scale(self, settings):
        for it in self.scale_items:
            self.scene().removeItem(it)
        self.scale_items = []
        if self.img_size is None:
            return
        w, h = self.img_size
        sb = core.scale_bar(w, h, settings)
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
                    item = CircleItem(i, o, self.side, settings, editable, pair)
                    item.highlight = i == selected
                    self.scene().addItem(item)
                    self.circles[i] = item
        self.main.syncing -= 1

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
        r = core.diameter_px(pl["mag"]) / 2
        if self.ghost is None:
            self.ghost = QGraphicsEllipseItem()
            self.ghost.setZValue(1e6)
            self.ghost.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene().addItem(self.ghost)
        s = self.main.project.settings
        color = QColor(core.object_color(core.new_object(pl["mag"]), s, self.main.pair()))
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
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.update_cursor()
            return
        if self._press is not None and e.button() == Qt.MouseButton.LeftButton:
            moved = (e.position() - self._press).manhattanLength()
            self._press = None
            if moved < 6 and self.main.placing:
                p = self.mapToScene(e.position().toPoint())
                if self.img_size and 0 <= p.x() <= self.img_size[0] and 0 <= p.y() <= self.img_size[1]:
                    self.main.place_at(self.side, p.x(), p.y())
                else:
                    self.main.statusBar().showMessage("Щёлкните внутри изображения панорамы", 3000)
            return
        super().mouseReleaseEvent(e)
        if e.button() == Qt.MouseButton.LeftButton:
            self.main.sync_positions(self.side)

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
        self.color_btn = QPushButton()
        self.color_btn.clicked.connect(self._pick_color)
        self._show_color()
        form.addRow("Цвет новых окружностей", self.color_btn)
        note = QLabel("Размеры указаны в пикселях исходного изображения.")
        note.setObjectName("hint")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _show_color(self):
        self.color_btn.setText(self.s["default_color"])
        self.color_btn.setIcon(color_icon(self.s["default_color"]))

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.s["default_color"]), self, "Цвет новых окружностей")
        if c.isValid():
            self.s["default_color"] = c.name()
            self._show_color()

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


def color_icon(color, size=16):
    pm = QPixmap(size, size)
    pm.fill(QColor(color))
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
        self.act_view = QAction("Просмотр", self, checkable=True, checked=True)
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
        left, lv = card(self.pair_list, b_swap, b_assign, title="Шлифы")

        # центральная область — две панорамы
        center = QSplitter(Qt.Orientation.Horizontal)
        center.setHandleWidth(8)
        self.views = {}
        for side in core.SIDES:
            title = QLabel(core.SIDE_TITLES[side])
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
        hint = QLabel("Щелчок по панораме ставит окружность. Esc — курсор.")
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
        for n, (text, slot, tip) in enumerate((
                ("Цвет…", self.change_color, "Цвет выбранной окружности"),
                ("Указать положение", self.place_selected,
                 "Следующий щелчок по панораме задаст положение выбранного объекта"),
                ("Показать", self.goto_selected, "Показать выбранную окружность на панорамах"),
                ("Удалить", self.delete_selected, "Удалить выбранный объект (Delete)"))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bg.addWidget(b, n // 2, n % 2)
            self.obj_buttons.append(b)
        self.obj_buttons[3].setObjectName("danger")
        objects_card, _ = card(self.tree, mag_row, btns, title="Объекты")

        self.pair_color_btn = QPushButton("Один цвет для шлифа…")
        self.pair_color_btn.setToolTip("Все окружности и номера этого шлифа — выбранным цветом; "
                                       "другие шлифы не меняются")
        self.pair_color_btn.clicked.connect(self.set_pair_color)
        self.pair_color_reset = QPushButton("Сбросить")
        self.pair_color_reset.setToolTip("Вернуть этому шлифу цвет по умолчанию")
        self.pair_color_reset.clicked.connect(self.reset_pair_color)
        color_row = QWidget()
        cr = QHBoxLayout(color_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self.pair_color_btn, 1)
        cr.addWidget(self.pair_color_reset)
        color_card, _ = card(color_row, title="Цвет шлифа")

        self.right = QWidget()
        rv = QVBoxLayout(self.right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        rv.addWidget(tools_card)
        rv.addWidget(objects_card, 1)
        rv.addWidget(color_card)
        self.right.setVisible(False)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(8)
        split.addWidget(left)
        split.addWidget(center)
        split.addWidget(self.right)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 1200, 320])
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(8, 8, 8, 8)
        ol.addWidget(split)
        self.setCentralWidget(outer)
        self._update_controls()
        self._update_mag_buttons()

        for key, fn in (("1", lambda: self.start_placing("2.5")), ("2", lambda: self.start_placing("5")),
                        ("3", lambda: self.start_placing("10")), ("4", lambda: self.start_placing("20")),
                        ("Esc", self.stop_placing), ("Delete", self.delete_selected),
                        ("Ctrl+S", self.save_project), ("Ctrl+Z", self.do_undo), ("Ctrl+Y", self.do_redo)):
            QShortcut(QKeySequence(key), self, activated=lambda f=fn: self._hotkey(f))

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
        self.pair_color_btn.setEnabled(self.pair() is not None)
        self.pair_color_reset.setEnabled(bool(self.pair() and self.pair().get("color")))
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
            self._refresh_pair_item(i)
        self.pair_list.blockSignals(False)
        self._update_title()
        self._update_controls()
        if project.pairs:
            self.pair_list.setCurrentRow(0)

    def _refresh_pair_item(self, i):
        p = self.project.pairs[i]
        missing = [p[s] for s in core.SIDES if p[s] and not os.path.isfile(self.project.image_path(p, s))]
        problem = p["problem"] or ("файл не найден: " + ", ".join(missing) if missing else "")
        item = self.pair_list.item(i)
        item.setText(("⚠ " if problem else "") + p["name"])
        tip = f"Параллельные: {p['parallel'] or '—'}\nСкрещённые: {p['crossed'] or '—'}"
        item.setToolTip(tip + (f"\n\n{problem}" if problem else ""))

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
        self.stop_placing()
        self.cur, self.sel = i, None
        self.show_pair(fit=True)
        self.rebuild_tree()

    def show_pair(self, fit=False):
        pair = self.pair()
        if not pair:
            return
        info = []
        for side, view in self.views.items():
            path = self.project.image_path(pair, side)
            size = core.image_size(path)
            if not pair[side]:
                msg = "Изображение не назначено"
            elif not os.path.isfile(path):
                msg = f"Файл не найден:\n{pair[side]}"
            elif size is None:
                msg = f"Не удалось прочитать файл:\n{pair[side]}"
            else:
                msg = "Загрузка…"
            view.load(size, msg)
            if size:
                img = self.loader.get(path)
                if img is not None:
                    view.set_pixmap(img)
                elif path in self.loader.errors:
                    view.load(None, f"Не удалось загрузить:\n{pair[side]}\n{self.loader.errors[path]}")
                info.append(f"{core.SIDE_TITLES[side]}: {size[0]}×{size[1]} px, "
                            f"{size[0] * core.MM_PER_PX:.2f} мм")
            view.build_scale(self.project.settings)
        self.rebuild_circles()
        self.statusBar().showMessage(" · ".join(info))
        wanted = [self.project.image_path(pair, s) for s in core.SIDES]
        if self.cur + 1 < len(self.project.pairs):
            nxt = self.project.pairs[self.cur + 1]
            wanted += [self.project.image_path(nxt, s) for s in core.SIDES]
        self.loader.want([p for p in wanted if p and os.path.isfile(p)])
        if fit:
            QTimer.singleShot(0, self.fit_views)

    def _preview_loaded(self, path):
        pair = self.pair()
        if not pair:
            return
        for side, view in self.views.items():
            if self.project.image_path(pair, side) == path and view.img_size:
                img = self.loader.get(path)
                if img is not None:
                    view.set_pixmap(img)
                else:
                    view.load(None, f"Не удалось загрузить:\n{pair[side]}\n{self.loader.errors.get(path, '')}")
                    view.build_scale(self.project.settings)
                    view.build_circles(None, self.project.settings, False, None)

    def fit_views(self):
        for v in self.views.values():
            v.fit()

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
            item.setIcon(0, color_icon(core.object_color(o, s, self.pair())))
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
        if not on:
            self.stop_placing()
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
            for v in self.views.values():
                v.build_scale(self.project.settings)
            self.rebuild_circles()
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
        core.place_object(obj, side, x, y, sizes)
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

    def sync_positions(self, side):
        """Перенос в проект положений окружностей и номеров после перетаскивания мышью."""
        if not self.project:
            return
        objs = self.objects()
        s = self.project.settings
        updates = []
        for i, item in self.views[side].circles.items():
            o = objs[i]
            p, lp = item.pos(), item.label.pos()
            old_p, old_l = o["pos"][side], core.label_offset(o, side, s)
            new_p = [p.x(), p.y()] if abs(p.x() - old_p[0]) + abs(p.y() - old_p[1]) > 0.01 else None
            new_l = [lp.x(), lp.y()] if abs(lp.x() - old_l[0]) + abs(lp.y() - old_l[1]) > 0.01 else None
            if new_p or new_l:
                updates.append((o, new_p, new_l))
        if not updates:
            return
        self.push_undo()
        for o, new_p, new_l in updates:
            if new_p:
                o["pos"][side] = new_p
            if new_l:
                o["label"][side] = new_l
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

    def change_color(self):
        if self.sel is None:
            return
        o = self.objects()[self.sel]
        c = QColorDialog.getColor(QColor(core.object_color(o, self.project.settings, self.pair())), self,
                                  "Цвет окружности")
        if c.isValid():
            self.push_undo()
            o["color"] = c.name()
            self.rebuild_circles()
            self.rebuild_tree()
            self.changed()

    def set_pair_color(self):
        pair = self.pair()
        if not pair:
            return
        start = QColor(pair.get("color") or self.project.settings["default_color"])
        c = QColorDialog.getColor(start, self, f"Цвет всех окружностей — {pair['name']}")
        if c.isValid():
            self._apply_pair_color(c.name())

    def reset_pair_color(self):
        if self.pair():
            self._apply_pair_color(None)

    def _apply_pair_color(self, color):
        """Общий цвет шлифа; индивидуальные цвета его окружностей сбрасываются."""
        self.push_undo()
        pair = self.pair()
        pair["color"] = color
        for o in pair["objects"]:
            o["color"] = None
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
        d = core.diameter_px(o["mag"])
        for side, v in self.views.items():
            if o["pos"][side] and v.img_size:
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
            pair.update(vals)
            pair["problem"] = "" if all(vals.values()) else "не назначены оба изображения"
            self._pair_files_changed()

    def _pair_files_changed(self):
        self.undo.pop(self.cur, None)
        self.redo.pop(self.cur, None)
        self.stop_placing()
        self._refresh_pair_item(self.cur)
        self.show_pair(fit=True)
        self.changed()

    # --- отмена / повтор
    def _snapshot(self):
        p = self.pair()
        return copy.deepcopy({"objects": p["objects"], "color": p.get("color")})

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
