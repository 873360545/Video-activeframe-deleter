# -*- coding: utf-8 -*-
"""实时监控纯逻辑单元测试（无需屏幕捕获）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.live_monitor import (assemble_events, sanitize_name,
                                   _clip_for_rect, region_local_mask)

MONS = [{"left": 0, "top": 0, "width": 2560, "height": 1600},
        {"left": 0, "top": 0, "width": 2560, "height": 1600}]


class TestAssembleEvents(unittest.TestCase):
    def test_basic_with_pre_post(self):
        ratios = [0.0] * 10 + [1.0] * 5 + [0.0] * 10
        ev = assemble_events(ratios, 0.5, 2, 3)
        self.assertEqual(ev, [(8, 17)])

    def test_close_segments_merge(self):
        # 两段运动间隔 ≤ post_roll → 合并
        ratios = [0, 0, 1, 0, 0, 1, 0, 0]
        ev = assemble_events(ratios, 0.5, 0, 2)
        self.assertEqual(ev, [(2, 7)])

    def test_far_segments_separate(self):
        ratios = [1, 0, 0, 0, 0, 0, 1]
        ev = assemble_events(ratios, 0.5, 0, 1)
        self.assertEqual(ev, [(0, 1), (6, 6)])

    def test_no_motion(self):
        self.assertEqual(assemble_events([0.0] * 50, 0.5, 3, 5), [])

    def test_all_motion(self):
        self.assertEqual(assemble_events([1.0] * 30, 0.5, 2, 2), [(0, 29)])

    def test_threshold_respected(self):
        ratios = [0.4] * 20
        self.assertEqual(assemble_events(ratios, 0.5, 1, 1), [])
        self.assertEqual(assemble_events(ratios, 0.3, 1, 1), [(0, 19)])


class TestClipMapping(unittest.TestCase):
    def test_single_monitor(self):
        clip = _clip_for_rect((100, 120, 320, 240), MONS)
        self.assertEqual(clip, {"left": 100, "top": 120, "width": 320,
                                "height": 240})

    def test_second_monitor_negative_origin(self):
        mons = [{"left": -1920, "top": 0, "width": 5760, "height": 1080},
                {"left": -1920, "top": 0, "width": 1920, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": 1920, "top": 0, "width": 1920, "height": 1080}]
        # 位于第二台显示器（x∈[0,1920)）上的矩形
        clip = _clip_for_rect((100, 50, 200, 100), mons)
        self.assertEqual(clip, {"left": 100, "top": 50, "width": 200,
                                "height": 100})
        # 位于第三台显示器（x∈[1920,3840)）上的矩形 → 相对其原点
        clip = _clip_for_rect((2000, 50, 200, 100), mons)
        self.assertEqual(clip, {"left": 80, "top": 50, "width": 200,
                                "height": 100})

    def test_fallback_virtual(self):
        clip = _clip_for_rect((5000, 0, 100, 100), MONS)  # 超出已知显示器
        self.assertEqual(clip["width"], 100)


class TestSanitize(unittest.TestCase):
    def test_illegal_chars(self):
        self.assertEqual(sanitize_name('区域<A>:"b/c|?*\\d'), "区域_A_b_c_d")

    def test_empty(self):
        self.assertEqual(sanitize_name("   "), "区域")
        self.assertEqual(sanitize_name("..."), "区域")


class TestEventCsv(unittest.TestCase):
    def test_write_event_csv(self):
        import csv as _csv
        import os
        import tempfile
        from app.core.live_monitor import LiveMonitor

        path = os.path.join(tempfile.gettempdir(), "selftest_events.csv")
        events = [
            {"region": 1, "name": "区域1", "start": "2026-10-20 10:00:00",
             "end": "2026-10-20 10:00:03", "duration": 3.2, "max_ratio": 5.12,
             "frames": 48, "clip": r"C:\out\a.mp4"},
            {"region": 2, "name": "区域2", "start": "2026-10-20 10:01:00",
             "end": "2026-10-20 10:01:01", "duration": 1.0, "max_ratio": 2.0,
             "frames": 15, "clip": ""},
        ]
        n = LiveMonitor.write_event_csv(path, events)
        self.assertEqual(n, 2)
        with open(path, encoding="utf-8-sig") as f:
            rows = list(_csv.reader(f))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "区域")
        self.assertEqual(rows[1][7], r"C:\out\a.mp4")
        self.assertEqual(rows[2][5], "2.00")
        os.remove(path)


class TestMergeClips(unittest.TestCase):
    @staticmethod
    def _make_clip(path, frames, gray_value, size=(80, 60)):
        import cv2
        import numpy as np
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                             15.0, size)
        assert vw.isOpened()
        for _ in range(frames):
            vw.write(np.full((size[1], size[0], 3), gray_value, np.uint8))
        vw.release()

    def test_merge_two_clips(self):
        import cv2
        import tempfile
        from app.core.live_monitor import merge_event_clips

        d = tempfile.mkdtemp(prefix="test_merge_")
        c1 = os.path.join(d, "a.mp4")
        c2 = os.path.join(d, "b.mp4")
        self._make_clip(c1, 10, 60)
        self._make_clip(c2, 15, 200)
        out = os.path.join(d, "merged.mp4")
        total, used = merge_event_clips([c1, c2], out, 15.0)
        self.assertEqual((total, used), (25, 2))
        cap = cv2.VideoCapture(out)
        count, first, last = 0, None, None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if first is None:
                first = frame.mean()
            last = frame.mean()
            count += 1
        cap.release()
        self.assertEqual(count, 25)
        self.assertLess(first, 90)    # 片段1 在前（灰度 60）
        self.assertGreater(last, 150)  # 片段2 在后（灰度 200）
        os.remove(out)

    def test_missing_and_empty(self):
        import tempfile
        from app.core.live_monitor import merge_event_clips

        d = tempfile.mkdtemp(prefix="test_merge2_")
        out = os.path.join(d, "m.mp4")
        with self.assertRaises(ValueError):
            merge_event_clips([], out, 15.0)
        with self.assertRaises(ValueError):
            merge_event_clips([os.path.join(d, "不存在.mp4")], out, 15.0)


class TestRegionMask(unittest.TestCase):
    """v1.7：监控区域内的分析子区域掩码。"""

    def test_no_analysis_is_none(self):
        self.assertIsNone(region_local_mask((0, 0, 100, 80), []))
        self.assertIsNone(region_local_mask((0, 0, 100, 80), None))

    def test_full_coverage(self):
        m = region_local_mask((0, 0, 100, 80), [(0, 0, 100, 80)])
        self.assertEqual(m.shape, (80, 100))
        self.assertEqual(int(m.sum()) // 255, 100 * 80)

    def test_partial_region(self):
        # 监控区域 (0,0,100,80)，分析区域 (50,40,100,100) → 只留右下 50×40
        m = region_local_mask((0, 0, 100, 80), [(50, 40, 100, 100)])
        self.assertEqual(m.shape, (80, 100))
        self.assertEqual(int(m.sum()) // 255, 50 * 40)
        self.assertEqual(m[0, 0], 0)      # 左上不参与
        self.assertEqual(m[79, 99], 255)  # 右下参与

    def test_outside_dropped(self):
        # 完全在监控区域外 → 全零 → None（整区域分析）
        self.assertIsNone(region_local_mask((0, 0, 100, 80), [(200, 200, 50, 50)]))

    def test_region_offset(self):
        # 监控区域 (1000, 500, 200, 150)，分析区域是其中心 (1100,550,100,75)
        m = region_local_mask((1000, 500, 200, 150), [(1100, 550, 100, 75)])
        self.assertEqual(m.shape, (150, 200))
        self.assertEqual(int(m.sum()) // 255, 100 * 75)
        self.assertEqual(m[0, 0], 0)
        self.assertEqual(m[100, 150], 255)


if __name__ == "__main__":
    unittest.main()
