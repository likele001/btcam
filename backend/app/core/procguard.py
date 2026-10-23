"""父进程守护：Tauri 主程序消失时，后端自杀，防止端口被残留进程占用。

为什么需要它（比只在 Rust 侧 kill 更可靠）：
    1. PyInstaller `--onefile` 产物是"引导进程 + 真实进程"两层结构，
       Rust 侧 Kill 掉引导进程后，真实进程可能继续存活（孤儿）；
    2. 主程序被任务管理器强制结束 / 崩溃时，Rust 的 RunEvent::Exit 根本不会执行；
    3. 开发态 `cargo tauri dev` 的 uvicorn 也是同样问题。

机制：由启动方通过环境变量 BTCAM_PARENT_PID 传入自己的 PID，本进程开一个
守护线程用 WaitForSingleObject 阻塞等待该进程句柄。Windows 的进程句柄在
目标进程结束时进入 signaled 状态，因此这里零轮询、零 CPU 占用。
句柄一 signaled 就 os._exit(0)：端口由操作系统回收。
"""

from __future__ import annotations

import os
import sys
import threading

# 环境变量名：Rust 侧 lib.rs 与 scripts/dev.ps1 必须写同一个
PARENT_PID_ENV = "BTCAM_PARENT_PID"


def watch_parent() -> bool:
    """启动守护线程；返回是否真正启动了守护。

    非 Windows、未设置环境变量、PID 非法、OpenProcess 失败（权限/进程已退出）
    都安静返回 False —— 手动 `python -m app.main` 调试时不应有任何副作用。
    """
    raw = os.environ.get(PARENT_PID_ENV)
    if not raw or sys.platform != "win32":
        return False

    try:
        parent_pid = int(raw)
    except ValueError:
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x0010_0000
    INFINITE = 0xFFFF_FFFF
    WAIT_FAILED = 0xFFFF_FFFF

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        return False

    def _wait() -> None:
        if kernel32.WaitForSingleObject(handle, INFINITE) != WAIT_FAILED:
            # 父进程已退出：硬退出，交给 OS 回收监听端口与句柄
            os._exit(0)

    # 必须是 daemon：主线程正常退出时不能被这个阻塞线程拖住
    threading.Thread(target=_wait, name="parent-watch", daemon=True).start()
    return True
