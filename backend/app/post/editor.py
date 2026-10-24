"""后处理可视化编辑器的支撑层（模块 11）。

三件事，全部在后端做，前端只负责渲染与交互：

    variable_dictionary()  变量字典：能用哪些占位符、各类模板有哪些指令槽位
    validate()             语法/范围校验：一次性列全错误与告警，不"首错即抛"
    preview()              用一条示例方框拼出 ToolpathPlan，走真实渲染器出文本

为什么不复用 `registry.normalize()` 做校验：那个函数遇到第一个问题就抛
`PostError`，只能给编辑器一条提示；可视化编辑器要的是"每一行错在哪"，
所以这里按字段逐条收集，字段名与前端输入框一一对应（如 `gcode.cutXY`）。

预览与导出共用 `renderer`，因此"预览长什么样，导出的文件就长什么样"，
不存在两套渲染逻辑漂移的问题。预览不落盘、不写库，纯内存计算。
"""

from __future__ import annotations

import re
import string
from typing import Any, Dict, List, Optional, Tuple

from app.post import registry, renderer
from app.post.renderer import RenderContext
from app.post.registry import PostError
from app.services.toolpath_service import Move, ToolpathPlan

# ---------------------------------------------------------------- 变量字典
# 顺序即前端变量面板的展示顺序（任务信息 -> 机床参数 -> 坐标/进给）
VARIABLES: Tuple[Dict[str, str], ...] = (
    {"name": "name", "group": "任务", "desc": "任务名 / 文件名",
     "example": "BT_CAM"},
    {"name": "date", "group": "任务", "desc": "生成时间（YYYY-MM-DD HH:MM:SS）",
     "example": "2026-10-23 10:30:00"},
    {"name": "programNo", "group": "任务", "desc": "程序号（O 号）",
     "example": "0001"},
    {"name": "tool", "group": "机床", "desc": "刀具号（未绑定刀具时为 1）",
     "example": "1"},
    {"name": "spindle", "group": "机床", "desc": "主轴转速 r/min",
     "example": "18000"},
    {"name": "safeZ", "group": "机床", "desc": "安全高度 mm（抬刀位置）",
     "example": "5"},
    {"name": "x", "group": "坐标", "desc": "当前点 X 坐标 mm",
     "example": "100"},
    {"name": "y", "group": "坐标", "desc": "当前点 Y 坐标 mm（可被翻转）",
     "example": "60"},
    {"name": "z", "group": "坐标", "desc": "当前点 Z 坐标 mm（下刀为负）",
     "example": "-3"},
    {"name": "feed", "group": "坐标", "desc": "进给速度 mm/min",
     "example": "3000"},
    {"name": "speed", "group": "刻绘", "desc": "刻绘速度档位（HPGL VS）",
     "example": "30"},
    {"name": "force", "group": "刻绘", "desc": "刻绘压力档位（HPGL FS）",
     "example": "20"},
)

GCODE_SLOTS = ("rapidZ", "rapidXY", "plungeZ", "feed", "cutXY")
HPGL_SLOTS = ("penUp", "penDown", "penLift")

# 各类后处理的"指令槽位"：前端据此生成分组编辑区，不要在前端硬编码
INSTRUCTIONS: Dict[str, List[str]] = {
    "gcode": list(GCODE_SLOTS),
    "hpgl": ["header", "footer", "penUp", "penDown", "penLift"],
    "dxf": ["version"],
}

INSTRUCTION_LABELS: Dict[str, Dict[str, str]] = {
    "gcode": {
        "header": "程序头", "footer": "程序尾", "toolChange": "换刀",
        "rapidZ": "抬刀快移（Z）", "rapidXY": "快速定位（XY）",
        "plungeZ": "下刀（Z+F）", "feed": "单独进给",
        "cutXY": "切削移动（XY）",
    },
    "hpgl": {
        "header": "初始化指令", "footer": "收尾指令",
        "penUp": "空走抬笔", "penDown": "落笔移动", "penLift": "抬笔",
    },
    "dxf": {"version": "DXF 版本"},
}

KINDS = ("gcode", "hpgl", "dxf")

# 与 registry 保持一致的字段约束（这里是"逐条报错"的副本，不复制渲染逻辑）
ID_RE = re.compile(r"[a-z0-9][a-z0-9_]{1,39}")
EXT_RE = re.compile(r"[a-z0-9]{1,8}")
MAX_TEXT = 2000              # 单行模板最大长度
MAX_PREVIEW_CHARS = 200000   # 预览文本上限，防止误存超大文件卡死前端


def variable_dictionary() -> Dict[str, Any]:
    """变量字典 + 指令槽位：前端变量面板的唯一数据源。"""
    return {
        "variables": [dict(item) for item in VARIABLES],
        "instructions": {kind: list(slots) for kind, slots in INSTRUCTIONS.items()},
        "instructionLabels": {kind: dict(labels)
                              for kind, labels in INSTRUCTION_LABELS.items()},
        "kinds": list(KINDS),
        "notes": "占位符用 {名称} 书写，渲染时由后端替换；"
                 "花括号本身要输出时写 {{ 或 }}。",
    }


# ---------------------------------------------------------------- 校验
def _issue(field: str, message: str, hint: str = "") -> Dict[str, str]:
    item = {"field": field, "message": message}
    if hint:
        item["hint"] = hint
    return item


def _template_lines(processor: Dict[str, Any], kind: str) -> List[Tuple[str, Any]]:
    """按"字段名 -> 文本"展开所有模板行，字段名与前端表单结构一致。"""
    config = processor.get(kind)
    if not isinstance(config, dict):
        return []
    rows: List[Tuple[str, Any]] = []
    for section in ("header", "footer", "toolChange"):
        value = config.get(section)
        if isinstance(value, list):
            for index, text in enumerate(value):
                rows.append(("%s.%s[%d]" % (kind, section, index), text))
    slots = GCODE_SLOTS if kind == "gcode" else HPGL_SLOTS if kind == "hpgl" else ()
    for key in slots:
        rows.append(("%s.%s" % (kind, key), config.get(key)))
    return rows


def _scan_placeholders(text: str, field: str, used: set, unknown: Dict[str, str],
                       errors: List[Dict[str, str]]) -> None:
    """扫一行模板：收集用到的变量，未知占位符/括号不配对记成错误。

    `unknown` 以变量名为键做去重：同一行重复写两次 {foo} 只报一条，
    否则复杂模板会刷出一屏重复提示。
    """
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        errors.append(_issue(field, "%s 模板括号不配对：%s" % (field, exc),
                             "只想输出花括号请写成 {{ 和 }}"))
        return
    for _literal, name, _spec, _conv in parsed:
        if name is None:
            continue
        # 支持 {x:6.2f} 这类格式说明，只取根变量名判断是否已知
        root = name.split(".", 1)[0].split("[", 1)[0]
        if root in registry.PLACEHOLDERS:
            used.add(root)
            continue
        if root in unknown:
            continue
        unknown[root] = field
        errors.append(_issue(
            field, "%s 使用了未知占位符 {%s}" % (field, name),
            "可用变量：%s" % ", ".join(sorted(registry.PLACEHOLDERS)),
        ))


def _check_gcode_numbers(config: Dict[str, Any], errors: List[Dict[str, str]]) -> None:
    """数值字段单独校验：这些值直接决定文件格式，错了导出必崩。"""
    try:
        precision = int(config.get("precision", 3))
        if precision < 0 or precision > 6:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(_issue("gcode.precision", "precision 必须是 0~6 的整数",
                             "木工常用 2~3 位小数"))
    try:
        start = int(config.get("lineNumberStart", 10))
        if start < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(_issue("gcode.lineNumberStart", "行号起始值必须是不小于 0 的整数"))
    try:
        step = int(config.get("lineNumberStep", 10))
        if step <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(_issue("gcode.lineNumberStep", "行号步长必须是正整数"))

    program_no = config.get("programNo", "0001")
    if not isinstance(program_no, str):
        errors.append(_issue("gcode.programNo", "程序号必须是字符串"))
    elif len(program_no.strip()) > 16:
        errors.append(_issue("gcode.programNo", "程序号过长（最多 16 位）"))


def _check_hpgl_numbers(config: Dict[str, Any], errors: List[Dict[str, str]]) -> None:
    try:
        units = float(config.get("unitsPerMm", 40.0))
        if units <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(_issue("hpgl.unitsPerMm", "unitsPerMm 必须是大于 0 的数字",
                             "常见绘图仪每毫米 40 个单位"))


def validate(processor: Any, processor_id: Optional[str] = None) -> Dict[str, Any]:
    """逐字段校验一份后处理草稿，返回可直接渲染给用户看的结果。

    返回结构（前端按字段高亮输入框）：

        ok              是否通过（只有 errors 会拦住保存/导出）
        builtin         是否是内置模板（内置只读，要改必须另存为）
        errors[]        {field, message, hint?}
        warnings[]      工艺建议，不拦保存
        usedVariables   模板里真正出现的占位符（按变量字典顺序）
        unusedVariables 没出现的占位符（提示用，不是错）
        unknownVariables 出现了但字典里没有的名字
    """
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    used: set = set()
    unknown: Dict[str, str] = {}

    if not isinstance(processor, dict):
        return {
            "ok": False, "builtin": False,
            "errors": [_issue("processor", "配置必须是 JSON 对象")],
            "warnings": [], "usedVariables": [], "unusedVariables": [],
            "unknownVariables": [],
        }

    payload = processor
    pid = str(processor_id or payload.get("id") or "").strip().lower()
    # 是否内置只看 id 是否撞上内置清单：用户把一个内置模板改个 id
    # 另存为草稿时它就是可编辑的新模板，不能因为残留的 builtin 标记被拦住。
    builtin = bool(pid and pid in registry.builtin_ids())
    if builtin:
        errors.append(_issue(
            "id", "内置后处理不可直接修改，请另存为自定义后处理",
            "保存时改一个 id 即可变成自定义模板，内置模板保持不动",
        ))

    if pid and not ID_RE.fullmatch(pid):
        errors.append(_issue("id", "id 只能用 2~40 位小写字母/数字/下划线"))
    if not pid:
        errors.append(_issue("id", "id 不能为空",
                             "新建模板需要给一个唯一 id，用于存盘与导出时引用"))

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(_issue("name", "名称不能为空"))
    elif len(name.strip()) > 64:
        errors.append(_issue("name", "名称过长（最多 64 字）"))

    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in KINDS:
        errors.append(_issue("kind", "kind 必须是 %s 之一" % "/".join(KINDS)))
        kind = ""

    ext = str(payload.get("ext") or "").strip().lower().lstrip(".")
    if not EXT_RE.fullmatch(ext):
        errors.append(_issue("ext", "扩展名只能是 1~8 位字母/数字（不带点）"))

    if not kind:
        return {
            "ok": False, "builtin": builtin, "errors": errors, "warnings": warnings,
            "usedVariables": [], "unusedVariables": list(registry.PLACEHOLDERS),
            "unknownVariables": [],
        }

    config = payload.get(kind)
    if kind != "dxf" and not isinstance(config, dict):
        errors.append(_issue(kind, "%s 配置缺失" % kind))

    # --- 模板行：类型/长度/占位符
    for field, text in _template_lines(payload, kind):
        # 没有方括号的是指令槽位（rapidZ/cutXY 等）：缺键、空串都不允许，
        # 否则整段刀路会静默消失，导出文件看上去“少了一半”
        is_slot = field.count("[") == 0
        if text is None:
            if is_slot:
                errors.append(_issue(field, "%s 不能为空" % field))
            continue
        if not isinstance(text, str):
            errors.append(_issue(field, "%s 必须是字符串" % field))
            continue
        if len(text) > MAX_TEXT:
            errors.append(_issue(field, "%s 过长（最多 %d 字符）" % (field, MAX_TEXT)))
            continue
        if is_slot and not text.strip():
            errors.append(_issue(field, "%s 不能为空" % field,
                                 "这个槽位是必填的，决定每段刀路怎么走"))
            continue
        _scan_placeholders(text, field, used, unknown, errors)

    # --- 数值与必填文本
    if kind == "gcode" and isinstance(config, dict):
        _check_gcode_numbers(config, errors)
    if kind == "hpgl" and isinstance(config, dict):
        _check_hpgl_numbers(config, errors)

    description = payload.get("description")
    if isinstance(description, str) and len(description) > 500:
        errors.append(_issue("description", "说明过长（最多 500 字）"))

    # --- 工艺告警：只提醒，不拦保存（很多专机确实不用 M30）
    if kind == "gcode" and isinstance(config, dict):
        header = "\n".join(str(t) for t in config.get("header") or [] if isinstance(t, str))
        footer = "\n".join(str(t) for t in config.get("footer") or [] if isinstance(t, str))
        if "M30" not in footer and "M02" not in footer:
            warnings.append(_issue(
                "gcode.footer", "程序尾没有 M30/M02 结束指令，部分机床会停在程序末",
                "维宏/新代/山龙一般都要求 M30 收尾",
            ))
        if "M5" not in footer and "M05" not in footer:
            warnings.append(_issue(
                "gcode.footer", "程序尾没有停主轴（M5/M05），空转可能伤料",
            ))
        if "safeZ" not in used:
            warnings.append(_issue(
                "gcode.header", "程序头/尾没有引用 {safeZ}，换刀或结束时机头可能停在工件上",
            ))
        if config.get("lineNumbers") and "%" in header:
            warnings.append(_issue(
                "gcode.lineNumbers", "开启行号后 % 开头的行不会被编号（渲染器行为），"
                                     "部分系统要求 % 独占一行",
            ))
    if kind == "hpgl" and isinstance(config, dict):
        footer = "\n".join(str(t) for t in config.get("footer") or [] if isinstance(t, str))
        if "SP0" not in footer:
            warnings.append(_issue(
                "hpgl.footer", "收尾指令里没有 SP0（归笔），笔可能停在纸上",
            ))

    ordered_used = [item["name"] for item in VARIABLES if item["name"] in used]
    return {
        "ok": not errors,
        "builtin": builtin,
        "errors": errors,
        "warnings": warnings,
        "usedVariables": ordered_used,
        "unusedVariables": [item["name"] for item in VARIABLES
                            if item["name"] not in used],
        "unknownVariables": sorted(unknown),
    }


# ---------------------------------------------------------------- 预览
def _num(options: Dict[str, Any], key: str, default: float,
         low: float, high: float) -> float:
    """把前端传来的数字夹到合理区间：预览不能因为一个手滑值崩掉。"""
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    if value != value:  # NaN
        value = float(default)
    return max(low, min(high, value))


def _sample_plan(side: float, depth: float, feed: float) -> ToolpathPlan:
    """一条示例刀路：矩形 四周走一圈。

    形状刻意做得简单且不对称（宽 > 高），这样预览里"X 变了 / Y 翻转了"
    一眼就能看出来，比一条斜线更容易核对模板是不是接错了变量。
    """
    height = side * 0.6
    corners = [(0.0, 0.0), (side, 0.0), (side, height), (0.0, height), (0.0, 0.0)]
    moves: List[Move] = [
        Move(0.0, 0.0, abs(depth) + 5.0, "rapid", 0.0),
        Move(0.0, 0.0, -abs(depth), "plunge", feed),
    ]
    for x, y in corners[1:]:
        moves.append(Move(x, y, -abs(depth), "cut", feed))
    return ToolpathPlan(moves=moves, stats={"preview": True})


def _preview_note(kind: str) -> str:
    if kind == "dxf":
        return "DXF 后处理没有模板文本：导出时由 ezdxf 直接写图形文件"
    if kind == "hpgl":
        return "HPGL 预览按 unitsPerMm 换算成绘图仪单位，坐标为整数"
    return "G 代码预览使用与导出完全相同的渲染器，改一行就能看到结果"


def preview(processor: Any, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """渲染预览文本；校验不通过时返回空文本，避免"半成品"误导用户。"""
    opts = dict(options or {})
    result = validate(processor)
    base: Dict[str, Any] = {
        "ok": False,
        "text": "",
        "lineCount": 0,
        "bytes": 0,
        "note": "",
        "kind": str((processor or {}).get("kind") or "") if isinstance(processor, dict) else "",
        "errors": result["errors"],
        "warnings": result["warnings"],
        "usedVariables": result["usedVariables"],
        "unusedVariables": result["unusedVariables"],
    }
    if not result["ok"]:
        base["note"] = "模板校验未通过，先按提示改好再看预览"
        return base

    payload = dict(processor)  # type: ignore[arg-type]
    kind = str(payload.get("kind") or "").lower()
    base["kind"] = kind
    if kind == "dxf":
        base.update({"ok": True, "note": _preview_note(kind)})
        return base

    size_mm = _num(opts, "sizeMm", 100.0, 5.0, 5000.0)
    depth = _num(opts, "depth", 3.0, 0.0, 100.0)
    feed = _num(opts, "feed", 3000.0, 1.0, 100000.0)
    context = RenderContext(
        name=str(opts.get("name") or payload.get("name") or "BT_CAM")[:80],
        safe_z=_num(opts, "safeZ", 5.0, 0.0, 500.0),
        spindle=_num(opts, "spindle", 18000.0, 0.0, 60000.0),
        feed=feed,
        tool=int(_num(opts, "tool", 1.0, 0.0, 999.0)),
        flip_y=bool(opts.get("flipY")),
        speed=int(_num(opts, "speed", 30.0, 0.0, 100.0)),
        force=int(_num(opts, "force", 20.0, 0.0, 100.0)),
        program_no=str(opts.get("programNo") or "0001")[:16],
    )
    plan = _sample_plan(size_mm, depth, feed)

    # 程序号优先用预览参数，其次跟导出保持一致取模板里的 programNo
    config = payload.get("gcode") if isinstance(payload.get("gcode"), dict) else {}
    program_no = str(opts.get("programNo")
                     or config.get("programNo")
                     or "0001")[:16]
    context.program_no = program_no

    try:
        # 走 registry.normalize 补齐默认值：保证预览用的配置结构与导出完全一致
        normalized = registry.normalize(payload, processor_id=str(payload.get("id") or ""))
        text = renderer.render(plan, normalized, context)
    except (PostError, ValueError, KeyError) as exc:
        base["errors"] = [_issue("processor", "渲染失败：%s" % exc)]
        base["note"] = "模板能通过校验但渲染失败，请检查指令槽位"
        return base

    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS]
        note = "预览已截断（模板输出超过 %d 字符）" % MAX_PREVIEW_CHARS
    else:
        note = _preview_note(kind)

    base.update({
        "ok": True,
        "text": text,
        "lineCount": len(text.splitlines()),
        "bytes": len(text.encode("utf-8")),
        "note": note,
        "options": {
            "sizeMm": size_mm,
            "depth": depth,
            "feed": feed,
            "safeZ": context.safe_z,
            "spindle": context.spindle,
            "tool": context.tool,
            "flipY": context.flip_y,
        },
    })
    return base
