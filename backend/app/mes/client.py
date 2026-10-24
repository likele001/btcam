"""MES HTTP 客户端（模块 12）。

只依赖标准库 urllib：后端依赖表里没有 requests/httpx，也不想为了一个可选模块
给 sidecar 增重（打包体积、证书链、代理行为都要重新验证）。

三个能力：
    ping()                       连通性测试（设置页的"测试连接"）
    list_orders(limit)           拉取待加工工单（原始 JSON，字段归一化在 service）
    archive(id, payload, files)  回传工单信息 / 用料统计 / 加工文件

鉴权同时带两种头，兼容不同 MES 的取法：
    Authorization: Bearer <key>
    X-Api-Key:     <key>

重试策略：
    - 只对**可恢复**的失败重试：网络错误，408/425/429/5xx；
    - 401/403/400/404 立即失败——重试也不会变对，还容易被 MES 记账成异常登录；
    - 退避 0.25s → 0.5s → 上限 2s，次数由配置 retries（0~5）；
    - 每次请求的实际尝试次数回传给 service 落库，便于排查"到底试了几次"。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.mes.settings import MesConfig

logger = logging.getLogger(__name__)

# 可恢复：限流 / 网关抖动 / 服务端临时故障
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BACKOFF = 2.0
USER_AGENT = "BtCAM/0.1 (+module12)"


class MesError(RuntimeError):
    """MES 交互失败；带 HTTP 状态码与尝试次数，路由层翻译成 502。"""

    def __init__(self, message: str, status_code: int = 0, attempts: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code or 0)
        self.attempts = int(attempts or 0)


def _parse(raw: bytes) -> Any:
    """尽量解析 JSON；非 JSON 响应（HTML 错误页）原样回字符串。"""
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def _detail_of(data: Any) -> str:
    """从错误响应里抠出人话：{detail}/{message}/{error}/{msg} 都认。"""
    if isinstance(data, dict):
        for key in ("detail", "message", "error", "msg"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)[:300]
    if isinstance(data, str) and data.strip():
        return data.strip()[:300]
    return ""


def _backoff(attempt: int) -> float:
    """指数退避：0.25 / 0.5 / 1 / 2 / 2 …（attempt 从 1 开始）。"""
    return min(MAX_BACKOFF, 0.25 * (2 ** max(0, attempt - 1)))


def _multipart(fields: Dict[str, str],
               files: List[Tuple[str, str, bytes]]) -> Tuple[bytes, str]:
    """手搓 multipart/form-data：文件名用 UTF-8，Content-Type 按后缀猜。"""
    boundary = "----BtCAM%s" % uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in (fields or {}).items():
        chunks.append((
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
            % (boundary, name, value)
        ).encode("utf-8"))
    for field, filename, content in files:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append((
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, field, filename, mime)
        ).encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


class MesClient:
    """一次业务动作一个实例：配置不可变，避免并发时互相串味。"""

    def __init__(self, config: MesConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ 内部
    def _headers(self, content_type: Optional[str] = "application/json") -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        api_key = (self.config.api_key or "").strip()
        if api_key:
            headers["Authorization"] = "Bearer %s" % api_key
            headers["X-Api-Key"] = api_key
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        if not path:
            raise MesError("MES 请求路径未配置")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return "%s%s" % (self.config.base_url, path)

    def request(self, method: str, path: str,
                payload: Optional[Dict[str, Any]] = None,
                files: Optional[List[Tuple[str, str, bytes]]] = None,
                fields: Optional[Dict[str, str]] = None) -> Tuple[Any, int, int]:
        """返回 (解析后的响应, HTTP 状态码, 尝试次数)；失败抛 MesError。"""
        url = self._url(path)
        headers = self._headers()
        body: Optional[bytes] = None
        if files:
            body, content_type = _multipart(fields or {}, files)
            headers["Content-Type"] = content_type
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        total = max(0, int(self.config.retries)) + 1
        attempt = 1
        for attempt in range(1, total + 1):
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                    status = int(getattr(resp, "status", 0) or 0)
                    logger.debug("MES %s %s -> %s", method, path, status)
                    return _parse(resp.read()), status, attempt
            except urllib.error.HTTPError as exc:
                status = int(exc.code or 0)
                data = _parse(exc.read())
                detail = _detail_of(data) or ("MES 返回 HTTP %s" % status)
                if status in RETRY_STATUS and attempt < total:
                    logger.warning("MES %s %s -> HTTP %s，将重试（第 %d 次）",
                                   method, path, status, attempt)
                    time.sleep(_backoff(attempt))
                    continue
                raise MesError(detail, status, attempt) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                if attempt < total:
                    logger.warning("MES %s %s 连接失败，将重试（第 %d 次）：%s",
                                   method, path, attempt, reason)
                    time.sleep(_backoff(attempt))
                    continue
                raise MesError("无法连接 MES：%s" % reason, 0, attempt) from exc

        raise MesError("MES 请求失败", 0, attempt)

    # ------------------------------------------------------------ 业务
    def ping(self) -> Tuple[Any, int, int]:
        return self.request("GET", self.config.path("ping"))

    def list_orders(self, limit: int = 50) -> Tuple[Any, int, int]:
        path = self.config.path("orders")
        if not path:
            raise MesError("MES 请求路径未配置")
        sep = "&" if "?" in path else "?"
        return self.request("GET", "%s%slimit=%d" % (path, sep, max(1, int(limit))))

    def archive(self, remote_id: str, payload: Dict[str, Any],
                files: Optional[List[Tuple[str, str, bytes]]] = None) -> Tuple[Any, int, int]:
        """回传归档：有文件走 multipart（payload 作为 JSON 字段），无文件走 JSON。"""
        path = self.config.path("archive").replace(
            "{remoteId}", urllib.parse.quote(str(remote_id), safe=""))
        if files:
            return self.request("POST", path, files=files,
                                fields={"payload": json.dumps(payload, ensure_ascii=False)})
        return self.request("POST", path, payload=payload)
