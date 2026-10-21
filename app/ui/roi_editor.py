# -*- coding: utf-8 -*-
"""单视频分析区域编辑对话框：批量模式下双击视频行打开，为该视频单独框选区域。

与视频分析页共用 RoiCanvas：拖拽框选（分析区域语义——只分析框内画面），
框外内容（如时间数字）不参与变化分析。区域坐标为该视频的像素坐标。
"""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout)

from app.core.video_io import VideoOpenError, open_capture, probe_video, read_frame
from app.ui.roi_canvas import RoiCanvas, Roi
from app.version import APP_NAME


class RoiEditorDialog(QDialog):
    """为单个视频框选分析区域。确认后通过 `rois` 属性取回结果。"""

    def __init__(self, video_path: str, rois: Optional[List[Roi]] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"分析区域编辑 — {os.path.basename(video_path)}")
        self.setModal(True)
        self.resize(900, 640)
        self.rois: List[Roi] = []

        try:
            self._info = probe_video(video_path)
            self._cap, backend = open_capture(video_path, verify=True)
        except VideoOpenError as e:
            QMessageBox.critical(self, APP_NAME, f"无法打开视频：{e}")
            self.reject()
            return
        self._path = video_path
        self._idx = 0

        v = QVBoxLayout(self)
        hint = QLabel("拖拽框选该视频的**分析区域**（只分析框内画面，可多个；"
                      "不设置=分析整个画面）。时间数字等固定内容请留在框外。")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.canvas = RoiCanvas()
        self.canvas.setRois(list(rois or []))
        v.addWidget(self.canvas, 1)

        nav = QHBoxLayout()
        self.btn_prev10 = QPushButton("⏪ 10")
        self.btn_prev10.clicked.connect(lambda: self._step(-10))
        self.btn_prev = QPushButton("◀ 上一帧")
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.frame_label = QLabel("")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.btn_next = QPushButton("下一帧 ▶")
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_next10 = QPushButton("10 ⏩")
        self.btn_next10.clicked.connect(lambda: self._step(10))
        self.btn_clear = QPushButton("清空全部区域")
        self.btn_clear.clicked.connect(self.canvas.clearRois)
        for w in (self.btn_prev10, self.btn_prev, self.frame_label,
                  self.btn_next, self.btn_next10, self.btn_clear):
            nav.addWidget(w, 2 if w is self.frame_label else 1)
        v.addLayout(nav)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setStyleSheet("font-weight:bold; padding:6px 22px;")
        self.btn_ok.clicked.connect(self._on_accept)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        foot.addWidget(self.btn_ok)
        foot.addWidget(self.btn_cancel)
        v.addLayout(foot)

        self._show(0)

    # ------------------------------------------------------------------
    def _show(self, idx: int) -> None:
        total = self._info.frame_count if self._info else 0
        idx = max(0, min(idx, total - 1)) if total > 0 else max(0, idx)
        ok, frame = read_frame(self._cap, idx, self._path)
        if ok:
            self._idx = idx
            self.canvas.setFrame(frame)
            tail = f" / {total}" if total > 0 else ""
            self.frame_label.setText(f"帧 {idx}{tail}")
        else:
            self.frame_label.setText(f"帧 {idx} 读取失败")

    def _step(self, delta: int) -> None:
        self._show(self._idx + delta)

    def _on_accept(self) -> None:
        self.rois = self.canvas.rois()
        self.accept()

    def closeEvent(self, ev) -> None:  # noqa: N802
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        super().closeEvent(ev)
