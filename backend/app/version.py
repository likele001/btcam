"""版本与发布信息（后端侧唯一事实来源）。

改版本号时要同步这四处，缺一处就会出现"关于页、安装包、健康检查"三处数字不一致：

    backend/app/version.py      ← 本文件（/api/health 与启动日志）
    frontend/src/version.ts     ← 关于页与 WS HELLO
    frontend/package.json
    frontend/src-tauri/tauri.conf.json / Cargo.toml（安装包与 exe 属性）

`SCHEMA_VERSION` 是**库结构**的版本号，与发布版本解耦：只有 _migrate 里新增
迁移步骤时 +1，用于排障时判断某个 btcam.db 被哪一版程序迁移过。
"""

from __future__ import annotations

__version__ = "0.4.0"

# 对外展示的版本标识（关于页、安装包说明里出现的字样）
EDITION = "V0.4 完整版"

PRODUCT = "BtCAM"

# 库结构版本：db._migrate 每完成一次结构变更就 +1
SCHEMA_VERSION = 4

# 内置后处理模板 / 默认刀具种子的版本随库结构一起走，
# 由 post.registry.ensure_builtin 与 db._seed_tool_library 各自判断是否覆盖。
