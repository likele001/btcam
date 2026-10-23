"""WebSocket 客户端连接中心（Hub）。

职责单一：维护已连接的客户端集合，提供线程安全的广播能力。
业务模块只依赖 `hub.broadcast(...)`，不关心底层是几个窗口。

注意：任务在子线程 / 子进程中完成，广播必须通过
`loop.call_soon_threadsafe` 切回事件循环，否则会踩 asyncio 的线程安全问题。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from app.core import protocol
from app.core.protocol import MsgType

logger = logging.getLogger(__name__)


class ClientHub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        # 用 threading.Lock 而不是 asyncio.Lock：Python 3.8/3.9 的 asyncio.Lock
        # 在构造时绑定当前 loop，本类在模块导入期实例化，会绑到隐式创建的废 loop。
        # 临界区只有集合增删（微秒级），同步锁不会阻塞事件循环。
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------ 生命周期
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.add(ws)
            logger.info("WS 客户端接入，当前连接数=%d", len(self._clients))

    async def unregister(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.discard(ws)
            logger.info("WS 客户端断开，当前连接数=%d", len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------ 发送
    async def send_frame(
        self,
        ws: WebSocket,
        msg_type: MsgType,
        meta: Optional[Dict[str, Any]] = None,
        payload: bytes = b"",
        seq: int = 0,
    ) -> None:
        # 大 payload 自动拆成多个 CHUNK 帧逐条发送，避免单条 WS 消息过大
        for data in protocol.iter_frames(msg_type, meta, payload, seq):
            await ws.send_bytes(data)

    async def broadcast(
        self,
        msg_type: MsgType,
        meta: Optional[Dict[str, Any]] = None,
        payload: bytes = b"",
        seq: int = 0,
    ) -> None:
        """广播一帧；单连接失败只做剔除，不影响其它客户端。"""
        if not self._clients:
            return
        # 先编码一次，多客户端复用同一批分片字节
        frames = list(protocol.iter_frames(msg_type, meta, payload, seq))
        dead: List[WebSocket] = []
        for ws in list(self._clients):
            try:
                for data in frames:
                    await ws.send_bytes(data)
            except Exception:  # 连接已断
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)

    def broadcast_threadsafe(
        self,
        msg_type: MsgType,
        meta: Optional[Dict[str, Any]] = None,
        payload: bytes = b"",
    ) -> None:
        """供工作线程调用的广播入口（自动切回事件循环）。"""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast(msg_type, meta, payload), self._loop
        )


# 全局单例：整个进程一个 Hub
hub = ClientHub()
