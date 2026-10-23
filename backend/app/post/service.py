"""后处理导出编排（模块 7）。

只做三件事：选渲染器 -> 拼文件名 -> 落盘。批量多板由路由层循环调用，
每块板一个文件（NC/PLT/CUT 混排没有意义，机床一次只吃一种程序）。

文件名规则：

    <name>_<YYYYmmdd_HHMMSS>[_S<板号>].<ext>

name 会过滤 Windows 非法字符；同秒重复导出自动加 _1/_2，不覆盖旧文件。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings
from app.models.geometry import GeometryDoc
from app.post import renderer
from app.post.dxf_writer import write_dxf
from app.post.renderer import RenderContext
from app.services.toolpath_service import ToolpathPlan

logger = logging.getLogger(__name__)

ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def export_dir() -> Path:
    path = settings.data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(text: Optional[str], fallback: str = "BT_CAM") -> str:
    cleaned = ILLEGAL_CHARS.sub("_", str(text or "")).strip(" .")
    return cleaned[:80] or fallback


def _unique_path(stem: str, ext: str) -> Path:
    directory = export_dir()
    base = directory / ("%s.%s" % (stem, ext))
    if not base.exists():
        return base.resolve()
    for index in range(1, 1000):
        candidate = directory / ("%s_%d.%s" % (stem, index, ext))
        if not candidate.exists():
            return candidate.resolve()
    raise ValueError("导出目录下同名文件过多：%s" % stem)


def _stamp(stamp: Optional[str]) -> str:
    return stamp or time.strftime("%Y%m%d_%H%M%S")


def build_path(name: str, ext: str, tag: Optional[str] = None,
               stamp: Optional[str] = None) -> Path:
    stem = "%s_%s" % (safe_name(name), _stamp(stamp))
    if tag:
        stem = "%s_%s" % (stem, safe_name(tag))
    return _unique_path(stem, ext.lstrip("."))


def export_toolpath(plan: ToolpathPlan, processor: Dict[str, Any],
                    ctx: RenderContext, tag: Optional[str] = None,
                    stamp: Optional[str] = None) -> Dict[str, Any]:
    """把刀路写成加工文件，返回 {path, format, bytes, processor}。"""
    if str(processor.get("kind")) == "dxf":
        raise ValueError("DXF 后处理导出的是图形，不是刀路")
    content = renderer.render(plan, processor, ctx)
    path = build_path(ctx.name, processor["ext"], tag=tag, stamp=stamp)
    path.write_text(content, encoding="ascii", errors="replace")
    logger.info("导出 %s 成功: %s (%d 字节)",
                processor["name"], path, len(content))
    return {
        "path": str(path),
        "format": processor["ext"],
        "processor": processor["id"],
        "bytes": len(content),
    }


def export_drawing(doc: GeometryDoc, processor: Dict[str, Any],
                   name: str, tag: Optional[str] = None,
                   stamp: Optional[str] = None) -> Dict[str, Any]:
    """按 DXF 后处理写回工程图形，返回 {path, format, contours, processor}。"""
    path = build_path(name, processor["ext"], tag=tag, stamp=stamp)
    contours = write_dxf(path, doc, processor.get("dxf"))
    logger.info("导出 DXF 成功: %s (%d 条轮廓)", path, contours)
    return {
        "path": str(path),
        "format": processor["ext"],
        "processor": processor["id"],
        "contours": contours,
        "bytes": path.stat().st_size,
    }
