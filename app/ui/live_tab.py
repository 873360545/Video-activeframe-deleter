# -*- coding: utf-8 -*-
"""实时监控页：屏幕区域框选 → 多区域并行实时分析 → 逐区域独立结果。

每个区域独立输出：实时变化率/运动状态卡片、事件列表（开始/结束/时长/最大变化率）、
运动片段视频（自动含运动前后缓冲）；停止后可导出全部区域的事件报告 CSV。
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import List, Optional

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app.core.live_monitor import LiveMonitor, RegionConfig, merge_event_clips, sanitize_name
from app.version import APP_NAME


def _bgr_pixmap(bgr) -> QPixmap:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
    return QPixmap.fromImage(img.copy())


class _RegionTile(QFrame):
    """单个监控区域的实时卡片。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight:bold;")
        v.addWidget(self.title)
        self.preview = QLabel()
        self.preview.setFixedHeight(170)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#101010;")
        v.addWidget(self.preview, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFormat("变化率 0.00 %")
        v.addWidget(self.bar)
        self.status = QLabel("待机")
        v.addWidget(self.status)

    def set_preview(self, bgr):
        self.preview.setPixmap(_bgr_pixmap(bgr).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def set_stats(self, ratio: float, motion: bool, events: int, recording: bool):
        self.bar.setValue(min(1000, int(max(0.0, ratio) * 1000 / 10)))  # 满格=10%
        self.bar.setFormat(f"变化率 {ratio:.2f} %")
        if motion:
            st = "● 运动中" + ("（录制中）" if recording else "")
            self.status.setStyleSheet("color:#e74c3c; font-weight:bold;")
        else:
            st = "静止"
            self.status.setStyleSheet("color:#9a9a9a;")
        self.status.setText(f"{st} · 事件 {events}")


class LiveTab(QWidget):
    """实时监控标签页。"""

    _quiet = False  # selftest 模式静默

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rects: List[tuple] = []
        self._analysis_rects: List[tuple] = []   # 分析子区域（屏幕全局物理坐标）
        self._monitor: Optional[LiveMonitor] = None
        self._tiles: List[_RegionTile] = []
        self._events: List[dict] = []
        self._t0 = 0.0
        self._out_dir = ""
        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.btn_select = QPushButton("＋ 框选屏幕区域…")
        self.btn_select.setStyleSheet("font-weight:bold;")
        self.btn_select.clicked.connect(self._on_select_regions)
        self.btn_clear = QPushButton("清空区域")
        self.btn_clear.clicked.connect(self._on_clear_regions)
        self.region_label = QLabel("尚未框选区域")
        top.addWidget(self.btn_select)
        top.addWidget(self.btn_clear)
        top.addWidget(self.region_label, 1)
        root.addLayout(top)

        g = QGridLayout()
        g.addWidget(QLabel("帧率"), 0, 0)
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(5, 30)
        self.spin_fps.setValue(15)
        self.spin_fps.setSuffix(" fps")
        g.addWidget(self.spin_fps, 0, 1)
        g.addWidget(QLabel("像素敏感度"), 0, 2)
        self.spin_pixel = QSpinBox()
        self.spin_pixel.setRange(0, 255)
        self.spin_pixel.setValue(25)
        self.spin_pixel.setToolTip("单像素灰度变化超过该值才计为变化。")
        g.addWidget(self.spin_pixel, 0, 3)
        g.addWidget(QLabel("有效变化阈值"), 0, 4)
        self.spin_motion = QDoubleSpinBox()
        self.spin_motion.setRange(0.01, 100.0)
        self.spin_motion.setDecimals(2)
        self.spin_motion.setValue(0.5)
        self.spin_motion.setSuffix(" %")
        g.addWidget(self.spin_motion, 0, 5)
        g.addWidget(QLabel("运动前保留"), 1, 0)
        self.spin_pre = QDoubleSpinBox()
        self.spin_pre.setRange(0.0, 10.0)
        self.spin_pre.setValue(1.0)
        self.spin_pre.setSuffix(" 秒")
        g.addWidget(self.spin_pre, 1, 1)
        g.addWidget(QLabel("运动后保留"), 1, 2)
        self.spin_post = QDoubleSpinBox()
        self.spin_post.setRange(0.0, 30.0)
        self.spin_post.setValue(2.0)
        self.spin_post.setSuffix(" 秒")
        g.addWidget(self.spin_post, 1, 3)
        self.chk_record = QCheckBox("保存运动片段视频")
        self.chk_record.setChecked(True)
        g.addWidget(self.chk_record, 1, 4)
        self.chk_merge = QCheckBox("合并输出（停止后每个区域的片段合并为一个视频）")
        self.chk_merge.setChecked(False)
        g.addWidget(self.chk_merge, 2, 0, 1, 3)
        self.btn_out = QPushButton("结果文件夹…")
        self.btn_out.clicked.connect(self._on_pick_out_dir)
        g.addWidget(self.btn_out, 1, 5)
        self.out_label = QLabel()
        self.out_label.setStyleSheet("color:#9a9a9a;")
        g.addWidget(self.out_label, 3, 0, 1, 6)
        for c in range(6):
            g.setColumnStretch(c, 1 if c % 2 else 0)
        root.addLayout(g)

        # 区域卡片
        self.tiles_scroll = QScrollArea()
        self.tiles_scroll.setWidgetResizable(True)
        self.tiles_scroll.setMinimumHeight(300)
        self._tiles_host = QWidget()
        self._tiles_grid = QGridLayout(self._tiles_host)
        self._tiles_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tiles_scroll.setWidget(self._tiles_host)
        root.addWidget(self.tiles_scroll, 1)

        # 运行行
        run = QHBoxLayout()
        self.btn_start = QPushButton("开始监控")
        self.btn_start.setStyleSheet("font-weight:bold; padding:8px;")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("停止监控")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        self.status_label = QLabel("待机")
        self.btn_open_dir = QPushButton("打开结果文件夹")
        self.btn_open_dir.clicked.connect(self._on_open_dir)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_stop)
        run.addWidget(self.status_label, 1)
        run.addWidget(self.btn_open_dir)
        root.addLayout(run)

        # 事件日志
        self.event_table = QTableWidget(0, 6)
        self.event_table.setHorizontalHeaderLabels(
            ["开始时间", "区域", "结束时间", "时长(秒)", "最大变化率(%)", "片段文件"])
        self.event_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch)
        self.event_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.event_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.event_table.setMaximumHeight(190)
        self.event_table.verticalHeader().setVisible(False)
        root.addWidget(self.event_table)

        self._update_out_label()

    # ---------------------------------------------------------------- 区域框选
    def _on_select_regions(self):
        if self._monitor is not None:
            QMessageBox.information(self, APP_NAME, "监控进行中，请先停止再修改区域。")
            return
        from app.ui.screen_select import ScreenRegionSelector
        sel = ScreenRegionSelector()
        rects, arects = sel.exec_regions(self._rects, self._analysis_rects)
        if rects:
            self._rects = rects
            self._analysis_rects = arects
        self._update_region_label()

    def _on_clear_regions(self):
        if self._monitor is not None:
            return
        self._rects = []
        self._analysis_rects = []
        self._update_region_label()

    def _update_region_label(self):
        if not self._rects:
            self.region_label.setText("尚未框选区域")
            return
        parts = [f"{w}×{h}" for (_x, _y, w, h) in self._rects]
        if self._analysis_rects:
            parts.append(f"{len(self._analysis_rects)} 个分析子区域")
        self.region_label.setText(f"已选 {len(self._rects)} 个区域：  " + "，".join(parts))

    def _default_out_dir(self) -> str:
        videos = os.path.join(os.path.expanduser("~"), "Videos")
        base = videos if os.path.isdir(videos) else os.path.expanduser("~")
        return os.path.join(base, "监控分析器_实时")

    def _on_pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择结果文件夹", self._out_dir or "")
        if d:
            self._out_dir = d
            self._update_out_label()

    def _update_out_label(self):
        d = self._out_dir or self._default_out_dir()
        self.out_label.setText(f"结果保存到：{d}（运动片段 + 事件报告 CSV）")

    # ---------------------------------------------------------------- 运行
    def _on_start(self):
        if self._monitor is not None:
            return
        if not self._rects:
            QMessageBox.warning(self, APP_NAME,
                                "请先点击「＋ 框选屏幕区域…」框选要监控的区域。")
            return
        fps = self.spin_fps.value()
        pre_f = round(self.spin_pre.value() * fps)
        post_f = round(self.spin_post.value() * fps)
        out_dir = self._out_dir or self._default_out_dir()
        regions = []
        for i, (x, y, w, h) in enumerate(self._rects):
            # 归属本区域的分析子区域（屏幕全局坐标 → 传给掩码生成）
            arects = [a for a in self._analysis_rects
                      if a[0] >= x - 64 and a[1] >= y - 64
                      and a[0] + a[2] <= x + w + 64
                      and a[1] + a[3] <= y + h + 64]
            regions.append(RegionConfig(
                name=f"区域{i + 1}", rect=(x, y, w, h),
                pixel_threshold=self.spin_pixel.value(),
                motion_percent=self.spin_motion.value(),
                pre_frames=pre_f, post_frames=post_f,
                record=self.chk_record.isChecked(),
                analysis_rects=arects))
        self._events = []
        self.event_table.setRowCount(0)
        self._rebuild_tiles(len(regions))
        self._out_dir = out_dir
        self._t0 = _dt.datetime.now()
        self._monitor = LiveMonitor(regions, fps=fps, output_dir=out_dir)
        self._monitor.regionStats.connect(self._on_stats)
        self._monitor.previewReady.connect(self._on_preview)
        self._monitor.eventAdded.connect(self._on_event)
        self._monitor.failed.connect(self._on_failed)
        self._monitor.stoppedLive.connect(self._on_stopped)
        self._set_running(True)
        self.status_label.setText("监控中…  00:00:00")
        self._clock_start = self._t0
        self._monitor.start()

    def _rebuild_tiles(self, n: int):
        self._tiles = []
        for i in reversed(range(self._tiles_grid.count())):
            w = self._tiles_grid.itemAt(i).widget()
            if w is not None:
                w.deleteLater()
        per_row = 3
        for k in range(n):
            t = _RegionTile(f"区域{k + 1}")
            self._tiles_grid.addWidget(t, k // per_row, k % per_row)
            self._tiles.append(t)

    def _on_stop(self):
        if self._monitor is not None:
            self.status_label.setText("正在停止…")
            self.btn_stop.setEnabled(False)
            self._monitor.stop()

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_select.setEnabled(not running)
        self.btn_clear.setEnabled(not running)
        for b in (self.spin_fps, self.spin_pixel, self.spin_motion,
                  self.spin_pre, self.spin_post, self.chk_record,
                  self.chk_merge, self.btn_out):
            b.setEnabled(not running)

    # ---------------------------------------------------------------- 信号槽
    def _on_preview(self, rid: int, bgr):
        if 0 <= rid < len(self._tiles):
            self._tiles[rid].set_preview(bgr)

    def _on_stats(self, stats: dict):
        for rid, s in stats.items():
            if 0 <= rid < len(self._tiles):
                self._tiles[rid].set_stats(s["ratio"], s["motion"],
                                           s["events"], s["recording"])
        el = _dt.datetime.now() - self._clock_start
        mm, ss = divmod(int(el.total_seconds()), 60)
        hh, mm = divmod(mm, 60)
        self.status_label.setText(f"监控中…  {hh:02d}:{mm:02d}:{ss:02d}   "
                                  f"事件 {len(self._events)}")

    def _on_event(self, rec: dict):
        self._events.append(rec)
        r = self.event_table.rowCount()
        self.event_table.insertRow(r)
        vals = [rec.get("start", ""), f"区域{rec.get('region', '?')} {rec.get('name', '')}",
                rec.get("end", ""), f"{rec.get('duration', 0):.2f}",
                f"{rec.get('max_ratio', 0):.2f}",
                rec.get("clip", "") or "（未保存视频）"]
        for c, v in enumerate(vals):
            self.event_table.setItem(r, c, QTableWidgetItem(str(v)))
        self.event_table.scrollToBottom()

    def _on_failed(self, msg: str):
        self._set_running(False)
        if self._monitor is not None:
            self._monitor.wait(2000)
            self._monitor = None
        self.status_label.setText("监控出错")
        self._rebuild_tiles(0)
        if not self._quiet:
            QMessageBox.critical(self, "实时监控失败", msg)

    def _on_stopped(self):
        n_ev = len(self._events)
        csv_path = ""
        if n_ev and self._out_dir:
            csv_path = os.path.join(
                self._out_dir, "事件报告_" + self._t0.strftime("%Y%m%d_%H%M%S") + ".csv")
            LiveMonitor.write_event_csv(csv_path, self._events)
        merged_info = self._merge_outputs()
        self._set_running(False)
        if self._monitor is not None:
            self._monitor.wait(2000)
            self._monitor = None
        self.status_label.setText(
            f"已停止 · 共 {n_ev} 个事件"
            + (f" · 报告：{csv_path}" if csv_path else "") + merged_info)

    def _merge_outputs(self) -> str:
        """勾选合并输出时：把每个区域的全部运动片段合并为一个视频。"""
        if not (self.chk_merge.isChecked() and self._events and self._out_dir):
            return ""
        groups = {}
        for rec in self._events:
            cp = rec.get("clip", "")
            if cp:
                key = (rec.get("region", 0), rec.get("name", "区域"))
                groups.setdefault(key, []).append(cp)
        if not groups:
            return ""
        stamp = self._t0.strftime("%Y%m%d_%H%M%S")
        merged = []
        try:
            for (rid, name), paths in sorted(groups.items()):
                out = os.path.join(self._out_dir,
                                   f"{sanitize_name(name)}_合并_{stamp}.mp4")
                frames, n_clips = merge_event_clips(
                    paths, out, float(self.spin_fps.value()))
                merged.append((name, n_clips, frames))
        except Exception as exc:
            if not self._quiet:
                QMessageBox.warning(self, "合并输出失败", str(exc))
            return " · 合并失败"
        return " · 合并：" + "，".join(
            f"{name}（{nc} 段 {fr} 帧）" for name, nc, fr in merged)

    def _on_open_dir(self):
        d = self._out_dir or self._default_out_dir()
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ---------------------------------------------------------------- 生命周期
    def shutdown(self):
        """主窗口关闭时调用：停止监控并等待线程退出。"""
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor.wait(3000)
            self._monitor = None
