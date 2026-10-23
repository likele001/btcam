"""后处理配置注册表（模块 7）。

存储约定：

    %LOCALAPPDATA%\\BtCAM\\post\\<id>.json    一份配置一台机床

内置模板来自 `builtin.py`，首次启动落盘；之后每次 list/get 都重新扫盘，
手工改 JSON 不需要重启。内置配置带 version，代码升级时会覆盖旧的同名文件，
用户自定义配置（builtin=False）永远不动。

校验只做“能不能安全渲染”这一层：kind/ext/模板占位符/数值范围；
机床语义（哪些 M 代码有效）属于工艺知识，交给配置作者。
"""

from __future__ import annotations

import json
import logging
import re
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.post.builtin import BUILTIN, BUILTIN_VERSION

logger = logging.getLogger(__name__)

KINDS = ("gcode", "hpgl", "dxf")
ID_RE = re.compile(r"[a-z0-9][a-z0-9_]{1,39}")
EXT_RE = re.compile(r"[a-z0-9]{1,8}")

# 模板里允许出现的占位符（渲染器保证这些键一定存在）
PLACEHOLDERS = {
    "name", "date", "programNo", "tool", "spindle", "safeZ",
    "x", "y", "z", "feed", "speed", "force",
}

# gcode 指令模板必须提供的键
GCODE_INSTRUCTIONS = (
    "rapidZ", "rapidXY", "plungeZ", "feed", "cutXY",
)


class PostError(ValueError):
    """后处理配置校验失败：路由层统一翻译成 HTTP 400。"""


# ---------------------------------------------------------------- 文件读写
def post_dir() -> Path:
    return settings.data_dir / "post"


def _path(processor_id: str) -> Path:
    return post_dir() / ("%s.json" % processor_id)


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("后处理配置读取失败 %s: %s", path.name, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _write(payload: Dict[str, Any]) -> Path:
    post_dir().mkdir(parents=True, exist_ok=True)
    path = _path(str(payload["id"]))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def ensure_builtin() -> int:
    """把内置模板落盘/升级；返回写入的份数（启动时调一次）。"""
    written = 0
    for item in BUILTIN:
        path = _path(item["id"])
        existing = _read(path)
        if existing is not None:
            try:
                if int(existing.get("version") or 0) >= int(item.get("version") or 0):
                    continue
            except (TypeError, ValueError):
                pass
        _write(item)
        written += 1
    if written:
        logger.info("后处理模板已同步: %d 份", written)
    return written


# ---------------------------------------------------------------- 校验
def _as_text(value: Any, field: str, max_length: int = 2000) -> str:
    if not isinstance(value, str):
        raise PostError("%s 必须是字符串" % field)
    text = value.strip()
    if len(text) > max_length:
        raise PostError("%s 过长" % field)
    return text


def _as_template_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PostError("%s 必须是字符串数组" % field)
    rows: List[str] = []
    for index, item in enumerate(value):
        text = _as_text(item, "%s[%d]" % (field, index))
        _check_placeholders(text, "%s[%d]" % (field, index))
        rows.append(text)
    return rows


def _check_placeholders(text: str, field: str) -> None:
    """拒绝未知占位符，避免导出时才 500。"""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise PostError("%s 模板括号不配对: %s" % (field, exc))
    for _literal, name, _spec, _conv in parsed:
        if name is None:
            continue
        root = name.split(".", 1)[0].split("[", 1)[0]
        if root not in PLACEHOLDERS:
            raise PostError("%s 使用了未知占位符 {%s}（可用：%s）" % (
                field, name, ", ".join(sorted(PLACEHOLDERS))))


def _normalize_gcode(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PostError("gcode 配置缺失")
    precision = int(raw.get("precision", 3))
    if precision < 0 or precision > 6:
        raise PostError("precision 必须在 0~6 之间")
    start = int(raw.get("lineNumberStart", 10))
    step = int(raw.get("lineNumberStep", 10))
    if start < 0 or step <= 0:
        raise PostError("行号起始/步长不合法")
    config: Dict[str, Any] = {
        "precision": precision,
        "lineNumbers": bool(raw.get("lineNumbers", False)),
        "lineNumberStart": start,
        "lineNumberStep": step,
        "programNo": _as_text(raw.get("programNo", "0001"), "programNo", 16),
        "header": _as_template_list(raw.get("header"), "header"),
        "footer": _as_template_list(raw.get("footer"), "footer"),
        "toolChange": _as_template_list(raw.get("toolChange"), "toolChange"),
    }
    for key in GCODE_INSTRUCTIONS:
        text = _as_text(raw.get(key, ""), key)
        if not text:
            raise PostError("gcode.%s 不能为空" % key)
        _check_placeholders(text, "gcode.%s" % key)
        config[key] = text
    return config


def _normalize_hpgl(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PostError("hpgl 配置缺失")
    units = float(raw.get("unitsPerMm", 40.0))
    if units <= 0:
        raise PostError("unitsPerMm 必须大于 0")
    config: Dict[str, Any] = {
        "unitsPerMm": round(units, 6),
        "header": _as_template_list(raw.get("header"), "header"),
        "footer": _as_template_list(raw.get("footer"), "footer"),
    }
    for key in ("penUp", "penDown", "penLift"):
        text = _as_text(raw.get(key, ""), key)
        if not text:
            raise PostError("hpgl.%s 不能为空" % key)
        _check_placeholders(text, "hpgl.%s" % key)
        config[key] = text
    return config


def normalize(payload: Dict[str, Any], processor_id: Optional[str] = None,
              builtin: Optional[bool] = None) -> Dict[str, Any]:
    """校验并补齐一份配置；processor_id 给定表示覆盖已有文件。"""
    if not isinstance(payload, dict):
        raise PostError("配置必须是 JSON 对象")
    pid = _as_text(processor_id or payload.get("id", ""), "id", 40).lower()
    if not ID_RE.fullmatch(pid):
        raise PostError("id 只能用 2~40 位小写字母/数字/下划线")

    kind = _as_text(payload.get("kind", ""), "kind", 16).lower()
    if kind not in KINDS:
        raise PostError("kind 必须是 %s 之一" % "/".join(KINDS))

    ext = _as_text(payload.get("ext", ""), "ext", 8).lower().lstrip(".")
    if not EXT_RE.fullmatch(ext):
        raise PostError("ext 只能是 1~8 位字母/数字（不带点）")

    name = _as_text(payload.get("name", ""), "name", 64)
    if not name:
        raise PostError("name 不能为空")

    result: Dict[str, Any] = {
        "id": pid,
        "name": name,
        "kind": kind,
        "ext": ext,
        "description": _as_text(payload.get("description", ""), "description", 500),
        "builtin": bool(builtin if builtin is not None else payload.get("builtin", False)),
        "version": int(payload.get("version", 1) or 1),
    }
    if kind == "gcode":
        result["gcode"] = _normalize_gcode(payload.get("gcode"))
    elif kind == "hpgl":
        result["hpgl"] = _normalize_hpgl(payload.get("hpgl"))
    else:
        dxf = payload.get("dxf") if isinstance(payload.get("dxf"), dict) else {}
        result["dxf"] = {"version": _as_text(dxf.get("version", "R2010"), "dxf.version", 16)}
    return result


# ---------------------------------------------------------------- 查询 / 增删改
def list_processors() -> List[Dict[str, Any]]:
    """全部后处理：内置在前，按 id 排序；坏文件跳过并记日志。"""
    ensure_builtin()
    rows: List[Dict[str, Any]] = []
    seen = set()
    for path in sorted(post_dir().glob("*.json")):
        payload = _read(path)
        if payload is None:
            continue
        try:
            item = normalize(payload, processor_id=path.stem)
        except (PostError, TypeError, ValueError) as exc:
            logger.warning("跳过非法后处理配置 %s: %s", path.name, exc)
            continue
        seen.add(item["id"])
        rows.append(item)
    # 内置模板即使磁盘文件被删也补回来（ensure_builtin 已写，这里只兜底）
    for item in BUILTIN:
        if item["id"] not in seen:
            rows.append(dict(item))
    rows.sort(key=lambda row: (0 if row.get("builtin") else 1, row["id"]))
    return rows


def find_processor(processor_id: str) -> Optional[Dict[str, Any]]:
    pid = str(processor_id or "").strip().lower()
    if not ID_RE.fullmatch(pid):
        return None
    payload = _read(_path(pid))
    if payload is None:
        return None
    try:
        return normalize(payload, processor_id=pid)
    except (PostError, TypeError, ValueError):
        return None


def get_processor(processor_id: str) -> Dict[str, Any]:
    item = find_processor(processor_id)
    if item is None:
        raise PostError("后处理 #%s 不存在" % processor_id)
    return item


def save_processor(payload: Dict[str, Any],
                   processor_id: Optional[str] = None) -> Dict[str, Any]:
    """新建或修改自定义配置；内置模板只允许“另存为”新 id。"""
    pid = (processor_id or str(payload.get("id") or "")).strip().lower()
    existing = find_processor(pid) if pid else None
    if existing is not None and existing.get("builtin"):
        raise PostError("内置后处理不可修改，请另存为自定义后处理")
    if existing is None and processor_id:
        raise PostError("后处理 #%s 不存在" % pid)
    item = normalize(payload, processor_id=pid, builtin=False)
    if existing is None and _path(item["id"]).exists():
        raise PostError("后处理 #%s 已存在" % item["id"])
    _write(item)
    logger.info("后处理已保存: %s (%s)", item["id"], item["name"])
    return item


def remove_processor(processor_id: str) -> None:
    item = get_processor(processor_id)
    if item.get("builtin"):
        raise PostError("内置后处理不可删除")
    try:
        _path(item["id"]).unlink()
    except FileNotFoundError:
        raise PostError("后处理 #%s 不存在" % processor_id)
    logger.info("后处理已删除: %s", item["id"])


def builtin_ids() -> List[str]:
    return [item["id"] for item in BUILTIN]

