# BtCAM 打包说明（V0.4 完整版）

> 面向：负责出包的开发/运维。只要照本流程走，不需要改动任何业务代码。
> 部署与现场配置见 [DEPLOYMENT.md](DEPLOYMENT.md)。

---

## 1. 产物一览

一键打包（在项目根目录 `btcam\` 下执行）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

| 产物 | 路径 | 用途 |
| --- | --- | --- |
| 便携单文件（**主交付**） | `dist\BtCAM-0.4.0-portable.exe` | 后端已内嵌，拷贝即用，车间交付只用这个 |
| NSIS 安装包 | `dist\BtCAM-0.4.0-setup.exe` | 要开始菜单/固定安装目录时用；**当前用户安装**，不需要管理员 |
| MSI | `dist\BtCAM-0.4.0.msi` | 企业域部署（`msiexec /i` 静默安装） |

只出便携版（不打安装包，最省时间）：`scripts\build.ps1 -SkipBundle`
后端没改（复用现有 `cam-backend.exe`）：`scripts\build.ps1 -SkipSidecar`

---

## 2. 构建机环境

| 依赖 | 版本要求 | 检查命令 |
| --- | --- | --- |
| Node.js | 18+（本仓库实测 v23） | `node --version` |
| Rust | stable-msvc 1.77+ | `cargo --version` |
| VS Build Tools | 勾选「使用 C++ 的桌面开发」（提供 `link.exe`） | — |
| Python | 3.8（`backend\.venv`，由 `install_backend.ps1` 创建） | `backend\.venv\Scripts\python.exe --version` |
| PyInstaller | 6.x（脚本缺了会自动 `pip install`） | `backend\.venv\Scripts\python.exe -m PyInstaller --version` |

首次准备：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_backend.ps1   # 建 venv + 装后端依赖
cd frontend; npm install; cd ..
```

> 想缩短安装时间可加 `-SkipOcct`（不装 110MB 的 OCCT 绑定）。安装包里是否带 OCCT 由
> 打包时的 `-WithOcct` 决定，与 venv 里装没装无关，见 §4。

磁盘预留：`frontend\src-tauri\target` 约 3~5 GB（LTO + 单 codegen 单元），`backend\build` 约 200 MB。

---

## 3. 版本号固化：一条命令改 5 处

版本号分布在 5 个文件里，**必须同步**，否则会出现「关于页写 0.4.0、安装包属性写 0.1.0」：

| # | 文件 | 字段 | 作用 |
| --- | --- | --- | --- |
| 1 | `frontend/src/version.ts` | `APP_VERSION` / `APP_EDITION` | 关于页、状态栏角标、WS HELLO |
| 2 | `frontend/package.json` | `version` | 前端构建标识（`package-lock.json` 顶层同步） |
| 3 | `frontend/src-tauri/tauri.conf.json` | `version` | **安装包/exe 的文件版本**；`build.ps1` 用它给产物命名 |
| 4 | `frontend/src-tauri/Cargo.toml` | `package.version` | 主程序 crate 版本 |
| 5 | `backend/app/version.py` | `__version__` / `EDITION` | `/api/health` 返回的后端版本、启动日志 |

`backend/app/__init__.py` 的 `__version__` 是从 `app/version.py` 转发的，不用单独改。

`SCHEMA_VERSION`（在 `backend/app/version.py`）是**库结构**版本，与发布版本解耦：
只有 `db._migrate()` 里新增迁移步骤时才 +1。

改完自查：

```powershell
Select-String -Path frontend\src-tauri\tauri.conf.json,frontend\package.json,frontend\src-tauri\Cargo.toml -Pattern '"version"|^version'
```

V0.4 的取值：`0.4.0` + 展示名 `V0.4 完整版`。

---

## 4. 后端 sidecar：`scripts\build_sidecar.ps1`

六步：环境检查 → 依赖探测（`probe_env.py` 输出 JSON）→ PyInstaller `--onefile` → 产物校验 →
**启动自检**（用 8976 端口真跑一遍 `/api/health`）→ 注入 `frontend\src-tauri\backend\` 并打印 SHA256。

产物 `cam-backend.exe` 由 `build.rs` 用 `include_bytes!` 编进主程序，所以安装目录里不会再出现第二份后端。

### 4.1 排除冗余依赖（体积）

脚本对下列包统一加 `--exclude-module`。它们在后端代码里从未被 import，排除只在
「分析工具真的把它们带进来」时才起作用，不影响任何已用到的库：

```
tkinter/_tkinter/turtle/turtledemo、PIL、matplotlib/mpl_toolkits、
pandas/scipy/sklearn、PyQt5/PyQt6/PySide2/PySide6、
IPython/jupyter/notebook/nbformat/nbconvert、pytest/_pytest、pip/wheel
```

`vtkmodules`（OCCT 可视化依赖，40MB）只在 `-WithOcct` 时保留。

### 4.2 OCCT 是可选增强

| 打包方式 | 后端体积 | 几何引擎 | 何时用 |
| --- | --- | --- | --- |
| 默认（`build_sidecar.ps1`） | 约 25 MB（实测 25.29 MB） | pyclipper | **生产默认**。功能完整，圆弧按 0.02mm 弦高离散，精度够用 |
| `build_sidecar.ps1 -WithOcct` | 约 130 MB | OCCT（OCP） | 需要精确圆弧/NURBS、或客户指定 OCCT 时 |

原因：`cadquery-ocp` 的 `OCP` 是单个 110 MB 的扩展模块（不是包），默认不打包；
加上后 onefile 每次启动要多解包 110MB，安装包和启动耗时都会明显变大。
所以默认把「体积」放第一位，把 OCCT 做成显式开关。

`-WithOcct` 时脚本会自动改用 `--hidden-import OCP`（对单文件 `.pyd` 有效；
`--collect-submodules` 对它无效），并保留 `vtkmodules`。打完用自检行的
`几何引擎 = occt` 确认生效。

### 4.3 打包后的实测基线（本仓库，V0.4）

| 指标 | 值 |
| --- | --- |
| `cam-backend.exe` | **25.29 MB**（V0.4 实测，PyInstaller 65s） |
| 自检输出 | `/api/health` = ok，几何引擎 = `clipper`，排料引擎 = `shelf` |
| 后端版本 | `/api/health` 的 `version` = `0.4.0` |
| 主程序 `btcam.exe` | 前端 dist（index+css+react+pixi 共 ~0.9 MB，gzip 后 ~0.25 MB）+ Rust + 内嵌 sidecar |

体积核算（打包后核对 `backend\build\work\cam-backend\PYZ-00.toc`）：`pip`、`tkinter`、`matplotlib`、
`vtkmodules`、`OCP` 条目均为 0，说明排除列表生效；包内主体是 numpy（116 条目）、ezdxf（211 条目）、
uvicorn/websockets 与 Python 运行时。

> 安装包体积 ≈ 内嵌 sidecar + Rust 主程序，本质由 sidecar 决定。
> 想再压体积：只发便携版（便携版与安装包内是同一个 exe），或用默认包（不带 OCCT）。

---

## 5. Tauri 正式打包配置要点（`frontend/src-tauri/tauri.conf.json`）

| 配置 | 当前值 | 说明 |
| --- | --- | --- |
| `version` | `0.4.0` | 安装包/exe 文件版本，与 §3 同步 |
| `identifier` | `com.btsoft.btcam` | 决定 Tauri 应用数据目录 `%LOCALAPPDATA%\com.btsoft.btcam` |
| `app.windows[0].devtools` | `false` | **关闭 WebView 开发者工具**（右键「检查」不再可用） |
| `tauri` crate features | `[]`（`Cargo.toml`） | release 构建里根本不编译 devtools 代码 |
| `main.rs` | `#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]` | 非调试构建不弹控制台黑窗 |
| `backend` 拉起点 | `CREATE_NO_WINDOW` + stdout/stderr 丢弃 | sidecar 也不弹窗 |
| `app.security.csp` | 生产 CSP（无 `unsafe-eval`，仅放行 `127.0.0.1:8975`） | 与 `devCsp` 分离；开发态才允许 Vite 5173 与 eval |
| `bundle.resources` | `[]` | 故意留空：后端已内嵌，避免安装目录多一份 exe |
| `bundle.windows.webviewInstallMode` | `embedBootstrapper` + `silent` | 目标机缺 WebView2 时静默补装（随包携带引导程序） |
| `bundle.windows.nsis.installMode` | `currentUser` | 装到用户目录，**不需要管理员权限** |
| `bundle.windows.nsis.compression` | `lzma` | 安装包压到最小 |
| `[profile.release]` | `opt-level=3 / lto / codegen-units=1 / panic=abort / strip=symbols` | 体积与速度取舍见 `Cargo.toml` 注释 |

关闭开发者工具后，排障入口是**软件内部的日志面板**：工具栏「日志…」。

---

## 6. 打包步骤（照抄即可）

```powershell
# 0) 关掉正在运行的 btcam.exe / cam-backend.exe（否则链接报 LNK1104）
Get-Process btcam,cam-backend -ErrorAction SilentlyContinue | Stop-Process -Force

# 1) 版本号 5 处同步（见 §3）

# 2) 后端 + 前端 + Tauri + 汇总产物
powershell -ExecutionPolicy Bypass -File scripts\build.ps1

# 3) 验收（见 §7）
```

分步执行（便于定位问题）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1   # 只打后端并自检
cd frontend; npm run build; cargo tauri build --bundles nsis,msi      # 只打主程序/安装包
```

---

## 7. 出包验收清单

| # | 检查项 | 期望 |
| --- | --- | --- |
| 1 | `scripts\build_sidecar.ps1` 第 4 步自检 | `/api/health` = ok，打印几何/排料引擎 |
| 2 | `dist\` 三个文件都在 | portable / setup / msi，文件名带 `0.4.0` |
| 3 | 双击便携版 | 无控制台黑窗；3 秒内出现主界面，状态栏「已连接」 |
| 4 | 工具栏「关于…」 | 显示 `V0.4 完整版`、版本号 `0.4.0`、后端版本 `0.4.0`、数据目录 |
| 5 | 状态栏右下角 | `V0.4 完整版` 角标 |
| 6 | 全新机器首次启动 | 数据目录自动创建；刀具库有 3 把默认刀、工艺模板 3 条；后处理列表有 6 个内置模板 |
| 7 | 右键主界面 | **不出现**「检查/Inspect」菜单项 |
| 8 | 关闭窗口 | 任务管理器里没有残留 `cam-backend.exe` |
| 9 | 覆盖安装上一版 | 打开旧工单不报错，列表显示件数，工艺/刀具不丢 |

---

## 8. 常见失败与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `cargo: 无法识别` | 未装 Rust 或 PATH 未刷新 | `winget install Rustlang.Rustup` → `rustup default stable-msvc` → 重开终端 |
| 链接错误 `LNK1104` | `btcam.exe`/`cam-backend.exe` 正被占用 | 结束进程后重试 |
| `link.exe not found` | VS Build Tools 缺 C++ 工作负载 | 装「使用 C++ 的桌面开发」 |
| NSIS 包缺失 | Tauri 首次打包要下载 NSIS | 联网重试；离线机器可先在有网机器打包 |
| MSI 缺失 | 需要 WiX（Tauri 自动下载） | 企业不用 MSI 时可忽略，`--bundles nsis` 即可 |
| PyInstaller 打包失败 | 杀软锁文件 / 依赖缺失 | `backend\.venv` 加白名单；`pip install -r backend\requirements.txt` |
| 打包成功但启动即退出 | 没内嵌后端 | 先跑 `build_sidecar.ps1`，再 `cargo tauri build` |
| 主界面一直「连接中」 | 8975 端口被占 / 后端启动超时 | 关掉占用进程；看 `%LOCALAPPDATA%\BtCAM\logs` 与软件内日志面板 |

---

## 9. 不要做的事

- **不要**把 `tauri.conf.json` 的 `bundle.resources` 改成包含 `backend/cam-backend.exe`：会让安装目录出现第二份后端。
- **不要**为了调试把 `devtools` 打开后出包（`tauri` crate 的 devtools feature 也不要在 release 打开）。
- **不要**手工改 `backend\build\dist\cam-backend.exe` 后跳过 `build.rs` 重新编译——主程序内嵌的是构建期读到的字节。
- **不要**在数据库已经产生业务数据后手工改表结构：迁移统一写在 `db._migrate()` 里，见 `DEPLOYMENT.md` §7。
