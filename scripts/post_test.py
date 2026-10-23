"""模块 7 验收：后处理系统（配置驱动 + NC/PLT/CUT/DXF + 批量多板）。

两种跑法：

    # 1) 只测服务层（不需要后端，秒级）
    backend\\.venv\\Scripts\\python.exe scripts\\post_test.py

    # 2) 服务层 + 接口全链路（后端需先启动在 8975）
    backend\\.venv\\Scripts\\python.exe scripts\\post_test.py --url http://127.0.0.1:8975

覆盖的验收项：

    A1  内置注册表      6 份内置模板落盘、幂等、字段齐全
    A2  维宏 G 代码     % / G21 / G90 / G40 / G0 Z / M3 / M5 / M30
    A3  行号与换刀      syntec 行号 N10/N20…、T7 M6、G43 H7
    A4  小数精度        自定义 precision=2 时坐标两位小数
    A5  HPGL            IN/SP1/PU/PD/SP0，坐标按 40 unit/mm 换算
    A6  CUT             VS/FS 速度压力头
    A7  Y 轴翻转        flipY 后 Y 坐标取反
    A8  DXF 写回        图层与轮廓数一致，ezdxf 可读回
    A9  配置校验        未知占位符/非法 kind/内置不可改删被拒绝
    A10 占位符填充      {name} {date} {safeZ} {spindle} 全部生效

    B1  后处理列表      至少 6 份，gcode/hpgl/dxf 三种 kind 齐全
    B2  自定义 CRUD     POST/GET/PUT/DELETE 全链路
    B3  NC 导出         processorId -> 文件含 G21/G1/M30
    B4  DXF 导出        工程图形写回，轮廓数 > 0
    B5  批量多板        两块板 -> 2 个文件，文件名带 S1/S2
    B6  未知后处理      400
    B7  内置保护        改/删内置后处理 -> 400

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

CHECKS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print("  %s %s%s" % ("[OK]  " if ok else "[FAIL]", name, ("  " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- 图形工具
def square(x: float, y: float, w: float, h: float) -> List[List[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def contour(contour_id: int, pts: List[List[float]]) -> Dict[str, Any]:
    return {"id": contour_id, "layerId": 1, "closed": True,
            "isHole": False, "partId": 0, "pts": pts}


def base_params(contours: List[Dict[str, Any]], **overrides: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "contours": contours,
        "cutType": "auto",
        "direction": "ccw",
        "optimize": True,
        "startPoints": [],
        "microJoint": False,
        "microJointWidth": 2.0,
        "microJointCount": 2,
        "toolDiameter": 0.0,
        "side": "auto",
        "depth": 3.0,
        "passDepth": 3.0,
        "feed": 1234.0,
        "plungeFeed": 456.0,
        "safeZ": 7.0,
        "liftZ": 7.0,
        "leadIn": False,
        "leadRadius": 2.0,
    }
    params.update(overrides)
    return params


def lines_of(text: str) -> List[str]:
    return [line for line in text.splitlines() if line]


def any_line(lines: List[str], pattern: str) -> bool:
    return any(re.search(pattern, line) for line in lines)


# ---------------------------------------------------------------- 服务层
def service_tests() -> Optional[str]:
    """返回自定义后处理 id（调用方负责删除）。"""
    import numpy as np

    from app.models.geometry import GeometryDoc, Layer, Polyline
    from app.post import registry
    from app.post.dxf_writer import count_dxf_polylines, write_dxf
    from app.post.registry import PostError
    from app.post.renderer import RenderContext, render
    from app.post.service import safe_name
    from app.services.toolpath_service import build_toolpath

    print("== A. 服务层：后处理配置与渲染 ==")
    registry.ensure_builtin()

    # --- A1 内置注册表
    again = registry.ensure_builtin()
    processors = registry.list_processors()
    builtin_ids = set(registry.builtin_ids())
    ids = {p["id"] for p in processors}
    files = list(registry.post_dir().glob("*.json"))
    check("A1 内置注册表",
          again == 0 and builtin_ids <= ids and len(builtin_ids) >= 6
          and len(files) >= len(builtin_ids)
          and all(p["kind"] in ("gcode", "hpgl", "dxf") for p in processors),
          "%d 份配置 / %d 份内置 / 重复落盘 %d" % (
              len(processors), len(builtin_ids), again))

    plan = build_toolpath(base_params(
        [contour(1, square(0, 0, 100, 60))], depth=3.0, passDepth=3.0))
    ctx = RenderContext(name="POST_TEST", safe_z=7.0, spindle=15000.0, feed=1234.0)

    # --- A2 维宏 G 代码
    weihong = registry.get_processor("weihong_nc")
    text = render(plan, weihong, ctx)
    lines = lines_of(text)
    check("A2 维宏 G 代码",
          lines[0] == "%" and any_line(lines, r"^G21$") and any_line(lines, r"^G90$")
          and any_line(lines, r"^G40$") and any_line(lines, r"^G0 Z7$")
          and any_line(lines, r"^M3 S15000$") and any_line(lines, r"^G1 X")
          and any_line(lines, r"^F1234$") and any_line(lines, r"^M30$"),
          "%d 行 / 头 %s / 尾 %s" % (len(lines), lines[:3], lines[-3:]))

    # --- A3 行号与换刀（新代模板：行号开 + T 指令）
    syntec = registry.get_processor("syntec_nc")
    text = render(plan, syntec, ctx_tool := RenderContext(
        name="POST_TEST", safe_z=7.0, spindle=15000.0, feed=1234.0, tool=7))
    lines = lines_of(text)
    numbered = [line for line in lines if line.startswith("N")]
    check("A3 行号与换刀",
          numbered and numbered[0].startswith("N10 ")
          and any_line(lines, r"T7 M6$") and any_line(lines, r"G43 H7$")
          and lines[0] == "%" and any_line(lines, r"^N10 O0001$"),
          "带行号 %d 行 / 首行 %s" % (len(numbered), numbered[0] if numbered else "无"))

    # --- A4 小数精度（自定义 2 位小数）
    payload = json.loads(json.dumps(weihong))
    payload.update({"id": "post_test_2dp", "name": "验收 2 位小数", "builtin": False})
    payload["gcode"]["precision"] = 2
    payload["gcode"]["lineNumbers"] = False
    custom = registry.save_processor(payload)
    plan2 = build_toolpath(base_params([contour(1, square(0, 0, 33.333, 20))]))
    text2 = render(plan2, custom, ctx)
    check("A4 小数精度",
          "X33.33" in text2 and "X33.333" not in text2,
          "precision=2 -> %s" % next((l for l in text2.splitlines() if "X33" in l), ""))

    # --- A5 HPGL 绘图仪
    plotter = registry.get_processor("plotter_plt")
    text = render(plan, plotter, RenderContext(
        name="POST_TEST", safe_z=0.0, spindle=0.0, feed=0.0))
    lines = lines_of(text)
    check("A5 HPGL",
          lines[0] == "IN;" and lines[1] == "SP1;" and lines[-1] == "SP0;"
          and any_line(lines, r"^PU-?\d+,-?\d+;$")
          and any_line(lines, r"^PD-?\d+,-?\d+;$")
          and "PD4000,2400;" in lines,
          "%d 条指令 / 首尾 %s ... %s" % (len(lines), lines[:2], lines[-2:]))

    # --- A6 CUT 速度压力
    cut = registry.get_processor("cutting_cut")
    text = render(plan, cut, RenderContext(
        name="POST_TEST", safe_z=0.0, spindle=0.0, feed=0.0, speed=25, force=30))
    check("A6 CUT 速度压力",
          "VS25;" in text and "FS30;" in text and "SP0;" in text,
          "VS25 / FS30 / SP0")

    # --- A7 Y 轴翻转：取一条 y=10 的直边，翻转后应为 -400
    plan3 = build_toolpath(base_params(
        [contour(2, [[0, 0], [10, 0], [10, 10], [0, 10]])]))
    normal = render(plan3, plotter, RenderContext(
        name="POST_TEST", safe_z=0.0, spindle=0.0, feed=0.0))
    flipped = render(plan3, plotter, RenderContext(
        name="POST_TEST", safe_z=0.0, spindle=0.0, feed=0.0, flip_y=True))
    check("A7 Y 轴翻转",
          "PD0,400;" in normal and "PD0,-400;" in flipped
          and "PD0,-400;" not in normal,
          "正常 PD0,400 / 翻转 PD0,-400")

    # --- A8 DXF 写回
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "post_test.dxf"
        doc = GeometryDoc(
            layers=[Layer(1, "切割", 0x4FA3FF), Layer(2, "划槽", 0xFFB74F)],
            polylines=[
                Polyline(1, 1, np.array([[0, 0], [10, 0], [10, 5], [0, 5]],
                                        dtype=np.float32), closed=True),
                Polyline(2, 2, np.array([[2, 2], [8, 2], [8, 3]], dtype=np.float32)),
            ])
        written = write_dxf(path, doc)
        read_back = count_dxf_polylines(path)
        size = path.stat().st_size
        check("A8 DXF 写回",
              written == 2 and read_back == 2 and size > 500,
              "%d 条写入 / %d 条读回 / %d 字节" % (written, read_back, size))

    # --- A9 配置校验
    unknown = dict(payload)
    unknown = json.loads(json.dumps(payload))
    unknown.update({"id": "post_test_bad", "name": "坏模板"})
    unknown["gcode"]["header"] = ["{bad_placeholder}"]
    rejected: List[str] = []
    for label, mutate in (
        ("未知占位符", lambda: registry.save_processor(unknown)),
        ("非法 kind", lambda: registry.save_processor({
            "id": "post_test_kind", "name": "x", "kind": "pdf", "ext": "pdf"})),
        ("内置不可改", lambda: registry.save_processor(weihong, processor_id="weihong_nc")),
        ("内置不可删", lambda: registry.remove_processor("weihong_nc")),
    ):
        try:
            mutate()
        except PostError:
            rejected.append(label)
    check("A9 配置校验", len(rejected) == 4,
          "已拒绝：%s" % "、".join(rejected))

    # --- A10 占位符填充 + 文件名清洗
    lines = lines_of(render(plan, weihong, ctx))
    check("A10 占位符填充",
          any_line(lines, r"BTCAM 2D CONTOUR - POST_TEST")
          and any_line(lines, r"Generated \d{4}-\d{2}-\d{2}")
          and any_line(lines, r"^G0 Z7$") and any_line(lines, r"^M3 S15000$")
          and safe_name('a/b:c*?"<>|') == "a_b_c______",
          "name/date/safeZ/spindle 全部生效")
    return "post_test_2dp"


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


def api_custom_payload() -> Dict[str, Any]:
    return {
        "id": "post_test_api",
        "name": "验收 API 后处理",
        "kind": "gcode",
        "ext": "nc",
        "description": "接口验收用，测试结束删除",
        "gcode": {
            "precision": 2,
            "lineNumbers": True,
            "lineNumberStart": 10,
            "lineNumberStep": 10,
            "programNo": "0042",
            "header": ["%", "O{programNo}", "( {name} )", "G21", "G90",
                       "G0 Z{safeZ}", "M3 S{spindle}"],
            "footer": ["G0 Z{safeZ}", "M5", "M30", "%"],
            "toolChange": ["T{tool} M6"],
            "rapidZ": "G0 Z{z}",
            "rapidXY": "G0 X{x} Y{y}",
            "plungeZ": "G1 Z{z} F{feed}",
            "feed": "F{feed}",
            "cutXY": "G1 X{x} Y{y}",
        },
    }


def rest_tests(base: str) -> None:
    from app.models import db
    from app.post.dxf_writer import count_dxf_polylines

    print("== B. 接口：后处理 CRUD 与导出 ==")
    square_contour = [contour(1, square(0, 0, 100, 60))]
    created: List[Path] = []
    touched_part: Optional[Tuple[int, int, Dict[str, Any]]] = None
    try:
        # --- B1 列表
        listing = http_json("GET", base + "/api/post")
        items = listing["processors"]
        kinds = {row["kind"] for row in items}
        check("B1 后处理列表",
              len(items) >= 6 and {"gcode", "hpgl", "dxf"} <= kinds
              and all("gcode" in row or "hpgl" in row or "dxf" in row for row in items),
              "%d 份 / kinds=%s" % (len(items), ",".join(sorted(kinds))))

        # --- B2 自定义 CRUD
        created_item = http_json("POST", base + "/api/post", api_custom_payload())
        single = http_json("GET", base + "/api/post/post_test_api")
        updated_payload = api_custom_payload()
        updated_payload.update({"name": "验收 API 后处理-改", "description": "已修改"})
        updated = http_json("PUT", base + "/api/post/post_test_api", updated_payload)
        removed = http_json("DELETE", base + "/api/post/post_test_api")
        gone = http_error_code("GET", base + "/api/post/post_test_api")
        check("B2 自定义 CRUD",
              created_item["id"] == "post_test_api"
              and single["gcode"]["precision"] == 2
              and updated["name"].endswith("-改")
              and removed.get("deleted") is True and gone == 404,
              "新建/读取/修改/删除 + 删除后 404")

        # --- B3 NC 导出（内置维宏）
        export = http_json("POST", base + "/api/export", {
            "contours": square_contour, "processorId": "weihong_nc",
            "name": "POST_TEST", "depth": 3.0, "passDepth": 3.0,
            "toolDiameter": 6.0,
        })
        path = Path(export["path"])
        created.append(path)
        content = path.read_text(encoding="utf-8")
        check("B3 NC 导出",
              path.suffix == ".nc" and "G21" in content and "G1 X" in content
              and "M30" in content and export["processor"] == "weihong_nc"
              and export["files"][0]["sheetIndex"] == 0,
              "%s（%d 字节）" % (path.name, path.stat().st_size))

        # --- B4 DXF 导出（工程图形）
        http_json("POST", base + "/api/demo", {})
        export = http_json("POST", base + "/api/export", {
            "processorId": "generic_dxf", "name": "POST_TEST_DXF",
        })
        path = Path(export["path"])
        created.append(path)
        contours = count_dxf_polylines(path)
        check("B4 DXF 导出",
              path.suffix == ".dxf" and contours > 0
              and export["stats"]["contourCount"] == contours,
              "%s（%d 条轮廓）" % (path.name, contours))

        # --- B5 批量多板：把一个摆放挪到第 2 块板
        http_json("POST", base + "/api/layout/open", {})
        with db.get_conn() as conn:
            project_id = db.get_active_project_id(conn)
            placements = db.load_placements(conn, project_id)
            part_id = sorted(placements)[0]
            row = dict(placements[part_id])
            db.save_placement(conn, project_id, part_id, 1,
                              float(row.get("tx", 0)), float(row.get("ty", 0)),
                              float(row.get("rot", 0)),
                              bool(row.get("locked", False)),
                              int(row.get("zorder", 0)),
                              bool(row.get("fixedRot", False)))
            touched_part = (project_id, part_id, row)
        export = http_json("POST", base + "/api/export", {
            "source": "layout", "allSheets": True,
            "processorId": "weihong_nc", "name": "POST_TEST_BATCH",
            "depth": 6.0, "passDepth": 3.0, "toolDiameter": 6.0,
        })
        files = export["files"]
        for item in files:
            created.append(Path(item["path"]))
        names = [Path(item["path"]).name for item in files]
        check("B5 批量多板",
              len(files) == 2
              and {item["sheetIndex"] for item in files} == {0, 1}
              and any("_S1." in name for name in names)
              and any("_S2." in name for name in names)
              and export["stats"].get("fileCount") == 2,
              "%d 个文件：%s" % (len(files), " / ".join(names)))

        # --- B6 未知后处理
        code = http_error_code("POST", base + "/api/export", {
            "contours": square_contour, "processorId": "no_such_processor",
        })
        check("B6 未知后处理", code == 400, "POST /api/export -> HTTP %d" % code)

        # --- B7 内置保护
        put_code = http_error_code("PUT", base + "/api/post/weihong_nc",
                                   api_custom_payload())
        del_code = http_error_code("DELETE", base + "/api/post/weihong_nc")
        check("B7 内置保护", put_code == 400 and del_code == 400,
              "修改 %d / 删除 %d" % (put_code, del_code))
    finally:
        # 恢复被挪到第 2 板的零件
        if touched_part is not None:
            project_id, part_id, row = touched_part
            with db.get_conn() as conn:
                db.save_placement(conn, project_id, part_id, 0,
                                  float(row.get("tx", 0)), float(row.get("ty", 0)),
                                  float(row.get("rot", 0)),
                                  bool(row.get("locked", False)),
                                  int(row.get("zorder", 0)),
                                  bool(row.get("fixedRot", False)))
        # 清掉本脚本产生的文件与自定义后处理
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        from app.post import registry
        for pid in ("post_test_api",):
            try:
                registry.remove_processor(pid)
            except Exception:
                pass


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="BtCAM 模块 7 后处理验收测试")
    parser.add_argument("--url", default="", help="后端地址；给定时追加接口测试")
    args = parser.parse_args()

    custom_id: Optional[str] = None
    try:
        custom_id = service_tests()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("服务层异常", False, str(exc))
    finally:
        if custom_id:
            try:
                from app.post import registry
                registry.remove_processor(custom_id)
            except Exception:
                pass

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
