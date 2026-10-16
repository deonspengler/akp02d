"""The panel at working size: drag and resize widgets over the live frame.

The frame underneath is the engine's composite -- real widgets drawing
real (or stand-in) data -- and the boxes on top are the theme's
geometry. Interaction happens in panel pixels, so a box is always at a
whole coordinate, and every drag is checked against the same two rules
the daemon enforces at load:

* colour-safe placement: the coordinate the panel's rule binds always
  snaps to the nearest offset that renders in true colour, so a box can
  never be placed where it would draw in the wrong colours;
* no overlaps: a drop onto another widget is refused and the box goes
  back where it was, unless Alt is held -- for the moment when swapping
  two widgets needs one to sit on the other briefly.

A new widget can be dragged in from the Add Widget buttons: an outline
of its default size follows the pointer, snapped by the same rule, red
where it would overlap; dropping it there does nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from akp02 import AKP02, Orientation
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPen, QPixmap, QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QMenu,
)

from .. import layout

# What an Add Widget button carries when dragged: the plugin's name.
MIME = "application/x-akp02d-plugin"
from ..layout import Rect
from .engine import snap
from . import style

HANDLE = 7  # handle size in screen pixels


@dataclass(slots=True)
class Box:
    id: str
    plugin: str
    rect: Rect
    state: str = "ok"      # ok | warning | error


class Canvas(QGraphicsView):
    """Panel-sized scene: the frame as a pixmap, boxes drawn on top."""

    selected = Signal(object)                 # widget id or None
    geometry_committed = Signal(str, object)  # widget id, Rect
    delete_requested = Signal(str)
    duplicate_requested = Signal(str)
    status = Signal(str)
    view_changed = Signal()
    add_requested = Signal(str, object)       # plugin, Rect: dropped from Add Widget

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self.scene().addItem(self._pixmap_item)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setBackgroundBrush(QColor(style.CANVAS))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.view_changed.emit())
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.view_changed.emit())

        self.boxes: list[Box] = []
        self.selected_id: str | None = None
        self.show_boxes = True
        self.accent = QColor(style.ACCENT)
        self.orientation = Orientation.PORTRAIT
        self.inverted = False
        self.screen = layout.screen_size(Orientation.PORTRAIT)
        self._zoom = 1.0
        # Fit is a mode, not a one-off: while on, the panel stays fitted
        # as the window is resized. Any other zoom turns it off.
        self.fitted = True
        self._drag: _Drag | None = None
        self._hover_handle: str | None = None
        # A widget being dragged in from Add Widget, and the size a new
        # one of a kind starts at (the window's say).
        self._incoming: _Incoming | None = None
        self.size_for: Callable[[str], tuple[int, int]] = lambda plugin: (200, 100)
        self.setAcceptDrops(True)

    # -- content --

    def set_frame(self, image: QImage) -> None:
        self._pixmap_item.setPixmap(QPixmap.fromImage(image))

    def set_rules(self, orientation: Orientation, inverted: bool) -> None:
        size = layout.screen_size(orientation)
        changed = size != self.screen
        self.orientation, self.inverted, self.screen = orientation, inverted, size
        margin = 48
        self.setSceneRect(QRectF(-margin, -margin, size[0] + 2 * margin, size[1] + 2 * margin))
        if changed:
            # After the layout settles: the view's own size may be about
            # to change with the orientation.
            QTimer.singleShot(0, self.fit)

    def set_boxes(self, boxes: list[Box]) -> None:
        self.boxes = boxes
        if self._drag is not None and not any(b.id == self._drag.id for b in boxes):
            self._drag = None
        self.viewport().update()

    def select(self, widget_id: str | None) -> None:
        self.selected_id = widget_id
        self.viewport().update()

    def reveal(self, widget_id: str) -> None:
        box = self._box(widget_id)
        if box is not None:
            self.ensureVisible(QRectF(*box.rect), 40, 40)

    # -- zoom --

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, factor: float, *, fitting: bool = False) -> None:
        self.fitted = fitting
        factor = max(0.1, min(8.0, factor))
        self._zoom = factor
        self.resetTransform()
        self.scale(factor, factor)
        # Below 1:1, nearest-neighbour drops thin lines -- a 2 px graph
        # trace can vanish at a third of the size -- so average instead.
        # At 1:1 and above, keep pixels square: that is what is there.
        self._pixmap_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation if factor < 1
            else Qt.TransformationMode.FastTransformation)
        self.view_changed.emit()

    def fit(self) -> None:
        width, height = self.screen
        view = self.viewport().size()
        if view.width() < 10 or view.height() < 10:
            return
        factor = min((view.width() - 40) / width, (view.height() - 40) / height)
        self.set_zoom(max(0.1, factor), fitting=True)
        self.centerOn(width / 2, height / 2)

    def visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.fitted:
            self.fit()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.set_zoom(self._zoom * step)
            event.accept()
            return
        super().wheelEvent(event)

    # -- hit testing --

    def _box(self, widget_id: str | None) -> Box | None:
        return next((b for b in self.boxes if b.id == widget_id), None)

    def _scene_point(self, event: QMouseEvent) -> QPointF:
        return self.mapToScene(event.position().toPoint())

    def _handles(self, rect: Rect) -> dict[str, QRect]:
        """Handle squares in viewport coordinates, keyed by edges."""
        top_left = self.mapFromScene(QPointF(rect.x, rect.y))
        bottom_right = self.mapFromScene(QPointF(rect.x + rect.width, rect.y + rect.height))
        x0, y0, x1, y1 = top_left.x(), top_left.y(), bottom_right.x(), bottom_right.y()
        xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
        points = {"lt": (x0, y0), "t": (xm, y0), "rt": (x1, y0), "l": (x0, ym),
                  "r": (x1, ym), "lb": (x0, y1), "b": (xm, y1), "rb": (x1, y1)}
        h = HANDLE
        return {k: QRect(x - h // 2 - 1, y - h // 2 - 1, h + 2, h + 2)
                for k, (x, y) in points.items()}

    def _handle_at(self, pos: QPoint) -> str | None:
        box = self._box(self.selected_id)
        if box is None or not self.show_boxes:
            return None
        for name, square in self._handles(box.rect).items():
            if square.adjusted(-3, -3, 3, 3).contains(pos):
                return name
        return None

    def _box_at(self, point: QPointF) -> Box | None:
        hits = [b for b in self.boxes
                if QRectF(*b.rect).contains(point)]
        if not hits:
            return None
        # The smallest box wins, so a label sitting inside a big
        # spectrum's region is still reachable -- and the selected one
        # wins over others its size, so clicking it again keeps it.
        hits.sort(key=lambda b: (b.rect.width * b.rect.height, b.id != self.selected_id))
        return hits[0]

    # -- mouse --

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            fake = QMouseEvent(event.type(), event.position(), event.globalPosition(),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               event.modifiers())
            super().mousePressEvent(fake)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        handle = self._handle_at(event.position().toPoint())
        point = self._scene_point(event)
        if handle is not None:
            box = self._box(self.selected_id)
            assert box is not None
            self._drag = _Drag(box.id, handle, point, box.rect, box.rect)
            return
        box = self._box_at(point)
        if box is None:
            if self.selected_id is not None:
                self.selected_id = None
                self.selected.emit(None)
                self.viewport().update()
            return
        if box.id != self.selected_id:
            self.selected_id = box.id
            self.selected.emit(box.id)
        self._drag = _Drag(box.id, "move", point, box.rect, box.rect)
        self.viewport().update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseMoveEvent(event)
            return
        drag = self._drag
        if drag is None:
            handle = self._handle_at(event.position().toPoint())
            if handle != self._hover_handle:
                self._hover_handle = handle
                self.viewport().setCursor(_CURSORS.get(handle or "", Qt.CursorShape.ArrowCursor))
            if handle is None:
                over = self._box_at(self._scene_point(event))
                self.viewport().setCursor(
                    Qt.CursorShape.SizeAllCursor if over is not None and over.id == self.selected_id
                    else Qt.CursorShape.ArrowCursor)
            return
        point = self._scene_point(event)
        dx = round(point.x() - drag.origin.x())
        dy = round(point.y() - drag.origin.y())
        if not drag.started and abs(dx) + abs(dy) < 2:
            return
        drag.started = True
        raw = _apply(drag.start, drag.mode, dx, dy)
        snapped = snap(raw, self.orientation, self.inverted, self.screen, drag.mode)
        drag.raw, drag.rect = raw, snapped
        drag.overlaps = [b.id for b in self.boxes
                         if b.id != drag.id and b.rect.overlaps(snapped)]
        self.status.emit(self._drag_text(drag))
        self.viewport().update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            return
        drag, self._drag = self._drag, None
        if drag is None or not drag.started:
            self.viewport().update()
            return
        allow = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if drag.overlaps and not allow:
            self.status.emit(
                f"{drag.id} would overlap {', '.join(drag.overlaps)}; put back "
                f"(hold Alt to place it anyway)")
        elif drag.rect != drag.start:
            self.geometry_committed.emit(drag.id, drag.rect)
        self.viewport().update()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        box = self._box_at(self.mapToScene(event.pos()))
        if box is None:
            return
        if box.id != self.selected_id:
            self.selected_id = box.id
            self.selected.emit(box.id)
        menu = QMenu(self)
        duplicate = menu.addAction("Duplicate")
        delete = menu.addAction("Delete")
        chosen = menu.exec(event.globalPos())
        if chosen is duplicate:
            self.duplicate_requested.emit(box.id)
        elif chosen is delete:
            self.delete_requested.emit(box.id)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        box = self._box(self.selected_id)
        key = event.key()
        if key == Qt.Key.Key_Escape and self._drag is not None:
            self._drag = None
            self.viewport().update()
            return
        if box is None:
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_requested.emit(box.id)
            return
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if key not in arrows:
            super().keyPressEvent(event)
            return
        dx, dy = arrows[key]
        axis, _, _, _ = layout.bound_axis(box.rect, self.orientation, self.inverted)
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        # Along the bound axis, one step is one colour-safe slot.
        modulus = AKP02.SHORT_AXIS_ALIGN_MODULUS
        dx *= step * (modulus if axis == "x" else 1)
        dy *= step * (modulus if axis == "y" else 1)
        moved = Rect(box.rect.x + dx, box.rect.y + dy, box.rect.width, box.rect.height)
        moved = snap(moved, self.orientation, self.inverted, self.screen)
        clash = [b.id for b in self.boxes if b.id != box.id and b.rect.overlaps(moved)]
        if clash:
            self.status.emit(f"{box.id} would overlap {', '.join(clash)}")
            return
        if moved != box.rect:
            self.geometry_committed.emit(box.id, moved)

    # -- a new widget, dragged in --

    def incoming_at(self, plugin: str, point: QPointF) -> _Incoming | None:
        """Where a new `plugin` would land with the pointer at `point`
        (scene, panel pixels): centred on it, kept on the panel, snapped
        colour-safe. None off the panel."""
        width, height = self.screen
        if not (0 <= point.x() < width and 0 <= point.y() < height):
            return None
        w, h = self.size_for(plugin)
        w, h = min(w, width), min(h, height)
        x = min(max(round(point.x() - w / 2), 0), width - w)
        y = min(max(round(point.y() - h / 2), 0), height - h)
        rect = snap(Rect(x, y, w, h), self.orientation, self.inverted, self.screen)
        overlaps = [b.id for b in self.boxes if b.rect.overlaps(rect)]
        return _Incoming(plugin, rect, overlaps)

    def _track_incoming(self, event) -> bool:
        data = event.mimeData()
        if not data.hasFormat(MIME):
            return False
        plugin = bytes(data.data(MIME)).decode()
        self._incoming = self.incoming_at(plugin, self.mapToScene(event.position().toPoint()))
        incoming = self._incoming
        if incoming is None:
            self.status.emit(f"New {plugin}: drop it on the panel")
        elif incoming.overlaps:
            self.status.emit(f"New {plugin} would overlap {', '.join(incoming.overlaps)}")
        else:
            r = incoming.rect
            self.status.emit(f"New {plugin}  {r.x}, {r.y}  {r.width}×{r.height}")
        self.viewport().update()
        return True

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._track_incoming(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._track_incoming(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._incoming = None
        self.viewport().update()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not self._track_incoming(event):
            event.ignore()
            return
        incoming, self._incoming = self._incoming, None
        self.viewport().update()
        if incoming is None or incoming.overlaps:
            event.ignore()           # nowhere it can go: nothing added
            return
        event.acceptProposedAction()
        self.add_requested.emit(incoming.plugin, incoming.rect)

    def _drag_text(self, drag: _Drag) -> str:
        r = drag.rect
        text = f"{drag.id}  {r.x}, {r.y}  {r.width}×{r.height}"
        if drag.raw != drag.rect:
            axis, value, _, _ = layout.bound_axis(drag.raw, self.orientation, self.inverted)
            snapped = getattr(drag.rect, axis)
            raw_value = getattr(drag.raw, axis)
            if raw_value != snapped:
                text += f"   {axis} {raw_value} → {snapped} (colour-safe)"
            else:
                extent = "width" if axis == "x" else "height"
                text += (f"   {extent} {getattr(drag.raw, extent)} → "
                         f"{getattr(drag.rect, extent)} (colour-safe)")
        if drag.overlaps:
            text += f"   overlaps {', '.join(drag.overlaps)}"
        return text

    # -- drawing --

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        painter.fillRect(rect, QColor(style.CANVAS))
        # A faint dot grid, in view space so it does not scale.
        painter.save()
        painter.resetTransform()
        view = self.viewport().rect()
        painter.setPen(QPen(QColor("#22262e"), 1))
        for x in range(0, view.width(), 16):
            for y in range(0, view.height(), 16):
                painter.drawPoint(x, y)
        painter.restore()
        width, height = self.screen
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 120))
        painter.drawRect(QRectF(4, 8, width, height))  # drop shadow

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        width, height = self.screen
        border = QPen(QColor(style.BORDER), 0)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(0, 0, width, height))
        drag = self._drag if self._drag is not None and self._drag.started else None

        if drag is not None:
            self._draw_guides(painter, drag)

        painter.save()
        painter.resetTransform()
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        for box in self.boxes:
            if box.id == self.selected_id:
                continue
            if not self.show_boxes and box.state == "ok":
                continue
            area = self._view_rect(box.rect)
            colour = {"error": QColor(style.BAD), "warning": QColor(style.WARN)}.get(
                box.state, QColor(255, 255, 255, 110))
            pen = QPen(colour, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(area.adjusted(0, 0, -1, -1))
            if box.state == "error":
                painter.fillRect(area, QBrush(QColor(255, 107, 107, 60), Qt.BrushStyle.BDiagPattern))
            if self._zoom >= 0.45 and area.height() > metrics.height() + 4:
                painter.setPen(QColor(255, 255, 255, 150) if box.state == "ok" else colour)
                painter.drawText(area.adjusted(4, 2, -4, -2),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                                 box.id)

        selected = self._box(self.selected_id)
        if selected is not None:
            current = drag.rect if drag is not None and drag.id == selected.id else selected.rect
            if drag is not None and drag.id == selected.id:
                # Where the pointer actually is, before snapping.
                ghost = self._view_rect(drag.raw)
                painter.setPen(QPen(QColor(style.BAD) if drag.raw != drag.rect
                                    else QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
                painter.drawRect(ghost.adjusted(0, 0, -1, -1))
                for other_id in drag.overlaps:
                    other = self._box(other_id)
                    if other is None:
                        continue
                    clash = QRectF(*current).intersected(QRectF(*other.rect))
                    area = self._view_rect(Rect(int(clash.x()), int(clash.y()),
                                                int(clash.width()), int(clash.height())))
                    painter.fillRect(area, QBrush(QColor(style.BAD), Qt.BrushStyle.BDiagPattern))
            area = self._view_rect(current)
            bad = (drag is not None and drag.overlaps) or (
                drag is None and selected.state == "error")
            colour = QColor(style.BAD) if bad else self.accent
            painter.setPen(QPen(colour, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(area).adjusted(0.5, 0.5, -0.5, -0.5))
            if self.show_boxes and drag is None:
                painter.setPen(QPen(colour, 1))
                painter.setBrush(QColor("#ffffff"))
                for square in self._handles(current).values():
                    painter.drawRect(square.adjusted(1, 1, -1, -1))
            tag = f"{selected.id} · {current.x}, {current.y} · {current.width}×{current.height}"
            tag_width = metrics.horizontalAdvance(tag) + 12
            tag_rect = QRect(area.x(), area.y() - metrics.height() - 8, tag_width,
                             metrics.height() + 4)
            if tag_rect.y() < 2:
                tag_rect.moveTop(area.bottom() + 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(tag_rect), 3, 3)
            painter.setPen(QColor("#0d1117"))
            painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag)
        incoming = self._incoming
        if incoming is not None:
            area = self._view_rect(incoming.rect)
            colour = QColor(style.BAD) if incoming.overlaps else self.accent
            fill = QColor(colour)
            fill.setAlpha(40)
            painter.setPen(QPen(colour, 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(fill)
            painter.drawRect(QRectF(area).adjusted(0.5, 0.5, -0.5, -0.5))
            if incoming.overlaps:
                painter.fillRect(area, QBrush(colour, Qt.BrushStyle.BDiagPattern))
            painter.setPen(colour)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, f"new {incoming.plugin}")
        painter.restore()

    def _draw_guides(self, painter: QPainter, drag: _Drag) -> None:
        """The colour-safe offsets near the dragged edge, every slot."""
        width, height = self.screen
        rect = drag.rect
        axis, value, extent, reflected = layout.bound_axis(rect, self.orientation, self.inverted)
        modulus = AKP02.SHORT_AXIS_ALIGN_MODULUS
        residue = AKP02.SHORT_AXIS_ALIGN_RESIDUE
        short = AKP02.PANEL_SHORT_SIDE
        # The edge the rule constrains: the near one, or the far one when
        # the axis is reflected.
        if reflected:
            edge = value + extent
            target = (short - residue) % modulus
        else:
            edge = value
            target = residue % modulus
        start = edge - 6 * modulus
        start -= (start - target) % modulus
        pen = QPen(QColor(61, 174, 233, 70), 0)
        strong = QPen(QColor(61, 174, 233, 200), 0)
        for position in range(start, edge + 7 * modulus, modulus):
            painter.setPen(strong if position == edge else pen)
            if axis == "x":
                if 0 <= position <= width:
                    painter.drawLine(QPointF(position, rect.y - 40),
                                     QPointF(position, rect.y + rect.height + 40))
            elif 0 <= position <= height:
                painter.drawLine(QPointF(rect.x - 40, position),
                                 QPointF(rect.x + rect.width + 40, position))

    def _view_rect(self, rect: Rect) -> QRect:
        top_left = self.mapFromScene(QPointF(rect.x, rect.y))
        bottom_right = self.mapFromScene(QPointF(rect.x + rect.width, rect.y + rect.height))
        return QRect(top_left, bottom_right - QPoint(1, 1)).adjusted(0, 0, 1, 1)


@dataclass(slots=True)
class _Incoming:
    plugin: str
    rect: Rect
    overlaps: list[str]


@dataclass(slots=True)
class _Drag:
    id: str
    mode: str          # "move" or edges: l t r b combinations
    origin: QPointF
    start: Rect
    rect: Rect
    raw: Rect | None = None
    started: bool = False
    overlaps: list[str] | None = None

    def __post_init__(self) -> None:
        self.raw = self.rect
        self.overlaps = []


def _apply(rect: Rect, mode: str, dx: int, dy: int) -> Rect:
    x, y, w, h = rect
    if mode == "move":
        return Rect(x + dx, y + dy, w, h)
    if "l" in mode:
        dx = min(dx, w - 1)
        x, w = x + dx, w - dx
    if "r" in mode:
        w = max(1, w + dx)
    if "t" in mode:
        dy = min(dy, h - 1)
        y, h = y + dy, h - dy
    if "b" in mode:
        h = max(1, h + dy)
    return Rect(x, y, w, h)


_CURSORS = {
    "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
    "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
}
