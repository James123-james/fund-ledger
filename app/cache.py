"""进程内 TTL 缓存：只缓存成功的 GET JSON 响应。

数据只在写操作后变化，所以写请求统一 clear；读请求 30 秒内直接命中。
线程安全，条目数和总字节都有限制，避免长跑占内存。
"""

import threading
import time


class TTLCache:
    def __init__(self, ttl=30.0, max_entries=128, max_bytes=2 * 1024 * 1024):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._data = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if not item:
                self.misses += 1
                return None
            expires, raw, headers = item
            if expires <= now:
                self._bytes -= len(raw)
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return raw, headers

    def set(self, key, raw, headers):
        if len(raw) > 512 * 1024:
            return
        now = time.monotonic()
        with self._lock:
            old = self._data.pop(key, None)
            if old:
                self._bytes -= len(old[1])
            if len(self._data) >= self.max_entries:
                # 简单淘汰最早过期的条目；条目少，线性扫描可接受
                oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                old = self._data.pop(oldest, None)
                if old:
                    self._bytes -= len(old[1])
            if self._bytes + len(raw) > self.max_bytes:
                return
            self._data[key] = (now + self.ttl, raw, dict(headers or {}))
            self._bytes += len(raw)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self):
        with self._lock:
            return {"entries": len(self._data), "bytes": self._bytes, "hits": self.hits, "misses": self.misses}


CACHE = TTLCache()
