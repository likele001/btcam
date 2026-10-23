"""PyInstaller 打包入口（Release sidecar 由 Tauri 主程序拉起）。

为什么单独一个文件：
1. `app/main.py` 是给 `uvicorn app.main:app` 用的，没有 freeze 适配；
   这里补上 multiprocessing 的 freeze_support，避免打包后子进程重复启动 Web 服务。
2. 这里还负责"父进程守护"：Tauri 主程序通过环境变量 BTCAM_PARENT_PID 传入自己的 PID，
   本进程在后台阻塞等待该句柄，主程序一退出（正常关闭 / 崩溃 / 被任务管理器结束）
   就立即 os._exit(0)。这是防止后端残留占端口的最后一道保险，
   比只在 Rust 侧 kill 更可靠。
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading


def _watch_parent() -> None:
    """守护父进程（仅 Windows + 由 Tauri 拉起时生效）。

    Windows 的进程句柄在进程结束时进入 signaled 状态，
    用 WaitForSingleObject 阻塞等待即可，无需轮询，CPU 占用为零。
    """
    raw = os.environ.get("BTCAM_PARENT_PID")
    if not raw or sys.platform != "win32":
        return
    try:
        parent_pid = int(raw)
    except ValueError:
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    WAIT_FAILED = 0xFFFFFFFF

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        return

    def _wait() -> None:
        result = kernel32.WaitForSingleObject(handle, INFINITE)
        if result != WAIT_FAILED:
            # 父进程已退出：直接终止，不做清理（端口由 OS 释放）
            os._exit(0)

    # 必须是 daemon：主线程正常退出时不能被这个阻塞线程拖住
    threading.Thread(target=_wait, name="parent-watch", daemon=True).start()


def _run() -> None:
    from app.main import main

    main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _watch_parent()
    # 打包后 uvicorn 的 reload 不可用，这里直接单进程启动
    sys.exit(_run())
