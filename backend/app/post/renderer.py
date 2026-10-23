"""后处理渲染器（模块 7）。

输入是模块 5 产出的 `ToolpathPlan`（已含补偿、方向、微连接、Z 分层），
输出是机床能直接读的文本。三种渲染器都不碰几何，只做“运动 -> 指令”的翻译：

    gcode  程序头/尾 + G0/G1 指令模板，支持行号与小数精度
    hpgl   PU/PD 笔式指令，坐标乘 unitsPerMm 取整
    dxf    DxfWriter 用 ezdxf 写回图形（见 dxf_writer.py）

所以新增机床 = 新增一份 JSON 配置，不改本文件；只有出现全新指令体系
（比如某些专机用 M 代码下刀）才需要加渲染器。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services.toolpath_service import ToolpathPlan


@dataclass
class RenderContext:
    """一次导出用到的“机器事实”，来自刀具/请求，不是刀路本身。"""

    name: str = "BT_CAM"
    safe_z: float = 5.0
    spindle: float = 18000.0
    feed: float = 3000.0
    tool: Optional[int] = None
    flip_y: bool = False
    speed: int = 30
    force: int = 20
    program_no: str = "0001"


def format_number(value: float, precision: int) -> str:
    """固定精度后去掉多余的 0，减小文件体积；-0 统一成 0。"""
    text = f"{float(value):.{int(precision)}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0", "-") else text


def _mapping(ctx: RenderContext, precision: int,
             x: Optional[float] = None, y: Optional[float] = None,
             z: Optional[float] = None, feed: Optional[float] = None) -> Dict[str, Any]:
    return {
        "name": ctx.name,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "programNo": ctx.program_no,
        "tool": int(ctx.tool or 1),
        "spindle": int(round(ctx.spindle)),
        "safeZ": format_number(ctx.safe_z, precision),
        "x": format_number(x if x is not None else 0.0, precision),
        "y": format_number(y if y is not None else 0.0, precision),
        "z": format_number(z if z is not None else 0.0, precision),
        "feed": format_number(feed if feed is not None else ctx.feed, precision),
        "speed": int(ctx.speed),
        "force": int(ctx.force),
    }


def _fill(template: str, ctx: RenderContext, precision: int,
          x: Optional[float] = None, y: Optional[float] = None,
          z: Optional[float] = None, feed: Optional[float] = None) -> str:
    mapping = _mapping(ctx, precision, x=x, y=y, z=z, feed=feed)
    try:
        return template.format_map(mapping)
    except (KeyError, ValueError) as exc:
        raise ValueError("后处理模板占位符错误: %s（%s）" % (template, exc))


# ---------------------------------------------------------------- G 代码
def _apply_line_numbers(lines: List[str], config: Dict[str, Any]) -> List[str]:
    if not config.get("lineNumbers"):
        return lines
    seq = int(config.get("lineNumberStart", 10))
    step = int(config.get("lineNumberStep", 10))
    out: List[str] = []
    for line in lines:
        # 程序分隔符与空行不加行号，其余（含注释）统一编号
        if not line or line.startswith("%"):
            out.append(line)
            continue
        out.append("N%d %s" % (seq, line))
        seq += step
    return out


def render_gcode(plan: ToolpathPlan, config: Dict[str, Any],
                 ctx: RenderContext) -> str:
    precision = int(config.get("precision", 3))
    lines: List[str] = []

    for template in config.get("header", []):
        lines.append(_fill(template, ctx, precision))
    if ctx.tool and config.get("toolChange"):
        for template in config["toolChange"]:
            lines.append(_fill(template, ctx, precision))

    last_feed: Optional[float] = None
    last_z: Optional[float] = None
    for mv in plan.moves:
        y = -mv.y if ctx.flip_y else mv.y
        if mv.kind == "rapid":
            if last_z is None or abs(last_z - mv.z) > 1e-6:
                lines.append(_fill(config["rapidZ"], ctx, precision, z=mv.z))
                last_z = mv.z
            lines.append(_fill(config["rapidXY"], ctx, precision, x=mv.x, y=y))
        elif mv.kind == "plunge":
            feed = mv.feed or ctx.feed
            if last_z is None or abs(last_z - mv.z) > 1e-6:
                # 下刀沿用 G1，进给写在同一行，避免多一行 F
                lines.append(_fill(config["plungeZ"], ctx, precision,
                                   z=mv.z, feed=feed))
                last_z = mv.z
                last_feed = feed
        else:  # cut
            feed = mv.feed or ctx.feed
            if last_z is None or abs(last_z - mv.z) > 1e-6:
                # 跨微连接抬到抬刀高度，属于快移
                lines.append(_fill(config["rapidZ"], ctx, precision, z=mv.z))
                last_z = mv.z
            if last_feed is None or abs(last_feed - feed) > 1e-6:
                lines.append(_fill(config["feed"], ctx, precision, feed=feed))
                last_feed = feed
            lines.append(_fill(config["cutXY"], ctx, precision, x=mv.x, y=y))

    for template in config.get("footer", []):
        lines.append(_fill(template, ctx, precision))
    return "\n".join(_apply_line_numbers(lines, config)) + "\n"


# ---------------------------------------------------------------- HPGL
def render_hpgl(plan: ToolpathPlan, config: Dict[str, Any],
                ctx: RenderContext) -> str:
    units = float(config.get("unitsPerMm", 40.0))
    lines: List[str] = [_fill(t, ctx, 0) for t in config.get("header", [])]

    pen_down = False
    for mv in plan.moves:
        x = int(round(mv.x * units))
        y = int(round((-mv.y if ctx.flip_y else mv.y) * units))
        if mv.kind in ("cut", "plunge"):
            if pen_down:
                lines.append(_fill(config["penDown"], ctx, 0, x=float(x), y=float(y)))
            else:
                lines.append(_fill(config["penUp"], ctx, 0, x=float(x), y=float(y)))
                lines.append(_fill(config["penDown"], ctx, 0, x=float(x), y=float(y)))
                pen_down = True
        else:
            if pen_down:
                lines.append(_fill(config["penLift"], ctx, 0))
                pen_down = False
            lines.append(_fill(config["penUp"], ctx, 0, x=float(x), y=float(y)))
    if pen_down:
        lines.append(_fill(config["penLift"], ctx, 0))

    lines.extend(_fill(t, ctx, 0) for t in config.get("footer", []))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 分派
def render(plan: ToolpathPlan, processor: Dict[str, Any],
           ctx: RenderContext) -> str:
    """按配置里的 kind 渲染文本；dxf 不走这里（由 DxfWriter 直接写文件）。"""
    kind = str(processor.get("kind") or "").lower()
    if kind == "gcode":
        return render_gcode(plan, processor["gcode"], ctx)
    if kind == "hpgl":
        return render_hpgl(plan, processor["hpgl"], ctx)
    raise ValueError("后处理 %s 不支持文本渲染（kind=%s）" % (
        processor.get("id"), kind))
