"""内置后处理模板（模块 7）。

后处理与刀路内核完全解耦：这里只是数据，不含任何运动计算。
新增机床不需要改刀路代码 —— 往 `%LOCALAPPDATA%/BtCAM/post/` 放一份
JSON 配置即可；内置模板首次启动时也会落盘到同一目录，便于直接查看/复制。

模板占位符（由 renderer.py 统一填充）：

    {name}       任务名/文件名
    {date}       生成时间
    {programNo}  程序号（默认 0001）
    {tool}       刀具号（未绑定刀具时用 1）
    {spindle}    主轴转速
    {safeZ}      安全高度
    {x} {y} {z}  坐标（按 precision 格式化）
    {feed}       进给 mm/min
    {speed}      刻绘速度档位
    {force}      刻绘压力档位

`kind` 决定用哪个渲染器：

    gcode  程序头/尾 + G0/G1 指令模板（维宏/新代/山龙等）
    hpgl   PU/PD 笔式绘图仪（PLT/CUT）
    dxf    直接用 ezdxf 写回图形（DxfRenderer）
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

BUILTIN_VERSION = 1

# 内置模板清单：顺序即前端下拉顺序
BUILTIN: Tuple[Dict[str, Any], ...] = (
    {
        "id": "weihong_nc",
        "name": "维宏 Weihong 雕刻机",
        "kind": "gcode",
        "ext": "nc",
        "description": "维宏 NcStudio：G21/G90/G17/G40，M3 开主轴，M5/M30 收尾",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "gcode": {
            "precision": 3,
            "lineNumbers": False,
            "lineNumberStart": 10,
            "lineNumberStep": 10,
            "programNo": "0001",
            "header": [
                "%",
                "( BTCAM 2D CONTOUR - {name} )",
                "( Generated {date} )",
                "G21",
                "G90",
                "G17",
                "G40",
                "G0 Z{safeZ}",
                "M3 S{spindle}",
            ],
            "footer": [
                "G0 Z{safeZ}",
                "M5",
                "M30",
                "%",
            ],
            "toolChange": [],
            "rapidZ": "G0 Z{z}",
            "rapidXY": "G0 X{x} Y{y}",
            "plungeZ": "G1 Z{z} F{feed}",
            "feed": "F{feed}",
            "cutXY": "G1 X{x} Y{y}",
        },
    },
    {
        "id": "syntec_nc",
        "name": "新代 Syntec 雕刻机",
        "kind": "gcode",
        "ext": "nc",
        "description": "新代系统：O 程序号 + G54 工件坐标 + M08/M09 冷却",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "gcode": {
            "precision": 3,
            "lineNumbers": True,
            "lineNumberStart": 10,
            "lineNumberStep": 10,
            "programNo": "0001",
            "header": [
                "%",
                "O{programNo}",
                "( BTCAM 2D CONTOUR - {name} )",
                "( Generated {date} )",
                "G17 G40 G49 G80",
                "G54",
                "G21",
                "G90",
                "G0 Z{safeZ}",
                "M08",
                "M3 S{spindle}",
            ],
            "footer": [
                "G0 Z{safeZ}",
                "M09",
                "M5",
                "M30",
                "%",
            ],
            "toolChange": [
                "T{tool} M6",
                "G43 H{tool}",
            ],
            "rapidZ": "G0 Z{z}",
            "rapidXY": "G0 X{x} Y{y}",
            "plungeZ": "G1 Z{z} F{feed}",
            "feed": "F{feed}",
            "cutXY": "G1 X{x} Y{y}",
        },
    },
    {
        "id": "shanlong_nc",
        "name": "山龙 Shanlong 雕刻机",
        "kind": "gcode",
        "ext": "nc",
        "description": "山龙系统：O 程序号 + G00/G01 两位写法 + M03/M05",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "gcode": {
            "precision": 3,
            "lineNumbers": False,
            "lineNumberStart": 10,
            "lineNumberStep": 10,
            "programNo": "0001",
            "header": [
                "%",
                "O{programNo}",
                "( {name} )",
                "( {date} )",
                "G21",
                "G90",
                "G17",
                "G00 Z{safeZ}",
                "M03 S{spindle}",
            ],
            "footer": [
                "G00 Z{safeZ}",
                "M05",
                "M30",
                "%",
            ],
            "toolChange": [
                "T{tool} M06",
            ],
            "rapidZ": "G00 Z{z}",
            "rapidXY": "G00 X{x} Y{y}",
            "plungeZ": "G01 Z{z} F{feed}",
            "feed": "F{feed}",
            "cutXY": "G01 X{x} Y{y}",
        },
    },
    {
        "id": "plotter_plt",
        "name": "绘图仪 HPGL（PLT）",
        "kind": "hpgl",
        "ext": "plt",
        "description": "笔式绘图仪：IN/SP1 初始化，PU 空走、PD 落笔，SP0 收尾",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "hpgl": {
            "unitsPerMm": 40.0,
            "header": ["IN;", "SP1;"],
            "footer": ["SP0;"],
            "penUp": "PU{x},{y};",
            "penDown": "PD{x},{y};",
            "penLift": "PU;",
        },
    },
    {
        "id": "cutting_cut",
        "name": "裁割设备 CUT（兼容 AlphaCAM）",
        "kind": "hpgl",
        "ext": "cut",
        "description": "刻绘/裁割一体机：HP-GL 基础上增加 VS 速度与 FS 压力",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "hpgl": {
            "unitsPerMm": 40.0,
            "header": ["IN;", "SP1;", "VS{speed};", "FS{force};"],
            "footer": ["SP0;"],
            "penUp": "PU{x},{y};",
            "penDown": "PD{x},{y};",
            "penLift": "PU;",
        },
    },
    {
        "id": "generic_dxf",
        "name": "通用 DXF 图形",
        "kind": "dxf",
        "ext": "dxf",
        "description": "按图层原样写回 DXF，便于第三方软件复核或二次排版",
        "builtin": True,
        "version": BUILTIN_VERSION,
        "dxf": {
            "version": "R2010",
        },
    },
)

BUILTIN_BY_ID: Dict[str, Dict[str, Any]] = {item["id"]: item for item in BUILTIN}
