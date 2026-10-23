"""异形排料服务。

DeepNest 官方实现是 JS 生态，Python 侧没有等价的可 pip 安装的高质量端口，
因此这里采用"桥接"设计：

    业务层  ->  INester 接口
                   ├── DeepNestBridge ：调用外部 DeepNest 进程（Node sidecar / 自研移植版）
                   └── ShelfNester    ：内置行排算法，零依赖，保证软件永远可用

DeepNest 桥接协议（stdin/stdout 各一段 JSON）：
    请求: {"sheet": {"w":1220,"h":2440}, "gap":5, "rotation":true,
           "parts": [{"id":1,"pts":[[x,y],...]}]}
    响应: {"placements": [{"partId":1,"sheet":0,"tx":12.5,"ty":30.0,"rot":90}],
           "fitness": 0.87}

将来接入真正的 DeepNest 时，只要桥接进程满足上面协议，业务代码零改动。
"""

from __future__ import annotations

import json
import logging
import math
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.models.geometry import Polyline

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[float, str], None]]


# ---------------------------------------------------------------- 数据结构
@dataclass
class Part:
    id: int
    pts: np.ndarray            # 原点归一化后的轮廓（可直接按 (tx,ty,rot) 摆放）
    closed: bool = True
    # True = 木纹方向锁定：桥接协议里明确告知引擎该件不允许旋转
    fixed_rot: bool = False

    @property
    def size(self) -> Tuple[float, float]:
        return float(self.pts[:, 0].max()), float(self.pts[:, 1].max())

    def transformed(self, tx: float, ty: float, rot_deg: float) -> np.ndarray:
        """按摆料结果还原到板材坐标系的顶点。"""
        return transform_points(self.pts, tx, ty, rot_deg)


@dataclass
class Placement:
    part_id: int
    sheet: int
    tx: float
    ty: float
    rot: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"partId": self.part_id, "sheet": self.sheet,
                "tx": round(self.tx, 3), "ty": round(self.ty, 3), "rot": self.rot}


@dataclass
class NestResult:
    placements: List[Placement] = field(default_factory=list)
    sheet_count: int = 1
    stats: Dict[str, Any] = field(default_factory=dict)


def transform_points(pts: np.ndarray, tx: float, ty: float, rot_deg: float) -> np.ndarray:
    """先绕原点旋转，再平移（rot 以角度计，逆时针为正）。"""
    arr = np.asarray(pts, dtype=np.float64)
    if abs(rot_deg) > 1e-9:
        rad = math.radians(rot_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        rot = np.array([[cos_a, -sin_a], [sin_a, cos_a]])
        arr = arr @ rot.T
    return (arr + np.array([tx, ty], dtype=np.float64)).astype(np.float32)


def polygon_area(pts: np.ndarray) -> float:
    """鞋带公式求面积绝对值（排料利用率统计用）。"""
    arr = np.asarray(pts, dtype=np.float64)
    if len(arr) < 3:
        return 0.0
    x, y = arr[:, 0], arr[:, 1]
    return abs(0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)))


def normalize_part(pts: np.ndarray) -> np.ndarray:
    """把轮廓左下角平移到原点，排料只需要关心形状本身。"""
    arr = np.asarray(pts, dtype=np.float64)
    min_xy = arr.min(axis=0)
    return (arr - min_xy).astype(np.float32)


# ---------------------------------------------------------------- 行排算法
class ShelfNester:
    """行排（Shelf / Next-Fit）算法。

    优点：O(n log n)，结果可预测，板材利用率在板材排板场景约 70%~85%；
    缺点：不做 NFP 贴合，异形件利用率不如 DeepNest。
    """

    name = "shelf"

    def __init__(self, gap: float = 5.0, margin: float = 10.0, allow_rotation: bool = True) -> None:
        self.gap = gap
        self.margin = margin
        self.allow_rotation = allow_rotation

    def pack(self, parts: Sequence[Part], sheet_w: float, sheet_h: float,
             report: ProgressCb = None) -> NestResult:
        # 大件优先，减少碎片
        ordered = sorted(parts, key=lambda p: p.size[1], reverse=True)
        sheets: List[Dict[str, float]] = []
        placements: List[Placement] = []

        for idx, part in enumerate(ordered):
            placed = False
            for sheet_index, sheet in enumerate(sheets):
                pos = self._try_place(sheet, part, sheet_w, sheet_h)
                if pos is not None:
                    placements.append(Placement(part.id, sheet_index, pos[0], pos[1], pos[2]))
                    placed = True
                    break
            if not placed:
                sheet = {"x": self.margin, "y": self.margin, "row_h": 0.0}
                pos = self._try_place(sheet, part, sheet_w, sheet_h)
                if pos is None:
                    raise ValueError(
                        f"零件 #{part.id} 尺寸超过板材可用范围 "
                        f"({sheet_w}x{sheet_h})，请检查板材规格或零件尺寸"
                    )
                sheets.append(sheet)
                placements.append(Placement(part.id, len(sheets) - 1, pos[0], pos[1], pos[2]))

            if report and (idx % 5 == 0 or idx == len(ordered) - 1):
                report(0.1 + 0.8 * (idx + 1) / len(ordered), f"排料 {idx + 1}/{len(ordered)}")

        return NestResult(placements=placements, sheet_count=max(1, len(sheets)))

    def _try_place(self, sheet: Dict[str, float], part: Part,
                   sheet_w: float, sheet_h: float) -> Optional[Tuple[float, float, float]]:
        """在指定板材的当前游标位置尝试摆放，成功返回 (tx, ty, rot)。"""
        w, h = part.size
        candidates = [(w, h, 0.0)]
        if self.allow_rotation and abs(w - h) > 1e-6:
            candidates.append((h, w, 90.0))

        for cw, ch, rot in candidates:
            x, y, row_h = sheet["x"], sheet["y"], sheet["row_h"]
            if x + cw + self.margin > sheet_w:                 # 换行
                x = self.margin
                y = y + row_h + self.gap
                row_h = 0.0
            if y + ch + self.margin > sheet_h:                 # 本板放不下
                continue
            # 旋转后需要用原始 (w,h) 反推平移量：旋转以原点为基准，再补回尺寸差
            tx, ty = x, y
            if rot == 90.0:
                tx = x + h
                ty = y
            sheet["x"] = x + cw + self.gap
            sheet["y"] = y
            sheet["row_h"] = max(row_h, ch)
            return tx, ty, rot
        return None


# ---------------------------------------------------------------- DeepNest 桥接
class DeepNestBridge:
    """通过独立进程调用 DeepNest（Node sidecar 或移植版）。"""

    name = "deepnest"

    def __init__(self, command: Optional[str] = None, timeout: float = 180.0) -> None:
        self.command = command or os.environ.get("BTCAM_DEEPNEST_CMD", "")
        self.timeout = timeout
        # 由任务层按板材参数覆盖（与 ShelfNester 的字段同名，避免两套配置）
        self.gap = 5.0
        self.rotation = True

    def available(self) -> bool:
        return bool(self.command)

    def pack(self, parts: Sequence[Part], sheet_w: float, sheet_h: float,
             report: ProgressCb = None) -> NestResult:
        if report:
            report(0.1, "提交 DeepNest 计算")
        payload = {
            "sheet": {"w": sheet_w, "h": sheet_h},
            "gap": self.gap,
            "rotation": self.rotation,
            # rotation 为逐件角度许可：木纹锁定的零件固定为 false（协议扩展字段）
            "parts": [{"id": p.id, "pts": p.pts.round(3).tolist(),
                       "rotation": not p.fixed_rot} for p in parts],
        }
        # 用 Popen + 轮询而不是 subprocess.run：run() 在计算期间完全阻塞，既看不到
        # 取消标志，也无法在超时时主动回收子进程。
        # 读写交给后台线程（communicate），主线程每 100ms 检查取消/超时：既不阻塞，
        # 也不会因为子进程大量写 stdout（管道缓冲区满）而死锁。
        try:
            proc = subprocess.Popen(
                shlex.split(self.command, posix=False),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            raise RuntimeError("DeepNest 桥接进程启动失败：%s" % exc)

        box: Dict[str, Any] = {}

        def _communicate() -> None:
            try:
                box["out"], box["err"] = proc.communicate(json.dumps(payload))
            except Exception as exc:      # 进程被 kill 时可能抛 BrokenPipe 等
                box["error"] = exc

        reader = threading.Thread(target=_communicate, name="deepnest-io", daemon=True)
        reader.start()
        try:
            started = time.monotonic()
            while reader.is_alive():
                waited = time.monotonic() - started
                if waited > self.timeout:
                    raise RuntimeError("DeepNest 计算超时（%.0fs）" % self.timeout)
                if report:
                    check = getattr(report, "check_cancelled", None)
                    if check is not None:
                        check()          # 用户取消 -> TaskCancelled，由 finally 杀进程
                    ratio = min(1.0, waited / max(1.0, self.timeout))
                    report(0.1 + 0.79 * ratio, "DeepNest 计算中 %d%%" % int(ratio * 100))
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:            # 子进程已僵死时 wait 也可能失败，忽略
                pass
            reader.join(timeout=5)

        if box.get("error") is not None:
            raise RuntimeError("DeepNest 桥接通信失败：%s" % box["error"])
        stdout = box.get("out") or ""
        stderr = box.get("err") or ""

        if proc.returncode != 0:
            raise RuntimeError(f"DeepNest 桥接进程退出码 {proc.returncode}: {stderr[:500]}")
        try:
            data = json.loads(stdout)
        except ValueError as exc:
            raise RuntimeError("DeepNest 返回不是合法 JSON：%s" % exc)
        placements = [Placement(int(p["partId"]), int(p.get("sheet", 0)),
                                float(p["tx"]), float(p["ty"]), float(p.get("rot", 0)))
                      for p in data.get("placements", [])]
        if report:
            report(0.9, "DeepNest 计算完成")
        return NestResult(placements=placements,
                          sheet_count=int(data.get("sheetCount", 1)),
                          stats={"fitness": data.get("fitness")})


def get_nester(preferred: str = "auto"):
    """选排料引擎：优先 DeepNest 桥接，不可用则回退行排。"""
    bridge = DeepNestBridge()
    if preferred in ("auto", "deepnest") and bridge.available():
        logger.info("排料引擎: DeepNest 桥接")
        return bridge
    if preferred == "deepnest":
        logger.warning("已指定 DeepNest 但未配置 BTCAM_DEEPNEST_CMD，回退行排算法")
    logger.info("排料引擎: 内置行排(shelf)")
    return ShelfNester()


# ---------------------------------------------------------------- 任务入口
def nest_job(params: Dict[str, Any], report) -> Dict[str, Any]:
    """任务队列调用的排料任务。"""
    started = time.perf_counter()
    sheet_w = float(params["sheetWidth"])
    sheet_h = float(params["sheetHeight"])
    gap = float(params.get("gap", 5.0))

    parts: List[Part] = []
    raw_parts = params.get("parts", [])
    if not raw_parts:
        raise ValueError("没有可排料的闭合轮廓")

    for item in raw_parts:
        pts = np.asarray(item["pts"], dtype=np.float32).reshape(-1, 2)
        if len(pts) < 3:
            continue
        parts.append(Part(int(item["id"]), normalize_part(pts), True))

    report(0.05, f"读入零件 {len(parts)} 个")
    nester = get_nester(str(params.get("engine", "auto")))
    if isinstance(nester, ShelfNester):
        nester.gap = gap
        nester.allow_rotation = bool(params.get("rotation", True))
    elif isinstance(nester, DeepNestBridge):
        nester.gap = gap
        nester.rotation = bool(params.get("rotation", True))

    result = nester.pack(parts, sheet_w, sheet_h, report)
    report(0.9, "生成摆料预览")

    # 生成预览轮廓（按摆放结果变换回板材坐标）
    by_id = {p.id: p for p in parts}
    preview: List[Polyline] = []
    pid = 1
    for placement in result.placements:
        part = by_id.get(placement.part_id)
        if part is None:
            continue
        preview.append(Polyline(pid, placement.sheet + 1,
                                part.transformed(placement.tx, placement.ty, placement.rot), True))
        pid += 1

    used_area = sum(polygon_area(by_id[p.part_id].pts)
                    for p in result.placements if p.part_id in by_id)

    elapsed_ms = (time.perf_counter() - started) * 1000
    stats = {
        "partCount": len(parts),
        "sheetCount": result.sheet_count,
        "engine": nester.name,
        "utilization": round(used_area / max(1.0, sheet_w * sheet_h * result.sheet_count), 4),
        "calcMs": round(elapsed_ms, 1),
        "sheetWidth": sheet_w,
        "sheetHeight": sheet_h,
    }
    stats.update(result.stats)

    from app.services import state
    state.LAST_NEST = {"placements": [p.as_dict() for p in result.placements], "stats": stats}

    report(0.98, "排料完成")
    return {
        "polylines": preview,
        "placements": [p.as_dict() for p in result.placements],
        "stats": stats,
        "layers": [{"id": i + 1, "name": f"板材 {i + 1}", "color": 0x4FA3FF}
                   for i in range(result.sheet_count)],
    }
