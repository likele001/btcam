"""临时诊断脚本：直连 WS 观察「导入任务」到底有没有把图形推过来（用完即删）。"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core import protocol  # noqa: E402
from app.core.protocol import MsgType  # noqa: E402

DXF = ROOT / "demo_cam_part.dxf"
BASE = "http://127.0.0.1:8975"
WS = "ws://127.0.0.1:8975/ws"

NAMES = {v: k for k, v in vars(MsgType).items() if isinstance(v, int)}


def upload() -> dict:
    boundary = "----BtCAM" + uuid.uuid4().hex
    body = bytearray()
    for name, value in {"options": json.dumps({"replace": True, "splitParts": True})}.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, name, value)).encode("utf-8")
    body += ("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
             "Content-Type: application/dxf\r\n\r\n" % (boundary, DXF.name)).encode("utf-8")
    body += DXF.read_bytes() + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/import/dxf", data=bytes(body), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def drain(ws, seconds: float, tag: str) -> None:
    end = asyncio.get_running_loop().time() + seconds
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            print("   [%s] 静默 %.1fs，结束等待" % (tag, seconds))
            return
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=left)
        except asyncio.TimeoutError:
            print("   [%s] 静默 %.1fs，结束等待" % (tag, seconds))
            return
        if isinstance(data, str):
            print("   [%s] TEXT %s" % (tag, data[:120]))
            continue
        msg_type, seq, meta_len, pay_len, _total = protocol.decode_header(data)
        meta = protocol.decode_meta(data[protocol.HEADER_SIZE:
                                          protocol.HEADER_SIZE + meta_len])
        name = NAMES.get(msg_type, hex(msg_type))
        brief = {k: meta.get(k) for k in ("taskId", "kind", "status", "progress", "message",
                                          "polylineCount", "count") if k in meta}
        print("   [%s] %s payLen=%d %s" % (tag, name, pay_len,
                                           json.dumps(brief, ensure_ascii=False)))


async def main() -> None:
    import websockets

    async with websockets.connect(WS, max_size=None) as ws:
        await ws.send(protocol.encode_frame(MsgType.HELLO, {"clientVersion": "probe"}, b""))
        print("== 握手后（HELLO 直推路径） ==")
        await drain(ws, 1.5, "hello")

        print("== 触发一次 DXF 导入（工作线程广播路径） ==")
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, upload)
        await drain(ws, 12.0, "import")
        print("   上传返回:", await task)


if __name__ == "__main__":
    asyncio.run(main())
