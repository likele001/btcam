"""刀具库与工艺模板服务（模块 6）。

分层约定：

    models/db.py      只管 SQL（增删改查、JSON 编解码）
    tool_service.py   业务校验、单位换算、参数解析（本文件）
    api/routes_tools  参数搬运与错误翻译
    toolpath_service  只认"已经解析好的扁平参数"

三条关键换算：

1. 补偿直径
       铣刀：diameter
       钼丝：wire_diameter + 2 * discharge_gap
   线切割的补偿半径 = 丝径/2 + 放电间隙，等效成一个直径交给刀路做偏置。

2. 顺逆铣
       climb（顺铣）       外轮廓 CCW、内孔 CW
       conventional（逆铣）外轮廓 CW、内孔 CCW
   点序由 _enforce_direction 落实，原始轮廓永远不改。

3. 参数优先级（高 -> 低）
       刀具字段 > 模板补偿参数 > 请求里的默认值
   刀具参数是"机床事实"，一旦绑定刀具就不再让请求体覆盖，
   保证同一把刀在任何入口（面板/脚本/导出）算出来的补偿完全一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.models import db

CATEGORIES = ("mill", "wire")
TOOL_TYPES = ("flat", "ball", "v")
DIRECTIONS = ("climb", "conventional")
CUT_TYPES = ("auto", "outer", "inner")
SIDES = ("auto", "outside", "inside", "on")

# 模板只保存补偿/微连接相关的字段，刀具与切深由刀具记录提供
TEMPLATE_PARAM_KEYS = (
    "cutType", "side", "optimize", "microJoint", "microJointWidth",
    "microJointCount", "leadIn", "leadRadius",
)

TEMPLATE_PARAM_DEFAULTS: Dict[str, Any] = {
    "cutType": "auto",
    "side": "auto",
    "optimize": True,
    "microJoint": False,
    "microJointWidth": 2.0,
    "microJointCount": 2,
    "leadIn": True,
    "leadRadius": 2.0,
}


class ToolError(ValueError):
    """业务校验失败：路由层统一翻译成 HTTP 400。"""


# ---------------------------------------------------------------- 类型转换
def _as_float(data: Dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError("%s 必须是数字" % key)


def _as_int(data: Dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if value is None:
        return int(default)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ToolError("%s 必须是整数" % key)


def _as_bool(data: Dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


# ---------------------------------------------------------------- 序列化
def _effective_diameter(row: Dict[str, Any]) -> float:
    """刀路偏置实际使用的等效直径 mm。"""
    if str(row.get("category") or "mill") == "wire":
        return max(0.0, float(row.get("wire_diameter") or 0.0)
                   + 2.0 * float(row.get("discharge_gap") or 0.0))
    return max(0.0, float(row.get("diameter") or 0.0))


def tool_to_api(row: Dict[str, Any]) -> Dict[str, Any]:
    """DB 行（蛇形）-> 前端/刀路参数（驼峰）。"""
    effective = _effective_diameter(row)
    return {
        "id": int(row.get("id") or 0),
        "projectId": int(row.get("project_id") or 0),
        "name": str(row.get("name") or ""),
        "category": str(row.get("category") or "mill"),
        "toolType": str(row.get("tool_type") or "flat"),
        "diameter": float(row.get("diameter") or 0.0),
        "tipRadius": float(row.get("tip_radius") or 0.0),
        "wireDiameter": float(row.get("wire_diameter") or 0.0),
        "dischargeGap": float(row.get("discharge_gap") or 0.0),
        "feed": float(row.get("feed") or 0.0),
        "plungeFeed": float(row.get("plunge_feed") or 0.0),
        "spindle": float(row.get("spindle") or 0.0),
        "passDepth": float(row.get("pass_depth") or 0.0),
        "safeZ": float(row.get("safe_z") or 0.0),
        "liftZ": float(row.get("lift_z") or 0.0),
        "direction": str(row.get("direction") or "climb"),
        "note": str(row.get("note") or ""),
        "effectiveDiameter": round(effective, 4),
        "compensationRadius": round(effective / 2.0, 4),
        "updatedAt": float(row.get("updated_at") or 0.0),
    }


def normalize_template_params(raw: Optional[Dict[str, Any]],
                              strict: bool = True) -> Dict[str, Any]:
    """白名单化模板参数：只保留 8 个补偿字段，顺带做区间校验。"""
    data = raw or {}
    if not isinstance(data, dict):
        raise ToolError("模板参数必须是对象")

    result: Dict[str, Any] = {}
    cut_type = str(data.get("cutType", TEMPLATE_PARAM_DEFAULTS["cutType"]))
    if cut_type not in CUT_TYPES:
        raise ToolError("cutType 只能是 %s" % "/".join(CUT_TYPES))
    result["cutType"] = cut_type

    side = str(data.get("side", TEMPLATE_PARAM_DEFAULTS["side"]))
    if side not in SIDES:
        raise ToolError("side 只能是 %s" % "/".join(SIDES))
    result["side"] = side

    result["optimize"] = _as_bool(data, "optimize", True)
    result["microJoint"] = _as_bool(data, "microJoint", False)

    width = _as_float(data, "microJointWidth", 2.0)
    if width < 0.0 or width > 100.0:
        raise ToolError("微连接宽度必须在 0~100mm")
    result["microJointWidth"] = round(width, 3)

    count = _as_int(data, "microJointCount", 2)
    if count < 0 or count > 20:
        raise ToolError("微连接数量必须在 0~20")
    result["microJointCount"] = count

    result["leadIn"] = _as_bool(data, "leadIn", True)
    radius = _as_float(data, "leadRadius", 2.0)
    if radius < 0.0 or radius > 500.0:
        raise ToolError("切入圆弧半径必须在 0~500mm")
    result["leadRadius"] = round(radius, 3)

    if strict and width > 0.0 and count > 0:
        # 只是提示性约束：桥总长不能超过周长，真正截断在刀路服务里做
        result["microJointWidth"] = min(width, 100.0)
    return result


def template_to_api(row: Dict[str, Any],
                    tool: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """模板行 -> 前端模型；带上刀具详情，前端一键加载不用再查一次。"""
    params = normalize_template_params(row.get("params") or {}, strict=False)
    return {
        "id": int(row.get("id") or 0),
        "projectId": int(row.get("project_id") or 0),
        "name": str(row.get("name") or ""),
        "material": str(row.get("material") or ""),
        "toolId": int(row["tool_id"]) if row.get("tool_id") else None,
        "params": params,
        "note": str(row.get("note") or ""),
        "createdAt": float(row.get("created_at") or 0.0),
        "updatedAt": float(row.get("updated_at") or 0.0),
        "tool": tool,
    }


# ---------------------------------------------------------------- 输入校验
def tool_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """前端请求 -> tool 表列；所有字段补全，非法值直接报错。"""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ToolError("刀具名称不能为空")
    if len(name) > 64:
        raise ToolError("刀具名称不能超过 64 个字符")

    category = str(data.get("category") or "mill")
    if category not in CATEGORIES:
        raise ToolError("刀具分类只能是 mill（铣刀）或 wire（钼丝）")

    tool_type = str(data.get("toolType") or "flat")
    if tool_type not in TOOL_TYPES:
        raise ToolError("刀具类型只能是 flat / ball / v")

    diameter = round(_as_float(data, "diameter", 0.0), 3)
    tip_radius = round(_as_float(data, "tipRadius", 0.0), 3)
    wire_diameter = round(_as_float(data, "wireDiameter", 0.0), 3)
    discharge_gap = round(_as_float(data, "dischargeGap", 0.0), 3)

    if category == "wire":
        if wire_diameter <= 0.0:
            raise ToolError("丝径必须大于 0")
        if discharge_gap < 0.0:
            raise ToolError("放电补偿间隙不能为负")
        diameter = 0.0
        tip_radius = 0.0
    else:
        if diameter <= 0.0:
            raise ToolError("铣刀直径必须大于 0")
        if diameter > 1000.0:
            raise ToolError("铣刀直径不能超过 1000mm")
        if tip_radius < 0.0 or tip_radius > diameter / 2.0 + 1e-9:
            raise ToolError("刀尖圆角必须在 0 ~ 直径/2 之间")
        wire_diameter = 0.0
        discharge_gap = 0.0

    feed = round(_as_float(data, "feed", 3000.0), 3)
    if feed <= 0.0:
        raise ToolError("进给速度必须大于 0")
    plunge_feed = round(_as_float(data, "plungeFeed", 800.0), 3)
    if plunge_feed <= 0.0:
        raise ToolError("下刀速度必须大于 0")

    spindle = round(_as_float(data, "spindle", 18000.0), 3)
    if spindle < 0.0:
        raise ToolError("主轴转速不能为负")
    if category == "wire":
        spindle = 0.0

    pass_depth = round(_as_float(data, "passDepth", 3.0), 3)
    if pass_depth <= 0.0:
        raise ToolError("单层切深必须大于 0")

    safe_z = round(_as_float(data, "safeZ", 5.0), 3)
    lift_z = round(_as_float(data, "liftZ", 5.0), 3)
    if safe_z < 0.0 or lift_z < 0.0:
        raise ToolError("安全高度/抬刀高度不能为负")

    direction = str(data.get("direction") or "climb")
    if direction not in DIRECTIONS:
        raise ToolError("走刀方向只能是 climb（顺铣）或 conventional（逆铣）")

    note = str(data.get("note") or "").strip()
    if len(note) > 500:
        raise ToolError("备注不能超过 500 个字符")

    return {
        "name": name,
        "category": category,
        "tool_type": tool_type,
        "diameter": diameter,
        "tip_radius": tip_radius,
        "wire_diameter": wire_diameter,
        "discharge_gap": discharge_gap,
        "feed": feed,
        "plunge_feed": plunge_feed,
        "spindle": spindle,
        "pass_depth": pass_depth,
        "safe_z": safe_z,
        "lift_z": lift_z,
        "direction": direction,
        "note": note,
    }


def template_payload(data: Dict[str, Any],
                     tool_exists) -> Dict[str, Any]:
    """前端请求 -> process_template 表列；tool_exists 由调用方注入。"""
    name = str(data.get("name") or "").strip()
    if not name:
        raise ToolError("模板名称不能为空")
    if len(name) > 64:
        raise ToolError("模板名称不能超过 64 个字符")

    material = str(data.get("material") or "").strip()
    if len(material) > 64:
        raise ToolError("物料名称不能超过 64 个字符")

    tool_id = data.get("toolId")
    if tool_id in ("", 0):
        tool_id = None
    if tool_id is not None:
        try:
            tool_id = int(tool_id)
        except (TypeError, ValueError):
            raise ToolError("toolId 必须是整数")
        if not tool_exists(tool_id):
            raise ToolError("刀具 #%d 不存在" % tool_id)

    note = str(data.get("note") or "").strip()
    if len(note) > 500:
        raise ToolError("备注不能超过 500 个字符")

    return {
        "name": name,
        "material": material,
        "tool_id": tool_id,
        "params": normalize_template_params(data.get("params") or {}),
        "note": note,
    }


# ---------------------------------------------------------------- 查询
def list_tools(conn, project_id: int) -> List[Dict[str, Any]]:
    return [tool_to_api(row) for row in db.load_tools(conn, project_id)]


def get_tool(conn, project_id: int, tool_id: int) -> Optional[Dict[str, Any]]:
    row = db.load_tool(conn, project_id, tool_id)
    return tool_to_api(row) if row is not None else None


def list_templates(conn, project_id: int) -> List[Dict[str, Any]]:
    tools = {row["id"]: tool_to_api(row) for row in db.load_tools(conn, project_id)}
    return [
        template_to_api(row, tools.get(row.get("tool_id")))
        for row in db.load_templates(conn, project_id)
    ]


def get_template(conn, project_id: int,
                 template_id: int) -> Optional[Dict[str, Any]]:
    row = db.load_template(conn, project_id, template_id)
    if row is None:
        return None
    tool = None
    if row.get("tool_id"):
        tool_row = db.load_tool(conn, project_id, int(row["tool_id"]))
        tool = tool_to_api(tool_row) if tool_row is not None else None
    return template_to_api(row, tool)


# ---------------------------------------------------------------- 增删改
def create_tool(conn, project_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    columns = tool_payload(data)
    tool_id = db.insert_tool(conn, project_id, columns)
    row = db.load_tool(conn, project_id, tool_id)
    if row is None:
        raise ToolError("刀具写入失败")
    return tool_to_api(row)


def edit_tool(conn, project_id: int, tool_id: int,
              data: Dict[str, Any]) -> Dict[str, Any]:
    columns = tool_payload(data)
    if not db.update_tool(conn, project_id, tool_id, columns):
        raise ToolError("刀具 #%d 不存在" % tool_id)
    row = db.load_tool(conn, project_id, tool_id)
    if row is None:
        raise ToolError("刀具 #%d 不存在" % tool_id)
    return tool_to_api(row)


def remove_tool(conn, project_id: int, tool_id: int) -> None:
    if db.load_tool(conn, project_id, tool_id) is None:
        raise ToolError("刀具 #%d 不存在" % tool_id)
    used = db.count_templates_using_tool(conn, project_id, tool_id)
    if used:
        raise ToolError("还有 %d 个工艺模板在使用该刀具，请先改绑模板" % used)
    if not db.delete_tool(conn, project_id, tool_id):
        raise ToolError("刀具 #%d 删除失败" % tool_id)


def create_template(conn, project_id: int,
                    data: Dict[str, Any]) -> Dict[str, Any]:
    columns = template_payload(
        data, lambda tid: db.load_tool(conn, project_id, tid) is not None)
    template_id = db.insert_template(conn, project_id, columns)
    result = get_template(conn, project_id, template_id)
    if result is None:
        raise ToolError("模板写入失败")
    return result


def edit_template(conn, project_id: int, template_id: int,
                  data: Dict[str, Any]) -> Dict[str, Any]:
    columns = template_payload(
        data, lambda tid: db.load_tool(conn, project_id, tid) is not None)
    if not db.update_template(conn, project_id, template_id, columns):
        raise ToolError("模板 #%d 不存在" % template_id)
    result = get_template(conn, project_id, template_id)
    if result is None:
        raise ToolError("模板 #%d 不存在" % template_id)
    return result


def remove_template(conn, project_id: int, template_id: int) -> None:
    if not db.delete_template(conn, project_id, template_id):
        raise ToolError("模板 #%d 不存在" % template_id)


# ---------------------------------------------------------------- 刀路参数解析
def resolve(conn, project_id: int, tool_id: Optional[int] = None,
            template_id: Optional[int] = None) -> Dict[str, Any]:
    """模板 -> 刀具 -> 扁平刀路参数。

    显式 toolId 优先于模板里绑定的刀具（面板上可以临时换刀试用），
    模板本身的补偿参数始终生效。
    """
    template = None
    if template_id:
        template = db.load_template(conn, project_id, int(template_id))
        if template is None:
            raise ToolError("工艺模板 #%d 不存在" % template_id)

    effective_tool_id: Optional[int] = None
    if tool_id:
        effective_tool_id = int(tool_id)
    elif template is not None and template.get("tool_id"):
        effective_tool_id = int(template["tool_id"])

    tool = None
    if effective_tool_id:
        tool = db.load_tool(conn, project_id, effective_tool_id)
        if tool is None:
            raise ToolError("刀具 #%d 不存在" % effective_tool_id)

    tool_api = tool_to_api(tool) if tool is not None else None
    template_api = template_to_api(template, tool_api) if template is not None else None

    tool_fields: Dict[str, Any] = {}
    if tool_api is not None:
        tool_fields = {
            "toolDiameter": tool_api["effectiveDiameter"],
            "feed": tool_api["feed"],
            "plungeFeed": tool_api["plungeFeed"],
            "spindle": tool_api["spindle"],
            "passDepth": tool_api["passDepth"],
            "safeZ": tool_api["safeZ"],
            "liftZ": tool_api["liftZ"],
            "direction": tool_api["direction"],
        }

    return {
        "tool": tool_api,
        "template": template_api,
        "toolFields": tool_fields,
        "templateParams": dict(template_api["params"]) if template_api else {},
    }


def apply_resolution(params: Dict[str, Any],
                     resolution: Dict[str, Any]) -> Dict[str, Any]:
    """把解析结果合进刀路参数：刀具 > 模板 > 请求默认值。"""
    for key, value in (resolution.get("toolFields") or {}).items():
        params[key] = value
    for key in TEMPLATE_PARAM_KEYS:
        value = (resolution.get("templateParams") or {}).get(key)
        if value is not None:
            params[key] = value
    params["tool"] = resolution.get("tool")
    params["template"] = resolution.get("template")
    params["toolId"] = (resolution.get("tool") or {}).get("id")
    params["templateId"] = (resolution.get("template") or {}).get("id")
    return params


def direction_for(direction: str, is_hole: bool) -> Optional[bool]:
    """物理走刀方向 -> 点序要求。返回 True 表示 CCW，None 表示不干预。

    顺铣：刀具切屑从材料实体侧离开，外轮廓逆时针、内孔顺时针；
    逆铣反过来。ccw/cw 是强制点序，供调试与特殊工艺使用。
    """
    if direction == "ccw":
        return True
    if direction == "cw":
        return False
    if direction == "climb":
        return not is_hole
    if direction == "conventional":
        return is_hole
    return None
