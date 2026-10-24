"""SQLite 本地存储层。

原则：
- 一次操作一个连接（sqlite3 打开成本极低），天然线程安全，避免跨线程共享连接；
- 顶点坐标以 float32 BLOB 落库，与二进制协议同构，读出即可直接渲染；
- 开启 WAL，读写互不阻塞（排料任务写结果时不卡 UI 读取）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

from app.config import settings
from app.version import SCHEMA_VERSION

# ---------------------------------------------------------------- 建表
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    note        TEXT    DEFAULT '',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS material (           -- 板材
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    width       REAL    NOT NULL,
    height      REAL    NOT NULL,
    thickness   REAL    NOT NULL DEFAULT 18.0,
    quantity    INTEGER NOT NULL DEFAULT 1,
    grain       INTEGER NOT NULL DEFAULT 0   -- 纹理方向: 0 无 1 横向 2 纵向
);

CREATE TABLE IF NOT EXISTS layer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    color       INTEGER NOT NULL DEFAULT 5243387,
    operation   TEXT    NOT NULL DEFAULT 'cut',
    visible     INTEGER NOT NULL DEFAULT 1,
    locked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS polyline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    part_id     INTEGER,                    -- 所属零件（null = 散线，如工艺线）
    layer_id    INTEGER NOT NULL,
    closed      INTEGER NOT NULL DEFAULT 0,
    is_hole     INTEGER NOT NULL DEFAULT 0, -- 1 = 内孔轮廓
    point_count INTEGER NOT NULL,
    bbox        TEXT    NOT NULL,           -- [minx,miny,maxx,maxy]
    pts         BLOB    NOT NULL            -- float32[N*2]
);
CREATE INDEX IF NOT EXISTS idx_polyline_project ON polyline(project_id);

-- 零件：一次 DXF 导入 = 一个零件；排料/刀路都以零件为操作单位
CREATE TABLE IF NOT EXISTS part (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    source      TEXT    DEFAULT '',
    bbox        TEXT    NOT NULL DEFAULT '[0,0,0,0]',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_part_project ON part(project_id);

CREATE TABLE IF NOT EXISTS tool (               -- 刀具库（模块 6）
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    category      TEXT    NOT NULL DEFAULT 'mill',  -- mill 铣刀 | wire 线切割钼丝
    tool_type     TEXT    NOT NULL DEFAULT 'flat',  -- flat | ball | v（wire 时忽略）
    diameter      REAL    NOT NULL DEFAULT 0.0,     -- 铣刀直径 mm
    tip_radius    REAL    NOT NULL DEFAULT 0.0,     -- 刀尖圆角 mm
    wire_diameter REAL    NOT NULL DEFAULT 0.0,     -- 钼丝丝径 mm
    discharge_gap REAL    NOT NULL DEFAULT 0.0,     -- 放电补偿间隙 mm
    feed          REAL    NOT NULL DEFAULT 3000,    -- mm/min
    plunge_feed   REAL    NOT NULL DEFAULT 800,
    spindle       REAL    NOT NULL DEFAULT 18000,
    pass_depth    REAL    NOT NULL DEFAULT 3.0,
    safe_z        REAL    NOT NULL DEFAULT 5.0,     -- 安全高度（快移）
    lift_z        REAL    NOT NULL DEFAULT 5.0,     -- 抬刀高度（收起切削层）
    direction     TEXT    NOT NULL DEFAULT 'climb', -- climb 顺铣 | conventional 逆铣
    note          TEXT    NOT NULL DEFAULT '',
    updated_at    REAL    NOT NULL DEFAULT 0
);

-- 工艺模板（模块 6）：一把刀 + 一组补偿/微连接参数，按物料分类一键加载
CREATE TABLE IF NOT EXISTS process_template (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    material    TEXT    NOT NULL DEFAULT '',
    tool_id     INTEGER REFERENCES tool(id) ON DELETE SET NULL,
    params      TEXT    NOT NULL DEFAULT '{}',
    note        TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_template_project ON process_template(project_id);

CREATE TABLE IF NOT EXISTS task_record (
    id          TEXT PRIMARY KEY,
    project_id  INTEGER,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    REAL NOT NULL DEFAULT 0,
    message     TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 排料板参数：一个工程一套（四边余量/间距随板材走，换板就是换这张表）
CREATE TABLE IF NOT EXISTS layout_sheet (
    project_id  INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    width       REAL NOT NULL DEFAULT 1220.0,
    height      REAL NOT NULL DEFAULT 2440.0,
    margin      REAL NOT NULL DEFAULT 10.0,   -- 四边预留余量
    gap         REAL NOT NULL DEFAULT 5.0,    -- 零件最小间距
    -- 模块 10：绑定的余料 id（0 = 规则整板）；非 0 时轮廓才是真正的板材边界
    scrap_id    INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

-- 零件摆料状态：tx/ty 是零件包围盒左下角在板材上的位置，rot 为逆时针角度
-- locked = 位置锁定（手动/自动都不搬动）；fixed_rot = 角度锁定（木纹方向，自动/手动都不旋转）
CREATE TABLE IF NOT EXISTS layout_placement (
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    part_id     INTEGER NOT NULL,
    sheet       INTEGER NOT NULL DEFAULT 0,
    tx          REAL NOT NULL DEFAULT 0.0,
    ty          REAL NOT NULL DEFAULT 0.0,
    rot         REAL NOT NULL DEFAULT 0.0,
    locked      INTEGER NOT NULL DEFAULT 0,
    fixed_rot   INTEGER NOT NULL DEFAULT 0,
    zorder      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, part_id)
);
CREATE INDEX IF NOT EXISTS idx_placement_project ON layout_placement(project_id);

-- 工单存档（模块 8）：一份工单 = 一份只读快照。
-- 快照以 JSON 落库（顶点用 base64 float32 压缩），与当前工程解耦：
-- 继续编辑当前工程不会改历史工单，打开旧工单也不会反向污染存档。
CREATE TABLE IF NOT EXISTS work_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    code        TEXT    NOT NULL DEFAULT '',   -- 工件编号/订单号（检索用）
    note        TEXT    NOT NULL DEFAULT '',
    snapshot    TEXT    NOT NULL,              -- JSON 快照
    stats       TEXT    NOT NULL DEFAULT '{}', -- 冗余统计，列表页不必解析快照
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workorder_project ON work_order(project_id);

CREATE TABLE IF NOT EXISTS work_order_file ( -- 加工文件归档（导出时自动登记）
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES work_order(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    format      TEXT    NOT NULL DEFAULT '',
    processor   TEXT    NOT NULL DEFAULT '',
    bytes       INTEGER NOT NULL DEFAULT 0,
    sheet_index INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workorder_file ON work_order_file(order_id);

-- 余料库（模块 10）：一次排板切割后剩下的料。
-- outline 存 JSON 多边形，坐标相对**自身包围盒左下角**（与板材坐标同构）；
-- width/height/area 由后端从轮廓算出并冗余存储，列表页不必解析轮廓。
CREATE TABLE IF NOT EXISTS scrap (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    code            TEXT    NOT NULL DEFAULT '',   -- 余料编号（贴标签/扫码）
    material        TEXT    NOT NULL DEFAULT '',
    thickness       REAL    NOT NULL DEFAULT 0.0,
    width           REAL    NOT NULL,              -- 包围盒宽 mm
    height          REAL    NOT NULL,              -- 包围盒高 mm
    area            REAL    NOT NULL DEFAULT 0.0,  -- 轮廓净面积 mm²
    outline         TEXT    NOT NULL,              -- JSON [[x,y],...]
    source          TEXT    NOT NULL DEFAULT 'manual', -- manual | layout
    source_order_id INTEGER NOT NULL DEFAULT 0,
    note            TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scrap_project ON scrap(project_id);

-- MES 工单缓存（模块 12）：从 MES 拉回来的单，按 remote_id 幂等 upsert。
-- project_id 只记录"哪张单是哪次拉进哪个工程的"，列表是全局的：
-- 同一张 MES 单在多个工程里被拉过仍然只有一行，避免重复归档。
CREATE TABLE IF NOT EXISTS mes_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL DEFAULT 0,
    remote_id   TEXT    NOT NULL,                 -- MES 单号
    name        TEXT    NOT NULL DEFAULT '',
    customer    TEXT    NOT NULL DEFAULT '',
    material    TEXT    NOT NULL DEFAULT '',
    thickness   REAL    NOT NULL DEFAULT 0.0,
    due_date    TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pulled', -- pulled 待推送 | archived 已归档
    part_count  INTEGER NOT NULL DEFAULT 0,
    parts       TEXT    NOT NULL DEFAULT '[]',     -- JSON 零件清单（已归一化）
    payload     TEXT    NOT NULL DEFAULT '{}',     -- 原始响应，排障用
    pulled_at   REAL    NOT NULL,
    archived_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mes_order_remote ON mes_order(remote_id);

-- MES 交互日志：测试连接 / 拉单 / 回传各一条，含尝试次数与状态码
-- （表名沿用旧版的 mes_push_log，升上来的库里已有历史记录）
CREATE TABLE IF NOT EXISTS mes_push_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL DEFAULT 0,
    action      TEXT    NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER NOT NULL DEFAULT 0,
    message     TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mes_push_log_order ON mes_push_log(order_id);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """上下文管理器：自动提交/回滚/关闭。"""
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """首次启动建表 + 写入默认工程（保证 UI 打开就有东西可画）。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM project").fetchone()
        if row["c"] == 0:
            now = time.time()
            cur = conn.execute(
                "INSERT INTO project(name, note, created_at, updated_at) VALUES (?,?,?,?)",
                ("未命名工程", "", now, now),
            )
            pid = cur.lastrowid
            # 默认板材 1220x2440（标准 4x8 尺板）
            conn.execute(
                "INSERT INTO material(project_id, name, width, height, thickness, quantity)"
                " VALUES (?,?,?,?,?,?)",
                (pid, "标准板 1220x2440", 1220.0, 2440.0, 18.0, 1),
            )
            for name, color, op in (
                ("切割", 0x4FA3FF, "cut"),
                ("划槽", 0xFFB74F, "engrave"),
                ("定位孔", 0x8BE38B, "drill"),
            ):
                conn.execute(
                    "INSERT INTO layer(project_id, name, color, operation) VALUES (?,?,?,?)",
                    (pid, name, color, op),
                )

        # 刀具库/工艺模板：每次启动幂等补齐，旧库升级也能拿到默认数据
        project_id = get_active_project_id(conn)
        _seed_tool_library(conn, project_id)


# ---------------------------------------------------------------- 行 <-> 对象
def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列：CREATE TABLE IF NOT EXISTS 不会给已有表加字段。

    模块1 新增 part_id / is_hole；用 PRAGMA 探测后 ALTER，幂等且不动旧数据。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(polyline)")}
    if "part_id" not in columns:
        conn.execute("ALTER TABLE polyline ADD COLUMN part_id INTEGER")
    if "is_hole" not in columns:
        conn.execute("ALTER TABLE polyline ADD COLUMN is_hole INTEGER NOT NULL DEFAULT 0")

    # 模块4 新增：木纹方向锁定（自动排料时该零件不允许旋转）
    placement_columns = {row["name"] for row in conn.execute("PRAGMA table_info(layout_placement)")}
    if "fixed_rot" not in placement_columns:
        conn.execute("ALTER TABLE layout_placement ADD COLUMN fixed_rot INTEGER NOT NULL DEFAULT 0")

    # 模块10 新增：排料板可绑定一块余料（旧库升级时补列，默认 0 = 整板）
    sheet_columns = {row["name"] for row in conn.execute("PRAGMA table_info(layout_sheet)")}
    if "scrap_id" not in sheet_columns:
        conn.execute("ALTER TABLE layout_sheet ADD COLUMN scrap_id INTEGER NOT NULL DEFAULT 0")

    # 模块12：旧版 mes_order 只有 parts JSON，没有冗余的 part_count。
    # 补列后用零件数量回填一次，列表页不必为了显示件数去解 JSON。
    mes_columns = {row["name"] for row in conn.execute("PRAGMA table_info(mes_order)")}
    if mes_columns and "part_count" not in mes_columns:
        conn.execute("ALTER TABLE mes_order ADD COLUMN part_count INTEGER NOT NULL DEFAULT 0")
        for row in conn.execute("SELECT id, parts FROM mes_order").fetchall():
            try:
                parts = json.loads(row["parts"] or "[]")
            except (TypeError, ValueError):
                parts = []
            total = 0
            if isinstance(parts, list):
                for item in parts:
                    total += int(item.get("qty") or 1) if isinstance(item, dict) else 1
            conn.execute("UPDATE mes_order SET part_count = ? WHERE id = ?",
                         (total, int(row["id"])))

    # 模块10 余料表：早期版本用过“多环 rings + 使用状态 status”一套字段，
    # 现在改成“单轮廓 + 尺寸冗余”。SQLite 不能删列，而且旧的 rings 是 NOT NULL
    # 又没有默认值，直接插新行会失败，所以这里做一次整表重建（按 id 保留数据）。
    scrap_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scrap)")}
    if scrap_columns and "outline" not in scrap_columns:
        # 上次升级中断可能留下临时表，先清掉再重建（迁移必须可反复执行）
        conn.execute("DROP TABLE IF EXISTS scrap_upgrade")
        legacy = (conn.execute("SELECT id, rings FROM scrap").fetchall()
                  if "rings" in scrap_columns else [])
        conn.execute(
            "CREATE TABLE scrap_upgrade ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,"
            " name TEXT NOT NULL, code TEXT NOT NULL DEFAULT '',"
            " material TEXT NOT NULL DEFAULT '', thickness REAL NOT NULL DEFAULT 0.0,"
            " width REAL NOT NULL, height REAL NOT NULL, area REAL NOT NULL DEFAULT 0.0,"
            " outline TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT 'manual',"
            " source_order_id INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        # 旧库里可能有指向已删除工程的孤儿行，新表带外键，留下来反而会拖垮启动
        conn.execute(
            "INSERT INTO scrap_upgrade(id, project_id, name, code, material, thickness,"
            " width, height, area, outline, source, source_order_id, note, created_at, updated_at)"
            " SELECT id, project_id, name, '', COALESCE(material, ''), COALESCE(thickness, 0),"
            " width, height, COALESCE(area, 0), '[]', COALESCE(source, 'manual'),"
            " COALESCE(source_order_id, 0), COALESCE(note, ''), created_at, created_at FROM scrap"
            " WHERE project_id IN (SELECT id FROM project)"
        )
        conn.execute("DROP TABLE scrap")
        conn.execute("ALTER TABLE scrap_upgrade RENAME TO scrap")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scrap_project ON scrap(project_id)")
        for row in legacy:
            outline = _outline_from_rings(row["rings"])
            if outline:
                conn.execute("UPDATE scrap SET outline = ? WHERE id = ?", (outline, row["id"]))

    # 模块6 新增：刀具库字段（旧库只有 name/diameter/tool_type/feed/...）
    tool_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tool)")}
    for name, ddl in (
        ("category", "TEXT NOT NULL DEFAULT 'mill'"),
        ("tip_radius", "REAL NOT NULL DEFAULT 0"),
        ("wire_diameter", "REAL NOT NULL DEFAULT 0"),
        ("discharge_gap", "REAL NOT NULL DEFAULT 0"),
        ("safe_z", "REAL NOT NULL DEFAULT 5"),
        ("lift_z", "REAL NOT NULL DEFAULT 5"),
        ("direction", "TEXT NOT NULL DEFAULT 'climb'"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in tool_columns:
            conn.execute("ALTER TABLE tool ADD COLUMN %s %s" % (name, ddl))

    _migrate_work_order(conn)

    # 结构版本标记：排障时能一眼看出这个库被哪一版程序迁移过，
    # 与发布版本解耦（只在本函数新增迁移步骤时才 +1）
    set_setting(conn, "schema.version", str(SCHEMA_VERSION))
    set_setting(conn, "schema.migrated_at", "%.0f" % time.time())


def _migrate_work_order(conn: sqlite3.Connection) -> None:
    """工单存档（模块 8）旧库升级。

    早期版本的 work_order 只有 name/snapshot/created_at，检索用的 code/note、
    列表排序用的 updated_at、以及冗余统计 stats 都是后加的；work_order_file
    也只存了 path。`_summary()` 是直接下标取列的，缺列会抛 KeyError，
    所以这里必须补齐，保证老工单打开不报错、列表能正常显示件数。
    """
    order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(work_order)")}
    if order_columns:
        added_stats = False
        for name, ddl in (
            ("code", "TEXT NOT NULL DEFAULT ''"),
            ("note", "TEXT NOT NULL DEFAULT ''"),
            ("stats", "TEXT NOT NULL DEFAULT '{}'"),
            ("updated_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in order_columns:
                conn.execute("ALTER TABLE work_order ADD COLUMN %s %s" % (name, ddl))
                if name == "stats":
                    added_stats = True

        # 只在真正补了 stats 列时回填一次，避免每次启动都扫全表：
        # 老快照把统计埋在 JSON 的 stats 段里，提到列上列表页才能直接读
        if added_stats:
            for row in conn.execute("SELECT id, snapshot FROM work_order").fetchall():
                stats = _stats_from_snapshot(row["snapshot"])
                if stats:
                    conn.execute(
                        "UPDATE work_order SET stats = ? WHERE id = ?",
                        (json.dumps(stats, ensure_ascii=False), int(row["id"])),
                    )

        # updated_at 默认 0 会让列表显示 1970 年，用创建时间兜底
        conn.execute("UPDATE work_order SET updated_at = created_at WHERE updated_at <= 0")

    file_columns = {row["name"] for row in conn.execute("PRAGMA table_info(work_order_file)")}
    if file_columns:
        for name, ddl in (
            ("format", "TEXT NOT NULL DEFAULT ''"),
            ("processor", "TEXT NOT NULL DEFAULT ''"),
            ("bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("sheet_index", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in file_columns:
                conn.execute("ALTER TABLE work_order_file ADD COLUMN %s %s" % (name, ddl))


def _stats_from_snapshot(snapshot: Any) -> Dict[str, Any]:
    """从旧工单快照里取出统计段（早期只把它嵌在快照 JSON 里）。"""
    try:
        payload = json.loads(snapshot or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    stats = payload.get("stats")
    return stats if isinstance(stats, dict) and stats else {}


# ---------------------------------------------------------------- 刀具库（模块 6）
def _seed_tool_library(conn: sqlite3.Connection, project_id: int) -> None:
    """默认刀具与工艺模板：幂等补齐，不覆盖用户数据。

    模块 5 及以前的 tool 表只有铣刀，升级到模块 6 时只补一把默认钼丝；
    已有记录一律不动，避免把用户改过的刀重新盖掉。
    """
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM tool WHERE project_id = ?", (project_id,)).fetchone()["c"]
    now = time.time()
    if count == 0:
        defaults = [
            ("T1 平底 6mm", "mill", "flat", 6.0, 0.5, 0.0, 0.0,
             3000.0, 800.0, 18000.0, 3.0, 5.0, 5.0, "climb", "通用密度板开料"),
            ("T2 平底 3mm", "mill", "flat", 3.0, 0.3, 0.0, 0.0,
             2400.0, 600.0, 20000.0, 2.0, 5.0, 5.0, "climb", "精细轮廓/小孔"),
            ("W1 钼丝 0.2mm", "wire", "flat", 0.0, 0.0, 0.2, 0.03,
             800.0, 200.0, 0.0, 18.0, 5.0, 5.0, "conventional",
             "线切割：补偿量 = 丝径/2 + 放电间隙"),
        ]
        for row in defaults:
            conn.execute(
                "INSERT INTO tool(project_id, name, category, tool_type, diameter, tip_radius,"
                " wire_diameter, discharge_gap, feed, plunge_feed, spindle, pass_depth,"
                " safe_z, lift_z, direction, note, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, *row, now),
            )
    else:
        has_wire = conn.execute(
            "SELECT 1 FROM tool WHERE project_id = ? AND category = 'wire' LIMIT 1",
            (project_id,)).fetchone()
        if not has_wire:
            conn.execute(
                "INSERT INTO tool(project_id, name, category, tool_type, diameter, tip_radius,"
                " wire_diameter, discharge_gap, feed, plunge_feed, spindle, pass_depth,"
                " safe_z, lift_z, direction, note, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, "W1 钼丝 0.2mm", "wire", "flat", 0.0, 0.0, 0.2, 0.03,
                 800.0, 200.0, 0.0, 18.0, 5.0, 5.0, "conventional",
                 "线切割：补偿量 = 丝径/2 + 放电间隙", now),
            )

    tpl_count = conn.execute(
        "SELECT COUNT(*) AS c FROM process_template WHERE project_id = ?",
        (project_id,)).fetchone()["c"]
    if tpl_count > 0:
        _repair_default_wire_template(conn, project_id)
        return
    tools = conn.execute(
        "SELECT id, name, category FROM tool WHERE project_id = ? ORDER BY id",
        (project_id,)).fetchall()
    if not tools:
        return
    mill_id = next((int(t["id"]) for t in tools if t["category"] == "mill"), int(tools[0]["id"]))
    wire_id = next((int(t["id"]) for t in tools if t["category"] == "wire"), mill_id)
    now = time.time()
    templates = [
        ("密度板 18mm 标准", "密度板", mill_id,
         {"cutType": "auto", "side": "auto", "optimize": True,
          "microJoint": False, "microJointWidth": 2.0, "microJointCount": 2,
          "leadIn": True, "leadRadius": 2.0}, "标准开料"),
        ("多层板 18mm 微连接", "多层板", mill_id,
         {"cutType": "auto", "side": "auto", "optimize": True,
          "microJoint": True, "microJointWidth": 2.5, "microJointCount": 3,
          "leadIn": True, "leadRadius": 2.0}, "防零件窜动"),
        ("无纺布 钼丝", "无纺布", wire_id,
         {"cutType": "auto", "side": "auto", "optimize": True,
          "microJoint": False, "microJointWidth": 1.0, "microJointCount": 2,
          "leadIn": False, "leadRadius": 0.0}, "线切割薄料"),
    ]
    for name, material, tool_id, params, note in templates:
        conn.execute(
            "INSERT INTO process_template(project_id, name, material, tool_id, params, note,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, name, material, tool_id, json.dumps(params, ensure_ascii=False), note,
             now, now),
        )


def _repair_default_wire_template(conn: sqlite3.Connection, project_id: int) -> None:
    """修复早期种子：旧库没有钼丝刀时，内置线切割模板曾绑到铣刀上。

    只处理内置的“无纺布 钼丝”这一条，并且只有当它仍指向铣刀时才改绑；
    用户自建模板或已绑定钼丝的模板完全不动。
    """
    wire = conn.execute(
        "SELECT id FROM tool WHERE project_id = ? AND category = 'wire' ORDER BY id LIMIT 1",
        (project_id,)).fetchone()
    if wire is None:
        return
    row = conn.execute(
        "SELECT t.id, t.tool_id, o.category AS tool_category FROM process_template t"
        " LEFT JOIN tool o ON o.id = t.tool_id"
        " WHERE t.project_id = ? AND t.name = ?",
        (project_id, "无纺布 钼丝")).fetchone()
    if row is None or row["tool_category"] == "wire":
        return
    conn.execute(
        "UPDATE process_template SET tool_id = ?, updated_at = ? WHERE id = ?",
        (int(wire["id"]), time.time(), int(row["id"])),
    )


def row_to_project(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def save_polylines(conn: sqlite3.Connection, project_id: int, items: List[Tuple[int, bool, Any]]) -> int:
    """批量写轮廓。items = [(layerId, closed, ndarray[(N,2)]) ...]"""
    rows = []
    for layer_id, closed, pts in items:
        arr = np.ascontiguousarray(pts, dtype="<f4")
        bbox = (
            [float(arr[:, 0].min()), float(arr[:, 1].min()),
             float(arr[:, 0].max()), float(arr[:, 1].max())]
            if arr.size else [0.0, 0.0, 0.0, 0.0]
        )
        rows.append((project_id, layer_id, int(closed), int(arr.shape[0]),
                     json.dumps(bbox), arr.tobytes()))
    conn.executemany(
        "INSERT INTO polyline(project_id, layer_id, closed, point_count, bbox, pts)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def load_polylines(conn: sqlite3.Connection, project_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, part_id, layer_id, closed, is_hole, point_count, bbox, pts FROM polyline"
        " WHERE project_id = ? ORDER BY id",
        (project_id,),
    )
    out: List[Dict[str, Any]] = []
    for row in cur:
        arr = np.frombuffer(row["pts"], dtype="<f4").reshape(-1, 2)
        out.append({
            "id": row["id"],
            "part_id": row["part_id"],
            "layer_id": row["layer_id"],
            "closed": bool(row["closed"]),
            "is_hole": bool(row["is_hole"]),
            "bbox": json.loads(row["bbox"]),
            "pts": arr,
        })
    return out


# ---------------------------------------------------------------- 零件与导入
def clear_document(conn: sqlite3.Connection, project_id: int) -> None:
    """清空当前工程的轮廓与零件（重新导入时调用，图层保留）。

    零件换了，旧摆料记录就全部失效（part_id 指向不存在的零件），
    因此一并清理，否则排料模式会拿到幽灵零件。
    """
    conn.execute("DELETE FROM polyline WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM part WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM layout_placement WHERE project_id = ?", (project_id,))


def create_part(conn: sqlite3.Connection, project_id: int, name: str, source: str,
                bbox: List[float]) -> int:
    cur = conn.execute(
        "INSERT INTO part(project_id, name, source, bbox, created_at) VALUES (?,?,?,?,?)",
        (project_id, name, source, json.dumps(bbox), time.time()),
    )
    return int(cur.lastrowid)


def save_part_polylines(conn: sqlite3.Connection, project_id: int, part_id: int,
                        items: List[Tuple[int, bool, bool, Any]]) -> List[int]:
    """写单个零件的轮廓。items = [(layerId, closed, isHole, ndarray)]，返回自增 id 列表。"""
    ids: List[int] = []
    for layer_id, closed, is_hole, pts in items:
        arr = np.ascontiguousarray(pts, dtype="<f4")
        bbox = (
            [float(arr[:, 0].min()), float(arr[:, 1].min()),
             float(arr[:, 0].max()), float(arr[:, 1].max())]
            if arr.size else [0.0, 0.0, 0.0, 0.0]
        )
        cur = conn.execute(
            "INSERT INTO polyline(project_id, part_id, layer_id, closed, is_hole, point_count, bbox, pts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (project_id, part_id, layer_id, int(closed), int(is_hole), int(arr.shape[0]),
             json.dumps(bbox), arr.tobytes()),
        )
        ids.append(int(cur.lastrowid))
    return ids


def load_parts(conn: sqlite3.Connection, project_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, name, source, bbox, created_at FROM part WHERE project_id = ? ORDER BY id",
        (project_id,),
    )
    return [
        {"id": r["id"], "name": r["name"], "source": r["source"],
         "bbox": json.loads(r["bbox"]), "created_at": r["created_at"]}
        for r in cur
    ]


def load_layers(conn: sqlite3.Connection, project_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, name, color, operation, visible, locked FROM layer"
        " WHERE project_id = ? ORDER BY id",
        (project_id,),
    )
    return [dict(r) for r in cur]


def ensure_layer(conn: sqlite3.Connection, project_id: int, name: str,
                 color: int = 0x4FA3FF, operation: str = "cut") -> int:
    """按图层名取图层 id，不存在则新建。

    导入 DXF 时用：同一个图层名重复导入不会产生重复图层行，
    保证轮廓的 layer_id 在多次导入之间保持稳定。
    """
    row = conn.execute(
        "SELECT id FROM layer WHERE project_id = ? AND name = ? LIMIT 1",
        (project_id, name),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO layer(project_id, name, color, operation) VALUES (?,?,?,?)",
        (project_id, name, int(color), operation),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 排料（模块 3）
def load_sheet(conn: sqlite3.Connection, project_id: int) -> Dict[str, Any]:
    """读板材参数；没有记录时返回默认值（不落库，由 save_sheet 决定何时固化）。

    `scrapId` 非 0 表示当前板材是余料库里的某块余料，轮廓由余料服务提供，
    这里只带出 id，避免排料服务反向依赖余料模块。
    """
    row = conn.execute(
        "SELECT width, height, margin, gap, scrap_id FROM layout_sheet WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return {"width": 1220.0, "height": 2440.0, "margin": 10.0, "gap": 5.0,
                "scrapId": 0}
    return {"width": float(row["width"]), "height": float(row["height"]),
            "margin": float(row["margin"]), "gap": float(row["gap"]),
            "scrapId": int(row["scrap_id"] or 0)}


def save_sheet(conn: sqlite3.Connection, project_id: int, sheet: Dict[str, float]) -> None:
    conn.execute(
        "INSERT INTO layout_sheet(project_id, width, height, margin, gap, updated_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(project_id) DO UPDATE SET width=excluded.width, height=excluded.height,"
        " margin=excluded.margin, gap=excluded.gap, updated_at=excluded.updated_at",
        (project_id, float(sheet["width"]), float(sheet["height"]),
         float(sheet["margin"]), float(sheet["gap"]), time.time()),
    )


def set_sheet_scrap(conn: sqlite3.Connection, project_id: int, scrap_id: int) -> None:
    """绑定/解绑排料板的余料（0 = 规则整板）。

    只改 scrap_id，不动宽高：调用方（scrap_service.apply_to_sheet）负责先写尺寸。
    余料被删除时排料服务会调这里解绑，避免留下指向不存在轮廓的幽灵边界。
    """
    conn.execute(
        "UPDATE layout_sheet SET scrap_id = ?, updated_at = ? WHERE project_id = ?",
        (int(scrap_id), time.time(), project_id),
    )


def load_placements(conn: sqlite3.Connection, project_id: int) -> Dict[int, Dict[str, Any]]:
    """part_id -> 摆料状态。"""
    cur = conn.execute(
        "SELECT part_id, sheet, tx, ty, rot, locked, fixed_rot, zorder FROM layout_placement"
        " WHERE project_id = ? ORDER BY zorder, part_id",
        (project_id,),
    )
    return {
        int(r["part_id"]): {
            "partId": int(r["part_id"]), "sheet": int(r["sheet"]),
            "tx": float(r["tx"]), "ty": float(r["ty"]), "rot": float(r["rot"]),
            "locked": bool(r["locked"]), "fixedRot": bool(r["fixed_rot"]),
            "zorder": int(r["zorder"]),
        }
        for r in cur
    }


def save_placement(conn: sqlite3.Connection, project_id: int, part_id: int,
                   sheet: int, tx: float, ty: float, rot: float,
                   locked: bool = False, zorder: int = 0,
                   fixed_rot: bool = False) -> None:
    conn.execute(
        "INSERT INTO layout_placement(project_id, part_id, sheet, tx, ty, rot, locked, fixed_rot, zorder)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, part_id) DO UPDATE SET sheet=excluded.sheet,"
        " tx=excluded.tx, ty=excluded.ty, rot=excluded.rot,"
        " locked=excluded.locked, fixed_rot=excluded.fixed_rot, zorder=excluded.zorder",
        (project_id, int(part_id), int(sheet), float(tx), float(ty), float(rot),
         int(locked), int(fixed_rot), int(zorder)),
    )


def clear_placements(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute("DELETE FROM layout_placement WHERE project_id = ?", (project_id,))


def get_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """读一条应用配置（app_setting 是 key/value 表，模块 12 用它存 mes.config）。"""
    row = conn.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写一条应用配置（同 key 覆盖）。"""
    conn.execute(
        "INSERT INTO app_setting(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_active_project_id(conn: sqlite3.Connection) -> int:
    """当前工程：暂时取最近更新的一个，后续可换成 app_setting 里的 last_project。"""
    row = conn.execute("SELECT id FROM project ORDER BY updated_at DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else 1


def touch_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute("UPDATE project SET updated_at = ? WHERE id = ?", (time.time(), project_id))


def record_task(task_id: str, project_id: Optional[int], kind: str, status: str,
                progress: float = 0.0, message: str = "", finished: bool = False) -> None:
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM task_record WHERE id = ?", (task_id,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE task_record SET status=?, progress=?, message=?, finished_at=? WHERE id=?",
                (status, progress, message, time.time() if finished else None, task_id),
            )
        else:
            conn.execute(
                "INSERT INTO task_record(id, project_id, kind, status, progress, message, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (task_id, project_id, kind, status, progress, message, time.time()),
            )


# ---------------------------------------------------------------- 刀具库 CRUD（模块 6）
TOOL_COLUMNS = (
    "name", "category", "tool_type", "diameter", "tip_radius",
    "wire_diameter", "discharge_gap", "feed", "plunge_feed",
    "spindle", "pass_depth", "safe_z", "lift_z", "direction", "note",
)
TEMPLATE_COLUMNS = ("name", "material", "tool_id", "params", "note")


def load_tools(conn: sqlite3.Connection, project_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute("SELECT * FROM tool WHERE project_id = ? ORDER BY id", (project_id,))
    return [dict(row) for row in cur]


def load_tool(conn: sqlite3.Connection, project_id: int,
              tool_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM tool WHERE project_id = ? AND id = ?", (project_id, int(tool_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def insert_tool(conn: sqlite3.Connection, project_id: int,
                data: Dict[str, Any]) -> int:
    columns = ", ".join(TOOL_COLUMNS)
    marks = ", ".join("?" for _ in TOOL_COLUMNS)
    values = [data.get(name) for name in TOOL_COLUMNS]
    cur = conn.execute(
        "INSERT INTO tool(project_id, %s, updated_at) VALUES (?, %s, ?)" % (columns, marks),
        (project_id, *values, time.time()),
    )
    return int(cur.lastrowid)


def update_tool(conn: sqlite3.Connection, project_id: int, tool_id: int,
                data: Dict[str, Any]) -> bool:
    assignments = ", ".join("%s = ?" % name for name in TOOL_COLUMNS)
    values = [data.get(name) for name in TOOL_COLUMNS]
    cur = conn.execute(
        "UPDATE tool SET %s, updated_at = ? WHERE project_id = ? AND id = ?" % assignments,
        (*values, time.time(), project_id, int(tool_id)),
    )
    return cur.rowcount > 0


def delete_tool(conn: sqlite3.Connection, project_id: int, tool_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM tool WHERE project_id = ? AND id = ?", (project_id, int(tool_id)))
    return cur.rowcount > 0


def count_templates_using_tool(conn: sqlite3.Connection, project_id: int,
                               tool_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM process_template WHERE project_id = ? AND tool_id = ?",
        (project_id, int(tool_id)),
    ).fetchone()
    return int(row["c"])


def _template_row(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    try:
        data["params"] = json.loads(data.get("params") or "{}")
    except (TypeError, ValueError):
        data["params"] = {}
    return data


def load_templates(conn: sqlite3.Connection, project_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM process_template WHERE project_id = ? ORDER BY id", (project_id,))
    return [_template_row(row) for row in cur]


def load_template(conn: sqlite3.Connection, project_id: int,
                  template_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM process_template WHERE project_id = ? AND id = ?",
        (project_id, int(template_id)),
    ).fetchone()
    return _template_row(row) if row is not None else None


def insert_template(conn: sqlite3.Connection, project_id: int,
                    data: Dict[str, Any]) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO process_template(project_id, name, material, tool_id, params, note,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (project_id, data.get("name"), data.get("material", ""), data.get("tool_id"),
         json.dumps(data.get("params") or {}, ensure_ascii=False), data.get("note", ""),
         now, now),
    )
    return int(cur.lastrowid)


def update_template(conn: sqlite3.Connection, project_id: int, template_id: int,
                    data: Dict[str, Any]) -> bool:
    cur = conn.execute(
        "UPDATE process_template SET name=?, material=?, tool_id=?, params=?, note=?,"
        " updated_at=? WHERE project_id=? AND id=?",
        (data.get("name"), data.get("material", ""), data.get("tool_id"),
         json.dumps(data.get("params") or {}, ensure_ascii=False), data.get("note", ""),
         time.time(), project_id, int(template_id)),
    )
    return cur.rowcount > 0


def delete_template(conn: sqlite3.Connection, project_id: int, template_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM process_template WHERE project_id = ? AND id = ?",
        (project_id, int(template_id)))
    return cur.rowcount > 0


# ---------------------------------------------------------------- 工单存档（模块 8）
def insert_work_order(conn: sqlite3.Connection, project_id: int, name: str, code: str,
                      note: str, snapshot: str, stats: str) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO work_order(project_id, name, code, note, snapshot, stats, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (project_id, name, code, note, snapshot, stats, now, now),
    )
    return int(cur.lastrowid)


def update_work_order(conn: sqlite3.Connection, project_id: int, order_id: int,
                      name: str, code: str, note: str, snapshot: str, stats: str) -> bool:
    cur = conn.execute(
        "UPDATE work_order SET name=?, code=?, note=?, snapshot=?, stats=?, updated_at=?"
        " WHERE project_id=? AND id=?",
        (name, code, note, snapshot, stats, time.time(), project_id, int(order_id)),
    )
    return cur.rowcount > 0


def load_work_order(conn: sqlite3.Connection, project_id: int,
                    order_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM work_order WHERE project_id = ? AND id = ?",
        (project_id, int(order_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def list_work_orders(conn: sqlite3.Connection, project_id: int, keyword: str = "",
                     start: Optional[float] = None, end: Optional[float] = None,
                     limit: int = 100) -> List[Dict[str, Any]]:
    """按名称/编号/备注模糊检索 + 创建时间区间过滤，最近创建的在前。"""
    sql = (
        "SELECT o.*, (SELECT COUNT(*) FROM work_order_file f WHERE f.order_id = o.id)"
        " AS file_count FROM work_order o WHERE o.project_id = ?"
    )
    args: List[Any] = [project_id]
    if keyword:
        like = "%" + keyword + "%"
        sql += " AND (o.name LIKE ? OR o.code LIKE ? OR o.note LIKE ?)"
        args.extend([like, like, like])
    if start is not None:
        sql += " AND o.created_at >= ?"
        args.append(float(start))
    if end is not None:
        sql += " AND o.created_at < ?"
        args.append(float(end))
    sql += " ORDER BY o.created_at DESC, o.id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(row) for row in conn.execute(sql, args)]


def delete_work_order(conn: sqlite3.Connection, project_id: int, order_id: int) -> bool:
    conn.execute("DELETE FROM work_order_file WHERE order_id = ?", (int(order_id),))
    cur = conn.execute(
        "DELETE FROM work_order WHERE project_id = ? AND id = ?",
        (project_id, int(order_id)))
    return cur.rowcount > 0


def insert_work_order_file(conn: sqlite3.Connection, order_id: int, path: str, fmt: str,
                           processor: str, size: int, sheet_index: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO work_order_file(order_id, path, format, processor, bytes, sheet_index,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (int(order_id), path, fmt, processor, int(size), int(sheet_index), time.time()),
    )
    return int(cur.lastrowid)


def load_work_order_files(conn: sqlite3.Connection, order_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM work_order_file WHERE order_id = ? ORDER BY id DESC",
        (int(order_id),))
    return [dict(row) for row in cur]


# ---------------------------------------------------------------- 余料库（模块 10）
SCRAP_COLUMNS = (
    "name", "code", "material", "thickness", "width", "height",
    "area", "outline", "source", "source_order_id", "note",
)


def _ring_area(ring: Any) -> float:
    """鞋带公式算一个环的面积（只用于升级时挑外轮廓，不参与业务）。"""
    try:
        points = [(float(p[0]), float(p[1])) for p in ring]
    except (TypeError, ValueError, IndexError):
        return 0.0
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(0.5 * total)


def _outline_from_rings(text: Any) -> str:
    """旧版余料存的是多环轮廓（rings = [外轮廓, 内孔...]）。

    升级时挑面积最大的那个环作为余料边界，并把包围盒左下角平移到 (0,0)，
    与当前“轮廓坐标相对自身包围盒左下角”的契约对齐。
    """
    try:
        rings = json.loads(text or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(rings, list):
        return ""
    candidates = [ring for ring in rings if isinstance(ring, list) and len(ring) >= 3]
    if not candidates:
        return ""
    best = max(candidates, key=_ring_area)
    try:
        points = [(float(p[0]), float(p[1])) for p in best]
    except (TypeError, ValueError, IndexError):
        return ""
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    outline = [[round(x - min_x, 3), round(y - min_y, 3)] for x, y in points]
    return json.dumps(outline, ensure_ascii=False, separators=(",", ":"))


def _scrap_row(row: sqlite3.Row) -> Dict[str, Any]:
    """行 -> 字典：轮廓从 JSON 还原成点数组，其余字段原样。"""
    data = dict(row)
    try:
        data["outline"] = json.loads(data.get("outline") or "[]")
    except (TypeError, ValueError):
        data["outline"] = []
    return data


def insert_scrap(conn: sqlite3.Connection, project_id: int, data: Dict[str, Any]) -> int:
    now = time.time()
    marks = ", ".join("?" for _ in SCRAP_COLUMNS)
    cur = conn.execute(
        "INSERT INTO scrap(project_id, %s, created_at, updated_at) VALUES (?, %s, ?, ?)"
        % (", ".join(SCRAP_COLUMNS), marks),
        (project_id, *[data.get(name) for name in SCRAP_COLUMNS], now, now),
    )
    return int(cur.lastrowid)


def update_scrap(conn: sqlite3.Connection, project_id: int, scrap_id: int,
                 data: Dict[str, Any]) -> bool:
    assignments = ", ".join("%s = ?" % name for name in SCRAP_COLUMNS)
    cur = conn.execute(
        "UPDATE scrap SET %s, updated_at = ? WHERE project_id = ? AND id = ?" % assignments,
        (*[data.get(name) for name in SCRAP_COLUMNS], time.time(), project_id, int(scrap_id)),
    )
    return cur.rowcount > 0


def load_scrap(conn: sqlite3.Connection, project_id: int,
               scrap_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM scrap WHERE project_id = ? AND id = ?",
        (project_id, int(scrap_id)),
    ).fetchone()
    return _scrap_row(row) if row is not None else None


def list_scraps(conn: sqlite3.Connection, project_id: int, keyword: str = "",
                limit: int = 200) -> List[Dict[str, Any]]:
    """余料列表：名称/编号/物料模糊检索，最近入库的在前。"""
    sql = "SELECT * FROM scrap WHERE project_id = ?"
    args: List[Any] = [project_id]
    if keyword:
        like = "%" + keyword + "%"
        sql += " AND (name LIKE ? OR code LIKE ? OR material LIKE ?)"
        args.extend([like, like, like])
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [_scrap_row(row) for row in conn.execute(sql, args)]


def delete_scrap(conn: sqlite3.Connection, project_id: int, scrap_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM scrap WHERE project_id = ? AND id = ?", (project_id, int(scrap_id)))
    return cur.rowcount > 0


# ---------------------------------------------------------------- MES 对接（模块 12）

def _json_or(value: Any, default: Any) -> Any:
    """宽松解析 JSON 字段：脏数据退回默认值，不让列表页崩掉。"""
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _mes_order_row(row: sqlite3.Row) -> Dict[str, Any]:
    """行 -> 接口对象（camelCase，零件与原始响应直接从 JSON 还原）。"""
    data = dict(row)
    return {
        "id": int(data["id"]),
        "remoteId": data.get("remote_id") or "",
        "projectId": int(data.get("project_id") or 0),
        "name": data.get("name") or "",
        "customer": data.get("customer") or "",
        "material": data.get("material") or "",
        "thickness": float(data.get("thickness") or 0.0),
        "dueDate": data.get("due_date") or "",
        "status": data.get("status") or "pulled",
        "partCount": int(data.get("part_count") or 0),
        "parts": _json_or(data.get("parts"), []),
        "payload": _json_or(data.get("payload"), {}),
        "pulledAt": float(data.get("pulled_at") or 0.0),
        "archivedAt": (float(data["archived_at"])
                       if data.get("archived_at") is not None else None),
    }


def upsert_mes_order(conn: sqlite3.Connection, project_id: int,
                     data: Dict[str, Any]) -> Tuple[int, bool]:
    """按 MES 单号 upsert；返回 (本地 id, 是否新增)。

    重复拉单只刷新业务字段与 pulled_at，**不动 status/archived_at**：
    已经回传归档的单不应因为又拉了一次就回到"待推送"。
    """
    remote_id = str(data.get("remote_id") or "").strip()
    if not remote_id:
        raise ValueError("MES 工单缺少单号")
    now = time.time()
    parts = json.dumps(data.get("parts") or [], ensure_ascii=False)
    payload = json.dumps(data.get("payload") or {}, ensure_ascii=False)
    row = conn.execute(
        "SELECT id FROM mes_order WHERE remote_id = ?", (remote_id,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO mes_order(project_id, remote_id, name, customer, material, thickness,"
            " due_date, status, part_count, parts, payload, pulled_at, archived_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(project_id or 0), remote_id, data.get("name") or "",
             data.get("customer") or "", data.get("material") or "",
             float(data.get("thickness") or 0.0), data.get("due_date") or "",
             "pulled", int(data.get("part_count") or 0), parts, payload, now, None),
        )
        return int(cur.lastrowid), True
    conn.execute(
        "UPDATE mes_order SET project_id=?, name=?, customer=?, material=?, thickness=?,"
        " due_date=?, part_count=?, parts=?, payload=?, pulled_at=? WHERE id=?",
        (int(project_id or 0), data.get("name") or "", data.get("customer") or "",
         data.get("material") or "", float(data.get("thickness") or 0.0),
         data.get("due_date") or "", int(data.get("part_count") or 0), parts, payload,
         now, int(row["id"])),
    )
    return int(row["id"]), False


def load_mes_order(conn: sqlite3.Connection, order_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM mes_order WHERE id = ?", (int(order_id),)).fetchone()
    return _mes_order_row(row) if row is not None else None


def find_mes_order_by_remote(conn: sqlite3.Connection,
                            remote_id: str) -> Optional[Dict[str, Any]]:
    """按 MES 单号找本地缓存（导出后自动推送靠它预检）。"""
    row = conn.execute(
        "SELECT * FROM mes_order WHERE remote_id = ?", (str(remote_id).strip(),)).fetchone()
    return _mes_order_row(row) if row is not None else None


def list_mes_orders(conn: sqlite3.Connection, status: str = "",
                    limit: int = 100) -> List[Dict[str, Any]]:
    """MES 工单列表：status 为空返回全部，最近拉取的在前。"""
    sql = "SELECT * FROM mes_order"
    args: List[Any] = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY pulled_at DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return [_mes_order_row(row) for row in conn.execute(sql, args)]


def count_mes_orders(conn: sqlite3.Connection) -> Dict[str, int]:
    """设置页的"工单 / 已归档"计数。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) AS archived FROM mes_order"
    ).fetchone()
    return {"total": int(row["total"] or 0), "archived": int(row["archived"] or 0)}


def mark_mes_order_archived(conn: sqlite3.Connection, order_id: int,
                           stamp: Optional[float] = None) -> bool:
    """标记已回传归档；stamp 为空时用当前时间。"""
    cur = conn.execute(
        "UPDATE mes_order SET status = 'archived', archived_at = ? WHERE id = ?",
        (float(stamp if stamp is not None else time.time()), int(order_id)),
    )
    return cur.rowcount > 0


def insert_mes_log(conn: sqlite3.Connection, action: str, ok: bool,
                   message: str = "", attempts: int = 0, status_code: int = 0,
                   order_id: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO mes_push_log(order_id, action, ok, attempts, status_code, message, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (int(order_id or 0), action, int(bool(ok)), int(attempts), int(status_code),
         message or "", time.time()),
    )
    return int(cur.lastrowid)


def list_mes_logs(conn: sqlite3.Connection, order_id: int = 0,
                  limit: int = 100) -> List[Dict[str, Any]]:
    """交互日志，最新在前；order_id 非 0 时只看该单。"""
    sql = "SELECT * FROM mes_push_log"
    args: List[Any] = []
    if order_id:
        sql += " WHERE order_id = ?"
        args.append(int(order_id))
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return [
        {
            "id": int(row["id"]),
            "orderId": int(row["order_id"] or 0),
            "action": row["action"] or "",
            "ok": bool(row["ok"]),
            "attempts": int(row["attempts"] or 0),
            "statusCode": int(row["status_code"] or 0),
            "message": row["message"] or "",
            "createdAt": float(row["created_at"]),
        }
        for row in conn.execute(sql, args)
    ]
