# -*- coding: utf-8 -*-
"""屏幕区域选择：全屏半透明覆盖层上框选监控区域与其中的分析区域。

- 每块屏幕一个覆盖层窗口，支持多显示器；
- 两级区域：
  · 监控区域（绿框）= 要捕获并显示的屏幕范围，返回【屏幕物理像素】全局坐标；
  · 分析区域（蓝框）= 监控区域之内参与变化分析的子区域（如把时间数字留在框外），
    不框选分析区域时整个监控区域都参与分析；
- 坐标换算已完成 Qt 逻辑坐标 × devicePixelRatio（与 mss 捕获坐标一致）；
- 交互（逐个框选）：左键拖拽=框选一个区域（完成即设定）；按 S 键=切换
  监控区域/分析区域模式；按 A 键=框选下一区域；回车=确认退出；
  右键矩形=删除（删监控区域会连带删除其内部的分析区域）；Esc=取消全部。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

Rect = Tuple[int, int, int, int]

_MIN_SIZE = 8
GREEN = QColor(70, 220, 90)      # 监控区域
BLUE = QColor(80, 160, 255)      # 分析区域
ORANGE = QColor(255, 170, 40)    # 拖拽中的矩形


def _to_physical(local_rect: QRect, screen) -> Rect:
    """覆盖层内 Qt 逻辑矩形 -> 全屏物理像素矩形 (x, y, w, h)。"""
    dpr = screen.devicePixelRatio() or 1.0
    geo = screen.geometry()
    x0 = (geo.x() + local_rect.x()) * dpr
    y0 = (geo.y() + local_rect.y()) * dpr
    return (int(round(x0)), int(round(y0)),
            max(2, int(round(local_rect.width() * dpr))),
            max(2, int(round(local_rect.height() * dpr))))


class _ScreenOverlay(QWidget):
    """单块屏幕的覆盖层。确认/取消由管理器统一广播。"""

    dragFinished = Signal()   # 一笔框选结束（刷新提示）

    def __init__(self, screen, confirm_cb, cancel_cb, count_cb=None):
        super().__init__(None,
                         Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Window)
        self._screen = screen
        self._confirm_cb = confirm_cb
        self._cancel_cb = cancel_cb
        self._count_cb = count_cb          # 返回全部屏幕已框选监控区域总数
        self._rects: List[QRect] = []       # 本屏内监控区域（逻辑坐标）
        self._analysis: List[QRect] = []    # 本屏内分析区域（逻辑坐标）
        self._mode = "monitor"              # "monitor" | "analysis"
        self._drag_start: Optional[QPoint] = None
        self._drag_cur: Optional[QPoint] = None
        self._armed = True                  # True=允许拖拽绘制下一个区域
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(1.0)

    # ---- 数据
    def physical_rects(self) -> List[Rect]:
        return [_to_physical(r, self._screen) for r in self._rects]

    def physical_analysis_rects(self) -> List[Rect]:
        return [_to_physical(r, self._screen) for r in self._analysis]

    def add_physical(self, rect: Rect):
        """把全局物理监控矩形转换回本屏逻辑矩形（用于多屏恢复显示）。"""
        self._rects.append(self._to_local(rect))
        self.update()

    def add_physical_analysis(self, rect: Rect):
        self._analysis.append(self._to_local(rect))
        self.update()

    def _to_local(self, rect: Rect) -> QRect:
        dpr = self._screen.devicePixelRatio() or 1.0
        geo = self._screen.geometry()
        lx = rect[0] / dpr - geo.x()
        ly = rect[1] / dpr - geo.y()
        return QRect(int(lx), int(ly),
                     max(2, int(round(rect[2] / dpr))),
                     max(2, int(round(rect[3] / dpr))))

    # ---- 交互
    def _total(self) -> int:
        return self._count_cb() if self._count_cb else len(self._rects)

    def _cur_list(self) -> List[QRect]:
        return self._rects if self._mode == "monitor" else self._analysis

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._armed:
            self._drag_start = e.position().toPoint()
            self._drag_cur = self._drag_start
        elif e.button() == Qt.MouseButton.RightButton:
            p = e.position().toPoint()
            lst = self._cur_list()
            for i in range(len(lst) - 1, -1, -1):
                if lst[i].contains(p):
                    removed = lst.pop(i)
                    if self._mode == "monitor":
                        # 连带删除中心点落在该监控区域内的分析区域
                        self._analysis = [a for a in self._analysis
                                          if not removed.contains(a.center())]
                    if not lst:
                        self._armed = True  # 全删完：允许直接重新框选
                    self.update()
                    break
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_start is not None:
            self._drag_cur = e.position().toPoint()
            self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_start is not None and self._drag_cur is not None:
            r = QRect(self._drag_start, self._drag_cur).normalized()
            if r.width() >= _MIN_SIZE and r.height() >= _MIN_SIZE:
                self._cur_list().append(r)
                self._armed = False   # 本区域已设定：按 A 框选下一个
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self._drag_start = None
            self._drag_cur = None
            self.update()
            self.dragFinished.emit()
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm_cb()
        elif k == Qt.Key.Key_Escape:
            self._cancel_cb()
        elif k == Qt.Key.Key_A:  # A 或 a：框选下一区域（Qt 对字母不区分大小写）
            if not self._armed:
                self._armed = True
                self.setCursor(Qt.CursorShape.CrossCursor)
                self.update()
            e.accept()
        elif k == Qt.Key.Key_S:  # S：切换 监控区域/分析区域 模式
            if self._mode == "monitor" and self._total() > 0:
                self._mode = "analysis"
            elif self._mode == "analysis":
                self._mode = "monitor"
            self._armed = True   # 切换模式后直接可拖拽
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            e.accept()
        else:
            super().keyPressEvent(e)

    # ---- 绘制
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        def draw_rect(r: QRect, color: QColor, label: str):
            p.setPen(QPen(color, 2))
            p.fillRect(r, QColor(color.red(), color.green(), color.blue(), 46))
            p.drawRect(r)
            p.setPen(QPen(color))
            p.setFont(QFont(self.font().family(), 11, QFont.Weight.Bold))
            p.drawText(r.adjusted(0, 4, 0, 0), Qt.AlignmentFlag.AlignTop
                       | Qt.AlignmentFlag.AlignHCenter, label)

        for i, r in enumerate(self._rects):
            draw_rect(r, GREEN, f"监控区域{i + 1}  {r.width()}×{r.height()}")
        for i, r in enumerate(self._analysis):
            draw_rect(r, BLUE, f"分析区域{i + 1}")
        if self._drag_start is not None and self._drag_cur is not None:
            r = QRect(self._drag_start, self._drag_cur).normalized()
            draw_rect(r, ORANGE, f"{r.width()}×{r.height()}")

        p.setPen(QPen(QColor(255, 255, 255, 220)))
        p.setFont(QFont(self.font().family(), 12))
        total = self._total()
        na = getattr(self, "_total_analysis_cb", lambda: 0)()
        mode_name = "监控区域" if self._mode == "monitor" else "分析区域"
        if self._drag_start is not None:
            tip = f"松开左键完成本【{mode_name}】框选"
        elif not self._armed:
            tip = (f"已框选 {total} 个监控区域 / {na} 个分析区域（已设定）")
        else:
            tip = f"正在框选【{mode_name}】：按住左键拖拽" + \
                ("（先框选监控区域才能切到分析区域）"
                 if self._mode == "monitor" and total == 0 else "")
        p.drawText(QRect(0, 14, self.width(), 26),
                   Qt.AlignmentFlag.AlignHCenter, tip)
        p.setFont(QFont(self.font().family(), 10))
        p.setPen(QPen(QColor(255, 255, 255, 170)))
        guide = ("按 S 键切换 监控区域/分析区域 · 按 A 键框选下一区域 · "
                 "按回车键退出框选 · 右键矩形=删除 · Esc=取消全部")
        p.drawText(QRect(0, 42, self.width(), 24),
                   Qt.AlignmentFlag.AlignHCenter, guide)
        p.end()


class ScreenRegionSelector(QWidget):
    """多屏覆盖层管理器：exec_regions() 阻塞返回 (监控矩形, 分析矩形)。"""

    def __init__(self):
        super().__init__(None)
        self._overlays: List[_ScreenOverlay] = []
        self._result: List[Rect] = []
        self._result_analysis: List[Rect] = []
        self._loop: Optional[QEventLoop] = None

    # ---- 广播
    def _monitor_rects(self) -> List[Rect]:
        out: List[Rect] = []
        for o in self._overlays:
            out.extend(o.physical_rects())
        return out

    def _confirm(self):
        if not self._monitor_rects():
            return  # 一个监控区域都没选，Enter 无效
        self._result = self._monitor_rects()
        self._result_analysis = self._clip_analysis(
            [a for o in self._overlays for a in o.physical_analysis_rects()],
            self._monitor_rects())
        self._close(True)

    @staticmethod
    def _clip_analysis(arects: List[Rect], mons: List[Rect]) -> List[Rect]:
        """把分析矩形裁剪到与之重叠最大的监控区域内（完全在外则丢弃）。"""
        out: List[Rect] = []
        for (ax, ay, aw, ah) in arects:
            best, best_area = None, 0
            for (mx, my, mw, mh) in mons:
                iw = min(ax + aw, mx + mw) - max(ax, mx)
                ih = min(ay + ah, my + mh) - max(ay, my)
                if iw > 0 and ih > 0 and iw * ih > best_area:
                    best_area = iw * ih
                    best = (mx, my, mw, mh)
            if best:
                x0 = max(ax, best[0]); y0 = max(ay, best[1])
                x1 = min(ax + aw, best[0] + best[2])
                y1 = min(ay + ah, best[1] + best[3])
                out.append((x0, y0, x1 - x0, y1 - y0))
        return out

    def _cancel(self):
        self._result = []
        self._result_analysis = []
        self._close(False)

    def _close(self, ok: bool):
        if self._loop is not None:
            self._loop.exit(1 if ok else 0)
        for o in self._overlays:
            o.close()
        self._overlays.clear()

    def exec_regions(self, initial: Optional[List[Rect]] = None,
                     initial_analysis: Optional[List[Rect]] = None,
                     ) -> Tuple[List[Rect], List[Rect]]:
        """显示覆盖层并阻塞；返回 (监控矩形, 分析矩形)（取消=([], [])）。"""
        screens = QGuiApplication.screens()
        initial = list(initial or [])
        initial_analysis = list(initial_analysis or [])
        for s in screens:
            ov = _ScreenOverlay(s, self._confirm, self._cancel,
                                count_cb=lambda: sum(len(o._rects)
                                                     for o in self._overlays))
            ov._total_analysis_cb = (
                lambda: sum(len(o._analysis) for o in self._overlays))
            for r in initial:
                ov.add_physical(r)
            for r in initial_analysis:
                ov.add_physical_analysis(r)
            if initial:
                ov._armed = False   # 已有区域：按 A 再框选下一个
            ov.show()
            self._overlays.append(ov)
        if self._overlays:
            self._overlays[0].activateWindow()
            self._overlays[0].setFocus()
        self._result = []
        self._result_analysis = []
        self._loop = QEventLoop()
        code = self._loop.exec()
        self._loop = None
        if code == 1:
            return self._result, self._result_analysis
        return [], []
