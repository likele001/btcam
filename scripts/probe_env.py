"""依赖探测：给 PowerShell 打包脚本输出单行 JSON。

为什么单独一个文件：
    PowerShell 5.1 里 `python -c "print(...)"` 的双引号会被命令行解析吃掉，
    内嵌字典/引号几乎必踩。放到 .py 里由 python 自己负责引号，PS 端只做 ConvertFrom-Json。
"""

import importlib.util
import json
import sys


def _find(module_name):
    """模块存在返回 True；不用 import，避免真正加载 OCCT 拖慢打包脚本。"""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


info = {
    "python": "%d.%d.%d" % sys.version_info[:3],
    # OCCT 的 Python 绑定有两个发行名：pythonocc-core → OCC，OCP → OCP
    "occt": "OCC" if _find("OCC") else ("OCP" if _find("OCP") else ""),
    "deepnest": _find("deepnest"),
    "pyclipper": _find("pyclipper"),
}
print(json.dumps(info))
