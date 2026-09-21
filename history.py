"""指标时序环形缓冲：为曲线图提供最近 N 秒的数据点。"""

from __future__ import annotations

import threading
import time
from collections import deque

WINDOW_SECONDS = 120.0  # 曲线时间窗口，与截图「最近 2 分钟」一致
MAXLEN = 1200           # 约 20 分钟 @1s，读取时再按时间窗口裁剪


class History:
    def __init__(self, maxlen=MAXLEN):
        self._lock = threading.Lock()
        self._series = {}
        self._maxlen = maxlen

    def append(self, key, ts, value):
        if value is None:
            return
        with self._lock:
            buf = self._series.get(key)
            if buf is None:
                buf = self._series[key] = deque(maxlen=self._maxlen)
            buf.append((float(ts), float(value)))

    def window(self, key, seconds=WINDOW_SECONDS):
        """返回最近 seconds 秒内的点，用于首次加载。"""
        cutoff = time.time() - seconds
        with self._lock:
            buf = self._series.get(key)
            if not buf:
                return []
            return [[ts, val] for ts, val in buf if ts >= cutoff]

    def since(self, key, since_ts):
        """只返回比 since_ts 更新的点，用于增量轮询。"""
        with self._lock:
            buf = self._series.get(key)
            if not buf:
                return []
            return [[ts, val] for ts, val in buf if ts > since_ts]

    def keys(self):
        with self._lock:
            return list(self._series.keys())
