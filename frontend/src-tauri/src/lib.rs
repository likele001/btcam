//! Tauri 主进程：窗口管理 + FastAPI 后端生命周期。
//!
//! 职责边界（刻意保持很薄）：
//!   - 窗口创建与安全策略由 tauri.conf.json 配置；
//!   - 几何/排料/刀路全部在 Python 后端，Rust 侧不做任何几何运算；
//!   - 只负责拉起与回收后端，保证"双击即用、关窗即清"。
//!
//! 两种运行模式：
//!   dev     (`cargo tauri dev`)   —— 用 backend/.venv 里的 Python 跑 `python -m app.main`，
//!                                    改 Python 代码只需重启 uvicorn，不必重编译 Rust；
//!   release (`cargo tauri build`) —— 后端 exe 已由 build.rs 编进主程序，运行时自解压到
//!                                    %LOCALAPPDATA%\BtCAM\bin\cam-backend-<hash>.exe 再隐藏拉起。
//!
//! 防残留三道保险：
//!   1. 退出事件里 taskkill /T /F 杀整棵进程树（PyInstaller onefile 是两层进程）；
//!   2. 启动时注入 BTCAM_PARENT_PID，后端 procguard 用 WaitForSingleObject 盯着主程序；
//!   3. 启动前探测端口，已有后端在跑就直接复用，不重复拉起也不误杀别人的进程。

use std::fs;
use std::io;
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent};

/// build.rs 生成：BACKEND_BYTES / BACKEND_SIZE / BACKEND_HASH
mod embedded {
    include!(concat!(env!("OUT_DIR"), "/embedded_backend.rs"));
}

/// 后端可执行文件名（与 scripts/build_sidecar.ps1 的 --name 一致）
const BACKEND_EXE: &str = "cam-backend.exe";
/// 后端端口，必须与 frontend/src/net/rest.ts 默认值一致
const BACKEND_PORT: u16 = 8975;
/// 父进程 PID 环境变量，必须与 backend/app/core/procguard.py 一致
const PARENT_PID_ENV: &str = "BTCAM_PARENT_PID";
/// 等待后端就绪的上限：PyInstaller 解包 + import OCCT 在慢机器上需要几秒
const READY_TIMEOUT: Duration = Duration::from_secs(60);

/// 后端进程状态。`owned=false` 表示端口上已有别人启动的后端，退出时不能杀。
struct BackendState {
    child: Option<Child>,
    owned: bool,
}

impl BackendState {
    fn new() -> Self {
        Self { child: None, owned: false }
    }
}

/// Windows 下隐藏控制台窗口，避免双击 exe 时闪出黑框
#[cfg(windows)]
fn no_window(cmd: &mut Command) -> &mut Command {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW)
}

#[cfg(not(windows))]
fn no_window(cmd: &mut Command) -> &mut Command {
    cmd
}

/// 端口探测：300ms 足够判断本机回环端口是否已被监听
fn port_busy() -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// 轮询等待后端监听端口（不阻塞窗口，调用方放独立线程）
fn wait_ready(timeout: Duration) -> bool {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if port_busy() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

/// 杀整棵进程树：PyInstaller `--onefile` 是"引导进程 + 真实进程"两层，
/// 只 kill 引导进程会留下孤儿，端口继续被占用。
fn kill_tree(child: &mut Child) {
    let pid = child.id();
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        no_window(&mut cmd);
        let _ = cmd.status();
    }
    // 非 Windows 或 taskkill 不可用时兜底；已退出的进程 kill 会返回错误，忽略即可
    let _ = child.kill();
    let _ = child.wait();
}

/// dev 模式：始终用本机 Python 跑源码，不依赖 sidecar 产物。
fn spawn_dev() -> io::Result<Child> {
    // src-tauri -> frontend -> btcam，再进 backend
    let backend_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("backend");
    let venv_python = backend_dir.join(".venv").join("Scripts").join("python.exe");
    let python = if venv_python.exists() {
        venv_python
    } else {
        // 回退到 PATH 里的 python；版本不符时后端日志会明确报错
        PathBuf::from("python")
    };

    let mut cmd = Command::new(&python);
    cmd.current_dir(&backend_dir)
        .args(["-m", "app.main"])
        .env("BTCAM_HOST", "127.0.0.1")
        .env("BTCAM_PORT", BACKEND_PORT.to_string())
        .env(PARENT_PID_ENV, std::process::id().to_string())
        .stdin(Stdio::null());
    // dev 保留终端输出，方便直接看 uvicorn 日志
    no_window(&mut cmd).spawn()
}

/// 把内嵌的 exe 写到本地数据目录。同哈希文件已存在且大小一致时直接复用，
/// 避免每次启动重复写 20+ MB。
fn extract_backend(dir: &Path) -> io::Result<PathBuf> {
    let stem = Path::new(BACKEND_EXE)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("cam-backend");
    let target = dir.join(format!("{stem}-{:016x}.exe", embedded::BACKEND_HASH));

    if target.exists() && fs::metadata(&target)?.len() as usize == embedded::BACKEND_SIZE {
        return Ok(target);
    }

    // 先写临时文件再改名：避免上次异常退出留下半截 exe 被下次直接复用
    let tmp = dir.join(format!("{stem}-{:016x}.tmp", embedded::BACKEND_HASH));
    fs::write(&tmp, embedded::BACKEND_BYTES)?;
    let _ = fs::remove_file(&target);
    fs::rename(&tmp, &target)?;
    println!("[btcam] 后端已解压到 {}", target.display());
    Ok(target)
}

/// release 模式：自解压 + 隐藏窗口拉起
fn spawn_release(app: &tauri::AppHandle) -> io::Result<Child> {
    if embedded::BACKEND_SIZE == 0 {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "本次构建未内嵌后端：先运行 scripts/build_sidecar.ps1 再 cargo tauri build",
        ));
    }

    let dir = app
        .path()
        .app_local_data_dir()
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?
        .join("bin");
    fs::create_dir_all(&dir)?;

    let exe = extract_backend(&dir)?;
    let mut cmd = Command::new(exe);
    cmd.env("BTCAM_HOST", "127.0.0.1")
        .env("BTCAM_PORT", BACKEND_PORT.to_string())
        .env(PARENT_PID_ENV, std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    no_window(&mut cmd).spawn()
}

/// 启动后端并把句柄交给状态机；就绪检测放后台线程，不阻塞窗口首帧。
fn start_backend(app: &tauri::AppHandle, state: &Mutex<BackendState>) {
    if port_busy() {
        println!("[btcam] 端口 {BACKEND_PORT} 已有后端在跑，直接复用（退出时不回收）");
        return;
    }

    let spawned = if cfg!(debug_assertions) {
        spawn_dev()
    } else {
        spawn_release(app)
    };

    let mut child = match spawned {
        Ok(child) => child,
        Err(err) => {
            eprintln!("[btcam] 后端启动失败: {err}");
            return;
        }
    };
    println!("[btcam] 后端已启动 pid={}", child.id());

    {
        let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
        guard.child = Some(child);
        guard.owned = true;
    }

    std::thread::spawn(|| {
        if wait_ready(READY_TIMEOUT) {
            println!("[btcam] 后端就绪 http://127.0.0.1:{BACKEND_PORT}");
        } else {
            eprintln!("[btcam] 后端 {} 秒内未就绪，请查看日志", READY_TIMEOUT.as_secs());
        }
    });
}

/// 退出时回收：只杀自己拉起的那个，端口复用场景不动别人的进程。
fn stop_backend(state: &Mutex<BackendState>) {
    let mut guard = state.lock().unwrap_or_else(|e| e.into_inner());
    if !guard.owned {
        return;
    }
    if let Some(mut child) = guard.child.take() {
        let pid = child.id();
        kill_tree(&mut child);
        println!("[btcam] 后端已回收 pid={pid}");
    }
    guard.owned = false;
}

/// 前端可 invoke('backend_endpoint') 拿到后端地址（将来支持改端口）
#[tauri::command]
fn backend_endpoint() -> String {
    format!("http://127.0.0.1:{BACKEND_PORT}")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(Mutex::new(BackendState::new()))
        .invoke_handler(tauri::generate_handler![backend_endpoint])
        .setup(|app| {
            let handle = app.handle().clone();
            let state = app.state::<Mutex<BackendState>>();
            start_backend(&handle, state.inner());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri 初始化失败");

    app.run(|app_handle, event| {
        // ExitRequested 覆盖"点关闭按钮"，Exit 覆盖"程序化退出"，两者都要回收
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            let state = app_handle.state::<Mutex<BackendState>>();
            stop_backend(state.inner());
        }
    });
}
