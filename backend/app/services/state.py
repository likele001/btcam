"""服务层进程内状态。

放"最近一次计算结果"这类跨模块共享的轻量数据，避免为了传一个对象去读数据库。
注意：仅在线程池模式下可靠（默认模式），进程池模式下各进程内存独立。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 最近一次刀路 plan（ToolpathPlan），供 /api/export 复用
LAST_PLAN: Optional[Any] = None

# 最近一次排料结果，供 UI 再次查询
LAST_NEST: Optional[Dict[str, Any]] = None
