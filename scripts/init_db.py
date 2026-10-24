"""SQLite 库初始化与自检（离线脚本，不属于业务流程）。

后端每次启动都会自己建表/迁移（`app.models.db.init_db()`），正常安装不需要
手动跑这个脚本。它是给下面这些场景准备的：

    python scripts/init_db.py                     # 在默认数据目录建库并落盘内置模板
    python scripts/init_db.py --data-dir D:\\BtCAM  # 指定数据目录（部署到别的盘）
    python scripts/init_db.py --db D:\\tmp\\old.db  # 拿旧库副本演练升级，不动线上库
    python scripts/init_db.py --check             # 只读自检，不改任何数据

退出码：
    0  成功 / 自检通过
    1  自检发现缺表、缺列或种子数据缺失
    2  --check 时库文件不存在（全新机器：先跑一次不带 --check 的初始化即可）

注意：脚本只做"建表 + 迁移 + 补种子"，不会删除或覆盖任何已有业务数据。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402

# 建表脚本里出现的全部表；自检逐个核对，缺一张就说明初始化没跑完
REQUIRED_TABLES = (
    "project", "material", "layer", "polyline", "part", "tool", "process_template",
    "app_setting", "layout_sheet", "layout_placement",
    "work_order", "work_order_file", "scrap", "mes_order", "mes_push_log",
)

# 老库升级必须补出来的列（缺了会在业务查询里抛 KeyError）
REQUIRED_COLUMNS = {
    "polyline": ("part_id", "is_hole"),
    "layout_placement": ("fixed_rot",),
    "layout_sheet": ("scrap_id",),
    "tool": ("category", "wire_diameter", "discharge_gap", "safe_z"),
    "work_order": ("code", "note", "stats", "updated_at"),
    "work_order_file": ("format", "processor", "bytes", "sheet_index"),
    "mes_order": ("part_count",),
    "scrap": ("outline",),
}


def _apply_paths(data_dir: Path | None, db_file: Path | None) -> Path:
    """把目标库路径写回全局配置；返回最终库文件路径。

    `db.get_conn()` 每次连接时才读 settings.db_path，所以在这里改
    data_dir / db_name 就够了，不用改业务代码。
    """
    if db_file is not None:
        target = db_file.resolve()
        settings.data_dir = target.parent
        settings.db_name = target.name
    elif data_dir is not None:
        settings.data_dir = data_dir.resolve()
    return settings.db_path


def _print_summary() -> None:
    from app.models import db

    with db.get_conn() as conn:
        print("数据库文件 : %s" % settings.db_path)
        print("库结构版本 : %s" % (db.get_setting(conn, "schema.version") or "(未标记)"))
        print("迁移时间   : %s" % (db.get_setting(conn, "schema.migrated_at") or "-"))
        for table in ("project", "part", "polyline", "tool", "process_template",
                      "work_order", "work_order_file", "scrap", "mes_order"):
            count = conn.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()["c"]
            print("  %-16s %d 行" % (table, count))


def _run_init() -> int:
    from app.models import db
    from app.post import registry as post_registry

    settings.ensure_dirs()
    db.init_db()
    written = post_registry.ensure_builtin()
    print("初始化完成：建表/迁移已执行，内置后处理模板写入 %d 份。" % written)
    _print_summary()
    return 0


def _run_check() -> int:
    from app.models import db
    from app.post.builtin import BUILTIN

    target = settings.db_path
    if not target.exists():
        print("库文件不存在：%s" % target)
        print("全新机器上这是正常的，首次启动后端会自动建库；"
              "也可以先执行 python scripts/init_db.py 离线建库。")
        return 2

    problems: list[str] = []
    # 只读打开，避免自检时不小心给库加上 -wal/-shm 或触发迁移
    conn = sqlite3.connect("file:%s?mode=ro" % target.as_posix(), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in REQUIRED_TABLES:
            if table not in tables:
                problems.append("缺表 %s" % table)

        for table, columns in REQUIRED_COLUMNS.items():
            if table not in tables:
                continue
            present = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            for column in columns:
                if column not in present:
                    problems.append("缺列 %s.%s" % (table, column))

        if "app_setting" in tables:
            row = conn.execute(
                "SELECT value FROM app_setting WHERE key = 'schema.version'").fetchone()
            if row is None:
                problems.append("未写入 schema.version（旧库尚未被本版本迁移过）")

        if "tool" in tables:
            if conn.execute("SELECT COUNT(*) AS c FROM tool").fetchone()["c"] == 0:
                problems.append("刀具库为空（默认刀具未种子化）")
        if "process_template" in tables:
            if conn.execute("SELECT COUNT(*) AS c FROM process_template").fetchone()["c"] == 0:
                problems.append("工艺模板为空（默认模板未种子化）")
    finally:
        conn.close()

    # 内置后处理模板是 JSON 文件，不在库里。
    # 这里直接对 BUILTIN 清单而不调 list_processors()，因为后者会顺手把缺的文件写回磁盘，
    # 而 --check 的约定是“只读”。
    post_dir = settings.data_dir / "post"
    for item in BUILTIN:
        if not (post_dir / ("%s.json" % item["id"])).exists():
            problems.append("缺后处理模板 post/%s.json" % item["id"])

    if problems:
        print("自检发现 %d 个问题：" % len(problems))
        for item in problems:
            print("  - %s" % item)
        print("\n处理办法：执行 python scripts/init_db.py 补齐（不会覆盖已有业务数据）。")
        return 1

    print("自检通过：%d 张表、老库升级所需的列、默认刀具/模板、内置后处理模板齐全。"
          % len(REQUIRED_TABLES))
    _print_summary()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BtCAM SQLite 库初始化 / 自检（不改动已有业务数据）")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="数据目录，默认为 %%LOCALAPPDATA%%\\BtCAM")
    parser.add_argument("--db", type=Path, default=None,
                        help="指定库文件（优先于 --data-dir），适合拿旧库副本做升级演练")
    parser.add_argument("--check", action="store_true",
                        help="只读自检：核对表、列、种子数据与内置模板是否齐全")
    args = parser.parse_args()

    target = _apply_paths(args.data_dir, args.db)
    print("目标库：%s" % target)
    return _run_check() if args.check else _run_init()


if __name__ == "__main__":
    raise SystemExit(main())
