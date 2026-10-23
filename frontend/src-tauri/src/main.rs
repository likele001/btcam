// Release 构建隐藏控制台窗口（Windows 下发布版不能弹黑框）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    btcam_lib::run()
}
