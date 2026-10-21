"""主窗口：视频选择、分析区域绘制、参数设置、分析运行与结果预览/保存。

流程：选择视频 → 框选分析区域（可选，不选=全画面）→ 开始分析
（结果先写入临时目录，不直接落在原视频旁）→ 内置播放器预览结果 →
「保存结果…」选择位置导出（视频 + CSV 报告 + 保留帧图片按所选位置自动配套保存）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

import cv2
import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDoubleSpinBox,
                               QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from app.core.analyzer import AnalyzerWorker, AnalysisConfig, build_region_mask
from app.core.video_io import VideoOpenError, open_capture, probe_video, read_frame
from app.ui.batch_tab import BatchTab
from app.ui.live_tab import LiveTab
from app.ui.roi_canvas import RoiCanvas
from app.version import APP_NAME, APP_VERSION


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            f"{APP_NAME} v{APP_VERSION} — 视频帧剔除 / 实时监控 / 批量分析")
        self.resize(1220, 780)
        self._cap = None
        self._video_path = ""
        self._info = None
        self._preview_idx = 0
        self._worker: AnalyzerWorker | None = None
        self._quiet = False          # selftest 模式：跳过模态对话框
        self._last_result = None
        self._kept = 0
        self._processed = 0
        self._ratio_txt = ""
        self._nav_btns: list = []
        # —— 结果预览（播放器）状态
        self._result_active = False
        self._result_cap = None
        self._result_frames = 0
        self._result_idx = 0
        self._result_fps = 25.0
        self._saved_rois: list = []
        self._temp_dir = ""
        self._slider_updating = False
        self._build_ui()
        self._set_running(False)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        page_video = QWidget()
        root = QVBoxLayout(page_video)  # 视频分析页（原有布局全部挂在它下面）
        self.tabs.addTab(page_video, "视频分析")

        top = QHBoxLayout()
        self.btn_open = QPushButton("选择视频…")
        self.btn_open.clicked.connect(self._on_open_video)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("尚未选择视频")
        self.info_label = QLabel("—")
        top.addWidget(self.btn_open)
        top.addWidget(self.path_edit, 3)
        top.addWidget(self.info_label, 2)
        root.addLayout(top)

        body = QHBoxLayout()
        root.addLayout(body, 1)

        # 左：预览画布 + 帧导航（编辑模式）/ 播放器（结果模式）
        left = QVBoxLayout()
        self.canvas = RoiCanvas()
        left.addWidget(self.canvas, 1)

        nav = QHBoxLayout()
        b10 = QPushButton("⏪ 10"); b10.clicked.connect(lambda: self._seek(-10))
        b1 = QPushButton("◀ 1"); b1.clicked.connect(lambda: self._seek(-1))
        self.frame_label = QLabel("帧 —")
        self.frame_label.setAlignment(Qt.AlignCenter)
        f1 = QPushButton("1 ▶"); f1.clicked.connect(lambda: self._seek(1))
        f10 = QPushButton("10 ⏩"); f10.clicked.connect(lambda: self._seek(10))
        self._nav_btns = [b10, b1, f1, f10]
        for w_ in (b10, b1):
            nav.addWidget(w_)
        nav.addWidget(self.frame_label, 1)
        for w_ in (f1, f10):
            nav.addWidget(w_)
        self.nav_widget = QWidget()
        self.nav_widget.setLayout(nav)
        left.addWidget(self.nav_widget)

        # 结果模式播放器行
        player = QHBoxLayout()
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self._on_play_pause)
        self.player_slider = QSlider(Qt.Horizontal)
        self.player_slider.setRange(0, 0)
        self.player_slider.sliderMoved.connect(self._on_slider_seek)
        self.player_slider.valueChanged.connect(self._on_slider_seek)
        self.player_time = QLabel("00:00 / 00:00")
        self.player_time.setMinimumWidth(110)
        self.player_time.setAlignment(Qt.AlignCenter)
        player.addWidget(self.btn_play)
        player.addWidget(self.player_slider, 1)
        player.addWidget(self.player_time)
        self.player_widget = QWidget()
        self.player_widget.setLayout(player)
        self.player_widget.setVisible(False)
        left.addWidget(self.player_widget)

        body.addLayout(left, 7)

        # 右：设置面板（滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(410)
        panel = QWidget()
        scroll.setWidget(panel)
        right = QVBoxLayout(panel)
        body.addWidget(scroll)

        # —— 分析区域
        g_roi = QGroupBox("分析区域（只分析选中的区域）")
        v = QVBoxLayout(g_roi)
        tip = QLabel("在左侧预览上按住鼠标拖拽框选要分析的区域，\n"
                     "例如大门口、车道等。不框选则分析整个画面，\n"
                     "时间数字等放在框外即可自动忽略。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        self.roi_list = QListWidget()
        self.roi_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.roi_list.setMaximumHeight(110)
        v.addWidget(self.roi_list)
        h = QHBoxLayout()
        self.btn_del_roi = QPushButton("删除选中")
        self.btn_del_roi.clicked.connect(self._on_del_roi)
        self.btn_clear_roi = QPushButton("清空全部")
        self.btn_clear_roi.clicked.connect(self._on_clear_roi)
        h.addWidget(self.btn_del_roi)
        h.addWidget(self.btn_clear_roi)
        v.addLayout(h)
        self.canvas.roiCreated.connect(self._on_roi_created)
        self.canvas.roiCountChanged.connect(lambda _n: self._update_roi_summary())
        right.addWidget(g_roi)
        self.roi_summary = QLabel("未设置分析区域（将分析整个画面）")
        self.roi_summary.setWordWrap(True)
        right.addWidget(self.roi_summary)

        # —— 分析参数
        g_param = QGroupBox("分析参数")
        grid = QGridLayout(g_param)
        grid.addWidget(QLabel("像素敏感度 (0-255)"), 0, 0)
        self.spin_pixel = QSpinBox()
        self.spin_pixel.setRange(0, 255)
        self.spin_pixel.setValue(25)
        self.spin_pixel.setToolTip("单个像素变化超过该灰度差才计为变化。夜间噪点多可调大。")
        grid.addWidget(self.spin_pixel, 0, 1)
        grid.addWidget(QLabel("有效变化阈值"), 1, 0)
        self.spin_motion = QDoubleSpinBox()
        self.spin_motion.setRange(0.01, 100.0)
        self.spin_motion.setDecimals(2)
        self.spin_motion.setValue(0.5)
        self.spin_motion.setSuffix(" %")
        self.spin_motion.setToolTip("分析区域内变化像素占比 ≥ 此值时，该帧判为有效运动帧并保留。")
        grid.addWidget(self.spin_motion, 1, 1)
        grid.addWidget(QLabel("运动开始前保留帧数"), 2, 0)
        self.spin_pre = QSpinBox()
        self.spin_pre.setRange(0, 120)
        self.spin_pre.setValue(5)
        grid.addWidget(self.spin_pre, 2, 1)
        grid.addWidget(QLabel("运动结束后保留帧数"), 3, 0)
        self.spin_post = QSpinBox()
        self.spin_post.setRange(0, 120)
        self.spin_post.setValue(10)
        self.spin_post.setToolTip("结束后多保留若干帧，避免动作被截断；两次运动靠得近时自动合并片段。")
        grid.addWidget(self.spin_post, 3, 1)
        grid.addWidget(QLabel("并行线程数"), 4, 0)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(0, 32)
        self.spin_threads.setValue(0)
        self.spin_threads.setToolTip(
            "0=自动（短视频单线程最快，长视频自动并行）；1=单线程顺序模式。\n"
            "多线程分片处理把视频分段并行解码分析，可加速长视频。")
        grid.addWidget(self.spin_threads, 4, 1)
        self.chk_fast = QCheckBox("高速模式（大分辨率视频降采样分析，可能漏检细小目标）")
        self.chk_fast.setChecked(False)
        self.chk_fast.setToolTip(
            "画面宽于 1280 像素时按比例缩小后做变化分析，明显提速（输出仍为原始分辨率）。\n"
            "注意：降采样会稀释信号，细小或微弱的目标（如远处火焰、小动物）可能达不到\n"
            "像素敏感度而漏检。默认关闭；普通监控画面（行人、车辆等较大目标）可放心开启。")
        grid.addWidget(self.chk_fast, 5, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        right.addWidget(g_param)

        # —— 输出选项
        g_out = QGroupBox("输出选项")
        v = QVBoxLayout(g_out)
        note = QLabel("分析完成后结果先在软件内预览，\n点「保存结果…」再选择位置导出。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9a9a9a;")
        v.addWidget(note)
        self.chk_csv = QCheckBox("生成 CSV 报告（每帧变化率与保留标记）")
        self.chk_csv.setChecked(True)
        self.chk_images = QCheckBox("保留帧另存为图片")
        self.chk_burn = QCheckBox("输出帧上烧录原始时间戳水印")
        self.chk_burn.setChecked(True)
        self.chk_preview = QCheckBox("分析时显示实时预览（当前画面+变化标红）")
        self.chk_preview.setChecked(True)
        for c in (self.chk_csv, self.chk_images, self.chk_burn, self.chk_preview):
            v.addWidget(c)
        right.addWidget(g_out)

        # —— 运行
        g_run = QGroupBox("运行")
        v = QVBoxLayout(g_run)
        h = QHBoxLayout()
        self.btn_start = QPushButton("开始分析")
        self.btn_start.setStyleSheet("font-weight:bold; padding:8px;")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        h.addWidget(self.btn_start, 1)
        h.addWidget(self.btn_cancel)
        v.addLayout(h)
        self.progress = QProgressBar()
        v.addWidget(self.progress)
        self.stats_label = QLabel("待机")
        v.addWidget(self.stats_label)
        self.preview_label = QLabel()
        self.preview_label.setFixedHeight(240)  # 固定高度：防止预览棘轮式撑大面板
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setStyleSheet("background:#141414; border:1px solid #333;")
        v.addWidget(self.preview_label)
        h = QHBoxLayout()
        self.btn_save_result = QPushButton("保存结果…")
        self.btn_save_result.setStyleSheet("font-weight:bold;")
        self.btn_save_result.setVisible(False)
        self.btn_save_result.clicked.connect(self._on_save_result)
        self.btn_discard_result = QPushButton("放弃结果")
        self.btn_discard_result.setVisible(False)
        self.btn_discard_result.clicked.connect(self._on_discard_result)
        h.addWidget(self.btn_save_result, 1)
        h.addWidget(self.btn_discard_result)
        v.addLayout(h)
        right.addWidget(g_run)
        right.addStretch(1)

        # 播放定时器
        self._player_timer = QTimer(self)
        self._player_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._player_timer.timeout.connect(self._player_tick)

        # 实时监控 / 批量分析 两个标签页
        self.live_tab = LiveTab()
        self.tabs.addTab(self.live_tab, "实时监控")
        self.batch_tab = BatchTab()
        self.tabs.addTab(self.batch_tab, "批量分析")

    # ---------------------------------------------------------------- 视频加载
    def _on_open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频文件", "",
            "视频文件 (*.mp4 *.avi *.mkv *.mov *.ts *.flv *.wmv *.webm);;所有文件 (*.*)")
        if not path:
            return
        try:
            self.load_video(path)
        except VideoOpenError as e:
            QMessageBox.critical(self, "打开失败", str(e))

    def load_video(self, path: str):
        info = probe_video(path)
        cap, backend = open_capture(path, verify=True)  # verify：确认真的能解码画面
        if self._cap is not None:
            self._cap.release()
        self._cap = cap
        self._video_path = path
        self._info = info
        self._preview_idx = 0
        self.path_edit.setText(path)
        self.info_label.setText(info.summary() + f"   解码：{backend}")
        # 打开新视频 = 全新开始：先作废旧结果并清空遗留的分析区域，
        # 再退出结果模式（否则 _leave_result_mode 会把 _saved_rois 画回画布）。
        self._saved_rois = []
        self._leave_result_mode(discard=True)
        self.canvas.clearRois()
        self.roi_list.clear()
        if not self._show_frame(0):
            self._cap.release()
            self._cap = None
            raise VideoOpenError(
                f"视频可以打开，但无法读取画面帧：{path}\n"
                f"该文件使用了系统缺失解码器的编码格式，请转换格式后重试。")
        if not (self._worker and self._worker.isRunning()):
            self.btn_start.setEnabled(True)

    def _show_frame(self, idx: int) -> bool:
        """显示源视频第 idx 帧；返回是否成功。"""
        if self._cap is None:
            return False
        ok, frame = read_frame(self._cap, max(0, idx), self._video_path)
        if ok:
            self._preview_idx = max(0, idx)
            self.canvas.setFrame(frame)
            total = self._info.frame_count if self._info else 0
            tail = f" / {total}" if total > 0 else ""
            self.frame_label.setText(f"帧 {self._preview_idx}{tail}")
        else:
            self.frame_label.setText(f"帧 {max(0, idx)} 读取失败")
        return ok

    def _seek(self, delta: int):
        if self._cap is None or self._result_active or (self._worker and self._worker.isRunning()):
            return
        self._show_frame(self._preview_idx + delta)

    # ---------------------------------------------------------------- ROI
    def _on_roi_created(self, x, y, w, h):
        n = self.roi_list.count() + 1
        self.roi_list.addItem(f"分析区域 {n}:  x={x}, y={y}, w={w}, h={h}")

    def _on_del_roi(self):
        row = self.roi_list.currentRow()
        if row >= 0:
            self.roi_list.takeItem(row)
            self.canvas.removeRoi(row)
            self._renumber_rois()

    def _on_clear_roi(self):
        self.roi_list.clear()
        self.canvas.clearRois()

    def _renumber_rois(self):
        rois = self.canvas.rois()
        for i in range(min(self.roi_list.count(), len(rois))):
            x, y, w, h = rois[i]
            self.roi_list.item(i).setText(f"分析区域 {i + 1}:  x={x}, y={y}, w={w}, h={h}")

    def _update_roi_summary(self):
        rois = self.canvas.rois()
        if not rois:
            self.roi_summary.setText("未设置分析区域（将分析整个画面）")
            return
        if self._info:
            amask = build_region_mask(self._info.width, self._info.height, rois)
            pct = int(np.count_nonzero(amask)) / amask.size * 100
            self.roi_summary.setText(
                f"已设 {len(rois)} 个分析区域，覆盖画面 {pct:.1f}%；\n"
                f"只分析这些区域，其余 {100 - pct:.1f}% 忽略。")
        else:
            self.roi_summary.setText(f"已设 {len(rois)} 个分析区域。")

    # ---------------------------------------------------------------- 运行
    def _on_start(self):
        if self._info is None:
            QMessageBox.warning(self, APP_NAME, "请先选择视频文件。")
            return
        if self._worker is not None:
            return  # 正在分析（结束后 _worker 会被置 None）
        if self._result_active:
            self._leave_result_mode(discard=True)
        rois = self.canvas.rois()
        if rois:
            amask = build_region_mask(self._info.width, self._info.height, rois)
            if int(np.count_nonzero(amask)) == 0:
                QMessageBox.warning(self, APP_NAME,
                                    "分析区域为空（面积 0）。\n请重新框选分析区域。")
                return

        # 结果一律先写到临时目录，由用户在结果页「保存结果…」决定去向
        self._discard_temp()
        self._temp_dir = tempfile.mkdtemp(prefix="监控分析器_")
        base = os.path.splitext(self._video_path)[0]
        csv_path = os.path.join(self._temp_dir, "result.csv") if self.chk_csv.isChecked() else ""
        images_dir = os.path.join(self._temp_dir, "images") if self.chk_images.isChecked() else ""
        cfg = AnalysisConfig(
            video_path=self._video_path,
            output_video_path=os.path.join(self._temp_dir, "result.mp4"),
            export_images=bool(images_dir),
            images_dir=images_dir,
            export_csv=bool(csv_path),
            csv_path=csv_path,
            burn_timestamp=self.chk_burn.isChecked(),
            show_preview=self.chk_preview.isChecked(),
            pixel_threshold=self.spin_pixel.value(),
            motion_percent=self.spin_motion.value(),
            pre_roll=self.spin_pre.value(),
            post_roll=self.spin_post.value(),
            rois=rois,
            threads=self.spin_threads.value(),
            fast_mode=self.chk_fast.isChecked(),
        )
        self._set_running(True)
        self.progress.setRange(0, max(1, self._info.frame_count))
        self.progress.setValue(0)
        self._kept = 0
        self._processed = 0
        self._ratio_txt = ""
        self._run_t0 = time.monotonic()
        self.stats_label.setText("分析中…")
        self._worker = AnalyzerWorker(cfg)
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.statsChanged.connect(self._on_stats)
        self._worker.previewReady.connect(self._on_preview)
        self._worker.finishedOk.connect(self._on_finished)
        self._worker.canceled.connect(self._on_canceled)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_cancel(self):
        if self._worker:
            self._worker.cancel()

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running and self._info is not None)
        self.btn_cancel.setEnabled(running)
        self.btn_open.setEnabled(not running)
        for b in self._nav_btns:
            b.setEnabled(not running)
        self.btn_save_result.setEnabled(not running)
        self.btn_discard_result.setEnabled(not running)

    def _on_progress(self, processed, total, ratio):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(processed, total))
        else:
            self.progress.setRange(0, 0)
        self._ratio_txt = f"{ratio:.2f}%"
        self._update_stats()

    def _on_stats(self, kept, processed):
        self._kept = kept
        self._processed = processed
        self._update_stats()

    def _update_stats(self):
        fps_txt = ""
        if self._processed > 0 and getattr(self, "_run_t0", None):
            el = time.monotonic() - self._run_t0
            if el > 0.5:
                fps_txt = f"   约 {self._processed / el:.0f} 帧/秒"
        self.stats_label.setText(
            f"已处理 {self._processed} 帧   保留 {self._kept} 帧   当前变化率 {self._ratio_txt}{fps_txt}")

    def _on_preview(self, img):
        lbl = self.preview_label
        tgt = lbl.size() - QSize(4, 4)  # 扣除边框余量，避免最小尺寸提示逐次撑大
        if tgt.width() < 32 or tgt.height() < 32:
            return
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        lbl.setPixmap(QPixmap.fromImage(qimg.copy()).scaled(
            tgt, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # ---------------------------------------------------------------- 结果模式
    def _on_finished(self, res):
        self._worker = None  # 线程已结束：清引用，允许再次「开始分析」
        self._last_result = res
        self._set_running(False)
        self.progress.setValue(self.progress.maximum())
        self.stats_label.setText(res.message)
        if res.ok and res.output_video_path and os.path.isfile(res.output_video_path):
            self._enter_result_mode(res)
        else:
            self._discard_temp()
            if not self._quiet:
                QMessageBox.warning(self, "分析未完成", res.message)

    def _on_canceled(self, res):
        self._worker = None
        self._last_result = None
        self._set_running(False)
        self._discard_temp()
        self.stats_label.setText(res.message)
        if not self._quiet:
            QMessageBox.warning(self, "已取消", res.message)

    def _on_failed(self, msg):
        self._worker = None
        self._set_running(False)
        self._discard_temp()
        self.stats_label.setText("分析失败")
        if not self._quiet:
            QMessageBox.critical(self, "分析失败", msg)

    def _enter_result_mode(self, res):
        """分析完成：进入内置预览播放器模式。"""
        self._last_result = res
        try:
            cap, _backend = open_capture(res.output_video_path)
        except VideoOpenError as e:
            self._discard_temp()
            self.stats_label.setText("结果视频打开失败")
            if not self._quiet:
                QMessageBox.critical(self, "错误", str(e))
            return
        self._result_cap = cap
        self._result_frames = int(max(0, res.kept_frames))
        self._result_idx = 0
        self._result_fps = self._info.fps if self._info else 25.0
        self._result_active = True
        self._saved_rois = self.canvas.rois()
        self.canvas.clearRois()
        self.canvas.setInteractive(False)
        self.nav_widget.setVisible(False)
        self.player_widget.setVisible(True)
        self.preview_label.setVisible(False)
        self.btn_save_result.setVisible(True)
        self.btn_discard_result.setVisible(True)
        self.player_slider.blockSignals(True)
        self.player_slider.setRange(0, max(0, self._result_frames - 1))
        self.player_slider.setValue(0)
        self.player_slider.blockSignals(False)
        self.btn_play.setText("⏸ 暂停")
        interval = max(15, int(round(1000.0 / max(1.0, self._result_fps))))
        self._player_timer.start(interval)
        self._update_player_time()

    def _leave_result_mode(self, discard: bool):
        """退出结果模式回到编辑状态（discard=True 时删除临时目录）。"""
        self._player_timer.stop()
        if self._result_cap is not None:
            self._result_cap.release()
            self._result_cap = None
        if self._result_active:
            self._result_active = False
            self.canvas.setInteractive(True)
            self.canvas.setRois(self._saved_rois)
            self._saved_rois = []
            self.nav_widget.setVisible(True)
            self.player_widget.setVisible(False)
            self.preview_label.setVisible(True)
            self.btn_save_result.setVisible(False)
            self.btn_discard_result.setVisible(False)
            if self._cap is not None:
                self._show_frame(self._preview_idx)
        if discard:
            self._discard_temp()

    def _discard_temp(self):
        if self._temp_dir and os.path.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = ""

    def _player_tick(self):
        if self._result_cap is None:
            self._player_timer.stop()
            return
        ok, frame = self._result_cap.read()
        if not ok:
            self._player_timer.stop()
            self.btn_play.setText("▶ 播放")
            return
        self.canvas.setFrame(frame)
        self._slider_updating = True
        self.player_slider.setValue(self._result_idx)
        self._slider_updating = False
        self._update_player_time()
        self._result_idx += 1

    def _on_play_pause(self):
        if self._result_cap is None:
            return
        if self._player_timer.isActive():
            self._player_timer.stop()
            self.btn_play.setText("▶ 播放")
        else:
            if self._result_idx >= self._result_frames:  # 播完后再按 = 从头播放
                self._result_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._result_idx = 0
            self.btn_play.setText("⏸ 暂停")
            self._player_timer.start(max(15, int(round(1000.0 / max(1.0, self._result_fps)))))

    def _on_slider_seek(self, value):
        if self._slider_updating or self._result_cap is None:
            return
        self._player_timer.stop()  # 拖动时暂停，松手位置即为停留帧
        self.btn_play.setText("▶ 播放")
        ok, frame = read_frame(self._result_cap, value, self._last_result.output_video_path
                               if self._last_result else "")
        if ok:
            self.canvas.setFrame(frame)
        self._result_idx = value
        self._update_player_time()

    def _update_player_time(self):
        fps = self._result_fps if self._result_fps > 0 else 25.0

        def fmt(sec: float) -> str:
            m, s = divmod(int(sec), 60)
            return f"{m:02d}:{s:02d}"

        cur = fmt(self._result_idx / fps)
        total = fmt(self._result_frames / fps)
        self.player_time.setText(f"{cur} / {total}")

    # ---------------------------------------------------------------- 保存
    def _on_save_result(self):
        res = self._last_result
        if not res or not res.output_video_path:
            return
        default_dir = os.path.dirname(self._video_path) if self._video_path else ""
        default_name = os.path.splitext(os.path.basename(self._video_path))[0] + "_剔除后.mp4" \
            if self._video_path else "result.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存结果视频", os.path.join(default_dir, default_name),
            "MP4 视频 (*.mp4)")
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        saved = self._save_result_to(path)
        if saved and not self._quiet:
            QMessageBox.information(self, "已保存", "已保存：\n" + "\n".join(saved))

    def _save_result_to(self, video_path: str) -> list:
        """把结果视频（及 CSV/图片）复制到指定位置，返回实际保存路径列表。"""
        res = self._last_result
        if not res or not res.output_video_path or not os.path.isfile(res.output_video_path):
            return []
        saved = []
        base = os.path.splitext(video_path)[0]
        shutil.copy2(res.output_video_path, video_path)
        saved.append(video_path)
        if res.csv_path and os.path.isfile(res.csv_path):
            csv_target = base + "_报告.csv"
            shutil.copy2(res.csv_path, csv_target)
            saved.append(csv_target)
        if res.images_dir and os.path.isdir(res.images_dir):
            images_target = base + "_保留帧"
            shutil.copytree(res.images_dir, images_target, dirs_exist_ok=True)
            saved.append(images_target)
        return saved

    def _on_discard_result(self):
        self._leave_result_mode(discard=True)
        self.stats_label.setText("已放弃分析结果。")
        if not self._quiet:
            QMessageBox.information(self, APP_NAME, "已放弃本次分析结果，临时文件已清理。")

    # ---------------------------------------------------------------- 退出
    def closeEvent(self, ev):
        if self._worker is not None:
            r = QMessageBox.question(self, "退出", "分析正在进行，确定要退出吗？")
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self._worker.cancel()
            self._worker.wait(5000)
        self.live_tab.shutdown()
        self.batch_tab.shutdown()
        self._player_timer.stop()
        if self._result_cap is not None:
            self._result_cap.release()
            self._result_cap = None
        self._discard_temp()
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        super().closeEvent(ev)
