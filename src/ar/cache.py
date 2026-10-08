"""讲解缓存的进程内 LRU（**只缓存成功结果**）。

缓存键（契约见 README §4）：

    (source_id, paragraph, kind, 规范化后的 selection, 画像指纹)

三条纪律：

- **进程内、重启即空**：不落盘、不加 HTTP 缓存头，服务重启后自然清空；
- **画像一改即失效**：画像指纹 = ``profiles/<reader>/profile.yaml`` 的
  ``(路径, 字节数, mtime_ns)``——自评写回（``POST /api/profile/rating``）或手改
  ``overrides`` 都会换指纹，旧条目再也不会被取到；
- **只缓存成功结果**（``degraded: false``）：失败与降级一律不进缓存，下一次请求照常重试。

线程安全：uvicorn 的线程池可能并发打同一个键，读写都走同一把 ``threading.Lock``。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .paths import DEFAULT_READER, profile_path

__all__ = ["DEFAULT_CAPACITY", "CacheKey", "ExplainCache", "profile_fingerprint", "explain_cache_key"]

# 契约容量：128 条（每条一次真实模型调用的产出，够一次阅读会话用）
DEFAULT_CAPACITY = 128

# 画像指纹：(profile.yaml 路径, 字节数, mtime_ns)；文件不在时后两项为 -1。
ProfileFingerprint = tuple[str, int, int]
CacheKey = tuple[str, str, str, str, ProfileFingerprint]


def profile_fingerprint(profiles_dir: Path | str, reader: str = DEFAULT_READER) -> ProfileFingerprint:
    """画像指纹：``profile.yaml`` 的 ``(路径, 字节数, mtime_ns)``。

    只 ``stat`` 不解析 YAML（缓存命中时连画像都不用读），文件不存在时给 ``-1`` 哨兵——
    文件被删掉同样会换指纹，缓存自然失效。
    """
    path = profile_path(profiles_dir, reader)
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), int(stat.st_size), int(stat.st_mtime_ns))


def explain_cache_key(
    *,
    source_id: str,
    paragraph_id: str,
    kind: str,
    selection: str,
    profiles_dir: Path | str,
    reader: str = DEFAULT_READER,
) -> CacheKey:
    """组装缓存键。

    ``selection`` 传**实际送模型的有效片段**（规范化 + 截断到 600 字符之后）：提示词只用得到
    这一段文字，超出 600 字符的部分本来就不进提示词，拿它当键的一部分只会白白降低命中率。
    """
    return (source_id, paragraph_id, kind, selection, profile_fingerprint(profiles_dir, reader))


class ExplainCache:
    """容量固定的 LRU（``OrderedDict`` + 一把锁）：命中即移到队尾，超容量丢队首。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if int(capacity) < 1:
            raise ValueError(f"缓存容量必须 ≥ 1，收到 {capacity!r}")
        self._capacity = int(capacity)
        self._items: OrderedDict[CacheKey, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        """取一条（命中同时把它移到队尾）；返回副本，调用方改不到缓存里的条目。"""
        with self._lock:
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return dict(value)

    def put(self, key: CacheKey, value: dict[str, Any]) -> None:
        """写入一条（同键覆盖），并按容量淘汰最久未用的条目。"""
        with self._lock:
            self._items[key] = dict(value)
            self._items.move_to_end(key)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items
