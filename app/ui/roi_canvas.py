"""预览画布：显示当前帧并用鼠标拖拽绘制"剔除区域"矩形。"""
from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel

Roi = Tuple[int, int, int, int]


class RoiCanvas(QLabel):
    roiCreated = Signal(int, int, int, int)  # 帧坐标 x, y, w, h
    roiCountChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(520, 380)
        self.setStyleSheet("background:#1e1e1e; border:1px solid #3c3c3c;")
        self.setCursor(Qt.CrossCursor)
        self._base: Optional[QPixmap] = None   # 原始帧位图
        self._display: Optional[QPixmap] = None  # 缩放后显示位图
        self._scale = 1.0
        self._offset = QPoint(0, 0)
        self._rois: List[Roi] = []
        self._drawing = False
        self._interactive = True
        self._drag_start = QPoint()
        self._drag_cur = QPoint()
        self._hint = "打开视频后，在此预览上拖拽鼠标框选“分析区域”"

    # ------------------------------------------------------------ 帧显示
    def setFrame(self, frame_bgr: np.ndarray) -> None:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._base = QPixmap.fromImage(img.copy())
        self._hint = ""
        self._relayout()

    def hasFrame(self) -> bool:
        return self._base is not None

    def frameSize(self) -> Tuple[int, int]:
        if self._base is None:
            return 0, 0
        return self._base.width(), self._base.height()

    # ------------------------------------------------------------ ROI 管理
    def rois(self) -> List[Roi]:
        return list(self._rois)

    def setRois(self, rois: List[Roi]) -> None:
        self._rois = [tuple(int(v) for v in r) for r in rois]
        self.roiCountChanged.emit(len(self._rois))
        self.update()

    def removeRoi(self, index: int) -> None:
        if 0 <= index < len(self._rois):
            self._rois.pop(index)
            self.roiCountChanged.emit(len(self._rois))
            self.update()

    def setInteractive(self, on: bool) -> None:
        """结果预览模式下关闭鼠标框选，避免误画分析区域。"""
        self._interactive = bool(on)
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def clearRois(self) -> None:
        if self._rois:
            self._rois.clear()
            self.roiCountChanged.emit(0)
            self.update()

    # ------------------------------------------------------------ 内部
    def _relayout(self) -> None:
        if self._base is None:
            self._display = None
            self.update()
            return
        avail = self.size()
        scaled = self._base.scaled(avail, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._display = scaled
        self._scale = scaled.width() / max(1, self._base.width())
        self._offset = QPoint(max(0, (avail.width() - scaled.width()) // 2),
                              max(0, (avail.height() - scaled.height()) // 2))
        self.update()

    def _to_frame(self, pos: QPoint) -> QPoint:
        fx = int(round((pos.x() - self._offset.x()) / self._scale)) if self._scale else 0
        fy = int(round((pos.y() - self._offset.y()) / self._scale)) if self._scale else 0
        w, h = self.frameSize()
        return QPoint(min(max(fx, 0), w), min(max(fy, 0), h))

    def _map_rect(self, roi: Roi) -> QRect:
        x, y, w, h = roi
        return QRect(self._offset.x() + int(x * self._scale),
                     self._offset.y() + int(y * self._scale),
                     max(1, int(w * self._scale)), max(1, int(h * self._scale)))

    # ------------------------------------------------------------ 事件
    def mousePressEvent(self, ev) -> None:
        if self._interactive and ev.button() == Qt.LeftButton and self._display is not None:
            self._drawing = True
            self._drag_start = self._to_frame(ev.position().toPoint())
            self._drag_cur = self._drag_start
            self.update()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._drawing:
            self._drag_cur = self._to_frame(ev.position().toPoint())
            self.update()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and self._drawing:
            self._drawing = False
            p1, p2 = self._drag_start, self._to_frame(ev.position().toPoint())
            x0, x1 = sorted((p1.x(), p2.x()))
            y0, y1 = sorted((p1.y(), p2.y()))
            w, h = self.frameSize()
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            if x1 - x0 >= 4 and y1 - y0 >= 4:
                roi = (x0, y0, x1 - x0, y1 - y0)
                self._rois.append(roi)
                self.roiCreated.emit(*roi)
                self.roiCountChanged.emit(len(self._rois))
            self.update()
        super().mouseReleaseEvent(ev)

    def resizeEvent(self, ev) -> None:
        self._relayout()
        super().resizeEvent(ev)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self._display is None:
            if self._hint:
                p = QPainter(self)
                p.setPen(QColor(160, 160, 160))
                p.drawText(self.rect(), Qt.AlignCenter, self._hint)
                p.end()
            return
        p = QPainter(self)
        # 画当前帧（_display 由 _relayout 缩放生成；QLabel 自身的 paint 不含它）
        if self._display is not None and not self._display.isNull():
            p.drawPixmap(self._offset, self._display)
        for i, roi in enumerate(self._rois):
            r = self._map_rect(roi)
            pen = QPen(QColor(70, 220, 90), 2, Qt.SolidLine)
            p.setPen(pen)
            p.drawRect(r)
            p.setPen(QColor(130, 255, 140))
            p.drawText(r.adjusted(0, -18, 0, 0), Qt.AlignLeft | Qt.AlignBottom,
                       f"分析区域 {i + 1}")
        if self._drawing:
            p1, p2 = self._drag_start, self._drag_cur
            r = QRect(min(p1.x(), p2.x()), min(p1.y(), p2.y()),
                      abs(p1.x() - p2.x()), abs(p1.y() - p2.y()))
            disp = QRect(self._offset.x() + int(r.x() * self._scale),
                         self._offset.y() + int(r.y() * self._scale),
                         int(r.width() * self._scale), int(r.height() * self._scale))
            p.setPen(QPen(QColor(255, 214, 10), 2, Qt.DashLine))
            p.fillRect(disp, QColor(255, 214, 10, 40))
            p.drawRect(disp)
        p.end()
