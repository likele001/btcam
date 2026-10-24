"""模块 11 验收：后处理可视化编辑器（变量字典 / 校验 / 预览 / 改模板导出生效）。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\post_editor_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\post_editor_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1  变量字典        12 个占位符 + 中文说明 + 5 条 G 代码指令
    A2  正常草稿        维宏副本校验通过，用到的变量清单正确
    A3  未用变量        能指出草稿里没被引用的变量（提示而非报错）
    A4  未知占位符      {foo} -> 错误，且只报一次
    A5  括号不配对      "M3 S{spindle" -> 错误
    A6  空指令          cutXY 为空 -> 错误
    A7  数值范围        precision=9 -> 错误
    A8  工艺警告        程序尾缺 M30 / 程序头缺 {safeZ} -> 只警告
    A9  行号提示        开行号且含 % -> 提示 % 行不编号
    A10 G 代码预览      G21/M3 S18000/G1 X100，行数与字节数
    A11 预览参数        sizeMm/flipY 立刻反映到坐标
    A12 HPGL 预览       IN;/SP1;/PD/SP0;
    A13 DXF 预览        kind=dxf 无模板文本，返回说明
    A14 错误草稿预览    校验不过就不渲染

    B1  变量字典接口    GET /api/post/variables
    B2  校验接口        POST /api/post/validate -> ok=true
    B3  校验错误        POST /api/post/validate -> ok=false + 错误明细
    B4  预览接口        POST /api/post/preview -> 文本含 G21
    B5  改模板保存导出  新建自定义后处理 -> 导出，文件含自定义标记
    B6  再改再导出      PUT 改模板 -> 导出/预览同时生效（核心验收）
    B7  内置保护        编辑 weihong_nc 被校验拦下
    B8  未知 id         修改不存在的后处理 -> 404
    B9  删除            清理后 GET 该 id -> 404

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []
CUSTOM_ID = "post_editor_test"


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


def problems(result: Dict[str, Any]) -> str:
    """把 errors 压成一行，方便失败时看原因。"""
    return "；".join("%s: %s" % (item.get("field"), item.get("message"))
                    for item in result.get("errors") or [])[:200]


# ---------------------------------------------------------------- 草稿工具
def weihong_draft() -> Dict[str, Any]:
    """取内置维宏模板的副本，改一个假 id 当作“正在编辑的新后处理”。"""
    from app.post import registry

    payload = copy.deepcopy(registry.get_processor("weihong_nc"))
    payload["id"] = "editor_probe"
    payload["builtin"] = False
    return payload


def gcode_draft(marker: str = "EDITOR V1") -> Dict[str, Any]:
    """全链路验收用的自定义模板：程序头带版本标记，便于断言“改动真的生效”。"""
    return {
        "id": CUSTOM_ID,
        "name": "验收 可视化编辑器",
        "kind": "gcode",
        "ext": "nc",
        "description": "模块 11 验收用，测试结束删除",
        "gcode": {
            "precision": 2,
            "lineNumbers": False,
            "lineNumberStart": 10,
            "lineNumberStep": 10,
            "programNo": "0777",
            "header": [
                "%",
                "O{programNo}",
                "( %s )" % marker,
                "( {name} {date} )",
                "G21",
                "G90",
                "G0 Z{safeZ}",
                "M3 S{spindle}",
            ],
            "footer": ["G0 Z{safeZ}", "M5", "M30", "%"],
            "toolChange": ["T{tool} M6"],
            "rapidZ": "G0 Z{z}",
            "rapidXY": "G0 X{x} Y{y}",
            "plungeZ": "G1 Z{z} F{feed}",
            "feed": "F{feed}",
            "cutXY": "G1 X{x} Y{y}",
        },
    }


def square_contour() -> List[Dict[str, Any]]:
    return [{
        "id": 1, "layerId": 1, "closed": True, "isHole": False, "partId": 0,
        "pts": [[0, 0], [100, 0], [100, 60], [0, 60]],
    }]


# ---------------------------------------------------------------- 服务层
def service_tests() -> None:
    from app.post import editor

    print("== A. 服务层：变量字典 / 校验 / 预览 ==")

    # --- A1 变量字典
    dictionary = editor.variable_dictionary()
    variables = dictionary["variables"]
    names = [item["name"] for item in variables]
    check("A1 变量字典",
          len(variables) == 12
          and all(item.get("desc") and item.get("example") for item in variables)
          and dictionary["instructions"]["gcode"] == ["rapidZ", "rapidXY", "plungeZ", "feed", "cutXY"]
          and "x" in names and "force" in names,
          "%d 个占位符" % len(variables))

    # --- A2 正常草稿
    draft = weihong_draft()
    result = editor.validate(draft)
    check("A2 正常草稿",
          result["ok"] and not result["errors"]
          and {"name", "date", "safeZ", "spindle", "x", "y"} <= set(result["usedVariables"]),
          "used=%s" % ",".join(result["usedVariables"]))

    # --- A3 未用变量（维宏模板没写 {programNo}/{tool}）
    unused = set(result["unusedVariables"])
    check("A3 未用变量",
          "tool" in unused and "programNo" in unused and "x" not in unused,
          "unused=%s" % ",".join(sorted(unused)))

    # --- A4 未知占位符（同一个名字只报一条）
    broken = copy.deepcopy(draft)
    broken["gcode"]["header"].append("( bad {foo} {foo} )")
    result = editor.validate(broken)
    unknown_msgs = [item for item in result["errors"] if "未知占位符" in item["message"]]
    check("A4 未知占位符",
          not result["ok"] and len(unknown_msgs) == 1 and "foo" in result["unknownVariables"],
          problems(result))

    # --- A5 括号不配对
    broken = copy.deepcopy(draft)
    broken["gcode"]["header"].append("M3 S{spindle")
    result = editor.validate(broken)
    check("A5 括号不配对",
          not result["ok"] and any("括号不配对" in item["message"] for item in result["errors"]),
          problems(result))

    # --- A6 空指令
    broken = copy.deepcopy(draft)
    broken["gcode"]["cutXY"] = "   "
    result = editor.validate(broken)
    check("A6 空指令",
          not result["ok"] and any("不能为空" in item["message"] for item in result["errors"]),
          problems(result))

    # --- A7 数值范围
    broken = copy.deepcopy(draft)
    broken["gcode"]["precision"] = 9
    result = editor.validate(broken)
    check("A7 数值范围",
          not result["ok"] and any(item["field"] == "gcode.precision" for item in result["errors"]),
          problems(result))

    # --- A8 工艺警告（只警告，不拦保存）
    soft = copy.deepcopy(draft)
    soft["gcode"]["footer"] = ["M5"]
    soft["gcode"]["header"] = ["%", "G21", "G90", "M3 S{spindle}"]
    result = editor.validate(soft)
    text = " ".join(item["message"] for item in result["warnings"])
    check("A8 工艺警告",
          result["ok"] and "M30" in text and "safeZ" in text,
          "%d 条警告：%s" % (len(result["warnings"]), text))

    # --- A9 行号提示
    numbered = copy.deepcopy(draft)
    numbered["gcode"]["lineNumbers"] = True
    result = editor.validate(numbered)
    check("A9 行号提示",
          result["ok"] and any("% 开头" in item["message"] for item in result["warnings"]),
          "%d 条警告" % len(result["warnings"]))

    # --- A10 G 代码预览
    result = editor.preview(draft, options={"sizeMm": 100, "spindle": 18000, "name": "EDITOR_PREVIEW"})
    text = result.get("text") or ""
    check("A10 G 代码预览",
          result["ok"] and "G21" in text and "M3 S18000" in text
          and "G1 X100 Y0" in text and "M30" in text
          and result["lineCount"] > 8 and result["bytes"] > 0,
          "%d 行 / %d 字节" % (result["lineCount"], result["bytes"]))

    # --- A11 预览参数（尺寸与 Y 翻转立刻反映）
    big = editor.preview(draft, options={"sizeMm": 200})
    flipped = editor.preview(draft, options={"sizeMm": 100, "flipY": True})
    check("A11 预览参数",
          "G1 X200 Y0" in (big.get("text") or "")
          and "Y-60" in (flipped.get("text") or ""),
          "sizeMm=200 -> X200；flipY -> Y-60")

    # --- A12 HPGL 预览
    from app.post import registry
    hpgl = copy.deepcopy(registry.get_processor("plotter_plt"))
    hpgl["id"] = "editor_probe_hpgl"
    result = editor.preview(hpgl)
    text = result.get("text") or ""
    check("A12 HPGL 预览",
          result["ok"] and "IN;" in text and "SP1;" in text
          and "PD4000,0;" in text and "SP0;" in text,
          "%d 行" % result["lineCount"])

    # --- A13 DXF 预览
    dxf = copy.deepcopy(registry.get_processor("generic_dxf"))
    dxf["id"] = "editor_probe_dxf"
    result = editor.preview(dxf)
    check("A13 DXF 预览",
          result["ok"] and result["text"] == "" and "没有模板文本" in result["note"],
          result["note"][:60])

    # --- A14 错误草稿不出来“半成品”
    result = editor.preview(broken)
    check("A14 错误草稿预览",
          not result["ok"] and result["text"] == "" and result["errors"],
          problems(result))


# ---------------------------------------------------------------- HTTP
def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
              timeout: float = 60.0) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError("%s %s -> HTTP %s: %s" % (method, url, exc.code, detail))


def http_error_code(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> int:
    try:
        http_json(method, url, body)
        return 200
    except RuntimeError as exc:
        text = str(exc)
        for code in ("400", "404", "409", "422", "500"):
            if "HTTP %s" % code in text:
                return int(code)
        raise


def export_nc(base: str) -> Tuple[str, Path]:
    """用自定义后处理导出一份 NC，返回 (内容, 路径)。"""
    result = http_json("POST", base + "/api/export", {
        "contours": square_contour(),
        "processorId": CUSTOM_ID,
        "name": "EDITOR_TEST",
        "depth": 3.0,
        "passDepth": 3.0,
        "toolDiameter": 6.0,
    })
    path = Path(result["path"])
    return path.read_text(encoding="utf-8", errors="replace"), path


def rest_tests(base: str) -> None:
    from app.post import registry

    print("== B. 接口：校验 / 预览 / 改模板导出生效 ==")
    created: List[Path] = []
    try:
        # --- B1 变量字典
        dictionary = http_json("GET", base + "/api/post/variables")
        check("B1 变量字典接口",
              len(dictionary.get("variables") or []) == 12
              and dictionary.get("instructions", {}).get("gcode"),
              "%d 个占位符" % len(dictionary.get("variables") or []))

        # --- B2 校验通过
        draft = gcode_draft("EDITOR V1")
        result = http_json("POST", base + "/api/post/validate", {"processor": draft})
        check("B2 校验接口",
              result["ok"] and not result["errors"]
              and "x" in result["usedVariables"],
              "used=%s" % ",".join(result["usedVariables"]))

        # --- B3 校验报错
        bad = copy.deepcopy(draft)
        bad["gcode"]["cutXY"] = "G1 X{x} Y{y} {nope}"
        bad_result = http_json("POST", base + "/api/post/validate", {"processor": bad})
        check("B3 校验错误",
              not bad_result["ok"]
              and any("未知占位符" in item["message"] for item in bad_result["errors"]),
              problems(bad_result))

        # --- B4 预览接口
        preview = http_json("POST", base + "/api/post/preview",
                            {"processor": draft, "options": {"sizeMm": 100}})
        text = preview.get("text") or ""
        check("B4 预览接口",
              preview["ok"] and "G21" in text and "( EDITOR V1 )" in text
              and "G1 X100 Y0" in text,
              "%d 行 / %d 字节" % (preview["lineCount"], preview["bytes"]))

        # --- B5 新建自定义后处理 -> 导出（模板改动体现在文件里）
        http_json("POST", base + "/api/post", draft)
        content, path = export_nc(base)
        created.append(path)
        check("B5 改模板保存导出",
              "O0777" in content and "( EDITOR V1 )" in content
              and "G1 X" in content and path.suffix == ".nc",
              "%s（%d 字节）" % (path.name, path.stat().st_size))

        # --- B6 再改一次模板：PUT 保存后导出与预览同时生效（核心验收）
        # cutXY 加尾部注释 " ; V2"：不依赖导入补偿后的第一个落刀点（带引入会偏），
        # 只看“模板改动是否原样进了加工文件”。
        v2 = gcode_draft("EDITOR V2")
        v2["gcode"]["cutXY"] = "G1 X{x} Y{y} ; V2"
        saved = http_json("PUT", base + "/api/post/" + CUSTOM_ID, v2)
        content, path2 = export_nc(base)
        created.append(path2)
        again = http_json("POST", base + "/api/post/preview",
                          {"processor": v2, "options": {"sizeMm": 100}})
        check("B6 再改再导出",
              saved["gcode"]["header"][2] == "( EDITOR V2 )"
              and "( EDITOR V2 )" in content and "( EDITOR V1 )" not in content
              and " ; V2" in content
              and "( EDITOR V2 )" in (again.get("text") or ""),
              "%s（%d 字节）" % (path2.name, path2.stat().st_size))

        # --- B7 内置保护：编辑内置模板时校验就拦下
        builtin_draft = http_json("GET", base + "/api/post/weihong_nc")
        blocked = http_json("POST", base + "/api/post/validate",
                            {"processor": builtin_draft, "processorId": "weihong_nc"})
        check("B7 内置保护",
              not blocked["ok"] and blocked["builtin"]
              and any("内置" in item["message"] for item in blocked["errors"]),
              problems(blocked))

        # --- B8 未知 id 修改
        code = http_error_code("PUT", base + "/api/post/no_such_post", gcode_draft())
        check("B8 未知 id", code == 404, "PUT /api/post/no_such_post -> HTTP %d" % code)

        # --- B9 删除
        http_json("DELETE", base + "/api/post/" + CUSTOM_ID)
        gone = http_error_code("GET", base + "/api/post/" + CUSTOM_ID)
        check("B9 删除", gone == 404, "删除后 GET -> HTTP %d" % gone)
    finally:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            registry.remove_processor(CUSTOM_ID)
        except Exception:
            pass


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 11 后处理可视化编辑器验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口测试")
    args = parser.parse_args()

    try:
        service_tests()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("服务层异常", False, str(exc))

    if args.url:
        try:
            rest_tests(args.url.rstrip("/"))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check("接口层异常", False, str(exc))

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n== 结果：%d/%d 通过 ==" % (passed, total))
    failed = [name for name, ok, _ in CHECKS if not ok]
    if failed:
        print("失败项：%s" % "，".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
