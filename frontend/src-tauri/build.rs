//! 构建期：把 PyInstaller 打出的 `cam-backend.exe` 编进主程序。
//!
//! 为什么不用 `tauri.conf.json` 的 resources：
//!   resources 方式会在安装目录里多留一份 exe，既占空间，又能被单独启动、替换、删除；
//!   `include_bytes!` 让后端字节成为主程序的一部分，车间交付物才真的只有一条 exe。
//!
//! 产物：`$OUT_DIR/embedded_backend.rs`，由 `src/lib.rs` 用 `include!` 引入。
//! 若 `backend/cam-backend.exe` 不存在（只做前端联调），生成空占位以保留可编译性，
//! 运行时由 `spawn_release` 给出明确报错。

use std::fmt::Write as _;
use std::path::PathBuf;

/// FNV-1a 64 位：仅用于给解压出来的文件命名，避免每次启动重复写盘，不做安全用途。
fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in bytes {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

fn main() {
    // 先生成 Tauri 上下文（读取 tauri.conf.json / capabilities / 图标）
    tauri_build::build();

    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("缺少 CARGO_MANIFEST_DIR"));
    let backend = manifest.join("backend").join("cam-backend.exe");
    println!("cargo:rerun-if-changed={}", backend.display());

    let out_dir = PathBuf::from(std::env::var("OUT_DIR").expect("缺少 OUT_DIR"));
    let generated = out_dir.join("embedded_backend.rs");

    let source = if backend.exists() {
        let bytes = std::fs::read(&backend).expect("读取 cam-backend.exe 失败");
        let hash = fnv1a(&bytes);
        let mut src = String::new();
        // 路径用 raw string：Windows 反斜杠在普通字符串里要转义，这里省掉麻烦
        writeln!(src, "pub const BACKEND_BYTES: &[u8] = include_bytes!(r\"{}\");", backend.display())
            .expect("拼接 embedded_backend.rs 失败");
        writeln!(src, "pub const BACKEND_SIZE: usize = {};", bytes.len()).expect("拼接失败");
        writeln!(src, "pub const BACKEND_HASH: u64 = 0x{hash:016x};").expect("拼接失败");
        println!(
            "cargo:warning=已嵌入后端 {:.1} MB（hash={hash:016x}）",
            bytes.len() as f64 / 1_048_576.0
        );
        src
    } else {
        println!(
            "cargo:warning=未找到 {}，本次构建不含内嵌后端；发布前请先运行 scripts/build_sidecar.ps1",
            backend.display()
        );
        // 空占位：保持 cargo check / 前端联调可用，运行时由 spawn_release 报错兜底
        "pub const BACKEND_BYTES: &[u8] = &[];\npub const BACKEND_SIZE: usize = 0;\npub const BACKEND_HASH: u64 = 0;\n"
            .to_string()
    };

    std::fs::write(&generated, source).expect("写入 embedded_backend.rs 失败");
}
