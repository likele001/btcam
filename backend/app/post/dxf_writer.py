"""DXF 后处理（模块 7）：把工程图形按图层原样写回 DXF。

为什么单独一个 writer：
DXF 不是“逐条指令”的文本流，而是结构化图纸，用 ezdxf 直接建实体比拼模板可靠。
它对应需求里的“通用导出：导出原始 DXF 图形文件”，常用于：
  - 给第三方软件复核排料/刀路；
  - 把修复后的轮廓另存，替代已损坏的原始图纸。

不导出刀路折线：刀路是加工数据，回写图纸类文件容易让下游误当零件图。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.models.geometry import GeometryDoc, Polyline

LAYER_FALLBACK = 7  # ezdxf 默认白色（ACI 7）


def _rgb_to_int(color: Any) -> int:
    try:
        return int(color) & 0xFFFFFF
    except (TypeError, ValueError):
        return 0x4FA3FF


def write_dxf(path: Path, doc: GeometryDoc,
              options: Optional[Dict[str, Any]] = None) -> int:
    """写出 DXF，返回实际写入的轮廓数；缺 ezdxf 时给出明确错误。"""
    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("未安装 ezdxf，无法导出 DXF：pip install ezdxf") from exc

    opts = options or {}
    version = str(opts.get("version") or "R2010")

    drawing = ezdxf.new(version)
    drawing.header["$INSUNITS"] = 4        # 4 = 毫米
    layer_names: Dict[int, str] = {}
    for layer in doc.layers:
        name = (layer.name or "LAYER_%d" % layer.id).strip() or "LAYER_%d" % layer.id
        if name not in drawing.layers:
            drawing.layers.add(name=name, color=LAYER_FALLBACK)
        layer_names[layer.id] = name

    modelspace = drawing.modelspace()
    written = 0
    used_names: Set[str] = set(layer_names.values())
    for poly in doc.polylines:
        pts: List[Any] = [(float(x), float(y)) for x, y in poly.pts]
        if len(pts) < 2:
            continue
        layer_id = int(poly.layer_id)
        if layer_id not in layer_names:
            # 图纸里存在孤儿图层：补一个同 id 的图层，避免实体丢到 0 层
            name = "LAYER_%d" % layer_id
            if name not in used_names:
                drawing.layers.add(name=name, color=LAYER_FALLBACK)
                used_names.add(name)
            layer_names[layer_id] = name
        attribs: Dict[str, Any] = {"layer": layer_names[layer_id]}
        color = _rgb_to_int(getattr(doc.find_layer(layer_id), "color", 0))
        if color:
            attribs["true_color"] = color
        modelspace.add_lwpolyline(pts, close=bool(poly.closed), dxfattribs=attribs)
        written += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    drawing.saveas(str(path))
    return written


def count_dxf_polylines(path: Path) -> int:
    """验收辅助：读回 DXF 统计 LWPOLYLINE/POLYLINE 数量。"""
    import ezdxf

    drawing = ezdxf.readfile(str(path))
    total = 0
    for entity in drawing.modelspace():
        if entity.dxftype() in ("LWPOLYLINE", "POLYLINE"):
            total += 1
    return total
