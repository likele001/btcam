"""加工文件导出的兼容入口（模块 7）。

模块 5 的 `/api/export` 最初只认 nc/plt/cut 三种格式，本文件保留旧函数签名，
真正实现已迁到 `app/post/`：

    app/post/registry.py   后处理配置（JSON 文件，新增机床只加配置）
    app/post/renderer.py   gcode / hpgl 渲染器
    app/post/dxf_writer.py DXF 图形导出
    app/post/service.py    文件名 + 落盘

format -> 内置后处理：

    nc  -> weihong_nc   维宏 G 代码
    plt -> plotter_plt  绘图仪 HPGL
    cut -> cutting_cut  裁割设备

新代码请直接使用 `post.service.export_toolpath` 并传后处理配置，
本入口只用于兼容模块 5 的调用方与旧验收脚本。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from app.post import registry, service as post_service
from app.post.renderer import RenderContext
from app.services.toolpath_service import ToolpathPlan


@dataclass
class ExportOptions:
    feed: float = 3000.0            # mm/min
    spindle: float = 18000.0
    safe_z: float = 5.0
    name: str = "BT_CAM"
    flip_y: bool = False
    speed: int = 30                 # 刻绘机速度档位（VS）
    force: int = 20                 # 刻绘机压力档位（FS）
    tool: Optional[int] = None      # 刀具号（有刀具库时写入 T 指令）


FMT_TO_PROCESSOR: Dict[str, str] = {
    "nc": "weihong_nc",
    "plt": "plotter_plt",
    "cut": "cutting_cut",
}


def export_plan(plan: ToolpathPlan, fmt: str,
                options: Optional[ExportOptions] = None) -> Path:
    """把刀路写成文件，返回绝对路径（兼容模块 5 的旧入口）。"""
    key = fmt.lower().lstrip(".")
    processor_id = FMT_TO_PROCESSOR.get(key)
    if processor_id is None:
        raise ValueError("不支持的导出格式: %s（可选 %s）" % (
            fmt, ", ".join(FMT_TO_PROCESSOR)))
    processor = registry.find_processor(processor_id)
    if processor is None:
        raise ValueError("内置后处理 %s 缺失，请检查数据目录 post/" % processor_id)

    opts = options or ExportOptions()
    ctx = RenderContext(
        name=opts.name,
        safe_z=opts.safe_z,
        spindle=opts.spindle,
        feed=opts.feed,
        tool=opts.tool,
        flip_y=opts.flip_y,
        speed=opts.speed,
        force=opts.force,
    )
    result = post_service.export_toolpath(plan, processor, ctx)
    return Path(result["path"])
