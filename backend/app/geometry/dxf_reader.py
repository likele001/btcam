"""DXF 读取与图元离散化（模块 1 第一段）。

职责边界（很重要，别把修复代码塞进来）：
- 这里只做一件事：把 DXF 里的图元变成统一的 2D 折线（Polyline）；
- 不做修复、不做内外轮廓判定、不做任何几何运算 —— 那些在 repair.py；
- 输出坐标一律 **毫米**、Y 轴向上（与 DXF 自身坐标系一致，绝不翻转 Y，
  否则排料/刀路/后处理的坐标系全要跟着改）。

为什么所有图元都走 ezdxf.path 再 flattening：
- LINE / LWPOLYLINE（含 bulge 圆弧）/ POLYLINE / ARC / CIRCLE / ELLIPSE / SPLINE
  用同一套离散逻辑，圆的精度和圆弧的精度一致，不会出现"圆弧变尖角"；
- 块（INSERT）用 virtual_entities() 展开后同样走这条路，块内图元自动带上变换矩阵。

支持的图元：LINE、LWPOLYLINE、2D POLYLINE、ARC、CIRCLE、ELLIPSE、SPLINE、HATCH、INSERT。
不支持的（POINT / TEXT / MTEXT / 3D 多段线 / 面网格 等）只计数并记入 warnings，
不抛异常 —— 一张图纸里混着标注文字是常态，不能因此整份导入失败。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple, Union

import numpy as np

from app.models.geometry import Layer, Polyline

logger = logging.getLogger(__name__)


class DxfError(RuntimeError):
    """DXF 读取失败（文件损坏、版本不支持、不是 DXF 等）。"""


# ---------------------------------------------------------------- DXF 单位
# $INSUNITS 编码 -> 换算到毫米的比例。0（无单位）按 mm 处理：
# 国内板式家具图纸 99% 是 mm，且 ACAD 导出时常把 INSUNITS 留 0。
UNIT_TO_MM: Dict[int, float] = {
    0: 1.0,             # 无单位 -> 按 mm
    1: 25.4,            # 英寸
    2: 304.8,           # 英尺
    3: 1609344.0,       # 英里
    4: 1.0,             # 毫米
    5: 10.0,            # 厘米
    6: 1000.0,          # 米
    7: 1000000.0,       # 千米
    8: 0.0000254,       # 微英寸
    9: 0.0254,          # 密尔
    10: 914.4,          # 码
    11: 1e-7,           # 埃
    12: 1e-6,           # 纳米
    13: 0.001,          # 微米
    14: 100.0,          # 分米
    15: 10000.0,        # 十米
    16: 100000.0,       # 百米
    17: 1e12,           # 吉米（无实际意义，占位保证键完整）
}

# 单条图元离散后的点数上限：超过就加大离散间距重来，避免病态样条吃光内存
MAX_POINTS_PER_ENTITY = 20000
# 块递归展开深度上限：防止自引用块导致无限递归
MAX_BLOCK_DEPTH = 8

# 可直接 make_path 的图元类型
PATH_ENTITIES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}

# 图层名 -> 工艺类型 的启发式映射（导入后可人工在 UI 里改）
_DRILL_HINTS = ("孔", "hole", "drill", "bore")
_ENGRAVE_HINTS = ("划槽", "槽", "engrave", "groove", "mark", "scrib")


# ---------------------------------------------------------------- 参数 / 结果
@dataclass
class DxfReadOptions:
    """读取参数。

    flattenDistance: 曲线离散间距（mm）。0.05mm 对板式家具足够，点到点误差
                     小于刀径的 1%，又不会让点数爆炸。
    unitScale:       强制覆盖单位换算（None = 按 $INSUNITS 自动判断）。
    skipOffLayers:   跳过关闭/冻结图层（这些通常是标注、图框，不是加工轮廓）。
    explodeBlocks:   是否展开 INSERT 块。
    includeHatch:    是否把 HATCH 边界也算作轮廓（有些图纸用填充表达板件形状）。
    """

    flattenDistance: float = 0.05
    unitScale: Optional[float] = None
    skipOffLayers: bool = True
    explodeBlocks: bool = True
    includeHatch: bool = True
    maxBlockDepth: int = MAX_BLOCK_DEPTH

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "DxfReadOptions":
        """从 REST 参数构造；非法值回落默认，避免一次误传毁掉整份图纸。"""
        raw = raw or {}

        def num(key: str, default: float) -> float:
            try:
                return float(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        scale_raw = raw.get("unitScale")
        scale: Optional[float] = None
        if scale_raw not in (None, "", 0, "auto"):
            try:
                value = float(scale_raw)  # type: ignore[arg-type]
                if value > 0:
                    scale = value
            except (TypeError, ValueError):
                scale = None

        return cls(
            flattenDistance=max(0.001, min(10.0, num("flattenDistance", 0.05))),
            unitScale=scale,
            skipOffLayers=bool(raw.get("skipOffLayers", True)),
            explodeBlocks=bool(raw.get("explodeBlocks", True)),
            includeHatch=bool(raw.get("includeHatch", True)),
            maxBlockDepth=int(max(1, min(32, num("maxBlockDepth", MAX_BLOCK_DEPTH)))),
        )


@dataclass
class DxfReadStats:
    """读取统计，直接进导入结果的 meta，UI 和验收都用它。"""

    entities: int = 0            # 实际遍历到的图元数（展开块之后的）
    used: int = 0                # 成功转成折线的图元数
    skipped: int = 0             # 跳过的图元数（关闭图层/退化/不支持）
    closed: int = 0              # 闭合折线
    open: int = 0                # 开放折线
    points: int = 0              # 离散后总点数
    blocks: int = 0              # 展开的块实例数
    unsupported: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entities": self.entities,
            "used": self.used,
            "skipped": self.skipped,
            "closed": self.closed,
            "open": self.open,
            "points": self.points,
            "blocks": self.blocks,
            "unsupported": dict(self.unsupported),
        }


@dataclass
class DxfReadResult:
    """读取结果。

    polylines: 统一折线（坐标已换算成 mm），id 从 1 连续编号；
    layers:    图纸实际使用到的图层（已按 DXF 图层表顺序）；
    units:     原始 $INSUNITS 编码；
    unitScale: 实际使用的换算比例（1.0 = 本来就是 mm）。
    """

    polylines: List[Polyline] = field(default_factory=list)
    layers: List[Layer] = field(default_factory=list)
    units: int = 0
    unitScale: float = 1.0
    stats: DxfReadStats = field(default_factory=DxfReadStats)
    warnings: List[str] = field(default_factory=list)
    dxfVersion: str = ""


# ---------------------------------------------------------------- 内部工具
def _import_ezdxf():
    """延迟导入：ezdxf 只在导入 DXF 时用，未安装也不该影响后端启动。"""
    try:
        import ezdxf  # noqa: F401
        from ezdxf import path as ezpath
        from ezdxf import recover
        from ezdxf.colors import aci2rgb
    except Exception as exc:  # pragma: no cover - 仅在缺依赖时触发
        raise DxfError(
            "未安装 ezdxf，无法解析 DXF。请执行 pip install -r requirements.txt"
        ) from exc
    return ezdxf, ezpath, recover, aci2rgb


def _open_document(source: Union[str, bytes, bytearray, BinaryIO]):
    """打开 DXF。

    优先用 recover：现场图纸经常被别的软件写坏几个组码，
    普通 readfile 会直接抛异常，recover 会尽力修好并返回 auditor 报告。
    """
    ezdxf, _ezpath, recover, _aci2rgb = _import_ezdxf()

    if isinstance(source, (bytes, bytearray)):
        stream: Any = io.BytesIO(bytes(source))
    else:
        stream = source

    try:
        doc, auditor = recover.readfile(stream)
        if auditor.has_errors:
            logger.warning("DXF 存在 %d 个结构错误，已由 recover 尽量修复", len(auditor.errors))
        return doc
    except Exception as recover_exc:
        logger.warning("recover 读取失败（%s），回退普通读取", recover_exc)
        try:
            if hasattr(stream, "seek"):
                stream.seek(0)
            return ezdxf.readfile(stream)
        except Exception as exc:
            raise DxfError(f"无法解析 DXF 文件：{exc}") from exc


def _layer_operation(name: str) -> str:
    """按图层名猜工艺类型；猜错也只是默认值不准，不影响几何。"""
    lowered = name.lower()
    if any(hint in name or hint in lowered for hint in _DRILL_HINTS):
        return "drill"
    if any(hint in name or hint in lowered for hint in _ENGRAVE_HINTS):
        return "engrave"
    return "cut"


def _layer_color(layer_obj: Any, aci2rgb) -> int:
    """取图层颜色（0xRRGGBB）。

    优先 TrueColor（layer.rgb），没有再退化到 ACI 索引色，
    最后兜底项目默认色，保证 UI 永远拿到一个能画的颜色。
    """
    try:
        rgb = layer_obj.rgb
        if rgb is not None:
            r, g, b = rgb
            return (int(r) << 16) | (int(g) << 8) | int(b)
    except Exception:  # 某些损坏文件里 TrueColor 是脏数据
        pass
    try:
        aci = int(getattr(layer_obj.dxf, "color", 7))
        r, g, b = aci2rgb(aci)
        return (int(r) << 16) | (int(g) << 8) | int(b)
    except Exception:
        return 0x4FA3FF


def _flatten_path(path: Any, distance: float) -> List[Any]:
    """把 ezdxf Path 离散成点列；点数超限就放大间距重算。

    注意：ezdxf 1.0 的 Path.flattening() 返回的是生成器，必须先 list 化，
    否则拿不到 len() 也没法二次遍历。
    """
    step = max(0.001, float(distance))
    verts: List[Any] = []
    for _ in range(5):
        verts = list(path.flattening(step))
        if len(verts) <= MAX_POINTS_PER_ENTITY:
            break
        step *= 2.0
    return verts


def _verts_to_array(verts: List[Any], scale: float) -> np.ndarray:
    """Vec3 列表 -> (N,2) float32 点阵，同时做单位换算。"""
    if not verts:
        return np.zeros((0, 2), dtype=np.float32)
    arr = np.array([[v.x, v.y] for v in verts], dtype=np.float64)
    if scale != 1.0:
        arr *= scale
    return arr.astype(np.float32)


def _clean_points(arr: np.ndarray, tol: float = 1e-6) -> np.ndarray:
    """去掉相邻重复点（自己画的重复点会让后续串接算法产生零长边）。"""
    if len(arr) < 2:
        return arr
    keep = [0]
    for i in range(1, len(arr)):
        if float(np.linalg.norm(arr[i] - arr[keep[-1]])) > tol:
            keep.append(i)
    return arr[keep]


def _path_is_closed(path: Any) -> bool:
    """判断 Path 是否闭合；不同 ezdxf 版本属性名可能有差异，统一做好兜底。"""
    value = getattr(path, "is_closed", None)
    if value is None:
        try:
            return bool(path.is_closed)
        except Exception:
            return False
    return bool(value)


# ---------------------------------------------------------------- 遍历图元
def _iter_paths(entity: Any, ezpath: Any, options: DxfReadOptions,
                stats: DxfReadStats, warnings: List[str],
                depth: int = 0) -> List[Any]:
    """把一个图元（可能是块）展开成若干 ezdxf Path。"""
    kind = entity.dxftype()

    if kind == "INSERT":
        if not options.explodeBlocks:
            stats.unsupported["INSERT(未展开)"] = stats.unsupported.get("INSERT(未展开)", 0) + 1
            return []
        if depth >= options.maxBlockDepth:
            warnings.append("块嵌套超过 %d 层，已停止展开（可能存在自引用块）" % options.maxBlockDepth)
            return []
        stats.blocks += 1
        paths: List[Any] = []
        try:
            children = list(entity.virtual_entities())   # 已带块引用的变换矩阵
        except Exception as exc:
            warnings.append("块 %s 展开失败：%s" % (entity.dxf.get("name", "?"), exc))
            return []
        for child in children:
            paths.extend(_iter_paths(child, ezpath, options, stats, warnings, depth + 1))
        return paths

    if kind == "HATCH":
        if not options.includeHatch:
            return []
        try:
            from ezdxf.path import from_hatch  # 局部导入，避免顶层依赖
            return list(from_hatch(entity))
        except Exception as exc:
            warnings.append("HATCH 边界提取失败：%s" % exc)
            return []

    if kind not in PATH_ENTITIES:
        key = kind
        stats.unsupported[key] = stats.unsupported.get(key, 0) + 1
        return []

    # 3D 多段线 / 面网格不做：本项目只处理 2D 轮廓
    if kind == "POLYLINE":
        try:
            mode = entity.get_mode()
        except Exception:
            mode = "AcDb2dPolyline"
        if mode != "AcDb2dPolyline":
            stats.unsupported[mode] = stats.unsupported.get(mode, 0) + 1
            return []

    try:
        return [ezpath.make_path(entity)]
    except Exception as exc:
        warnings.append("%s 离散失败：%s" % (kind, exc))
        return []


def _collect_layers(doc: Any, aci2rgb) -> Tuple[List[Layer], Dict[str, int]]:
    """按 DXF 图层表顺序建立 图层名 -> layer_id 映射（只保留实际有图元的图层）。

    先建立全表映射，遇到图元时按需分配 id，保证同一个 DXF 多次导入
    得到的 layer_id 稳定（按图层表顺序），便于历史工单还原。
    """
    layers: List[Layer] = []
    index: Dict[str, int] = {}
    next_id = 1
    for layer_obj in doc.layers:
        name = str(layer_obj.dxf.name)
        index[name] = next_id
        layers.append(Layer(
            id=next_id,
            name=name,
            color=_layer_color(layer_obj, aci2rgb),
            operation=_layer_operation(name),
            visible=not (layer_obj.is_off() or layer_obj.is_frozen()),
        ))
        next_id += 1
    return layers, index


def _layer_state(doc: Any, name: str) -> Tuple[bool, bool]:
    """返回 (是否关闭, 是否冻结)；图层不存在时按可见处理。"""
    try:
        layer_obj = doc.layers.get(name)
    except Exception:
        return (False, False)
    return (bool(layer_obj.is_off()), bool(layer_obj.is_frozen()))


# ---------------------------------------------------------------- 主入口
def read_dxf(
    source: Union[str, bytes, bytearray, BinaryIO],
    options: Optional[DxfReadOptions] = None,
    report: Optional[Callable[[float, str], None]] = None,
) -> DxfReadResult:
    """读取 DXF 并离散成统一 2D 折线。

    入参：
        source  -- 文件路径 / 文件二进制（bytes）/ 已打开的二进制流
        options -- 见 DxfReadOptions
        report  -- 进度回调 report(ratio 0~1, message)，可为 None

    出参：
        DxfReadResult（polylines / layers / units / unitScale / stats / warnings）

    异常：
        DxfError -- 缺 ezdxf、文件损坏到无法解析、内容不是 DXF
    """
    options = options or DxfReadOptions()
    progress = report or (lambda _r, _m: None)

    progress(0.02, "打开 DXF 文件")
    doc = _open_document(source)

    ezdxf, ezpath, _recover, aci2rgb = _import_ezdxf()
    result = DxfReadResult(dxfVersion=str(getattr(doc, "dxfversion", "")))

    # ---- 单位换算
    try:
        units = int(doc.units)
    except Exception:
        units = 0
    if options.unitScale is not None:
        scale = float(options.unitScale)
    else:
        scale = UNIT_TO_MM.get(units, 1.0)
        if units == 0:
            measurement = 1
            try:
                measurement = int(doc.header.get("$MEASUREMENT", 1))
            except Exception:
                pass
            if measurement == 0:
                result.warnings.append(
                    "图纸未声明单位且测量制为英制，当前按毫米处理；"
                    "如尺寸明显偏小请用 unitScale=25.4 重新导入"
                )
    result.units = units
    result.unitScale = scale
    progress(0.08, "读取图元（单位比例 %.4g）" % scale)

    # ---- 图层
    all_layers, layer_index = _collect_layers(doc, aci2rgb)
    used_layer_names: List[str] = []

    def ensure_layer(name: str) -> int:
        """图层映射：DXF 里没登记过的图层名（少见但在坏文件里会出现）动态补一个。"""
        if name in layer_index:
            layer_id = layer_index[name]
        else:
            layer_id = len(all_layers) + 1
            layer_index[name] = layer_id
            all_layers.append(Layer(layer_id, name, 0x4FA3FF, _layer_operation(name)))
        if name not in used_layer_names:
            used_layer_names.append(name)
        return layer_id

    msp = doc.modelspace()
    try:
        total = len(msp)
    except Exception:
        total = 0

    stats = result.stats
    polylines: List[Polyline] = []
    next_pid = 1

    for position, entity in enumerate(msp, start=1):
        stats.entities += 1

        # 关闭/冻结图层：通常是图框和标注，直接跳过（可在参数里关掉这个行为）
        layer_name = str(getattr(entity.dxf, "layer", "0"))
        if options.skipOffLayers:
            off, frozen = _layer_state(doc, layer_name)
            if off or frozen:
                stats.skipped += 1
                continue

        for path in _iter_paths(entity, ezpath, options, stats, result.warnings):
            try:
                verts = _flatten_path(path, options.flattenDistance)
                pts = _clean_points(_verts_to_array(verts, scale))
                if len(pts) < 2:
                    stats.skipped += 1
                    continue
                closed = _path_is_closed(path) and len(pts) >= 3
                if closed:
                    # 闭合轮廓去掉与首点重合的尾点：Polyline.closed 已表达闭合语义，
                    # 留着尾点会让"去重/串接"把它当两条边处理
                    if float(np.linalg.norm(pts[0] - pts[-1])) <= 1e-4:
                        pts = pts[:-1]
                    if len(pts) < 3:
                        stats.skipped += 1
                        continue
                layer_id = ensure_layer(layer_name)
                polylines.append(Polyline(next_pid, layer_id, pts, closed))
                next_pid += 1
                stats.used += 1
                stats.points += len(pts)
                if closed:
                    stats.closed += 1
                else:
                    stats.open += 1
            except Exception as exc:
                # 单个图元坏掉不能影响整份导入
                stats.skipped += 1
                result.warnings.append("图元 %s 处理失败：%s" % (entity.dxftype(), exc))

        if total and position % 200 == 0:
            progress(0.1 + 0.8 * position / total, "解析图元 %d/%d" % (position, total))

    # 只把实际用到的图层返回给前端，避免图层下拉里塞满空图层；
    # 同时把 layer_id 重映射成 1..N 连续编号：DXF 图层表里总有无用的
    # "0" / "Defpoints"，直接用原 id 会让前端拿到一堆空洞编号。
    used_layers = [l for l in all_layers if l.name in used_layer_names]
    id_map = {l.id: index for index, l in enumerate(used_layers, start=1)}
    for layer in used_layers:
        layer.id = id_map[layer.id]
    for poly in polylines:
        poly.layer_id = id_map.get(poly.layer_id, 1)
    result.layers = used_layers
    result.polylines = polylines

    progress(0.95, "读取完成：%d 条轮廓 / %d 点" % (len(polylines), stats.points))
    if not polylines:
        result.warnings.append("文件中没有可用的 2D 轮廓（可能全是文字/标注，或图层被关闭）")

    logger.info(
        "DXF 读取完成：图元 %d，采用 %d，跳过 %d，轮廓 %d（闭合 %d / 开放 %d），单位比例 %.4g",
        stats.entities, stats.used, stats.skipped, len(polylines),
        stats.closed, stats.open, scale,
    )
    return result


def guess_units_name(units: int) -> str:
    """把 $INSUNITS 编码翻成人读的名字（日志/UI 提示用）。"""
    return {
        0: "无单位(按mm)", 1: "英寸", 2: "英尺", 3: "英里", 4: "毫米",
        5: "厘米", 6: "米", 7: "千米", 9: "密尔", 10: "码",
        13: "微米", 14: "分米",
    }.get(int(units), "未知(%d)" % int(units))


def bbox_of(polylines: List[Polyline]) -> Tuple[float, float, float, float]:
    """整组轮廓的包围盒；空集返回全 0（调用方自己决定兜底）。"""
    if not polylines:
        return (0.0, 0.0, 0.0, 0.0)
    boxes = np.array([p.bbox() for p in polylines], dtype=np.float64)
    return (float(boxes[:, 0].min()), float(boxes[:, 1].min()),
            float(boxes[:, 2].max()), float(boxes[:, 3].max()))


