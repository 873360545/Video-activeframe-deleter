"""CSV 报告输出（UTF-8 BOM，Excel 可直接打开中文）。"""
from __future__ import annotations

import csv
import os
from typing import Iterable, Sequence


def write_csv(path: str, rows: Iterable[Sequence],
              header: Sequence[str] = ("帧号", "时间", "变化率%", "保留"),
              convert: bool = True) -> None:
    """rows: 每行 (帧号, 时间戳, 变化率%, 保留0/1)。

    header/convert 供其他报表复用：header 为列名，convert=False 时
    按原样写出（不做「保留0/1 → 是/否」映射）。
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            if convert and len(row) == 4:
                frame_idx, ts, ratio, kept = row
                w.writerow([frame_idx, ts, ratio, "是" if kept else "否"])
            else:
                w.writerow(row)
