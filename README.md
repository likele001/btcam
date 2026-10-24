# BtCAM — Windows 桌面 2D 切割 CAM 软件骨架

对标 AlphaCAM 的 **2D 轮廓 CAM**：板材排板、轮廓刀路生成、配置驱动后处理（NC / PLT / CUT / DXF，含多板批量导出）。

> 本项目只做 2D 轮廓加工，**不含任何 3D 浮雕/曲面功能**。

- 桌面外壳：**Tauri 2.0**（Rust），Release 出**单文件便携 exe**（后端已内嵌），另附 NSIS / MSI
- 前端：**React 18 + TypeScript + Vite 6 + PixiJS 8**，只负责 UI、画布渲染、鼠标交互
- 后端：**Python FastAPI**，承担全部几何/排料/刀路重型计算
- 几何内核：**OCCT (OpenCASCADE)**，缺失时自动降级到 pyclipper → 纯 numpy
- 排料：**DeepNest**（外部进程桥接），缺失时用内置行排算法兜底
- 后处理：**JSON 配置驱动**（一台机床一份配置），内置维宏/新代/山龙/绘图仪/裁割/通用 DXF
- 存储：**SQLite**（WAL 模式），本地单文件数据库
- 工单：**本地快照存档**（图形/板材/摆料/刀具/刀路/后处理/加工文件路径），按名称与日期检索，可重复打开编辑
- 日志与告警：**内存环形缓冲**（默认 1000 条）+ 结构化查询接口；图形校验异常与任务失败即时弹窗，不靠用户盯控制台
- 余料管理（V0.2）：排板后的整块剩余区域入库，下一张工单可直接拿它当板材，在余料轮廓内继续排零件
- 后处理可视化编辑器（V0.3）：软件内改 G/HPGL/DXF 模板，边改边预览，保存即对导出生效
- MES 对接（V0.4，可选）：拉工单、回传用料统计与加工文件；离线自动旁路，不影响 CAM 任何功能
- 通信：**WebSocket 长连接 + 自定义二进制帧**，图形点数据零 JSON

---

## 一、核心设计原则

| 原则 | 落地方式 |
| --- | --- |
| 计算与界面彻底分离 | 前端不含任何几何算法；所有偏置、排料、刀路都在 Python 侧 |
| 大图形不走 JSON | 16 字节定长帧头 + BGC1 二进制几何块，meta 只放 taskId/进度等小字段 |
| 接口永不阻塞 | REST 只投递任务并立刻返回 `taskId`；进度与结果通过 WS 主动推送 |
| 单 exe 分发 | 后端用 PyInstaller 打成 `cam-backend.exe`，`build.rs` 用 `include_bytes!` 编进主程序；运行时自解压到用户数据目录再隐藏拉起，交付物真的只有一条 exe |
| 引擎可降级 | OCCT → pyclipper → numpy；DeepNest → 内置行排。缺依赖也能跑通全链路 |
| 模块解耦 | 协议层 / 传输层 / 渲染层 / 状态层 / 业务服务层各自独立，UI 只读 store |

数据流：

```
鼠标操作 ──> CanvasController(相机/拾取/框选) ──> zustand store ──> React UI
                    │
                    └──(二进制帧)──> FastAPI /ws ──> TaskQueue ──> OCCT/DeepNest
                                          │                          │
                                          └──── TASK_PROGRESS ───────┘
                                                TOOLPATH_PREVIEW / NEST_RESULT
```

---

## 二、目录结构

```
btcam/
├─ backend/                          # Python 计算服务
│  ├─ app/
│  │  ├─ main.py                     # FastAPI 入口 + lifespan（建库/日志/任务队列）
│  │  ├─ config.py                   # 全局配置（环境变量覆盖，端口 8975）
│  │  ├─ api/
│  │  │  ├─ ws.py                    # /ws 端点：二进制帧收发、全量图形下发
│  │  │  ├─ routes_project.py        # /api/health /api/project /api/demo /api/tasks（含取消）
│  │  │  ├─ routes_import.py         # /api/import/dxf（multipart 上传）/api/import/defaults
│  │  │  ├─ routes_geometry.py       # /api/geometry/*（模块 2 几何运算）
│  │  │  ├─ routes_layout.py         # /api/layout/*（模块 3 手动排料 + 模块 4 自动排料）
│  │  │  ├─ routes_tools.py          # /api/tools /api/templates（模块 6 刀具库与工艺模板）
│  │  │  ├─ routes_post.py           # /api/post（模块 7 CRUD + 模块 11 变量/校验/预览）
│  │  │  ├─ routes_workorders.py     # /api/workorders（模块 8 工单：快照/检索/恢复/另存）
│  │  │  ├─ routes_logs.py           # /api/logs（模块 9 日志查询/清空/前端异常上报）
│  │  │  ├─ routes_scrap.py          # /api/scrap（模块 10 余料库 / 剩余区域 / 设为板材）
│  │  │  ├─ routes_mes.py            # /api/mes（模块 12 配置/拉单/回传/日志，可选件）
│  │  │  └─ routes_cam.py            # /api/offset /api/nest /api/toolpath /api/export
│  │  ├─ core/
│  │  │  ├─ protocol.py              # ★ 二进制帧协议唯一真源（与前端 protocol.ts 对齐）
│  │  │  ├─ tasks.py                 # 异步任务队列（线程池/进程池可切；进度写日志）
│  │  │  ├─ log_store.py              # ★ 模块 9：日志环形缓冲 + ALERT 广播 + logging Handler
│  │  │  ├─ hub.py                   # WS 客户端注册与广播
│  │  │  ├─ procguard.py             # ★ 父进程守护：主程序消失即自杀，防端口残留
│  │  │  └─ logging_setup.py         # 控制台 + 滚动文件 + 内存缓冲（强制 UTF-8）
│  │  ├─ geometry/
│  │  │  ├─ base.py                  # GeometryEngine 协议 + 断线串接 + 引擎工厂
│  │  │  ├─ dxf_reader.py            # ★ ezdxf 读取：图元离散/块展开/单位换算/图层
│  │  │  ├─ repair.py                # ★ 拓扑修复：碎线/去重/串接/闭合/内外分类/自交
│  │  │  ├─ occt_engine.py           # OCCT 适配（兼容 OCC.Core / OCP 两套绑定）
│  │  │  ├─ clipper_engine.py        # pyclipper 兜底（含开放路径偏置）
│  │  │  └─ fallback_engine.py       # 纯 numpy 退化实现
│  │  ├─ services/
│  │  │  ├─ geometry_service.py      # 文档加载/持久化/示例图形/偏置任务
│  │  │  ├─ import_service.py        # ★ DXF 导入任务：读取→修复→拆零件→入库→推送
│  │  │  ├─ layout_service.py        # ★ 排料板：板材/摆放/越界与间距校验/轮廓变换
│  │  │  ├─ nest_service.py          # ★ 自动排料：底线扫描 + DeepNest 桥接（写回摆放）
│  │  │  ├─ nesting_service.py       # ShelfNester + DeepNestBridge + 通用排料任务
│  │  │  ├─ toolpath_service.py      # ★ 刀路：内外轮廓/补偿/方向/起割点/微连接/排序/分层
│  │  │  ├─ tool_service.py          # ★ 刀具库/工艺模板：校验/等效直径/参数优先级/顺逆铣映射
│  │  │  ├─ work_order_service.py    # ★ 工单：快照采集/恢复/检索/另存/加工文件归档
│  │  │  ├─ scrap_service.py         # ★ 模块10：剩余区域差集/余料入库/设为板材/解绑
│  │  │  ├─ export_service.py        # 导出兼容层：format -> 内置后处理映射（渲染已移到 post/）
│  │  │  └─ state.py                 # 最近一次刀路/排料结果的进程内缓存
│  │  ├─ post/                       # ★ 后处理（模块 7）：一台机床一份 JSON，渲染不碰几何
│  │  │  ├─ builtin.py               # 6 份内置模板：维宏/新代/山龙/绘图仪/裁割/通用 DXF
│  │  │  ├─ registry.py              # ★ 注册表：落盘/版本升级/占位符白名单/内置只读保护
│  │  │  ├─ renderer.py              # ★ G 代码 & HPGL 渲染（行号/精度/换刀/Y 翻转）
│  │  │  ├─ editor.py                # ★ 模块11：变量字典/逐字段校验/假刀路预览渲染
│  │  │  ├─ dxf_writer.py            # ezdxf 写回原始图形（图层 + true_color）
│  │  │  └─ service.py               # 文件名清洗/时间戳/多板 S1 标记/落盘
│  │  ├─ mes/                        # ★ 模块12（可选件）：离线时整包不参与 CAM 主链路
│  │  │  ├─ settings.py              # 配置（app_setting 的 mes.config）/ 密钥掩码 / 厂商路径
│  │  │  ├─ client.py                # urllib 客户端：Bearer+X-Api-Key / 指数退避 / multipart
│  │  │  └─ service.py               # 拉单归一化 / 用料统计 / 回传归档 / 导出后自动推送
│  │  └─ models/
│  │     ├─ geometry.py              # Polyline / Layer / GeometryDoc（numpy float32）
│  │     ├─ db.py                    # SQLite schema + CRUD（WAL）：含 scrap / mes_* 表
│  │     └─ schemas.py               # REST 请求/响应模型（camelCase）
│  ├─ requirements.txt               # 后端依赖清单
│  └─ sidecar.py                    # PyInstaller 打包入口（freeze_support + 起 uvicorn）
│
├─ frontend/                         # Tauri + React 前端
│  ├─ src/
│  │  ├─ net/
│  │  │  ├─ protocol.ts              # ★ 协议镜像（编码/解码/BGC1，必须与 Python 同步改）
│  │  │  ├─ ws-client.ts             # WS 客户端：心跳、指数退避重连、待发队列
│  │  │  ├─ rest.ts                  # REST 封装（camelCase 参数）
│  │  │  └─ session.ts               # 帧 → store 的唯一翻译层
│  │  ├─ render/
│  │  │  ├─ camera.ts                # 2D 相机：世界(mm)↔屏幕(px)、锚点缩放、自适应网格
│  │  │  ├─ viewport.ts              # CanvasController：平移/缩放/框选/起割点点选/叠加层
│  │  │  ├─ geometry-layer.ts        # Pixi Graphics 绘制（图层颜色/选中高亮/虚线/菱形标记）
│  │  │  └─ selection.ts             # bbox 排除 + 顶点包含 + Liang-Barsky 线段相交
│  │  ├─ state/app-store.ts          # zustand：连接/图形/选中/任务/视图模式
│  │  ├─ ui/                         # Toolbar / Viewport / 各面板 / ToolLibrary / PostPanel / PostEditor / ScrapPanel / MesPanel / WorkOrderPanel / LogPanel / AlertDialog / TaskPanel（动作层统一放 use*Actions.ts）
│  │  ├─ types/geometry.ts           # 前端几何类型
│  │  ├─ styles/global.css           # 深色工业风主题
│  │  ├─ App.tsx / main.tsx
│  ├─ src-tauri/
│  │  ├─ build.rs                    # ★ 把 cam-backend.exe 编进主程序（include_bytes!）
│  │  ├─ src/lib.rs                  # ★ 双模式启动 + 进程树回收（dev/release 分支）
│  │  ├─ src/main.rs
│  │  ├─ Cargo.toml                  # ★ Release 极致优化参数
│  │  ├─ tauri.conf.json             # 窗口/CSP/资源/NSIS 打包配置
│  │  └─ capabilities/default.json   # Tauri 2 权限
│  ├─ package.json
│  ├─ vite.config.ts
│  └─ tsconfig.json / tsconfig.node.json
│
└─ scripts/
   ├─ dev.ps1                        # 一键开发：后端 + Tauri dev（或仅 Vite）
   ├─ run_backend.ps1                # 只起后端（热重载）
   ├─ install_backend.ps1            # 建 venv + 装依赖 +（可选）装 OCCT
   ├─ build.ps1                      # 生产打包总入口
   ├─ build_sidecar.ps1              # ★ PyInstaller → cam-backend.exe（六步 + 启动自检）
   ├─ probe_env.py                   # 依赖探测（给打包脚本输出单行 JSON）
   ├─ gen_icons.ps1                  # 生成 PNG/ICO 占位图标（无设计稿时）
   ├─ dxf_import_test.py             # ★ 模块1 验收：修复算法 A1~A5 + 导入接口 B1~B4
   ├─ geometry_ops_test.py           # ★ 模块2 验收：几何服务 9 项 + 接口 7 项
   ├─ layout_test.py                 # ★ 模块3 验收：服务层 14 项 + 接口/WS 8 项
   ├─ auto_nest_test.py              # ★ 模块4 验收：服务层 11 项 + 接口/WS 8 项
   ├─ toolpath_test.py               # ★ 模块5 验收：服务层 9 项 + 接口/WS 4 项
   ├─ tool_library_test.py           # ★ 模块6 验收：服务层 12 项 + 接口/WS 7 项
   ├─ post_test.py                   # ★ 模块7 验收：服务层 10 项 + 接口 7 项
   ├─ workorder_test.py              # ★ 模块8 验收：服务层 7 项 + 接口 8 项
   ├─ log_alert_test.py              # ★ 模块9 验收：服务层 6 项 + 接口/WS 6 项
   ├─ scrap_test.py                  # ★ 模块10 验收：余料 38 项（剩余区域/入库/当板材/解绑）
   ├─ post_editor_test.py            # ★ 模块11 验收：编辑器 23 项（变量/校验/预览/改模板导出生效）
   ├─ mes_test.py                    # ★ 模块12 验收：MES 62 项（自带假 MES：配置/拉单/回传/离线/自动推送）
   └─ smoke_test.py                  # ★ 端到端自检：REST + WS 二进制 + 任务 + 三种导出
```

---

## 三、环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10 / 11 x64 | 目标平台 |
| Python | **3.11 推荐**，最低 3.8 | 3.8~3.12 均可（`requirements.txt` 用环境标记兼容） |
| Node.js | ≥ 20（实测 23.9） | 前端构建 |
| Rust | ≥ 1.77 + MSVC 工具链 | 仅 `tauri dev` / `tauri build` 需要 |
| WebView2 | 系统自带或安装包内嵌 | NSIS 用 `embedBootstrapper`，无网机器也能装 |

> 装 Rust：`winget install Rustlang.Rustup`，然后 `rustup default stable-msvc`。
> `cargo tauri` 子命令需要 `cargo install tauri-cli --version "^2"`；不想装也可以用 `npm run tauri`（走 `@tauri-apps/cli`，两者等价）。

---

## 四、快速开始

### 4.1 两条核心命令

| 场景 | 命令（在 `frontend\` 下执行） | 做什么 |
| --- | --- | --- |
| 本地调试 | `cargo tauri dev` | 编译 Rust 开发版；`lib.rs` 用 `backend\.venv\Scripts\python.exe -m app.main` 拉起后端，改 Python 只需重启后端 |
| 成品打包 | `cargo tauri build` | 出 Release 单文件 `btcam.exe`；后端 exe 已在构建期编入，双击即用 |

> 打包前必须先有 `frontend\src-tauri\backend\cam-backend.exe`。
> 它由 `scripts\build_sidecar.ps1` 生成，`scripts\build.ps1` 会自动串起来。
> 只想出便携 exe、不生成安装包：`cargo tauri build --no-bundle`。
>
> 完整的出包流程、版本号固化清单、验收用例见 **[docs/PACKAGING.md](docs/PACKAGING.md)**；
> 现场安装、首次启动注意事项、MES 配置、DeepNest 部署见 **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**。

### 4.2 首次准备（只做一次）

```powershell
# 1) 后端依赖（自动建 backend\.venv；加 -SkipOcct 可跳过 200MB 的 OCCT）
powershell -ExecutionPolicy Bypass -File scripts\install_backend.ps1 -SkipOcct

# 2) 前端依赖
cd frontend; npm install; cd ..
```

### 4.3 没有 Rust 环境时

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -NoTauri   # 后端 + Vite
# 浏览器打开 http://127.0.0.1:5173
```

`scripts\dev.ps1` 是便利入口（等价于“起后端 + `cargo tauri dev`”），
并向子进程注入 `BTCAM_PARENT_PID`：窗口一关，后端跟着退出。

### 验证后端是否正常

```powershell
# 端到端自检（需后端已启动）：REST + WS 二进制帧 + 刀路/排料任务 + 三种格式导出
backend\.venv\Scripts\python.exe scripts\smoke_test.py
```

期望输出（当前环境实测，OCCT 7.7.2 + shelf 引擎）：

```
== 1. REST 探活 ==        [OK] health geom=occt nest=shelf workers=4
== 2. 载入示例图形 ==      [OK] demo 9 条轮廓
== 3. WS 握手与图形同步 ==  [OK] hello_ack / geometry_doc 9 条 / 342 点 / 2892 字节
== 4. 异步任务：刀路 ==    [OK] toolpath 62 段预览 / 2250 条运动 / 切削 15564.7mm / 预估 11.04 分 / 用时 1136.8ms
== 5. 异步任务：智能排料 ==  [OK] nest 引擎=shelf 零件=9 板材=1 利用率=12.0%
== 6. 导出加工文件 ==      [OK] export_nc / export_plt / export_cut
== 结果：9 项通过，0 项失败 ==
```

> 刀路耗时与字节数为单次采样，随机器负载和轮廓点数波动，判定看的是 `[OK]` 与项数。

### 画布操作

| 操作 | 效果 |
| --- | --- |
| 滚轮 | 以鼠标位置为锚点缩放 |
| 中键拖动 / 空格+左键 | 平移 |
| 左键拖动 | 框选（Ctrl 追加） |
| 左键单击 | 点选轮廓（6px 容差） |
| 起割点模式 | 左键点击回传世界坐标，后端吸附到最近轮廓；Esc 退出 |
| `F` | 适配视图 |
| `Esc` | 取消当前操作 |

---

## 五、打包：单文件 exe（主）+ NSIS/MSI（次）

一条命令：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

### 5.1 六步流程

| 步骤 | 动作 | 失败时 |
| --- | --- | --- |
| 1/6 | 检查 Node / cargo / cargo-tauri | 给出 `winget install Rustlang.Rustup` 与 VS Build Tools 指引 |
| 2/6 | 检查/生成图标 | 缺 `icon.ico` 时调 `make_icons.py` |
| 3/6 | 调 `build_sidecar.ps1` 打后端 | 单独跑该脚本可看完整日志 |
| 4/6 | 检查 `node_modules` | 缺则 `npm install` |
| 5/6 | `cargo tauri build --bundles nsis,msi` | 对 LNK1104、缺 WebView2、缺 link.exe 给对应处理 |
| 6/6 | 汇总到 `dist\` | 缺安装包只告警，不影响便携版 |

`build_sidecar.ps1` 自己也是六步：环境检查 → 依赖探测（`probe_env.py` 输出 JSON）→ PyInstaller `--onefile` → 产物校验 → **启动自检**（拿 8976 端口真跑一遍 `/api/health`）→ 注入 `frontend\src-tauri\backend\` 并打印 SHA256。

### 5.2 三种产物

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| 便携单文件（**主**） | `dist\BtCAM-<ver>-portable.exe` | 后端已内嵌，免安装，拷贝即运行；车间交付只用这个 |
| NSIS 安装包（次） | `dist\BtCAM-<ver>-setup.exe` | 需要开始菜单/固定安装目录时用 |
| MSI（次） | `dist\BtCAM-<ver>.msi` | 企业域部署（msiexec 静默安装） |

### 5.3 为什么能真的只有一条 exe

1. `build.rs` 构建期读 `backend\cam-backend.exe`，生成 `embedded_backend.rs`（`include_bytes!` + FNV-1a 哈希）；
2. 字节成为 `btcam.exe` 的一部分，所以 `tauri.conf.json` 的 `bundle.resources` **故意留空**，安装目录不再多一份后端；
3. 首次启动把内嵌字节写到 `%LOCALAPPDATA%\com.btsoft.btcam\bin\cam-backend-<hash>.exe`（Tauri 应用数据目录，目录名 = `identifier`；同哈希只写一次），再以隐藏窗口拉起；
4. 代价是主程序体积 ≈ 前端 + Rust + 后端（当前后端 25.3 MB），换的是“零安装、单文件、不怕误删后端”。

### 5.4 进程回收三道保险

| 层 | 机制 | 覆盖场景 |
| --- | --- | --- |
| Rust | 退出事件里 `taskkill /PID <pid> /T /F` | 正常关窗；PyInstaller onefile 的“引导进程+真实进程”两层 |
| Python | `procguard` 用 `WaitForSingleObject` 盯主进程，句柄 signaled 即 `os._exit(0)` | 主程序被任务管理器结束/崩溃，Rust 的退出回调根本不会跑 |
| 启动前 | 8975 端口探测，已有后端则复用并标 `owned=false` | 重复双击、开发态残留，避免“杀错别人的进程” |

### 5.5 体积优化

`Cargo.toml [profile.release]`：`opt-level=3` + `lto=true` + `codegen-units=1` + `panic="abort"` + `strip="symbols"` + `incremental=false`。

后端侧两条：

1. `--onefile` 自带压缩，且 `build_sidecar.ps1` 对从未被 import 的开发依赖统一加 `--exclude-module`
   （tkinter/PIL/matplotlib/pandas/scipy/PyQt/PySide/IPython/jupyter/pytest/pip/wheel/vtkmodules），
   避免分析工具把开发期才用的包带进产物；打包后用 `PYZ-00.toc` 可核对 `pip`、`tkinter` 等条目为 0。
2. **OCCT 默认不打进包**：`cadquery-ocp` 的 `OCP` 是单个 110 MB 扩展模块，`--collect-submodules` 对它无效；
   默认包（约 25 MB）走 pyclipper，功能完整；需要 OCCT 精度时：

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build_sidecar.ps1 -WithOcct   # 约 130 MB
   ```

   `-WithOcct` 会自动改用 `--hidden-import OCP` 并保留 `vtkmodules`；打完看自检行的「几何引擎」应是 `occt`。

### 5.6 版本固化（V0.4）

发布版本 `0.4.0`，对外展示名 `V0.4 完整版`。版本号分布在 5 个文件，**必须同步**：

| 文件 | 字段 | 作用 |
| --- | --- | --- |
| `frontend/src/version.ts` | `APP_VERSION` / `APP_EDITION` | 关于页、状态栏角标、WS HELLO |
| `frontend/package.json` | `version` | 前端构建标识 |
| `frontend/src-tauri/tauri.conf.json` | `version` | 安装包/exe 文件版本，`build.ps1` 用它命名产物 |
| `frontend/src-tauri/Cargo.toml` | `package.version` | 主程序 crate 版本 |
| `backend/app/version.py` | `__version__` / `EDITION` | `/api/health` 与启动日志 |

关于页在工具栏「关于…」：展示 `V0.4 完整版`、前端/后端版本、几何/排料引擎、数据目录、已交付 12 模块。
生产包关闭了 WebView 开发者工具（[`tauri.conf.json`](frontend/src-tauri/tauri.conf.json) 的 `devtools=false` +
`tauri` crate 无 devtools feature），排障统一走软件内「日志…」面板。

---

## 六、通信协议

### 6.1 帧结构（小端）

```
+--------+------+------+--------+---------+---------+
| magic  | ver  | type | seq    | metaLen | payLen  |
| uint16 | u8   | u8   | uint32 | uint32  | uint32  |   共 16 字节
+--------+------+------+--------+---------+---------+
| meta (UTF-8 JSON，≤64KB，只放小字段) | payload (二进制) |
```

- `magic = 0xCA0F`，`version = 1`
- `payload` 里放几何点阵；`meta` 严格限制体积，超过 64KB 直接报错（从协议层面禁止"大 JSON"）
- 一条 WS 消息可包含多帧，接收端按 `total = 16 + metaLen + payLen` 循环切分

### 6.2 BGC1 几何块（payload）

```
magic uint32 = 0x31434742 ("BGC1") | version u16 | reserved u16 | polyCount u32
重复 polyCount 次：
  id u32 | layerId u32 | flags u8 (bit0=closed, bit1=is_hole) | pad[3] | ptCount u32
  pts float32[ptCount*2]        # x0,y0,x1,y1,... 单位 mm，Y 轴向上
```

后端用 `np.ascontiguousarray(...).tobytes()` 一次性输出；前端 `new Float32Array(buffer, offset, n)` 直接建视图，10 万点级图形毫秒级下发。

### 6.3 大数据分片（CHUNK）

阈值 `CHUNK_BYTES` 默认 1 MB（环境变量 `BTCAM_CHUNK_BYTES` 可覆盖，下限 1024）。
超过阈值的帧由发送端切成 N 片，每片仍是标准 16 字节帧头，`type = 0x40`。

- 首片 meta：`{"sid","i":0,"n","t","meta"}`，`t` 是原消息类型，`meta` 是原 meta；
- 后续片 meta：`{"sid","i","n"}`；
- 接收端按 `sid` 聚合（TTL 30 s，超时丢弃并报错），凑满 `n` 片后用首片 meta 还原原消息；
- 单条消息上限 64 MB，防内存被打爆。

这样百万点级的排料结果也不会撑爆单条 WS 消息，后端 `ChunkAssembler` 与前端 `ChunkAssembler` 逻辑一一对应。

### 6.4 消息类型

| 值 | 名称 | 方向 | 说明 |
| --- | --- | --- | --- |
| 0x01 | HELLO | C→S | 握手，meta 带 clientVersion |
| 0x02 | HELLO_ACK | S→C | 握手回执，随后自动推送 GEOMETRY_DOC |
| 0x03/0x04 | PING / PONG | 双向 | 心跳（前端 20s 一次） |
| 0x10 | GEOMETRY_SYNC | C→S | 请求全量图形 |
| 0x11 | GEOMETRY_DOC | S→C | 全量图形（二进制块 + 图层 meta） |
| 0x12 | SELECTION_CHANGED | C→S | 选中集变化（meta only） |
| 0x13 | GEOMETRY_ISSUES | S→C | 图形校验告警：payload=异常线段（BGC1），meta 带告警明细 |
| 0x14 | LAYOUT_STATE | S→C | 排料板状态：payload=按摆放变换后的轮廓（BGC1），meta 带板材/零件/统计 |
| 0x15 | LAYOUT_SYNC | C→S | 请求重发排料状态（丢帧/重连时用） |
| 0x20 | TASK_SUBMITTED | S→C | 任务已入队 |
| 0x21 | TASK_PROGRESS | S→C | 进度（meta only） |
| 0x22 | TASK_RESULT | S→C | 通用结果 |
| 0x23 | TASK_FAILED | S→C | 失败/取消 |
| 0x30 | TOOLPATH_PREVIEW | S→C | 刀路预览（9001 切削实线 / 9002 抬刀虚线 / 9003 起割点标记） |
| 0x31 | NEST_RESULT | S→C | 排料结果（摆料轮廓 + 统计） |
| 0x40 | CHUNK | 双向 | 大数据分片（见 6.3），meta 带 sid/i/n |
| 0x50 | ALERT | S→C | 运行告警：meta 即日志条目（id/time/level/source/message/taskId/kind/detail） |
| 0x7F | ERROR | S→C | 协议/业务错误 |

> 改动协议必须**同时**修改 `backend/app/core/protocol.py` 与 `frontend/src/net/protocol.ts`，两边常量逐字对应。

### 6.5 REST 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 引擎/连接数/并发度探活 |
| GET | `/api/project` | 工程、材料、刀具概要 |
| POST | `/api/demo` | 载入内置示例图形并广播 |
| GET | `/api/tasks` | 任务队列快照 |
| POST | `/api/tasks/{taskId}/cancel` | 取消排队/运行中的任务（自动排料可中途取消） |
| POST | `/api/import/dxf` | 上传 DXF（multipart，可多文件）→ 返回 `taskId` |
| GET | `/api/import/defaults` | DXF 导入默认参数 |
| GET | `/api/geometry/info` | 几何引擎与 OCCT 资源计数 |
| POST | `/api/geometry/offset` | 轮廓偏移/刀具补偿 → `taskId` |
| POST | `/api/geometry/intersect` | 轮廓求交 → `taskId` |
| POST | `/api/geometry/merge` | 断线合并 → `taskId` |
| POST | `/api/geometry/bbox` | 包围盒（同步） |
| POST | `/api/geometry/closure` | 闭合校验（同步） |
| POST | `/api/geometry/self-intersections` | 自相交检查（同步） |
| POST | `/api/offset` | 轮廓偏置兼容入口，内部转 `/api/geometry/offset` |
| GET | `/api/layout` | 排料状态快照（板材 + 零件 + 统计，不广播） |
| POST | `/api/layout/open` | 进入排料模式（首次做初始行排并广播） |
| POST | `/api/layout/sheet` | 保存板材参数（宽/高/四边余量/最小间距） |
| POST | `/api/layout/place` | 拖拽落库（越界/间距不足则拒绝） |
| POST | `/api/layout/rotate` | 旋转到指定角度（绕包围盒中心，自动夹回） |
| POST | `/api/layout/lock` | 锁定/解锁零件 |
| POST | `/api/layout/fixed-rot` | 木纹方向锁定/解锁（自动排料不旋转该件） |
| POST | `/api/layout/reset` | 清空摆放并重新行排 |
| POST | `/api/layout/nest` | 一键自动排料（写回摆放并广播 LAYOUT_STATE）→ `taskId` |
| POST | `/api/nest` | 自动排料 → 返回 `taskId` |
| GET | `/api/tools` | 刀具库列表（含等效直径 / 补偿半径） |
| GET | `/api/tools/{id}` | 单把刀具 |
| POST | `/api/tools` | 新建刀具（铣刀/钼丝） |
| PUT | `/api/tools/{id}` | 修改刀具（被模板引用不影响） |
| DELETE | `/api/tools/{id}` | 删除刀具（被模板引用时 400） |
| GET | `/api/templates` | 工艺模板列表（内嵌绑定的刀具） |
| POST | `/api/templates` | 新建工艺模板 |
| PUT | `/api/templates/{id}` | 修改工艺模板 |
| DELETE | `/api/templates/{id}` | 删除工艺模板 |
| POST | `/api/toolpath` | 生成刀路 → `taskId`；`source=doc`（图纸）/ `source=layout`（排料板整板）；可带 `toolId` / `templateId` |
| POST | `/api/export` | 同步导出：`processorId` 指定后处理（缺省按 `format` 映射），`allSheets` 多板每板一个文件；`kind=dxf` 走图形分支；带 `workOrderId` 时导出成功自动登记加工文件 |
| GET | `/api/export/dir` | 导出目录（"打开文件夹"用） |
| GET | `/api/post` | 后处理列表（内置在前，含 kind/ext/完整模板） |
| GET | `/api/post/{id}` | 单份后处理配置 |
| POST | `/api/post` | 新建自定义后处理 |
| PUT | `/api/post/{id}` | 修改自定义后处理（内置返回 400，需另存为） |
| DELETE | `/api/post/{id}` | 删除自定义后处理（内置返回 400） |
| GET | `/api/post/variables` | 变量字典：12 个占位符 + 各 kind 的指令槽位与中文标签 |
| POST | `/api/post/validate` | 校验后处理草稿 → `errors[]`（带 `field`，前端据此高亮）/ `warnings[]` / 变量用量 |
| POST | `/api/post/preview` | 用一条假刀路渲染预览文本（与导出共用同一渲染器） |
| GET | `/api/workorders` | 工单检索：`keyword`（名称/编号/备注）+ `dateFrom` / `dateTo` + `limit` |
| GET | `/api/workorders/{id}` | 工单详情（统计 + 刀路/后处理 + 刀具引用 + 缺失提示 + 文件列表） |
| GET | `/api/workorders/{id}/files` | 工单加工文件列表 |
| POST | `/api/workorders` | 保存当前工程为新工单（快照 + 刀路参数 + 后处理选择） |
| PUT | `/api/workorders/{id}` | 改名称/编号/备注；`recapture=true` 时重拍快照 |
| DELETE | `/api/workorders/{id}` | 删除工单及其文件登记 |
| POST | `/api/workorders/{id}/duplicate` | 另存为副本（快照深拷贝，完全独立） |
| POST | `/api/workorders/{id}/open` | 恢复工单到当前工程（整份替换）并广播全量图形 |
| GET | `/api/logs` | 日志查询：`level`（最低级别）/ `keyword` / `source`（前缀）/ `after`（增量游标）/ `limit` |
| DELETE | `/api/logs` | 清空内存缓冲（不动磁盘日志） |
| POST | `/api/logs/client` | 前端异常上报（`error` 级自动推 ALERT 帧） |
| GET | `/api/scrap` | 余料列表：`keyword`（名称/编号/物料）+ `limit`，带当前绑定的 `currentScrapId` |
| POST | `/api/scrap` | 新建余料（`outline` 为空时用宽高生成矩形） |
| GET | `/api/scrap/{id}` | 单块余料（含轮廓点串） |
| PUT | `/api/scrap/{id}` | 修改余料（轮廓可一起改，宽高/面积由后端重算） |
| DELETE | `/api/scrap/{id}` | 删除余料；正在被板材引用时自动解绑 |
| POST | `/api/scrap/remaining` | 计算当前板材的剩余区域（预览，不落库）：`minArea` / `gap` |
| POST | `/api/scrap/save` | 把 `indices[]` 选中的区域入库（几何以后端重算为准） |
| POST | `/api/scrap/{id}/apply` | 把余料设为当前板材（`resetPlacements` 默认 true） |
| POST | `/api/scrap/clear` | 解绑余料退回矩形板（可同时套用新尺寸） |
| GET | `/api/mes/settings` | MES 配置（密钥掩码）+ 厂商清单 + 默认路径 + 工单计数 |
| PUT | `/api/mes/settings` | 保存 MES 配置（`apiKey` 留空 = 沿用旧值） |
| POST | `/api/mes/test` | 测试连接（可带草稿配置，未启用也能先试通） |
| POST | `/api/mes/pull` | 从 MES 拉工单（`limit` 1~200），返回 `created/updated/skipped/problems` |
| GET | `/api/mes/orders` | 本地 MES 工单列表（`status` = `pulled` / `archived`） |
| GET | `/api/mes/orders/{id}` | 单张 MES 工单详情（含零件清单） |
| POST | `/api/mes/push` | 回传工单信息 + 用料统计 + 加工文件归档 |
| GET | `/api/mes/logs` | MES 交互日志（`orderId` / `limit`） |
| WS | `/ws` | 图形同步 + 任务推送（**只收二进制帧**） |

---

## 七、DXF 导入与轮廓修复（模块 1）

### 7.1 数据流

```
文件选择框（React，仅拿 File 对象）
   └─ multipart POST /api/import/dxf  ──> 落盘 tmp ──> TaskQueue（线程池）
                                                        │
                          ┌─────────────────────────────┘
                          ▼
   ezdxf 读取 → 离散成折线 → 拓扑修复 → 拆零件 → 写 SQLite
                          │
                          ├─ TASK_PROGRESS  (0~100%)
                          ├─ GEOMETRY_DOC    (全量图形 + importSummary)
                          ├─ GEOMETRY_ISSUES (异常线段，前端标红)
                          └─ TASK_RESULT     (partCount / stats)
```

前端**不做任何几何计算**：拿到的点阵直接进 PixiJS Graphics。

### 7.2 支持的 DXF 内容

| 项 | 说明 |
| --- | --- |
| 图元 | LINE / LWPOLYLINE / 2D POLYLINE / ARC / CIRCLE / ELLIPSE / SPLINE / HATCH / INSERT（已实测） |
| 块 | `virtual_entities()` 递归展开，深度上限 8 层（`maxBlockDepth`） |
| 单位 | 读 `$INSUNITS` 统一换算到 mm；无单位声明按 mm 处理 |
| 图层 | `$LAYER` 颜色转 RGB；图层名启发式推断工艺（CUT/切/轮廓 → 外轮廓切割） |
| 不支持 | TEXT/MTEXT/DIMENSION 等非轮廓实体计入 `stats.unsupported`，不报错 |

### 7.3 修复流程（`geometry/repair.py`）

| 步骤 | 做法 |
| --- | --- |
| 1. 碎线剔除 | 闭合轮廓按**总面积**（三角形面积绝对值累加）、开放轮廓按长度判定；阈值 `minArea` / `minSegmentLength` |
| 2. 重叠清理 | 全部拆成无向线段后分组去重（直线按 (角度, 法向截距)，共线区间取并集），只保留非重叠部分 |
| 3. 端点聚类 | 网格分桶 + 并查集，容差 `joinTolerance` |
| 4. 串接 | 从每个端点出发选**最直延续**（转角最小）的边，避免分叉处串错 |
| 5. 自动闭合 | 首尾缺口 ≤ `closeGap` 时把首尾**吸附到缺口中点**再合并（只置标志会导致伪自相交） |
| 6. 内外分类 | 包含深度奇偶判定，外轮廓 `is_hole=false`、内孔 `is_hole=true`；面积剪枝避免边界误判 |
| 7. 走向统一 | 外轮廓逆时针（面积>0）、内孔顺时针（面积<0），刀路补偿靠这个定方向 |
| 8. 自相交检测 | 网格分桶线段求交，命中即产出 `self_intersection` 告警 + 交点坐标 |
| 9. 拆零件 | `group_parts()` 按包含关系分组：最外层闭合轮廓 + 内部所有轮廓 = 一个零件 |

> 需求要求“拓扑修复交给 OCCT”。当前环境未装 OCCT 绑定，本模块用 **numpy + pyclipper** 实现同等能力；
> 接口（`RepairOptions` / `TopologyRepair.apply()`）与引擎无关，模块 2 封装 OCCT 后可直接替换内部实现。

### 7.4 接口

```powershell
# 上传（可多个文件），options 与 projectId 都是可选
curl.exe -X POST http://127.0.0.1:8975/api/import/dxf `
  -F "files=@D:\图纸\板件.dxf" `
  -F "files=@D:\图纸\垫块.dxf" `
  -F 'options={"replace":true,"splitParts":true,"read":{"skipOffLayers":true},"repair":{"closeGap":1.0}}' `
  -F "projectId=1"
```

`options` 是 JSON 对象，分三组：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `files` | 文件列表 | 必填 | `.dxf`，单文件 ≤ 200 MB，单次 ≤ 50 个 |
| `replace` | bool | `true` | 清空当前工程后导入；`false` 则追加 |
| `splitParts` | bool | `true` | 按包含关系拆成多个零件 |
| `projectId` | int | 当前工程 | 写入目标工程 |
| `read.flattenDistance` | float | `0.05` | 圆弧/样条离散弦高（mm，0.001~10） |
| `read.unitScale` | float | 自动 | 强制单位缩放，覆盖 `$INSUNITS` |
| `read.skipOffLayers` | bool | `true` | 跳过关闭/冻结图层 |
| `read.explodeBlocks` | bool | `true` | 展开块引用 |
| `read.includeHatch` | bool | `true` | 读取 HATCH 边界 |
| `read.maxBlockDepth` | int | `8` | 块递归深度上限 |
| `repair.mergeTol` | float | `0.2` | 端点吸附容差（mm） |
| `repair.minLength` | float | `0.5` | 碎线阈值：短于此长度丢弃 |
| `repair.minArea` | float | `0.5` | 碎面阈值：闭合面积小于此值丢弃 |
| `repair.closeGap` | float | `1.0` | 自动闭合允许的最大首尾缺口 |
| `repair.removeOverlap` | bool | `true` | 清理共线重叠/重复线段 |
| `repair.checkIssues` | bool | `true` | 做自相交检测并产出告警 |

返回 `{taskId, kind:"import"}`；结果通过 WS 推送，不阻塞 HTTP。

### 7.5 控制台验收

```powershell
# 只跑算法（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\dxf_import_test.py

# 算法 + 接口全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\dxf_import_test.py --url http://127.0.0.1:8975
```

实测输出（pyclipper + ezdxf 1.0.3）：

```
== A1 开放线闭合（4 条直线，缺口 0.3mm） ==  [OK] 轮廓=1 闭合=True 面积=6007.5
== A2 重叠线清理（同一矩形画两遍） ==      [OK] 去除重线=4 剩余顶点=4 面积=6000.0
== A3 自相交识别（蝴蝶结） ==             [OK] 告警=1 交点=(50.00, 50.00)
== A4 内外轮廓区分（外框 + 内孔） ==      [OK] 外轮廓 +24000(CCW) 内孔 -1248.6(CW)
== A5 碎线剔除（0.2mm 短线） ==          [OK] 剔除=1 保留轮廓=1
== B1 接口接受导入 ==                    [OK] taskId=...
== B2 任务结果 ==                        [OK] 零件=5 轮廓=11 去除重线=4 内孔=4
== B3 图形帧下发 ==                      [OK] 11 条轮廓 / 内孔 4 条 / 1876 字节
== B4 告警帧下发 ==                      [OK] 告警 3 条 / 异常线段 3 条
== 结果：9 项通过，0 项失败 ==
```

B 部分的测试图纸由 ezdxf 现场生成，覆盖：闭合件+内孔、4 条断线（0.4mm 缺口）+ 完全重复的矩形、
自相交蝴蝶结、块引用（内含圆/圆弧）+ 椭圆、HATCH 填充边界 + 样条曲线，
共 15 条原始轮廓 → 修复后 5 个零件 / 11 条轮廓 / 4 个内孔 / 3 条告警（2 开放 + 1 自交），
最后自动调 `/api/demo` 恢复内置示例。

---

## 八、2D 几何基础服务（模块 2）

### 8.1 设计要点

| 约束 | 落地方式 |
| --- | --- |
| 重型计算只在后端 | 前端只发参数、收 WS 帧；偏移/布尔/包围盒/自相交全在 `services/geometry_ops.py` |
| 独立服务类 | `GeometryOps`（单例 `get_ops()`）对外 6 个方法，不依赖 FastAPI |
| Handle 强制释放 | `ReleaseScope` 上下文管理器：wire/maker/shape 退出即置 None + `gc.collect()`，并留计数 |
| 复用既有数据结构 | 入参出参都是 `Polyline` / BGC1 二进制块，与模块 1、WS 协议完全一致 |
| 只做 2D | 只接受折线/闭合轮廓，不建实体、不做 3D 布尔 |

### 8.2 服务类接口（`services/geometry_ops.py`）

| 方法 | 入参 | 出参 | 说明 |
| --- | --- | --- | --- |
| `offset(polylines, distance, join, side)` | 轮廓、距离>0、拐角、方向 | Polyline 列表 | 刀具补偿；`side=auto` 按 `is_hole` 定方向 |
| `intersect(subject, clip)` | 两组闭合轮廓 | Polyline 列表 | 布尔交集 |
| `bbox(polylines)` | 轮廓 | `BBox` | 纯 numpy，不依赖引擎 |
| `self_intersections(polylines)` | 轮廓 | `ContourIssue` 列表 | 复用模块 1 的网格分桶求交 |
| `closure_report(polylines, tol)` | 轮廓、容差 | `ClosureInfo` 列表 | `closed=True` 视为隐含闭合边，gap=0 |
| `merge_gaps(polylines, tol)` | 轮廓、容差 | Polyline 列表 | 首尾接近的碎片串成一条 |

方向约定：`side=auto` 时外轮廓向外、内孔向内（孔变小），结果统一为外 CCW / 内 CW，
所以 `area()` 的正负号就是内外标志。

引擎契约在 `geometry/base.py`（`offset` / `union` / `intersect`），
OCCT（`occt_engine.py`）与 pyclipper（`clipper_engine.py`）各实现一份，
`get_engine()` 按 `occt → clipper → fallback` 顺序探测。

### 8.3 接口

同步（小 JSON 结果，线程池执行，不阻塞事件循环）：

| 方法 | 路径 | 入参 | 出参 |
| --- | --- | --- | --- |
| GET | `/api/geometry/info` | - | 引擎、调用计数、OCCT 资源快照 |
| POST | `/api/geometry/bbox` | 轮廓 / ids / layerIds | `{xmin,ymin,xmax,ymax,width,height,centerX,centerY,contourCount,pointCount}` |
| POST | `/api/geometry/closure` | 同上 + `tol` | `{count,openCount,items}` |
| POST | `/api/geometry/self-intersections` | 同上 | `{count,contourCount,items}` |

异步（会产出新轮廓，走任务队列 + BGC1）：

| 方法 | 路径 | 特有入参 |
| --- | --- | --- |
| POST | `/api/geometry/offset` | `distance`（>0）、`join`、`side` |
| POST | `/api/geometry/intersect` | `contours`/`ids` + `clip`/`clipIds` |
| POST | `/api/geometry/merge` | `tol`（默认 0.2） |

轮廓可用三种方式指定，优先级从高到低：请求内联 `contours` → `ids` 精确匹配 → `layerIds` 按图层过滤。
单次上限 20000 条，防止误传超大图纸撑爆内存。

### 8.4 Handle 生命周期

OCCT 对象（`TopoDS_Shape`、`BRepOffsetAPI_MakeOffset` 等）不是 Python 垃圾回收能及时清理的，
所以每次运算都收敛到一个作用域：

```python
with self._scope() as scope:        # 进入：记录本作用域持有的对象
    face = self._face(pts, scope)   # wire / maker / face 逐个登记
    ...
# 退出：逐个置 None → 清列表 → gc.collect() → 计数+1
```

`GET /api/geometry/info` 的 `resource` 字段给出 `{calls, released, gcRuns}`，
两者数量一致说明没有漏放。

### 8.5 控制台验收

```powershell
# 只跑服务层（秒级）
backend\.venv\Scripts\python.exe scripts\geometry_ops_test.py

# 服务层 + 接口全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\geometry_ops_test.py --url http://127.0.0.1:8975
```

实测输出（`backend=occt`，cadquery-ocp 7.7.2 + Python 3.8）：

```
== A1 刀具补偿偏移 ==  [OK] bbox=(-5,-5,105,65) 面积=7678.14（期望 7678.54）
== A2 偏移后无自交 ==  [OK] 告警=0
== A3 内外方向 ==      [OK] 外框宽=210.00 内孔直径=30.00 内孔面积=-706.43
== A4 轮廓求交 ==      [OK] 面积=2500.00
== A5 包围盒 ==        [OK] 210.0 x 110.0 轮廓=2
== A6 自相交检测 ==    [OK] 告警=1 交点=(50.00,50.00)
== A7 闭合校验 ==      [OK] 闭合 gap=0 / 缺口 gap=0.3
== A8 断线合并 ==      [OK] 轮廓=1 闭合=True 顶点=4 面积=6000.0
== A9 内存不泄漏 ==    [OK] RSS 231.1 -> 231.1 MB（增长 0.00 MB）/ 释放对象 1397 / gc 464
== B1~B7 接口 ==       [OK] info / bbox / closure / self-intersections / offset / intersect / merge
== 结果：16 项通过，0 项失败 ==
```

同一套用例在 `backend=clipper` 下也全过（A1 面积 7678.53、RSS 34.3 MB），
说明两套引擎行为一致；OCCT 的 230 MB 基数是绑定本体，判定看的是**增量**。

`A9` 用 `GetProcessMemoryInfo` 读进程 WorkingSet：预热 30 次摊掉一次性开销，
再连跑 200 次偏移，增长超过 10 MB 判失败。

---

## 九、画布手动排料（模块 3）

### 9.1 设计要点

| 约束 | 落地方式 |
| --- | --- |
| 重型计算只在后端 | 前端拖拽只做像素级视觉偏移，松手发一次 `POST /api/layout/place`；越界/间距/锁定全由 `services/layout_service.py` 判定 |
| 交互级同步接口 | 移动/旋转/锁定都是几十毫秒的写操作，不进任务队列；写成功后广播 `LAYOUT_STATE` |
| 失败不落库 | 校验不通过时既不写 SQLite 也不改内存状态，REST 回 `ok=false + reason`，前端撤掉偏移回弹 |
| 复用协议 | 排料视图复用 BGC1 二进制块与 `Polyline`（id 从 200000 起，与图纸轮廓 id 天然隔离） |
| 前端只渲染 | 画布不判重叠、不算旋转包围盒；板材底图只是两个矩形 |

### 9.2 坐标与数据模型

板材左下角为原点、Y 向上，与图纸坐标系一致：`tx/ty` 是「零件旋转后包围盒的左下角」，
旋转绕包围盒中心、位置自动补偿（大件在板边旋转会被夹回可用区域，而不是直接报越界）。

两张表（`models/db.py`）：

| 表 | 主键 | 字段 |
| --- | --- | --- |
| `layout_sheet` | `project_id` | `width/height/margin/gap/updated_at` |
| `layout_placement` | `(project_id, part_id)` | `sheet/tx/ty/rot/locked/zorder` |

零件几何不复制一份：排料直接读 `part` + `polyline` 两张表，`LayoutService.load()`
把每个零件的所有轮廓平移到自身包围盒左下角后再做变换，内孔不会错位。

### 9.3 服务类接口（`services/layout_service.py`）

| 方法 | 说明 |
| --- | --- |
| `load(conn, project_id)` | 组装 `LayoutState`（板材 + 零件归一化轮廓 + 摆放） |
| `open(conn, project_id)` | 进排料模式；没有摆放记录时按「高件优先、自下而上」做一次行排 |
| `set_sheet(...)` | 保存板材参数；板变小时把越界零件夹回可用区域 |
| `move(conn, pid, part_id, tx, ty, rot=None)` | 移动（可同时改角度），校验越界 + 最小间距 |
| `rotate(conn, pid, part_id, rot)` | 绕包围盒中心旋转，位置自动补偿后走 `move` |
| `set_locked(...)` | 锁定/解锁（锁定后 `move`/`rotate` 一律拒绝） |
| `reset(...)` | 清空摆放重新行排 |
| `build_polylines(state)` | 输出变换后的 `Polyline`，`is_hole` 按外轮廓索引标记 |
| `build_meta(state, id_map)` | 板材 + 零件 + 统计（利用率/锁定数/越界数） |

校验两条：

- **越界**：只比包围盒四边与 `margin`，不必变换全部顶点；
- **间距**：两个零件各外扩 `gap/2` 后用 pyclipper 求交，交面积 > 1e-3 mm² 判冲突；
  先用包围盒 + `gap` 粗筛，避免对每个零件都跑一次布尔运算。

净面积 = |最大面积轮廓| − Σ其余|面积|，不依赖 DXF 里内孔的方向。

### 9.4 接口与前端

`/api/layout/*` 八个接口（见 6.5），写操作统一 `run_in_threadpool` + `_publish()` 广播。
`LAYOUT_SYNC` 是兜底：断线重连或前端判定「广播丢了」时主动拉一次。

前端分工：

| 位置 | 职责 |
| --- | --- |
| `render/viewport.ts` | 画板材底图与余量内框；左键命中零件即选中整件并进入拖拽，松手回调新位置；R/Shift+R 旋转、L 锁定、Esc 取消 |
| `render/geometry-layer.ts` | 拖拽期间给零件加像素偏移（`DragVisual`），跳过视口裁剪避免大件拖出屏幕时轮廓闪断 |
| `ui/useLayoutActions.ts` | 三条交互路径（画布/面板/工具栏）共用的写操作收口：串行提交、失败回弹、结束后补一次 `LAYOUT_SYNC` |
| `ui/LayoutPanel.tsx` | 板材参数表单、零件列表（点击选中整件）、±90°/锁定/木纹按钮、自动排料区、利用率统计 |
| `state/app-store.ts` | `applyLayout(meta, polylines)`：排料轮廓与 `layout` 一起入 store，`viewMode` 切到 `layout` |

排料模式下左侧面板自动从「图层」切成「排料板」，工具栏的「排料模式」按钮调 `POST /api/layout/open`。

### 9.5 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\layout_test.py

# 服务层 + 接口/WS 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\layout_test.py --url http://127.0.0.1:8975
```

实测输出（内置示例图形，1220×2440 板 / 余量 10 / 间距 5）：

```
== A. 服务层：零件 / 排板 / 校验 ==
  [OK]   A1 零件拆分  5 个零件 · 板材 1220x2440 · 间距 5
  [OK]   A1 内孔归属  「示例零件-1」5 条轮廓（1 外框 + 4 孔）
  [OK]   A2 初始行排  摆放 5 个，全部在可用区内
  [OK]   A3 越界拒绝  零件「示例零件-1」超出板材左边界
  [OK]   A4 间距拒绝  零件「示例零件-2」与「示例零件-1」间距不足 5mm
  [OK]   A5 合法移动  落库坐标 (615.0, 450.0)
  [OK]   A6 旋转  角度 90° 包围盒 200x300
  [OK]   A7 锁定  已锁定，先解锁再移动 / 已锁定，不能旋转
  [OK]   A8 解锁  解锁后移动成功
  [OK]   A9 轮廓变换  9 条轮廓，包围盒与摆放一致
  [OK]   A10 统计一致  净面积 351449 mm² 利用率 11.8%
  [OK]   A11 进模式幂等  再次 open 后仍为 (615.0, 10.0)
  [OK]   A12 改板材  板材 1000x1200 余量 20 间距 8，越界零件已夹回
  [OK]   A13 复位  复位后 5 个摆放，锁定全部清零
== B. 接口 / WS 广播 ==
  [OK]   B1 载入示例  9 条轮廓
  [OK]   B2 进模式  5 个零件 / 9 条轮廓 / 利用率 11.8%
  [OK]   B3 拖拽拒绝  零件「示例零件-2」与「示例零件-1」间距不足 5mm（广播仍为原位 615.0,10.0）
  [OK]   B3 拖拽落库  (610.0, 2030.0)
  [OK]   B4 旋转  角度 90°
  [OK]   B5 锁定  零件「示例零件-1」已锁定，先解锁再移动
  [OK]   B6 改板材  板材 1000x1500 余量 15 间距 6
  [OK]   B7 复位  复位后 5 个摆放
== 结果：22 项通过，0 项失败 ==
```

脚本会用示例图形重建当前工程，结束时把板材恢复成 1220×2440 / 余量 10 / 间距 5 并复位。

---

## 十、自动排料（模块 4）

### 10.1 目标与边界

一键把当前排料板上的零件重排，结果**直接写回 `layout_placement`**，与手动排料共用同一套
`tx/ty/rot` 契约和同一套校验（`LayoutService._check`）。因此自动排料不会产生"另一种坐标"，
也不会出现"手动摆得下、自动却报错"的口径差异。

三条硬规则：

| 规则 | 行为 |
| --- | --- |
| 位置锁定 `locked` | 原地不动，作为障碍物；其余零件绕开它 |
| 木纹 `fixed_rot` | 自动与手动都不旋转（手动旋转提示先解锁） |
| 排不下 | 不报错、不丢失：零件停靠在板材上方，`unplacedCount` 与 `overflowCount` 同时可见 |

### 10.2 排料引擎

| 引擎 | 说明 |
| --- | --- |
| `scan`（内置） | 底线扫描：候选点取自"已放零件包围盒的右上角"，按 (y,x) 排序后逐个用 `_check` 验证；大件优先、支持障碍与定角、结果确定可复现 |
| `deepnest` | 调外部桥接进程（`BTCAM_DEEPNEST_CMD`），把 `gap` / `rotation` / 逐件角度许可传过去；子进程用 `Popen` + 100ms 轮询，取消/超时立即 kill；返回值按"旋转后包围盒左下角"换算后再复验，任一不合格整体回退 `scan` |
| `auto` | 有桥接且无锁定件 → DeepNest；否则 → `scan`，原因写在 `stats.engineNote` |

有锁定件或 `partIds` 之外的零件时不走 DeepNest：桥接协议没有"障碍物"语义，无法表达"这件必须留在原地"。

内置扫描的代价：每件只尝试 O(n) 个候选点，示例 5 件约 25ms；几十件量级可用，
上百件或复杂异形建议接 DeepNest。

### 10.3 数据模型增量

`layout_placement.fixed_rot`（0/1）：

| 字段 | 语义 | 手动移动 | 手动旋转 | 自动排料 |
| --- | --- | --- | --- | --- |
| `locked` | 位置锁定（模块 3） | 拒绝 | 拒绝 | 不动，当障碍 |
| `fixed_rot` | 角度锁定（木纹，模块 4） | 允许 | 拒绝 | 允许搬，不允许转 |

旧库启动时由 `db._migrate()` 用 `PRAGMA table_info` 探测后 `ALTER TABLE` 补列，不需要重建数据库。

### 10.4 任务流、入参与前端

入参（`POST /api/layout/nest`，全部可选；不传就重排当前排料板上的全部未锁定零件）：

| 字段 | 说明 |
| --- | --- |
| `width` / `height` | 板材宽高 mm；给了就先走 `set_sheet` 落库（越界零件被夹回） |
| `margin` / `gap` | 四边余量 / 零件最小间距 mm |
| `partIds` | 只重排这些零件；其余零件原地不动、当障碍物；不传 = 全部未锁定件 |
| `engine` | `auto` / `deepnest` / `scan`；有障碍物时 DeepNest 自动让给内置扫描 |
| `rotation` | 是否允许 90°；木纹锁定件始终不转 |

```
POST /api/layout/nest {engine, rotation, width?, height?, margin?, gap?, partIds?}
  → task_queue("layout_nest", auto_nest_job)
     → 读状态 →（可选）改板材 → 锁定/非参与件留作障碍 → 清空参与件 → 选引擎排料
     → 逐件写 layout_placement（排不下的停靠板材上方）
  → TASK_PROGRESS(0~100%，可 POST /api/tasks/{taskId}/cancel 取消)
  → TASK_RESULT（stats + sheet + placements，逐件 tx/ty/rot/fixedRot）
  → LAYOUT_STATE（BGC1 轮廓 + 板材/零件/统计）
```

出参在 `TASK_RESULT` 的 meta 里：`stats`（`engine`/`placedCount`/`unplacedCount`/`utilization`/`calcMs`）、
`sheet`、`placements`（每个零件的 `partId`/`tx`/`ty`/`rot`/`fixedRot`），调用方不用再发 GET。

取消是协作式的：取消接口只置标志，任务在下一个进度回调处抛 `TaskCancelled` 结束；
DeepNest 桥接用 `Popen` + 100ms 轮询，感知取消/超时都会立即 kill 子进程，不会等满 180s。
若取消时已经落库，任务层会补发一帧 `LAYOUT_STATE`，前端不会停在旧位置。

任务完成后由 `tasks._emit_result()` 紧跟着补一帧 `LAYOUT_STATE`，前端不需要再发同步请求；
`TASK_RESULT` 不带 polylines，所以不会像旧的 `/api/nest` 那样把视图切成"预览"。

前端入口三处，共用 `useLayoutActions()`（内部做了"同一排料板只跑一个任务"的并发保护）：

| 位置 | 行为 |
| --- | --- |
| 工具栏「自动排料」 | `engine=auto, rotation=true`，直接在当前板重排 |
| 排料面板「自动排料」 | 可选引擎、是否允许 90°；任务进行中按钮变「排料中…」并出现「取消排料」 |
| 任务面板 | 排队/计算中的每行任务都有「取消」按钮，走同一个取消接口 |

零件行新增「纹」标记，统计区新增「木纹锁定」；`POST /api/layout/fixed-rot` 与锁定按钮一样支持多选、
并会在广播帧里回带 `fixedRot`，刷新页面不会丢。

### 10.5 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\auto_nest_test.py

# 服务层 + 接口/WS 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\auto_nest_test.py --url http://127.0.0.1:8975
```

实测输出（内置示例图形，1220×2440 板 / 余量 10 / 间距 5）：

```
== A. 服务层：自动排料 ==
  [OK]   A1 全件落位  scan 引擎 · 5/5 落位 · 最长耗时 48.2ms
  [OK]   A2 紧凑度  最高点 410.0mm / 板高 2440mm · 利用率 11.81%
  [OK]   A3 锁定件不动  锁定件停在 (10.0, 2000.0) · 4 个落位
  [OK]   A4 木纹锁定  自动后角度 0° · 手动旋转: 零件「示例零件-4」已锁定旋转角度（木纹），先解锁再转
  [OK]   A5 关闭旋转  角度变化零件数 0
  [OK]   A6 未排下  4 个落位 / 1 个停靠板外
  [OK]   A7 幂等  两次利用率均 11.81%
  [OK]   A8 引擎回退  DeepNest 不可用，已回退内置扫描：未配置 BTCAM_DEEPNEST_CMD
  [OK]   A9 取消排料  第 3 次进度回调取消 · 摆放未变（5 个）
  [OK]   A10 入参出参  板材改为 1000x1200 · 只排 1 件 · 其余 4 件原位当障碍 · 回带 5 个摆放
  [OK]   A11 DeepNest 取消  0.12s 内中止（超时设 60s）
== B. 接口 / WS 广播 ==
  [OK]   B1 接口提交  2cae4728349d · scan · 利用率 11.81%
  [OK]   B2 落库一致  5 个摆放 / 利用率 11.81%
  [OK]   B3 木纹接口  零件 #122 角度 0°
  [OK]   B4 参数校验  engine=magic 被拒绝
  [OK]   B5 空工程  projectId=999999 返回 400
  [OK]   B6 取消接口  未知任务 404 · 已结束任务 409
  [OK]   B7 入参出参  板 1000x1200 · 参与 1 件 · 回带 5 个摆放
  [OK]   B8 partIds 校验  partIds=[999999] 返回 400
== 结果：19 项通过，0 项失败 ==
```

脚本会用示例图形重建当前工程；结束时清空锁定/木纹约束、恢复默认板材并复位。
A9/A11 用假 reporter 直接验证“取消不落库”与“桥接子进程被 kill”，不需要慢速任务配合。

---

## 十一、刀路生成（模块 5）

### 11.1 目标与数据源

一套参数、两种数据源：

| 数据源 | 轮廓来源 | 坐标系 | 说明 |
| --- | --- | --- | --- |
| `source=doc` | `polyline` 表里的图纸轮廓 | 图纸世界坐标 | 可只对选中轮廓生成（`ids` / `layerIds`） |
| `source=layout` | 当前排料板全部摆放件 | 板材左下角原点 | 整板一套连续刀路，忽略选中集 |

补偿、方向、起割点旋转、微连接切段、排序、Z 分层、切入圆弧全部在
`services/toolpath_service.py` 完成；前端只提交参数、按 BGC1 折线渲染。

### 11.2 刀路算法

| 步骤 | 做法 |
| --- | --- |
| 1. 轮廓筛选 | `cutType=auto/outer/inner`；开放轮廓只按中心线切、不做半径补偿 |
| 2. 刀具补偿 | 逐条调引擎 `offset(±R)`；`side=auto` 时外轮廓 +R、内孔 −R（孔变小）；`side=on` 或不闭合轮廓不补偿；单条失败不拖垮整板，记入 `skippedCount` / `skippedNote` |
| 3. 方向 | `direction=ccw/cw` 用有向面积判定，不符则反转点序（开放轮廓保持原走向） |
| 4. 起割点吸附 | `startPoints[]` 先吸附到最近轮廓的最近点（带 `contourId` 时限定该轮廓）；排序时作为这条轮廓的固定入口；未指定则自动取离当前刀位最近的投影点 |
| 5. 排序 | 从 `(0,0)` 起贪心选“入口最近”的轮廓；`optimize=false` 保持输入顺序；两种顺序各算一次空程，差值即 `orderGainMm` |
| 6. 旋转起点 | `_rotate_to_start`：吸附点落在顶点上直接重排点序，落在段中间则把投影点插到点首，闭合路径首尾仍重合 |
| 7. 微连接 | 桥中心按周长均分（从半间距起，避开起割点），按弧长切段；桥总长超过周长一半时自动收敛数量，段数不足则退回不拆 |
| 8. Z 分层 | `layers = ceil(depth / passDepth)`，每层 Z = −min(depth, passDepth × 层号)，每层完整走一遍全部轮廓 |
| 9. 切入圆弧 | 只在本轮廓第一条切削段加，半径取 `min(leadRadius, 短边 × 0.25)`，从“空区”一侧半圆切入并与走刀方向相切；跨微连接后直接下刀 |
| 10. 运动输出 | `rapid`（安全高度快移）→ `plunge`（下刀）→ `cut`（首条补一个起点→起点的切削，保证文件从起割点开始）→ `retract`；桥处先抬到安全高度再快移跨过 |

> 补偿后的刀路是独立数据：`/api/toolpath` 只读 `polyline`，不改动原始轮廓。

### 11.3 参数（`POST /api/toolpath`）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `source` | `doc` | `doc` 图纸 / `layout` 排料板整板 |
| `ids` / `layerIds` | 空 | 只加工这些轮廓；`source=layout` 时 `ids` 是排料轮廓 id |
| `sheetIndex` | 0 | 多板排料时选第几块板 |
| `cutType` | `auto` | `auto` / `outer` / `inner` |
| `direction` | `ccw` | `ccw` / `cw`；带刀具时可传 `climb`（顺铣）/ `conventional`（逆铣），由后端按内外轮廓映射 |
| `toolDiameter` | 6 | 刀具直径 mm，补偿半径 = 直径 / 2；**带 `toolId` 时由刀具接管** |
| `toolId` / `templateId` | 空 | 模块 6：加载刀具 / 工艺模板（优先级：刀具 > 模板 > 请求） |
| `side` | `auto` | `auto` / `outside` / `inside` / `on` |
| `depth` / `passDepth` | 18 / 3 | 总切深 / 每层切深 mm |
| `feed` / `plungeFeed` | 3000 / 800 | 切削 / 下刀进给 mm/min |
| `safeZ` / `liftZ` / `spindle` | 5 / 5 / 18000 | 安全高度 / 抬刀高度 mm / 主轴转速 rpm |
| `leadIn` / `leadRadius` | true / 2 | 圆弧切入开关与半径 |
| `microJoint` / `microJointWidth` / `microJointCount` | false / 2 / 2 | 微连接开关、桥宽、桥数量 |
| `optimize` | true | 贪心排序；关掉则保持输入顺序 |
| `startPoints` | `[]` | `[{x, y, contourId?}]`，手工起割点 |

参数校验由 pydantic 完成：`toolDiameter ≥ 0`、`passDepth > 0`、`microJointWidth ≥ 0`、
`microJointCount ∈ [0,20]`、`direction ∈ {ccw,cw,climb,conventional}`，越界直接 422。

### 11.4 任务流与前端

```
POST /api/toolpath
  → task_queue("toolpath", toolpath_job, params)
     → 轮廓筛选 → 补偿 → 方向 → 排序/起割点 → 逐层逐轮廓切段 → 统计
  → TASK_PROGRESS (3% 分类 / 12% 补偿 / 20% 排序 / 20~92% 逐层逐条 / 97% 预览)
  → TOOLPATH_PREVIEW（BGC1：9001 切削 / 9002 抬刀 / 9003 起割点；
     meta：stats/contours/bridges/startPoints/source/sheet/layers）
  → TASK_RESULT（stats + 同一份 meta，不含 polylines）
```

取消走 `POST /api/tasks/{taskId}/cancel`：任务在每个轮廓/每层结束处调
`check_cancelled()`，抛 `TaskCancelled` 后回 `TASK_FAILED(status=cancelled)`，
不会把半份刀路写进 `state.LAST_PLAN`。

前端分工：

| 位置 | 职责 |
| --- | --- |
| `ui/useToolpathActions.ts` | `buildToolpathRequest()` 统一组装参数（按当前视图判定数据源，预览态按 `toolpathPreview.contours` 过滤选中 id）；`generate/cancel` 带并发保护 |
| `ui/ToolpathPanel.tsx` | 参数表单、微连接、起割点列表（点选/删除/清空）、图层图例、统计（切削/空程/桥/排序节省/工时）、重新生成与取消 |
| `render/viewport.ts` | 起割点点选模式（`beginPointPick` 回传世界坐标、Esc 退出、十字光标）+ 已选点标记；整板预览用 `board` 画板框 |
| `render/geometry-layer.ts` | `dashed:true` 图层自绘虚线（7px 实 / 5px 空，按缩放换算世界长度）；`operation=marker` 的闭合小轮廓实心填充成菱形 |
| `state/app-store.ts` | `applyPreview` 识别刀路 meta：图层/板框/统计/起割点一起入 store，`viewMode='preview'` |

预览折线的 id 分段：切削沿用原始轮廓 id（点选起割点可直接回带 `contourId`），
抬刀从 10000000 起、起割点从 20000000 起，与图纸（1..N）和排料（200000+）天然隔离。

### 11.5 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\toolpath_test.py

# 服务层 + 接口/WS 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\toolpath_test.py --url http://127.0.0.1:8975
```

实测输出（OCCT 7.7.2 + shelf 引擎，1220×2440 板）：

```
== A. 服务层：刀路生成 ==
  [OK]   A1 外轮廓补偿  包围盒 (-5.00, -5.00) - (105.00, 65.00) / outside
  [OK]   A2 内孔补偿  内孔刀路包围盒 (35.00, 15.00) - (65.00, 45.00)
  [OK]   A3 切割方向  CCW 面积 1600.0 / CW 面积 -1600.0
  [OK]   A4 手工起割点  首刀 (100.000, 30.000)，末刀回到同一点
  [OK]   A5 微连接  切 3 段 / 桥 2 个 / 空走 20.0mm
  [OK]   A6 路径排序  空程 2420.0 -> 1000.0mm（节省 1420.0mm）
  [OK]   A7 刀路类型  内孔 1 条 / 外轮廓 1 条 / 无孔报错 True
  [OK]   A8 取消刀路  第 5 次进度回调中止
  [OK]   A9 整板刀路  9 条轮廓 / 外 5 / 内 4 / 切削 15289mm
== B. 接口 / WS 广播 ==
  [OK]   B1 接口提交  751290036b24 · 80 段预览 / 桥 9 / 切削 10322mm
  [OK]   B2 参数校验  负微连接宽度 / 非法方向均被拒绝
  [OK]   B3 取消接口  未知任务 404 / 已结束任务 409
  [OK]   B4 整板接口  6253615c2c40 · 9 条轮廓 / 板材 1220x2440
== 结果：13/13 通过 ==
```

A1/A2 验证补偿方向（外扩到 −5mm、内缩到 35mm）；A5 断言 2 个 10mm 桥把周长 300mm
切成 3 段、抬刀跨越距离正好等于桥宽；A6 用“远-近-中”乱序输入验证排序收益；
A8 用假 reporter 在第 5 次进度回调抛 `TaskCancelled`，验证不产出半份结果；
B1 检查 9002 图层带 `dashed:true` 且 9003 存在；B4 走 `source=layout`，回包带 `sheet`。

脚本会用示例图形重建当前工程并做一次排料，结束时板材恢复为 1220×2440 / 余量 10 / 间距 5。

---

## 十二、刀具库与工艺模板（模块 6）

### 12.1 目标与分层

刀具是“机床事实”，工艺模板是“物料经验”：同一块板下次再用，选模板即可复现
上一次的刀、补偿、微连接与排序方式，不再凭手感改数。

```
models/db.py        纯 SQL（tool / process_template 两张表）
tool_service.py     业务校验、等效直径换算、参数解析（本模块核心）
api/routes_tools    REST 搬运与错误翻译（ToolError -> 400/404）
toolpath_service    只认已解析好的扁平参数，不再回查数据库
```

### 12.2 数据模型

`tool`（刀具库）：

| 字段 | 说明 |
| --- | --- |
| `category` | `mill` 铣刀 / `wire` 线切割钼丝 |
| `tool_type` | `flat` 平底 / `ball` 球刀 / `v` 锥刀（wire 忽略） |
| `diameter` / `tip_radius` | 铣刀直径 / 刀尖圆角 mm |
| `wire_diameter` / `discharge_gap` | 钼丝丝径 / 放电补偿间隙 mm |
| `feed` / `plunge_feed` / `spindle` | 切削进给 / 下刀进给 / 主轴转速（wire 恒为 0） |
| `pass_depth` | 每层切深 mm |
| `safe_z` / `lift_z` | 安全高度（快移） / 抬刀高度（收起切削层） |
| `direction` | `climb` 顺铣 / `conventional` 逆铣 |
| `note` / `updated_at` | 备注 / 修改时间 |

`process_template`（工艺模板）：`name` / `material`（密度板、无纺布、多层板…）/
`tool_id`（改绑不删刀，删刀时 SET NULL）/ `params`（JSON） / `note`。

模板只保存 8 个补偿与切段字段，其余全部由刀具提供：

```
cutType  side  optimize  microJoint
microJointWidth  microJointCount  leadIn  leadRadius
```

> 旧库升级：`_migrate()` 用 `PRAGMA table_info` 探测后 `ALTER TABLE` 补 9 个新列；
> `_seed_tool_library()` 幂等补齐 3 把默认刀（T1 平底 6mm / T2 平底 3mm /
> W1 钼丝 0.2mm）与 3 个模板（密度板标准 / 多层板微连接 / 无纺布钼丝），
> 已有数据一律不动，只补缺失的 wire 刀并修复内置无纺布模板的绑刀。

### 12.3 三条关键换算

| 规则 | 做法 |
| --- | --- |
| 等效直径 | 铣刀 = `diameter`；钼丝 = `wire_diameter + 2 × discharge_gap`（补偿半径 = 丝径/2 + 间隙），统一换算成一个直径交给刀路做偏置 |
| 顺逆铣 | 顺铣：外轮廓 CCW、内孔 CW；逆铣反过来。`_enforce_direction` 只反转点序，原始图形不动 |
| 参数优先级 | **刀具 > 模板 > 请求默认值**；显式 `toolId` 覆盖模板绑定的刀，`source`/`ids`/`startPoints`/`depth` 始终用请求值 |

验收里的典型值：钼丝 0.2mm + 间隙 0.03 → 等效 ⌀0.26 / 补偿半径 0.13 / 主轴 0；
10mm 平底刀切 100×60 方框，刀路包围盒变为 (−5, −5)–(105, 65)。

### 12.4 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tools` | `{projectId, tools[]}`，每项含 `effectiveDiameter` / `compensationRadius` |
| GET | `/api/tools/{id}` | 单把刀具（驼峰字段） |
| POST | `/api/tools` | 新建；铣刀校验直径 > 0、圆角 ≤ 半径，wire 丝径 > 0 且主轴置 0 |
| PUT | `/api/tools/{id}` | 修改同上 |
| DELETE | `/api/tools/{id}` | 被模板引用时拒绝：「还有 N 个工艺模板在使用该刀具」 |
| GET | `/api/templates` | `{projectId, templates[]}`，模板内嵌 `tool` 快照 |
| POST | `/api/templates` | 新建；`toolId` 不存在直接拒绝 |
| PUT | `/api/templates/{id}` | 修改 |
| DELETE | `/api/templates/{id}` | 删除 |

刀路与导出不需要新路径，沿用 `POST /api/toolpath` / `POST /api/export`，只是多带：

```json
{
  "source": "doc",
  "toolId": 2,
  "templateId": 5,
  "depth": 18
}
```

解析在提交时一次性完成（`_toolpath_params`），任务参数里固化 `tool` / `template`
快照：任务排队期间改刀，不会影响已排队的刀路。

### 12.5 前端

| 位置 | 职责 |
| --- | --- |
| `ui/useToolLibraryActions.ts` | 拉取/新建/保存/删除；`toolToParams` / `templateToParams` 把选中项写回刀路面板；`TOOL_OWNED_KEYS` / `TEMPLATE_OWNED_KEYS` 定义接管范围 |
| `ui/ToolLibrary.tsx` | 双页签弹窗（刀具库 / 工艺模板）：左列表右表单、补偿量预览、二次确认删除、应用到刀路、Esc/遮罩关闭 |
| `ui/ToolpathPanel.tsx` | “刀具与工艺”区：刀具/模板下拉、只读补偿量、管理刀具库入口；被接管的字段置灰不可改 |
| `ui/Toolbar.tsx` | “刀具库”按钮 |
| `state/app-store.ts` | `tools` / `templates` / `toolLibraryOpen` 与 setter |

接管范围：

- 选刀具 → `toolDiameter` `feed` `plungeFeed` `spindle` `passDepth` `safeZ` `liftZ` `direction` 置灰
- 选模板 → `cutType` `side` `optimize` `microJoint` `microJointWidth` `microJointCount` `leadIn` `leadRadius` 置灰
- 下拉选「不绑定」即恢复手改；微连接总开关关闭时，桥宽/数量联动禁用

> 抬刀高度 `liftZ` 与安全高度 `safeZ` 分离：微连接处只抬到 `liftZ`，跨完落回，
> 长距离空移才回 `safeZ`；导出时 `G0` 用 `safeZ`。

### 12.6 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\tool_library_test.py

# 服务层 + 接口/WS 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\tool_library_test.py --url http://127.0.0.1:8975
```

实测输出：

```
== A. 服务层：刀具库与工艺模板 ==
  [OK]   A1 铣刀换算  等效 ⌀10.00 / R5.00；非法直径与刀尖圆角被拒绝
  [OK]   A2 钼丝换算  丝径 0.2 + 间隙 0.03 -> 等效 ⌀0.260 / R0.130 / 主轴 0
  [OK]   A3 模板 CRUD  参数白名单 8 项；非法 toolId 被拒绝
  [OK]   A3b 模板修改  名称/物料已更新
  [OK]   A4 参数优先级  刀具 ⌀5.0 / feed 2222；模板 cutType=inner side=inside
  [OK]   A5 顺逆铣映射  顺铣 外ccw/内cw；逆铣 外cw/内ccw
  [OK]   A6 刀具驱动补偿  包围盒 (-5.00, -5.00) - (105.00, 65.00) / outside
  [OK]   A7 线切割补偿  包围盒 (-0.130, -0.130) - (100.130, 60.130) / cw
  [OK]   A8 抬刀高度  retract Z=[2.0] / rapid Z=[8.0] / cut Z=[-3.0]
  [OK]   A9 模板微连接  桥 3 / 分段 4（模板 3 个桥）
  [OK]   A10 删除保护  被模板引用的刀具删除被拒绝，解绑后可删
  [OK]   A11 未知引用  不存在的刀具/模板都明确报错
== B. 接口 / WS 广播 ==
  [OK]   B1 刀具接口  更新/读取/列表字段齐全，等效 ⌀10.0
  [OK]   B2 模板接口  模板内嵌刀具 …；修改后物料 多层板
  [OK]   B3 刀路读刀具  … · 切削 351.4mm（10mm 刀外扩 5mm）· 工具 …
  [OK]   B4 模板一键加载  … · 桥 2 / 分段 3
  [OK]   B5 导出读刀具  文件 … 使用刀具进给 1234 / 安全高度 7
  [OK]   B6 参数校验  不存在的 toolId -> HTTP 400
  [OK]   B7 删除保护  被引用 -> 400；解绑后 -> 200
== 结果：19/19 通过 ==
```

A1/A2 验证两种刀的等效直径与非法参数；A4 用「请求 99mm + 模板内孔 + 显式 5mm 刀」
验证三级优先级；A9 把 3 个桥存进模板，加载后不用再传微连接参数；B3 校验 10mm 刀
外扩 5mm 后的切削长度 351.4mm（四角圆弧过渡），B5 校验导出文件里的 `F1234` 与 `G0 Z7`
确实来自刀具。脚本创建的临时刀具/模板在 `finally` 里逆序清理。

---

## 十三、后处理与导出（模块 7）

### 13.1 目标与分层

后处理不是几何问题，而是“把运动翻译成机床能读的文本”。所以本模块不做成代码分支，
而是配置驱动：**一台机床 = 一份 JSON**，新增设备只加配置，不改刀路内核。

```
post/builtin.py      6 份内置模板（代码常量，首次启动落盘）
post/registry.py     注册表：JSON 读写/校验/版本升级/内置只读保护（本模块核心）
post/renderer.py     G 代码与 HPGL 渲染：header -> toolChange -> moves -> footer
post/dxf_writer.py   ezdxf 写回原始图形（不走文本渲染）
post/service.py      文件名清洗/时间戳/多板 S1 标记/落盘
api/routes_post.py   /api/post CRUD，PostError -> 400/404
api/routes_cam.py    /api/export 编排：单板 / 多板批量 / DXF 分支
```

存储位置：`%LOCALAPPDATA%\BtCAM\post\<id>.json`。内置模板带 `version`，启动时
`ensure_builtin()` 比对版本覆盖旧文件；自定义配置（`builtin=false`）永不被覆盖。

### 13.2 配置格式

公共字段：`id`（2~40 位小写字母/数字/下划线）、`name`、`kind`（`gcode`/`hpgl`/`dxf`）、
`ext`（1~8 位字母数字，不带点）、`description`、`builtin`、`version`。

G 代码（`kind=gcode`）：

| 字段 | 说明 |
| --- | --- |
| `header` / `footer` | 程序头/尾，一行一条模板 |
| `toolChange` | 换刀指令；只有渲染上下文带刀具号时才插入 |
| `programNo` | 程序号，模板里填 `{programNo}` |
| `lineNumbers` / `lineNumberStart` / `lineNumberStep` | 行号开关与起始/步长（`%` 与空行不加） |
| `precision` | 坐标小数位 0~6，输出时去掉多余的 0 |
| `rapidZ` / `rapidXY` / `plungeZ` / `feed` / `cutXY` | 五条运动指令模板，必须非空 |

HPGL / 裁割（`kind=hpgl`）：

| 字段 | 说明 |
| --- | --- |
| `unitsPerMm` | 每毫米指令单位（绘图仪 40） |
| `penUp` / `penDown` / `penLift` | 抬笔（可带 `{x}{y}`）/ 落笔 / 断笔 |
| `header` / `footer` | 程序头尾；裁割用 `VS{speed};` / `FS{force};` 写速度压力 |

DXF（`kind=dxf`）：只有 `version`（R2000/R2010/R2013）。

模板占位符白名单（保存时用 `string.Formatter` 解析校验，未知占位符直接拒绝）：

```
{name} {date} {programNo} {tool} {spindle} {safeZ}
{x} {y} {z} {feed} {speed} {force}
```

### 13.3 三种渲染器

- **G 代码**：header -> toolChange（有刀号）-> 运动 -> footer -> 行号。
  运动按状态机去重：Z 变了才出抬刀/下刀，进给变了才出 `F`；微连接抬刀写成 `G0`。
  行号跳过 `%` 与空行，首条指令默认 `N10`。
- **HPGL**：坐标乘 `unitsPerMm` 四舍五入取整；切削/下刀落笔，快移抬笔；
  `flipY` 时 Y 取反（绘图仪原点在左下）。
- **DXF**：不渲染文本，直接用 ezdxf 建 LWPOLYLINE，保留图层名/颜色/闭合，
  `$INSUNITS=4`（毫米）。这条分支与刀路参数无关，导的是图纸原始轮廓。

### 13.4 内置模板

| id | 名称 | kind | 扩展名 | 关键特征 |
| --- | --- | --- | --- | --- |
| `weihong_nc` | 维宏雕刻机 | gcode | .nc | `%` 包围、G21/G90/G17/G40、M3/M5/M30 |
| `syntec_nc` | 新代雕刻机 | gcode | .nc | `O{programNo}`、行号 N10/N20、`T{tool} M6` + `G43 H{tool}`、G54、M08/M09 |
| `shanlong_nc` | 山龙雕刻机 | gcode | .nc | `G00`/`G01` 两位写法、`M03 S{spindle}`、`T{tool} M06` |
| `plotter_plt` | 绘图仪 | hpgl | .plt | `IN;`/`SP1;`、40 unit/mm、PU/PD |
| `cutting_cut` | 裁割设备 | hpgl | .cut | 兼容 AlphaCAM：`VS{speed};` / `FS{force};` |
| `generic_dxf` | 通用 DXF | dxf | .dxf | R2010、按图层分色 |

### 13.5 导出编排

`POST /api/export` 是同步接口（文件写入很快，且要立刻拿到路径）：

1. `processorId` 优先；只给 `format` 时映射内置（`nc→weihong_nc`、`plt→plotter_plt`、`cut→cutting_cut`）；
2. `kind=dxf` 走图形分支：读工程图形 → `export_drawing()`，不按板材拆分；
3. 刀路分支：`_toolpath_params()` 解析刀具/模板 → `build_toolpath()` → `render()`；
4. `source=layout && allSheets`：读全部板材号，每板生成一个文件；
5. 返回 `{path, format, processor, files[], stats, tool, template}`，`files[]` 供前端列文件。

文件名：单板 `{name}_{YYYYmmdd_HHMMSS}.{ext}`；多板追加 `_S{n}`（板号从 1 起），
同一秒重名自动加 `_1`。`name` 经 `safe_name()` 过滤 `<>:"/\|?*` 与控制字符。

### 13.6 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/post` | `{processors[]}`，内置在前，含完整模板 |
| GET | `/api/post/{id}` | 单份配置 |
| POST | `/api/post` | 新建自定义 |
| PUT | `/api/post/{id}` | 修改自定义；内置返回 400「请另存为」 |
| DELETE | `/api/post/{id}` | 删除自定义；内置返回 400 |

导出仍走 `POST /api/export`，新增字段：

```json
{
  "processorId": "syntec_nc",
  "format": "nc",
  "name": "柜体A",
  "allSheets": true,
  "flipY": false,
  "speed": 30,
  "force": 20
}
```

### 13.7 前端

| 位置 | 职责 |
| --- | --- |
| `ui/usePostActions.ts` | 列表缓存、保存/删除、`exportNow`（复用最近一次刀路请求；批量时清空 `ids`） |
| `ui/PostPanel.tsx` | 双页签弹窗：「导出」选机床/文件名/批量/速度压力/Y 翻转 + 结果列表；「后处理配置」左列表右编辑器 |
| `ui/Toolbar.tsx` | 「后处理…」入口 + NC/PLT/CUT/DXF 四个快捷导出 |
| `net/rest.ts` | `PostProcessorRecord` / `ExportResult` 类型与 `/api/post` CRUD |
| `state/app-store.ts` | `postProcessors` / `postOpen` / `lastExport` |

内置配置在编辑器里只读；「另存为」自动生成不冲突的 id，再保存即形成自定义机床。

### 13.8 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\post_test.py

# 服务层 + 接口全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\post_test.py --url http://127.0.0.1:8975
```

实测输出：

```
== A. 服务层：后处理配置与渲染 ==
  [OK]   A1 内置注册表  6 份配置 / 6 份内置 / 重复落盘 0
  [OK]   A2 维宏 G 代码  24 行 / 头 ['%', '( BTCAM 2D CONTOUR - POST_TEST )', ...]
  [OK]   A3 行号与换刀  带行号 27 行 / 首行 N10 O0001
  [OK]   A4 小数精度  precision=2 -> G1 X33.33 Y0
  [OK]   A5 HPGL  13 条指令 / 首尾 ['IN;', 'SP1;'] ... ['PU0,0;', 'SP0;']
  [OK]   A6 CUT 速度压力  VS25 / FS30 / SP0
  [OK]   A7 Y 轴翻转  正常 PD0,400 / 翻转 PD0,-400
  [OK]   A8 DXF 写回  2 条写入 / 2 条读回 / 19116 字节
  [OK]   A9 配置校验  已拒绝：未知占位符、非法 kind、内置不可改、内置不可删
  [OK]   A10 占位符填充  name/date/safeZ/spindle 全部生效
== B. 接口：后处理 CRUD 与导出 ==
  [OK]   B1 后处理列表  6 份 / kinds=dxf,gcode,hpgl
  [OK]   B2 自定义 CRUD  新建/读取/修改/删除 + 删除后 404
  [OK]   B3 NC 导出  POST_TEST_20261023_193838.nc（975 字节）
  [OK]   B4 DXF 导出  POST_TEST_DXF_20261023_193838.dxf（9 条轮廓）
  [OK]   B5 批量多板  2 个文件：..._S1.nc / ..._S2.nc
  [OK]   B6 未知后处理  POST /api/export -> HTTP 400
  [OK]   B7 内置保护  修改 400 / 删除 400
== 结果：17/17 通过 ==
```

A4 验证精度与去尾零；A5/A7 用 100×60 方框核对 HPGL 坐标（40 unit/mm -> 4000×2400）
与 Y 翻转；B5 直接把一个零件挪到 2 号板，验证一板一个文件；B7 校验内置模板的只读保护。
脚本创建的自定义后处理与导出文件在 `finally` 里清理。

### 13.9 新增一台机床

1. 在「后处理配置」里选一个同类内置模板 → 另存为；
2. 改程序头/尾、换刀指令、行号与精度（HPGL 改 `unitsPerMm` 与笔指令）；
3. 保存后在「导出」页选中它，先导一个小件；
4. 上机空跑确认 M 代码与坐标方向，再直接改 `%LOCALAPPDATA%\BtCAM\post\<id>.json` 微调。

---

## 十四、工单存档（模块 8）

### 14.1 目标与边界

工单 = 当前工程的一份**只读快照**，不是对工程的引用：

- 继续改图、重新导入、重做排料都不会改变已保存的工单；
- 「打开工单」是**整份替换**当前工程（与导入 DXF 同一语义），历史工单本身不受影响，可反复打开；
- 恢复后零件/轮廓会拿到新 id，所以快照只存**参数**（不存 `ids` / 选中集 / 临时轮廓），刀路按当前工程重新生成。

MES 对接（CenkorMes / LightMes）是 V0.4 的可选扩展：本模块只做本地 SQLite 存档，不引入任何网络接口。

### 14.2 快照内容

| 键 | 内容 |
| --- | --- |
| `version` / `capturedAt` | 快照版本（当前 1）与采集时间 |
| `sheet` | 板材宽/高/四边余量/最小间距 |
| `layers` | 图层 id/名称/颜色/工艺/可见/锁定 |
| `parts` | 零件 id/名称/来源/包围盒 + 轮廓（`layerId`/`closed`/`isHole`/`points`） |
| `loosePolylines` | 未归属零件的散轮廓 |
| `placements` | 摆放 `partId`/`sheet`/`tx`/`ty`/`rot`/`locked`/`fixedRot`/`zorder` |
| `tool` / `template` | 采集时的刀具与工艺模板完整对象（引用 + 快照） |
| `toolpath` | 21 个刀路参数（白名单，起割点最多 64 个） |
| `post` | `processorId`/`format`/`name`/`flipY`/`speed`/`force` |
| `stats` | 零件/轮廓/点数/图层/摆放/利用率/板宽高 |

### 14.3 存储与编解码

两张表（`models/db.py`）：

| 表 | 主键 | 字段 |
| --- | --- | --- |
| `work_order` | `id` | `project_id`（级联删除）、`name`、`code`、`note`、`snapshot`（JSON）、`stats`（JSON）、`created_at`、`updated_at` |
| `work_order_file` | `id` | `order_id`（级联删除）、`path`、`format`、`processor`、`bytes`、`sheet_index`、`created_at` |

顶点不按 JSON 文本存：`encode_points()` 把 `(N,2)` float32 按小端连续内存直接 `base64`，
`decode_points()` 还原成 numpy 数组。与 BGC1 协议同一套数制，快照体积约为文本形式的 1/10。

### 14.4 服务分层与恢复流程

```
models/db.py               工单两张表 + CRUD（list 带 file_count 子查询）
work_order_service.py      ★ 快照采集/恢复/检索/另存/文件归档
api/routes_workorders.py   REST 搬运，WorkOrderError -> 400 / 404
api/routes_cam.py          导出成功后按 workOrderId 登记文件
```

| 方法 | 说明 |
| --- | --- |
| `capture(conn, pid, toolpath, post)` | 把当前工程采集成快照字典（不落库） |
| `create_order` / `update_order` | 保存/更新；`recapture=false` 时只改元信息，未传 `post` 则沿用存档 |
| `list_orders` / `get_order` / `list_files` | 检索、详情、加工文件列表 |
| `duplicate_order` | 快照深拷贝，默认命名为「原名 + 副本」 |
| `remove_order` | 先删文件登记再删工单 |
| `open_order` | 快照写回当前工程，返回刀路参数/后处理/`missing` |
| `add_export_files` | 导出结果登记；工单已被删则静默跳过，不阻断导出 |

打开工单的完整链路：

```
POST /api/workorders/{id}/open
  → 读快照 → ensure_layer（按名字复用，写回颜色/工艺/可见/锁定）
  → clear_document（清空当前工程图形）
  → 逐零件 create_part + save_part_polylines（顶点解码，layerId 重映射）
  → 散轮廓 save_polylines → 板材 save_sheet
  → 摆放 save_placement（part_id 按新 id 重映射，保留 rot/locked/fixedRot）
  → 校验刀具/模板引用：已被删则记入 missing 并把 toolId/templateId 置 null
  → push_document()（GEOMETRY_DOC 全量刷新画布）
```

### 14.5 检索规则

- `keyword` 对 `name` / `code` / `note` 做 `LIKE '%kw%'`，空串不过滤；
- `dateFrom` / `dateTo` 按**创建时间**过滤，`dateTo` 取次日 0 点（右开区间），日期格式 `YYYY-MM-DD`；
- 结果按 `created_at DESC, id DESC` 排序，`limit` 默认 100、上限 500。

### 14.6 加工文件归档

导出接口不需要新增路径：给 `POST /api/export` 带上 `workOrderId` 即可。

```json
{
  "processorId": "weihong_nc",
  "format": "nc",
  "name": "柜体A",
  "workOrderId": 12,
  "allSheets": true
}
```

导出成功后 `routes_cam._record()` 把 `files[]` 写入 `work_order_file`（记录路径/格式/后处理/字节数/板号）。
登记失败只记 warning：导出本身已经落盘，不能因为工单被删而回滚文件。

### 14.7 前端

| 位置 | 职责 |
| --- | --- |
| `ui/useWorkOrderActions.ts` | 列表/详情/保存/更新快照/打开/另存/删除；打开后回填刀路参数与后处理 |
| `ui/WorkOrderPanel.tsx` | 弹窗：左侧保存表单 + 检索 + 工单列表，右侧统计/备注/缺失提示/加工文件/操作 |
| `ui/Toolbar.tsx` | 「工单…」入口；导入 DXF 时解除当前工单 |
| `ui/PostPanel.tsx` | 打开工单后用存档的后处理与速度/压力初始化导出表单 |
| `ui/usePostActions.ts` | 导出自动携带 `workOrderId`，成功后记录导出预设 |
| `net/rest.ts` / `state/app-store.ts` | 工单类型与接口、`workOrders` / `activeWorkOrder` / `exportPreset` |

删除是二次确认（按钮先变「再点一次确认删除」），打开工单成功后关闭弹窗；
`missing` 非空时状态栏直接给出「刀具/模板已不在库，请重新选刀」。

### 14.8 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\workorder_test.py

# 服务层 + 接口全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\workorder_test.py --url http://127.0.0.1:8975
```

实测输出：

```
== A. 服务层：快照 / 检索 / 打开 ==
  [OK]   A1 快照采集  5 个零件 / 5 条摆放 / 9 条轮廓
  [OK]   A2 保存与检索  关键词 1 / 编号 1 / 当日 1 / 区间外 0
  [OK]   A3 重复打开  恢复 5 零件 / 9 轮廓 / 5 摆放 / 板宽 1220
  [OK]   A4 另存  #19 -> #20「验收工单 A 副本」
  [OK]   A5 文件归档  登记 2 个文件 / 列表 fileCount=2
  [OK]   A6 参数清洗与校验  改名保留快照 / 空名称拒绝 / 未知工单拒绝
  [OK]   A7 删除  副本/原单删除 / 重新查询 0 条
== B. 接口：工单 CRUD / 打开 / 文件归档 ==
  [OK]   B1 保存接口  #21 5 零件 / 9 轮廓
  [OK]   B2 列表检索  关键词 1 / 编号 1 / 当日 1
  [OK]   B3 详情与改名  详情文件 0 / 改名后零件 5
  [OK]   B4 打开接口  恢复 5 个零件 / 刀路深度 9.0
  [OK]   B5 导出登记  WO_API_20261023_211845.nc（972 字节）
  [OK]   B6 另存接口  #21 -> #22
  [OK]   B7 参数校验  空名称 422 / 未知工单 404 / 非法日期 400
  [OK]   B8 删除接口  删除后 GET -> 404

== 结果：15/15 通过 ==
```

A1 用 `np.allclose` 校验顶点 base64 往返，并断言 `ids` / `contours` / 选中集不落库；
A3 先清空工程、把板宽改成 600，再打开工单，验证板材/摆放/轮廓全部还原；
A5 的 `fileCount` 同时出现在列表与详情里；B5 走真实导出（带 `workOrderId`）后回查文件列表。
临时工程与工单在 `finally` 里显式删除，不会污染本地库。

---

## 十五、运行日志与告警（模块 9）

### 15.1 目标与边界

- 面板里能看：**图形校验报错**（自相交/未闭合）、**排料与刀路进度**、接口与计算异常；
- 报错要**主动弹窗**：用户不看画布、不看控制台，也不能漏掉「这批图有自相交」；
- 前端异常（渲染报错、未捕获 Promise）同样进日志，便于事后定位；
- 不做的：日志检索后端落库、级别热切换、上报脱敏（日志只在本机，不联网）。

### 15.2 三层结构

```
logging（各模块 logger）
   └─ LogRingHandler ──> LogStore（deque，默认 1000 条，线程锁）
                            ├─ GET /api/logs  ← 日志面板轮询
                            └─ ALERT 帧 0x50   ← warning 以上即时弹窗
```

| 位置 | 职责 |
| --- | --- |
| `core/log_store.py` | ★ 环形缓冲、级别/关键词/来源过滤、警告去重、`alert()` 与 `task_progress()`、`LogRingHandler` |
| `core/logging_setup.py` | 控制台 + `logs/backend.log`（4MB×3 滚动）+ 环形缓冲三个 handler；强制 UTF-8 |
| `core/tasks.py` | 入队/开始/完成/取消/失败记日志；`ProgressReporter` 按阶段写进度日志 |
| `services/import_service.py` | 校验异常汇总后 `alert()`，文案形如「图形校验发现 N 处异常（自相交 x 处）」 |
| `api/routes_logs.py` | `GET /api/logs`、`DELETE /api/logs`、`POST /api/logs/client` |
| `ui/LogPanel.tsx` / `ui/AlertDialog.tsx` | 日志面板与告警弹窗 |

**为什么不让前端读日志文件**：文件格式随 formatter 变化，前端解析等于把格式再耦合一次；
结构化条目才能按级别/来源过滤，也才能直接复用为 ALERT 帧的 meta。

### 15.3 数据模型与查询

一条日志 = `{ id, time, level, source, message, taskId, kind, detail }`：

| 字段 | 说明 |
| --- | --- |
| `id` | 进程内自增，前端按它去重与增量拉取 |
| `level` | `debug` / `info` / `warning` / `error` / `critical`（`WARN`/`ERR`/`FATAL` 自动归一） |
| `source` | 来源：logger 名（`app.core.tasks`）、业务标签（`import` / `task` / `frontend`） |
| `taskId` / `kind` | 与任务关联时可回溯到具体任务与任务类型 |
| `detail` | 异常堆栈或明细（超长截断 2000 字符） |

`GET /api/logs` 的过滤规则：

- `level` 是**最低级别**（`level=warning` 含 warning/error/critical），空串表示全部；
- `keyword` 同时匹配文本与来源；`source` 是**前缀**匹配（`task` 能命中 `task` 与 `tasks.x`）；
- `after` 为增量游标，只返回 `id > after` 的条目；
- 返回按 id 升序的**最近** `limit` 条，并带 `seq`（当前最大 id）、`capacity`、`file`（磁盘日志路径）。

### 15.4 什么会进日志

| 场景 | 结果 |
| --- | --- |
| 任务入队/开始/完成/取消 | `info`（完成带耗时），失败为 `error` + 完整堆栈 |
| 任务进度 | `info`，形如「刀路生成 48% · 第 1/2 层 · 第 7/18 条轮廓」；按 **1.5s 或 8%** 节流 |
| 导入校验 | 一处 `warning` 汇总（「自相交 2 处、未闭合 1 处」），明细在 `detail` 里逐条列出轮廓号与坐标 |
| 排料/刀路异常 | `ValueError` 等计算异常由 `logger.exception` 记 `error`，自动弹窗 |
| 前端异常 | `window.onerror` / `unhandledrejection` → `POST /api/logs/client`，同文案 5 秒一次 |

进度日志的节流是必要的：刀路/排料是**按轮廓回调**的，逐条记录几分钟就能把 1000 条缓冲刷满，
真正有用的报错反而被挤掉。阶段文案（「刀具补偿完成」「生成预览数据」）仍会保留。

### 15.5 告警弹窗规则

- `error` / `critical`：**自动**推 ALERT 帧（不做任何业务判断）；
- `warning`：默认只进面板，由业务显式 `log_store.alert()` 推送（例如导入校验），避免普通提示刷成弹窗；
- 相同 `(source, level, message)` **3 秒内只推一次**，防止异常循环刷屏；
- 前端最多排队 5 条，一次弹一条，标题栏显示「还有 N 条待确认」；按钮为「查看日志」与「知道了」，`Esc` 关闭。

### 15.6 前端

| 位置 | 职责 |
| --- | --- |
| `ui/LogPanel.tsx` | 级别/来源下拉 + 关键词 + 刷新/测试写入/清空（二次确认）；每 2s 轮询，打开时才拉；点行展开 `detail`；底部显示容量与磁盘路径 |
| `ui/AlertDialog.tsx` | 告警队列弹窗，`error`/`critical` 配色区分，可直达日志面板 |
| `net/session.ts` | 订阅 `MsgType.ALERT`；注册全局 `error` / `unhandledrejection` 上报 |
| `state/app-store.ts` | `logs` / `logOpen` / `alerts` / `logCapacity` / `logFile`，按 id 合并去重，最多保留 500 行 |
| `ui/Toolbar.tsx` | 「日志…」入口 |

轮询而不是长连接推日志：日志量不稳定，轮询实现简单、可随时改过滤条件；
告警才走 WS，因为那是**必须立刻被看到**的信息。

### 15.7 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\log_alert_test.py

# 服务层 + 接口/WS 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\log_alert_test.py --url http://127.0.0.1:8975
```

实测输出：

```
== A. 服务层：缓冲 / 过滤 / 广播 ==
  [OK]   A1 环形缓冲  50 条 / seq=55 / 容量=50
  [OK]   A2 过滤查询  error 1 / 关键词 1 / 来源 50 / 增量 1
  [OK]   A3 告警广播  3 次推送（warning 去重 1 次，info 不推）
  [OK]   A4 logging 接入  2 条入环，异常堆栈 149 字符
  [OK]   A5 进度日志  2 条（文案变化才记）
  [OK]   A6 校验告警  图形校验发现 2 处异常（自相交 1 处、未闭合 1 处），已在画布标红
== B. 接口 / ALERT 帧 ==
  [OK]   B1 查询接口  total=6 seq=6 capacity=1000
  [OK]   B2 前端上报  #7 mod9-1792763725491 前端渲染失败
  [OK]   B3 过滤与增量  error+ 1 条 / after 之后 0 条
  [OK]   B4 参数校验  非法级别 400 / 空消息 422
  [OK]   B5 清空缓冲  清空 8 条，容量 1000
  [OK]   B6 ALERT 帧  0x50 #13 mod9-1792763725491 ALERT 帧校验

== 结果：12/12 通过 ==
```

A3 把 `hub.broadcast_threadsafe` 换成录制函数，断言「warning 需显式、error 自动、同文案被去重、info 不推」；
B6 用真实 WS 连接验证 `0x50` 帧的 meta 与接口返回的 `id` 一致。
测试结束会 `DELETE /api/logs`，不把验收噪音留给下次人工查看。

### 15.8 已知取舍

- 环形缓冲只在内存，**重启即清空**；长期留存看 `logs/backend.log`，面板底部直接展示路径；
- `BTCAM_LOG_BUFFER` 只能改容量，**日志级别仍需重启**（`BTCAM_LOG_LEVEL`）；
- 前端上报不做指纹聚类，只做 5 秒同文案限流；异常循环时仍可能占满缓冲，但已被限流压低到可接受程度。

---

## 十六、余料管理（模块 10）

排完一块板，剩下的料不该当废料扔掉。模块 10 把「板上还用得上的整块区域」算出来存进余料库，
下一张工单直接把它当板材，在余料轮廓里继续排零件。

### 16.1 目标与边界

- **算**：板材 − 已摆零件（按最小间距外扩）→ 若干可用区域，pyclipper 求差集，前端不做任何几何判断
- **存**：轮廓点串、包围盒宽高、净面积、板厚/物料/编号落 SQLite 的 `scrap` 表
- **用**：把余料设为当前板材（`layout_sheet.scrap_id` 记绑定），手动排料 / 自动排料 / 刀路 / 导出全部复用既有链路
- 余料轮廓就是**真实可用边界**：设板材时 `margin` 强制归零，再内缩一圈等于白白浪费
- 不做：余料拼接、跨工程共享（余料按 `project_id` 隔离）、条码/二维码标签

### 16.2 数据模型

`scrap` 表（`models/db.py`）关键列：

| 列 | 说明 |
| --- | --- |
| `outline` | JSON `[[x,y],...]`，**板材坐标**的闭合多边形（首点不重复） |
| `width` / `height` | 包围盒尺寸，就是「当板材」时的板面尺寸 |
| `area` | 轮廓净面积 mm²（pyclipper `Area` 算出，不是宽×高） |
| `source` | `layout` 从排料板抠出来 / `manual` 手工录一块方料 |
| `source_order_id` | 出处工单（当前写 0，给「这张单剩下的料」预留） |
| `code` / `material` / `thickness` | 贴标编号 / 物料 / 板厚 |

`layout_sheet` 增加 `scrap_id INTEGER NOT NULL DEFAULT 0`（0 = 普通矩形板），
老库由 `_migrate()` 补列，`load_sheet()` 一并读出，排料 meta 里叫 `scrapId`。

### 16.3 后端：剩余区域怎么算

`services/scrap_service.py` 的核心四步：

1. `_boundary_of(state)`：板材矩形按四边 `margin` 内缩 → SUBJECT
2. `_obstacles(state, gap)`：每块**已摆放**零件取旋转后包围盒，按 `gap/2` 外扩（相邻两件之间正好留一个间距）→ CLIP
3. `Clipper.Execute(CT_DIFFERENCE)` → `_simplify()` 去共线点 → 丢掉小于 `minArea` 的碎块 → 按面积降序、最多 30 块
4. 只把 `index` 回给前端；`save_regions()` 会**重新算一遍**再按 index 取，绝不信任前端回传的轮廓（防手改与版本错位）

`apply_to_sheet()` 把余料设为板材：宽高取包围盒、`margin=0`、写 `scrap_id`，然后清空摆放重做初始行排；
落在轮廓外的零件不删，统一停靠到板外上方（`_park_outside`），与自动排料「未排下」的表现一致。

真正的越界判定仍在 `layout_service._check` 里：它读到 `sheet.scrapId` 就去取轮廓，用 pyclipper 判断零件是否**整块**落在轮廓内，
所以 L 形余料的缺口、斜边都能拒绝（验收 D 系列就是这个用例）。

### 16.4 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/scrap` | 列表：`keyword`（名称/编号/物料）+ `limit`，带 `currentScrapId` |
| POST | `/api/scrap` | 新建（`outline` 为空时用宽高生成矩形，手工录料用） |
| GET | `/api/scrap/{id}` | 单块余料（含轮廓） |
| PUT | `/api/scrap/{id}` | 修改；宽高与面积由后端从轮廓重算 |
| DELETE | `/api/scrap/{id}` | 删除；**正被板材引用时自动解绑**，不留幽灵轮廓 |
| POST | `/api/scrap/remaining` | 剩余区域预览（`minArea` / `gap`），不落库 |
| POST | `/api/scrap/save` | 把 `indices[]` 选中的区域入库 |
| POST | `/api/scrap/{id}/apply` | 设为当前板材（`resetPlacements` 默认 true） |
| POST | `/api/scrap/clear` | 解绑回矩形板（可同时套用新尺寸） |

### 16.5 前端

| 文件 | 职责 |
| --- | --- |
| `ui/ScrapPanel.tsx` | 余料库列表（搜索/选用/编辑/删除）+「计算剩余区域」勾选区（面积/尺寸/点数）+ 保存表单；顶部显示当前板材是否绑着余料 |
| `ui/useScrapActions.ts` | 全部动作与请求；选用余料后拉排料快照，把轮廓交给画布 |
| `render/geometry-layer.ts` | 余料边界叠加层：橙色描边 + 半透明填充，缺料区从「空白」变成「明确不可用」 |
| `ui/Toolbar.tsx` | 「余料…」入口 |
| `ui/LayoutPanel.tsx` | 板材区显示余料绑定状态与「解绑」按钮 |

画布只按后端给的点串画线，不做任何包含/相交判断 —— 所有几何结论都来自后端。

### 16.6 控制台验收

```powershell
# 只跑服务层（不需要后端，秒级）
backend\.venv\Scripts\python.exe scripts\scrap_test.py

# 服务层 + HTTP 全链路（后端需先启动在 8975）
backend\.venv\Scripts\python.exe scripts\scrap_test.py --url http://127.0.0.1:8975
```

实测输出（**38/38**，节选）：

```text
== A. 余料入库 / 检索 ==
  [OK]   A1 手工矩形余料  手工方块 · 800×600 · 面积 480000 · 4 点
  [OK]   A2 轮廓归一化  首点 [0.0, 0.0] · 800×400
  [OK]   A4 非法轮廓拒绝  点不足/面积为零都抛 ScrapError
== B. 排板后的剩余区域 ==
  [OK]   B1 剩余区域非空  2 块 · 可用 2564660 mm² · 板 2976800 mm²
  [OK]   B4 勾选落库  测试余料 10-24 07:37-1 · 1200×2420 · 521 点
  [OK]   B5 非法勾选拒绝  空选 / 过期 index 都抛 ScrapError
== C. 余料当板材 ==
  [OK]   C2 余量归零并绑定 id  margin=0.0 · scrapId=24
  [OK]   C4 meta 下发余料  boundaryArea=2549085 mm²
== D. L 形余料边界判定 ==
  [OK]   D1 L 形余料当板材  包围盒 1000×1000 · 面积 640000 mm²（缺口 600×600 不可用）
  [OK]   D3 缺口内拒绝  零件「示例零件-2」超出余料轮廓（请放到余料范围内）
  [OK]   D5 轮廓内通过  落位 (20,700)
  [OK]   D6 板框外拒绝  零件「示例零件-2」超出板材右边界（可用宽度 1000.0mm）
== E. 复用排料接口：在余料内自动排料 ==
  [OK]   E1 确实排上了零件  5 落位 / 0 未排下 · 19 ms
  [OK]   E2 落位件全在余料轮廓内  5 件全部在轮廓内（含缺口避让）
== F. 解绑 / 删除 / 工单快照 ==
  [OK]   F3 打开工单恢复余料绑定  回开后 scrapId=25
  [OK]   F4 删除在用余料自动解绑  unbound=True · 业务不会挂着幽灵轮廓
== G. HTTP 接口全链路 ==
  [OK]   G4 排料 meta 带余料轮廓  轮廓 521 点 · boundaryArea=2549085 mm²
  [OK]   G6 越界拖拽被拒  零件「示例零件-2」超出板材右边界（可用宽度 1200.0mm）

结果：38 项通过 / 0 项失败 · 用时 0.9s
```

测试自己造余料、自己清场（删测试余料 + 测试工单并复位板材），不会污染真实数据。

### 16.7 已知取舍

- 剩余区域按**旋转后包围盒**挖洞：凹形零件的凹口不算可用料（保守，宁可少算也不给出放不下的区域）
- 碎料门槛 `minArea` 默认 1 万 mm²、单次最多 30 块；更细的需求请直接手工录料
- 余料**不跨工程**：`project_id` 隔离，换工程看不到别工程的料
- 余料当板材后还能再抠余料，链式复用没有层数限制，但也没有「余料树」追溯（只留 `source_order_id` 占位）

---

## 十七、后处理可视化编辑器（模块 11）

机床模板不该靠改代码。模块 11 在软件内直接编辑 G 代码 / HPGL / DXF 模板，一边改一边看渲染结果，
保存后立即对导出生效。

### 17.1 目标与边界

- 编辑器只处理**字符串与表单**：模板行、指令槽位、数值参数；校验与渲染全在后端
- 变量字典、指令槽位、字段约束都由 `/api/post/variables` 下发，前端不硬编码
- 预览用一条**示例刀路**（宽 > 高的矩形走一圈）渲染，与导出共用同一个 `renderer.render()`
- 内置 6 份模板只读，改内置必须换 id「另存为」；自定义模板落 `%LOCALAPPDATA%\BtCAM\post\<id>.json`
- 不做：G 代码语义仿真、机床在线通信（DNC）

### 17.2 变量与指令槽位

12 个占位符（`post/editor.py::VARIABLES`，与 `registry.PLACEHOLDERS` 白名单同源）：

| 组 | 变量 |
| --- | --- |
| 任务 | `{name}` 任务名 · `{date}` 生成时间 · `{programNo}` 程序号 |
| 机床 | `{tool}` 刀号 · `{spindle}` 转速 · `{safeZ}` 安全高度 |
| 坐标 | `{x}` · `{y}` · `{z}` · `{feed}` |
| 刻绘 | `{speed}` · `{force}`（HPGL 的 VS / FS） |

槽位随 `kind` 变化，前端按分组自动生成编辑区：

| kind | 槽位 |
| --- | --- |
| `gcode` | `header` / `footer` / `toolChange` + `rapidZ` / `rapidXY` / `plungeZ` / `feed` / `cutXY` |
| `hpgl` | `header` / `footer` / `penUp` / `penDown` / `penLift` |
| `dxf` | `version`（无模板文本，导出由 ezdxf 直接写） |

指令槽位**必填**（缺了整段刀路会静默消失），`header/footer` 这类行模板允许为空。
输出花括号本身写 `{{` / `}}`。

### 17.3 校验：errors 拦保存，warnings 只提醒

`post/editor.validate()` 返回 `ok / builtin / errors[] / warnings[] / usedVariables / unusedVariables / unknownVariables`，
每条错误带 `field`（如 `gcode.cutXY`、`gcode.precision`），前端据此高亮对应输入框。

拦保存的：内置 id 撞名、id 格式、名称空/超 64 字、`kind` 非法、`ext` 非法、槽位为空、
单行超 2000 字、未知占位符、括号不配对、数值越界（`precision` 0~6、HPGL `unitsPerMm` 等）。

只提醒的：程序尾没有 `M30/M02`、没有 `M5/M05` 停主轴、头尾没引用 `{safeZ}`、
开了行号却有 `%` 开头行、HPGL 收尾没有 `SP0`。

### 17.4 预览渲染

`POST /api/post/preview` 的 `options` 是假数据（`sizeMm/depth/feed/safeZ/spindle/tool/flipY/speed/force/programNo`），
后端夹到合理区间后构造示例刀路，再走 `registry.normalize()` + `renderer.render()` —— 与导出**完全同一条路径**，
所以「预览对了，导出就对了」。校验不通过的草稿返回空文本，不拿半成品误导用户。

### 17.5 前端

| 文件 | 职责 |
| --- | --- |
| `ui/PostEditor.tsx` | 三栏：左=元信息 + 指令槽位；中=模板行编辑（等宽字体、逐行错误）；右=变量面板 + 预览（行数/字节数/复制） |
| `ui/usePostEditorActions.ts` | 拉变量字典、防抖校验、预览、保存/另存为/删除，错误按 `field` 归并 |
| `ui/postDrafts.ts` | 纯函数：内置模板 → 可编辑草稿（自动换 id）、槽位默认值补齐 |
| `ui/PostPanel.tsx` | 列表页新增「编辑 / 新建模板」入口 |

输入即校验（400ms 防抖），`errors.length > 0` 时保存按钮禁用；warnings 用黄色条目列在下方，不拦保存。

### 17.6 控制台验收

```powershell
backend\.venv\Scripts\python.exe scripts\post_editor_test.py
backend\.venv\Scripts\python.exe scripts\post_editor_test.py --url http://127.0.0.1:8975
```

实测输出（**23/23**，节选）：

```text
== A. 服务层：变量字典 / 校验 / 预览 ==
  [OK]   A1 变量字典  12 个占位符
  [OK]   A4 未知占位符  gcode.header[9]: 使用了未知占位符 {foo}
  [OK]   A5 括号不配对  gcode.header[9]: 模板括号不配对：expected '}' before end of string
  [OK]   A6 空指令  gcode.cutXY: gcode.cutXY 不能为空
  [OK]   A7 数值范围  gcode.precision: precision 必须是 0~6 的整数
  [OK]   A8 工艺警告  2 条警告：程序尾没有 M30/M02 结束指令…
  [OK]   A10 G 代码预览  20 行 / 191 字节
  [OK]   A11 预览参数  sizeMm=200 -> X200；flipY -> Y-60
  [OK]   A12 HPGL 预览  11 行
  [OK]   A13 DXF 预览  DXF 后处理没有模板文本：导出时由 ezdxf 直接写图形文件
== B. 接口：校验 / 预览 / 改模板导出生效 ==
  [OK]   B3 校验错误  gcode.cutXY: 使用了未知占位符 {nope}
  [OK]   B4 预览接口  20 行 / 187 字节
  [OK]   B5 改模板保存导出  EDITOR_TEST_20261024_073757.nc（870 字节）
  [OK]   B6 再改再导出  EDITOR_TEST_20261024_073758.nc（1085 字节）
  [OK]   B7 内置保护  id: 内置后处理不可直接修改，请另存为自定义后处理
  [OK]   B9 删除  删除后 GET -> HTTP 404

== 结果：23/23 通过 ==
```

B5/B6 是这条模块的核心验收：**改模板 → 保存 → 导出**，两次文件字节数不同，说明改动真的进了渲染器，
而不是只改了配置。测试结束会删掉临时模板（`EDITOR_TEST_*`）与导出文件。

### 17.7 已知取舍

- 校验只到**语法/结构级**：知道占位符在白名单、括号配对、数值越界，但不判断某台机床是否接受这条 G 代码
- 预览只跑示例刀路，不读当前工程真实几何（快、与几何解耦，但看不到多板 `S1` 这类批量效果）
- 单行模板上限 2000 字符、预览上限 20 万字符，超出**截断而非拒绝**（避免前端卡死）
- 没有模板版本历史/回滚，保存即生效；要留底就「另存为」不同 id

---

## 十八、MES 对接接口（模块 12，可选）

车间已经有 MES 的厂，工单信息不该手工录两遍。模块 12 把 MES 的工单拉到本地，再把 CAM 的产出
（工单信息 + 用料统计 + 加工文件）回推归档。**它是可选件**：不开启时一条网络请求都不发，CAM 全部功能照旧。

### 18.1 目标与边界

- 支持 CenkorMes / LightMes / 自定义（路径可逐条改）
- **拉**：单号、名称、客户、物料、板厚、交期、零件清单 → `mes_order`（按 `remote_id` 幂等 upsert）
- **推**：工单信息 + 用料统计（零件数/轮廓数/摆放数/利用率/板尺寸/文件数/字节数）+ 加工文件（multipart）
- 开关 / 厂商 / 地址 / 密钥 / 超时 / 重试 / 自动推送 / 是否带文件，全在设置页配
- **隔离**：`app/mes/*` 不被 CAM 核心 import；导出钩子延迟 import + `try/except` + daemon 线程
- 不做：MES 侧排程/报工/物料扣减、双向状态机同步、离线队列补传（推送失败只留日志，不排队）

### 18.2 配置与数据表

配置存 `app_setting` 表 `key='mes.config'` 一条 JSON（明文落库，出接口只给掩码）：

| 键 | 说明 |
| --- | --- |
| `enabled` | 总开关；关闭时拉单/回传直接 400 |
| `vendor` | `cenkor` / `lightmes` / `custom` |
| `baseUrl` | `http(s)://host[:port][/prefix]` |
| `apiKey` | 同时放 `Authorization: Bearer` 与 `X-Api-Key`；出接口掩码为 `sk****cdef`，**留空 = 沿用旧值** |
| `timeout` | 0.5~60 秒 |
| `retries` | 0~5 次（总尝试 = retries + 1） |
| `autoPush` | 导出成功后自动回传 |
| `includeFiles` | 回传是否带加工文件 |
| `paths` | 覆盖厂商默认路径（`ping` / `orders` / `archive`） |

厂商默认路径：

| 厂商 | 探活 | 拉单 | 归档 |
| --- | --- | --- | --- |
| CenkorMes | `/api/v1/ping` | `/api/v1/workorders` | `/api/v1/workorders/{remoteId}/archive` |
| LightMes | `/api/ping` | `/api/workorders` | `/api/workorders/{remoteId}/archive` |
| 自定义 | 默认同 Cenkor，可逐条覆盖 | | |

数据表：

| 表 | 说明 |
| --- | --- |
| `mes_order` | `remote_id` 唯一索引；`status` = `pulled` / `archived`；`parts` / `payload` 存 JSON；`part_count` 是冗余计数 |
| `mes_push_log` | 动作 / 成功标记 / 尝试次数 / HTTP 状态码 / 文案，排障用 |

老库缺 `part_count` 时 `_migrate()` 自动补列并按 `parts` 的 `qty` 回填；表名沿用旧版 `mes_push_log`，升上来的库不断历史记录。

### 18.3 客户端与重试

`mes/client.py` 只用标准库 `urllib`（不新增依赖）：

- 错误统一成 `MesError(message, status_code, attempts)`；`_detail_of()` 依次取 `detail/message/error/msg`，把服务端 JSON 翻成人话
- **可重试**：`408/425/429/500/502/503/504`，退避 `min(2.0, 0.25×2^(n-1))` 秒；401/403/400/404 立即失败（密钥错了重试只会锁账号）
- **multipart 手搓**：`payload` 字段放 JSON，`file_1..file_N` 放二进制，boundary 每次随机
- `limit` 拼查询串时按已有 `?` 决定分隔符，`baseUrl` 末尾斜杠自动归一
- `User-Agent: BtCAM/0.1 (+module12)`，方便 MES 侧识别来源

### 18.4 拉单

`GET {orders}?limit=N` → `_extract_orders()` 认 `orders/data/list/records/items` 这类常见外壳 → 逐条 `_normalize_order()`：

- 单号依次尝试 `orderId / order_id / orderNo / order_no / code`；一个都没有就**跳过并留原因**，不中断整批
- 零件清单认 `parts/items/components/details/partList`，零件名认 `name/partName/partNo/no`；字符串零件也接受
- `partCount` 按每条 `qty/quantity/count` 累加（没有则记 1）
- upsert 只刷新业务字段与 `pulledAt`，**不会把已 `archived` 的单打回 `pulled`**（重复拉单不丢归档状态）

### 18.5 回传归档

`POST {archive}`，payload 示例：

```json
{
  "remoteId": "WO-1001",
  "source": "BtCAM",
  "status": "archived",
  "idempotencyKey": "70-20261024073757",
  "pushedAt": "2026-10-24T07:37:57",
  "usage": {
    "partCount": 12, "polylineCount": 34, "placementCount": 12,
    "utilization": 0.7321, "sheetWidth": 1220, "sheetHeight": 2440,
    "fileCount": 2, "fileBytes": 58
  },
  "workOrder": { "id": 70, "code": "WO-1001", "name": "…", "stats": { } },
  "notes": "CAM 导出完成"
}
```

`orderId` 不给 `workOrderId` 时，用 `work_order.code == remoteId` 自动配本地工单（编号就是约定）。
有文件走 multipart、没文件走 JSON；成功后置 `archived` + 时间戳，并写一条日志。

### 18.6 自动推送旁路

`routes_cam._record()` 在导出成功、登记加工文件之后调 `_notify_mes(project_id, workOrderId)`：

1. **同步预检**（开关 + 地址 + `autoPush` + 编号非空），不满足直接返回 false，零网络开销
2. 满足则起 `threading.Thread(name="mes-auto-push", daemon=True)` 去推
3. 整段 `try/except`，任何异常只记 debug 日志

所以 MES 挂掉/断网时，导出接口的返回与耗时**完全不受影响**（验收 E 系列断言：关闭开关后假 MES 请求数为 0）。

### 18.7 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/mes/settings` | 配置（密钥掩码）+ 厂商清单 + 默认路径 + `orderCount/archivedCount/lastLog` |
| PUT | `/api/mes/settings` | 保存配置（`apiKey` 留空 = 沿用） |
| POST | `/api/mes/test` | 试连接；可带草稿配置，未启用也能先试通 |
| POST | `/api/mes/pull` | 拉单（`limit` 1~200），返回 `created/updated/skipped/problems` |
| GET | `/api/mes/orders` | 本地 MES 工单列表（`status` 过滤） |
| GET | `/api/mes/orders/{id}` | 单张详情（含零件清单） |
| POST | `/api/mes/push` | 回传归档 |
| GET | `/api/mes/logs` | 交互日志（`orderId` / `limit`） |

错误口径：400（未启用 / 配置不合法 / 数据认不出）、404（工单不存在）、502（MES 不可达，`detail` 带「已尝试 N 次」）。

### 18.8 前端

| 文件 | 职责 |
| --- | --- |
| `ui/MesPanel.tsx` | 三个页签：连接（开关/厂商/地址/密钥/超时/重试/自动推送/带文件 + 测试连接）、工单（拉单 + 列表 + 详情零件清单 + 手动回传）、日志（动作/结果/尝试次数/状态码） |
| `ui/useMesActions.ts` | 打开时拉配置与列表；保存/测试/拉单/回传/刷新动作 |
| `ui/Toolbar.tsx` | 「MES…」入口；开关关闭时按钮仍在（离线也能看已拉的工单） |

### 18.9 控制台验收

```powershell
# 服务层：自带假 MES，不需要真的 MES 服务
backend\.venv\Scripts\python.exe scripts\mes_test.py

# 服务层 + HTTP 接口全链路
backend\.venv\Scripts\python.exe scripts\mes_test.py --url http://127.0.0.1:8975
```

`mes_test.py` 用 `ThreadingHTTPServer` 起一个假 MES（`/api/v1/ping`、`/api/v1/workorders`、
`/api/v1/workorders/{id}/archive`，同时校验 Bearer 与 `X-Api-Key`，可注入 503 / 401），
所以验收不依赖外部环境。实测 **62/62**（节选）：

```text
[A] 配置：校验 / 密钥 / 往返 / 厂商路径
  [OK]   A4 非法 vendor  vendor 必须是 cenkor/custom/lightmes 之一
  [OK]   A9 保存后启用并落掩码  sk****cdef
  [OK]   A10 密钥留空沿用旧值
  [OK]   A13 对外 payload 不含明文密钥
[B] 客户端：鉴权 / 参数 / 重试 / 错误详情
  [OK]   B2 同时带 Bearer 与 X-Api-Key
  [OK]   B4 503 指数退避重试后成功  attempts=3
  [OK]   B5 401 不重试  密钥无效
[C] 拉单：归一化 / 幂等 / 跳过脏数据
  [OK]   C1 首次拉单新增 2 张  created=2 updated=0
  [OK]   C2 脏数据被跳过并留原因  MES 工单缺少单号（id / orderNo / code 都没有）
  [OK]   C6 零件数按数量累加  3
  [OK]   C7 重复拉单只更新不新增  created=0 updated=2
[D] 回传：用料统计 / 文件 / 幂等键 / 归档状态 / 日志
  [OK]   D3 文件数 / 字节数正确  files=2 bytes=58
  [OK]   D4 走了 multipart（带文件）
  [OK]   D5 用料统计来自本地工单快照  {"partCount": 12, "utilization": 0.7321, …}
  [OK]   D10 本地标记已归档
  [OK]   D12 关闭文件上传后走 JSON
[E] 离线：开关关闭时一个包都不发
  [OK]   E1 未启用时拉单被拒  MES 对接未启用，请先到「MES 对接」打开开关
  [OK]   E2 一个请求都没发出去  requests=0
[F] 自动推送：导出后的 daemon 线程
  [OK]   F2 线程真的把工单推了过去  archives=1
  [OK]   F4 工单编号为空时跳过
[G] HTTP 接口全链路
  [OK]   G9 不存在工单回 404  HTTP 404: {"detail":"MES 工单不存在 #99999999"}
  [OK]   G10 未启用时回 400  HTTP 400: {"detail":"MES 对接未启用…"}
  [OK]   G11 MES 路由全部注册  missing=[]

全部通过  62/62 通过
```

测试前会备份 `mes.config` 并在结束时恢复，同时删掉 `MES-TEST-%` 的工单与日志，不把验收噪音留给下次人工查看。

### 18.10 已知取舍

- 密钥**明文存库**（本机单用户桌面程序），只有接口出参加掩码；要更强隔离得接系统凭据库（未做）
- 只按 `remoteId` 对单：本地工单编号必须**等于 MES 单号**才能自动关联，否则手动传 `workOrderId`
- 推送失败**不排队重发**：记一条失败日志，用户可在「工单」页手动重推（幂等键让重复推送安全）
- 拉单只取「最近 N 张」，不做增量游标（各家 MES 没有统一的时间戳约定）
- 自动推送是 daemon 线程，程序退出时正在进行的推送可能被中断（下次手动重推即可）

---

## 十九、配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BTCAM_HOST` | `127.0.0.1` | 监听地址（只对本机开放） |
| `BTCAM_PORT` | `8975` | 端口，需与 `lib.rs` 的 `BACKEND_PORT`、CSP 同步 |
| `BTCAM_WORKERS` | 核数/2 | 计算池并发度 |
| `BTCAM_PROCESS_POOL` | `0` | `1` 用 spawn 进程池（硬隔离，任务参数须可 pickle） |
| `BTCAM_GEOM_ENGINE` | `auto` | `auto` / `occt` / `clipper` / `fallback` |
| `BTCAM_NEST_ENGINE` | `auto` | `auto` / `deepnest` / `shelf` |
| `BTCAM_DEEPNEST_CMD` | 空 | DeepNest 桥接命令，如 `node D:\tools\deepnest\bridge.js`（stdin/stdout JSON） |
| `BTCAM_LOG_LEVEL` | `INFO` | 日志级别（重启生效） |
| `BTCAM_LOG_BUFFER` | `1000` | 日志面板环形缓冲容量（最小 50） |
| `BTCAM_CHUNK_BYTES` | `1048576` | 分片阈值，超过即切 CHUNK；调试可设 1024 |
| `BTCAM_PARENT_PID` | 空 | 由启动方注入；设置后后端盯着该 PID，进程消失即自杀 |

数据目录：`%LOCALAPPDATA%\BtCAM\`（`btcam.db`、`logs\`、`exports\`）。

前端端口配置：`frontend\.env` 的 `VITE_BACKEND_URL`。

---

## 二十、OCCT 与 DeepNest 接入

### OCCT（2D 几何核心）

```powershell
# 方案 A：pip 轮子（推荐，无需 conda）
backend\.venv\Scripts\python.exe -m pip install cadquery-ocp==7.8.1

# Python 3.8 只有 7.7.2 带 cp38 轮子，需显式降版：
backend\.venv\Scripts\python.exe -m pip install cadquery-ocp==7.7.2

# 方案 B：conda
conda install -c conda-forge pythonocc-core=7.8.0
```

`requirements.txt` 已按 Python 版本分好条（3.9+ → 7.8.1，3.8 → 7.7.2），
直接 `pip install -r requirements.txt` 即可。

`occt_engine.py` 会依次尝试 `OCC.Core.*`（pythonocc-core）与 `OCP.*`（cadquery-ocp），
两者 API 一致但包名不同，适配层已解决。未安装时 `get_engine()` 自动降级到 pyclipper，全链路仍可用。

### DeepNest（异形排料）

DeepNest 官方是 JS 实现，本项目通过**外部进程桥接**接入，避免把 Node 运行时塞进安装包：

1. 准备一个命令行桥接程序，读 stdin 的 JSON 任务、写 stdout 的 JSON 结果（格式见 `nesting_service.DeepNestBridge`）
2. 设置 `BTCAM_DEEPNEST_CMD="node D:\tools\deepnest\bridge.js"`
3. 排料面板选「DeepNest 桥接」，或给 `/api/layout/nest` / `/api/nest` 传 `engine: "deepnest"`

请求里带板材尺寸、`gap`、全局 `rotation`，以及逐件的 `rotation`（木纹锁定件为 `false`）；
响应 `placements[]` 的 `tx/ty` 原点是"零件自身原点"，`nest_service` 会换算成排料板的
"旋转后包围盒左下角"，并重新跑一遍 `_check`，不合格或进程失败都自动回退内置引擎。
桥接子进程用 `Popen` + 100ms 轮询：任务被取消或超时都会立即 kill，并在日志里给出回退原因。

未配置桥接时：`/api/layout/nest` 用内置 `ScanPacker`（底线扫描，支持障碍与定角），
旧 `/api/nest` 仍用 `ShelfNester`（行排 + 多板预览）。

---

## 二十一、依赖清单

### 后端（`backend/requirements.txt`）

| 包 | 版本 | 用途 |
| --- | --- | --- |
| fastapi | 0.115.6 | Web 框架 |
| uvicorn[standard] | 0.34.0（Py3.9+）/ 0.33.0（Py3.8） | ASGI 服务器 |
| websockets | 14.1（Py3.9+）/ 13.1（Py3.8） | WS 协议实现 |
| pydantic | 2.10.4 | 请求校验 |
| python-multipart | 0.0.20 | 表单/文件上传预留 |
| numpy | 2.2.1（Py3.10+）/ 1.24.4（Py3.8/3.9） | 点阵运算 |
| pyclipper | 1.3.0.post6 | 2D 布尔/偏置兜底引擎 |
| ezdxf | 1.3.4（Py3.9+）/ 1.0.3（Py3.8） | DXF 读取（模块1） |
| cadquery-ocp | 7.8.1（Py3.9+）/ 7.7.2（Py3.8） | OCCT 绑定（模块2 主力引擎） |
| pyinstaller | 6.22.3（实测） | 打 sidecar exe |

### 前端（`frontend/package.json`）

| 包 | 版本 | 用途 |
| --- | --- | --- |
| react / react-dom | ^18.3.1 | UI |
| pixi.js | ^8.6.6 | WebGL 画布渲染 |
| zustand | ^5.0.2 | 轻量状态管理 |
| @tauri-apps/api | ^2.2.0 | 前端调用 Tauri API |
| vite | ^6.0.7 | 构建 |
| typescript | ^5.7.2 | 类型检查 |
| @vitejs/plugin-react | ^4.3.4 | React 插件 |
| @tauri-apps/cli | ^2.2.0 | `tauri dev/build` |

### Rust（`frontend/src-tauri/Cargo.toml`）

| crate | 版本 | 用途 |
| --- | --- | --- |
| tauri | 2 | 桌面外壳 |
| serde / serde_json | 1 | 序列化 |
| tauri-build | 2 | 构建脚本 |

---

## 二十二、当前进度与已知限制

已跑通并验证：

- **整体联调已通过**：9 个基础模块串行完成，后端 151 项回归 + 浏览器全流程走查 + 断线重连现场验证全过，详见第二十三章（模块 10/11/12 在此基础上新增 123 项）
- 前端 `tsc --noEmit` + `vite build` 零错误（786 模块；业务 164.94 kB，CSS 36.32 kB，pixi 519.41 kB，react 141.61 kB）
- 后端 9 项端到端自检全过（WS 二进制帧解析、异步进度推送、NC/PLT/CUT 落盘）
- **模块 1 DXF 导入**：算法 5 项 + 接口 4 项全过（断线闭合/重叠清理/自相交/内外轮廓/碎线/块展开/多零件拆分）
- **模块 2 几何服务**：服务层 9 项 + 接口 7 项全过（补偿面积误差 <0.01%、偏移后无自交、内外方向正确、求交面积精确、200 次调用 RSS 无增长）
- **模块 3 画布手动排料**：服务层 14 项 + 接口/WS 8 项全过（零件拆分/行排/越界与间距拒绝/旋转夹回/锁定/改板/复位，BGC1 轮廓包围盒与摆放一致）
- **模块 4 自动排料**：服务层 11 项 + 接口/WS 8 项全过（全件落位/紧凑度/锁定件不动/木纹定角/关闭旋转/未排下停靠/幂等/DeepNest 回退/取消不落库/入参出参/桥接子进程可 kill；任务完成后自动补发 `LAYOUT_STATE`）
- **模块 5 刀路生成**：服务层 9 项 + 接口/WS 4 项全过（外扩/内缩补偿、CCW/CW、手工起割点首尾闭合、微连接切 3 段、贪心排序节省 1420mm、内外类型与无孔报错、取消不产出、整板 9 条轮廓；接口回带 9001/9002/9003 三层、`source=layout` 整板、参数 422、取消 404/409）
- **模块 6 刀具库与工艺模板**：服务层 12 项 + 接口/WS 7 项全过（铣刀/钼丝等效直径与非法参数、模板 CRUD 与 8 项白名单、刀具>模板>请求三级优先级、顺逆铣内外映射、10mm 刀驱动补偿、0.26mm 钼丝偏置 0.13 且逆铣 CW、`liftZ`/`safeZ` 分离、模板微连接 3 桥 4 段、删除保护、未知引用报错；接口新建/改/删/查字段齐全、刀路与导出读刀具参数、`toolId` 不存在 400、引用中的刀具 400 且解绑后可删）
- **模块 7 后处理与导出**：服务层 10 项 + 接口 7 项全过（6 份内置模板落盘且幂等、维宏 G 代码 24 行、新代行号 N10 与 `T7 M6`、2 位精度去尾零、HPGL 40 unit/mm 与 Y 翻转、CUT 的 VS/FS、DXF 写回可读、未知占位符/非法 kind/内置改删四类拒绝；接口自定义 CRUD、`processorId` 导出、DXF 图形导出、两块板批量出 2 个文件、未知后处理 400）
- **模块 8 工单存档**：服务层 7 项 + 接口 8 项全过（快照顶点 base64 往返一致且不落 ids/选中集、名称/编号/创建日期三类检索、清空工程后重复打开还原板材与摆放、另存深拷贝、加工文件登记、参数白名单清洗、删除连带文件；接口 CRUD、打开后回填刀路深度、真实导出带 `workOrderId` 自动归档、空名称 422 / 未知工单 404 / 非法日期 400）
- **模块 9 日志与告警**：服务层 6 项 + 接口/WS 6 项全过（环形缓冲满容量淘汰、级别/关键词/来源/增量四类过滤、warning 显式推送与 error 自动推送及 3 秒去重、LogRecord 结构化入环含堆栈且可跳过、进度日志文案去重与节流、导入校验汇总告警；接口查询/清空/前端上报、非法级别 400 / 空消息 422、`0x50` ALERT 帧 meta 与接口返回一致）
- **模块 10 余料管理**（V0.2）：服务层 28 项 + 接口 10 项全过（手工录料/轮廓归一化/非法轮廓拒绝、剩余区域面积降序且不越板边、按 index 入库且拒绝过期索引、余料当板材 `margin` 归零并绑定 `scrapId`、L 形余料缺口与板外拖拽双向拒绝而轮廓内落位通过、在余料内自动排料 5 件全落位、工单快照带余料绑定且重开恢复、解绑回整板、删除在用余料自动解绑）
- **模块 11 后处理可视化编辑器**（V0.3）：服务层 14 项 + 接口 9 项全过（变量字典 12 个占位符、used/unused/unknown 三类统计、括号不配对/空槽位/数值越界拦截、M30 与 `{safeZ}` 等工艺警告、G 代码/HPGL/DXF 三类预览、预览参数生效（sizeMm/flipY）、错误草稿不出预览；接口「改模板→保存→导出」两次文件字节数不同、内置只读保护、未知 id 404、删除后 404）
- **模块 12 MES 对接**（V0.4 可选）：服务层 51 项 + 接口 11 项全过（8 类配置校验文案、密钥掩码与留空沿用、Bearer + `X-Api-Key` 同发、503 指数退避 `attempts=3`、401 不重试、拉单脏数据跳过与按数量累加、重复拉单只更新、回传 multipart 带文件与幂等键、用料统计取自工单快照、归档状态与交互日志、关闭带文件走 JSON、离线零请求、导出后 daemon 自动推送、7 条路由全注册）
- sidecar 已实测打包：`cam-backend.exe` 23.6 MB，脚本自检 `/api/health` 通过（V0.4 精简依赖后实包 25.29 MB，见 [docs/PACKAGING.md](docs/PACKAGING.md) 实测基线）
- 分片双端验证：单帧 139,756 B → 140 片；用前端真实 `protocol.ts` 重组，轮廓/点数与原始一致
- 进程回收实测：杀掉伪父进程后，源码态 4 s 内退出、打包态两层进程 0 残留、端口释放

骨架阶段的限制（后续迭代方向）：

1. **Rust 侧未编译验证**：本机未装 Rust，`cargo tauri dev/build` 与 `tauri build --bundles nsis,msi` 都没实际跑过；单文件交付链目前只验证到 sidecar（PyInstaller）层
2. **模块 2 已在真机验证 OCCT**（cadquery-ocp 7.7.2，Python 3.8 的版本上限）；模块 1 的拓扑修复仍由 numpy 实现（纯拓扑操作，不依赖 OCCT）；未装 OCCT 时整体自动降级到 pyclipper
3. 自动排料内置底线扫描；DeepNest 只提供桥接协议与外部命令接口，未内置桥接程序（未配置时自动回退）
4. DXF 导入已支持 LINE/多段线/圆/弧/椭圆/样条/块/HATCH；**DWG 未支持**（需另接转换）
5. 刀路已覆盖内外轮廓、微连接、起割点、排序、Z 分层与圆弧切入；**没有过切/残料检查，也没有 G 代码仿真**，实际加工前需空跑验证；后处理只负责出文件，**不含机床在线通信（DNC）**
6. **模块 9（日志告警）已完成**，V0.1 的 9 个模块全部就绪；在此基础上又完成了 **模块 10 余料管理（V0.2）、模块 11 后处理可视化编辑器（V0.3）、模块 12 MES 对接（V0.4 可选）**。全量回归全过（dxf 9 / geometry 16 / layout 22 / auto_nest 19 / toolpath 13 / tool_library 19 / post 17 / workorder 15 / log_alert 12 / scrap 38 / post_editor 23 / mes 62 / smoke 9，共 274 项）

---

## 二十三、整体联调验收（V0.1）

九个模块全部完成后做的一次全链路联调：**后端脚本回归 + 浏览器内真实 UI 操作 + 断线重连现场验证**。

### 23.1 联调环境

| 项 | 值 |
| --- | --- |
| 后端 | `127.0.0.1:8975`，`occt` 几何引擎 / `shelf` 排料，4 线程计算池 |
| 前端 | Vite dev server `127.0.0.1:5173`（Tauri 开发同款入口，`scripts\dev.ps1 -NoTauri`） |
| 数据目录 | `%LOCALAPPDATA%\BtCAM`（SQLite + post 配置 + exports + logs） |
| 说明 | 本机未装 Rust，`cargo tauri dev` 不可用；浏览器与 Tauri 同为 Chromium 内核，UI 行为一致，差异只在窗口外壳 |

### 23.2 后端全量回归

V0.1 阶段：10 个验收脚本、共 151 项，全部通过（改动后各跑一遍，结果一致）。
模块 10/11/12 加入后补跑三份新脚本，共 13 个脚本 / 274 项：

```
dxf_import 9 · geometry 16 · layout 22 · auto_nest 19 · toolpath 13
tool_library 19 · post 17 · workorder 15 · log_alert 12 · smoke 9
scrap 38 · post_editor 23 · mes 62
```

### 23.3 UI 全流程走查

按真实操作顺序点一遍，全程控制台 **0 error、0 失败请求**：

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 | 打开应用 | 状态栏「已连接 ｜ 几何 occt ｜ 排料 shelf ｜ 计算进程 4 ｜ 已加载 9 条轮廓」 |
| 2 | 载入示例 | 画布出现矩形 + 4 圆孔/方框 + 星形，视图自动适配 |
| 3 | 偏置 1mm | 任务面板 `geometry 完成`，9 → 9 条，三种图层颜色保留 |
| 4 | 排料模式 | 左面板切板材/零件，5 个零件 5/5 已摆放 |
| 5 | 自动排料 | `5 落位 / 0 未排下`，状态栏提示完成；DeepNest 未配置时回退 scan 并记警告 |
| 6 | 生成刀路 | 3684 刀位点 / 6 层 / 9 轮廓，画布绿实线切削 + 虚线空走 + 橙色菱形起割点 |
| 7 | NC / PLT 导出 | `BT_CAM_20261023_221127.nc`（72812 B）、`..._221142.plt`（52562 B）落盘，日志记录字节数 |
| 8 | 日志面板 | 67 条，级别/来源/关键词过滤生效（`keyword=任务` → 15 条） |
| 9 | 工单保存 | `#35「联调工单01」`，5 零件 / 9 轮廓 / 利用率 11.8% |
| 10 | 后处理切换 | 6 个内置模板，切到维宏后「速度/压力/Y 翻转」字段自动隐藏 |
| 11 | 前端异常上报 | 注入 `ErrorEvent` → 状态栏「错误：…」+ `POST /api/logs/client` 200 + 日志面板出现 `frontend` 来源记录 |

### 23.4 断线重连现场验证

后端被强制结束后由脚本自动拉起，前端全程不刷新：

| 时刻 | 状态栏 | 说明 |
| --- | --- | --- |
| T−4 s | 已连接 · 已加载 9 条轮廓 | 基线 |
| T+0 s | 已断开 · 正在重连… | 后端停止后 ≤4 s 内 UI 翻转 |
| T+0~27 s | 已断开 ↔ 连接中 | 指数退避重试，控制台 `WebSocket connection failed` + `[ws] 连接异常`（预期） |
| T+36 s | 已连接 · 已加载 9 条轮廓 | 后端恢复后自动重连并重新同步全量图形 |

结论：断线 10 s 内可感知、恢复 20 s 内自愈、几何不丢、工具栏可用。

### 23.5 联调中发现并修复的问题

| 问题 | 根因 | 修复 |
| --- | --- | --- |
| 偏置后 9 条轮廓全变成同一颜色 | `GeometryOps.offset` 把全部结果写成 `items[0].layer_id`（引擎只回吐点串，原实现没有按来源回填图层） | 改为按「图层 + 偏移符号」分组调用引擎，结果继承本组 `layer_id`；调用次数仍等于图层数，不牺牲性能 |
| 点「偏置 1mm」后视图跳到刀路预览、左面板变成刀具参数 | 前端把所有 `TASK_RESULT` 都送给 `applyPreview`（`viewMode='preview'`） | 新增 `applyGeometryResult`：只替换轮廓，保持 `doc` 视图与图层面板，状态栏显示「偏置完成：N 条轮廓」 |
| 自动排料完成后状态栏停在「已提交」 | `applyLayout` 在排料态刻意保留 message，而完成提示没有别处写入 | `TASK_RESULT(kind=layout_nest)` 用 `placedCount/unplacedCount` 写完成提示，随后 `LAYOUT_STATE` 不会覆盖 |
| 刀路面板「路径排序（少抬刀）」被挤成两行、勾选框被撑成宽输入框 | `.form-check input` 特异性低于 `.toolpath-form .form-row input`，勾选框继承了 `width: 86px` | 选择器改为 `.form-row.form-check > input[type='checkbox']`，压过面板级规则 |
| 切割深度/进给等下刀值被标 `invalid` | `step` 以 `min` 为基准做整除校验：`min=1 + step=100` 会把 3000 判成 `stepMismatch` | `min` 统一给 0（只做 step 基准），业务下限移到 `floor`，由输入回调夹紧 |

复测结果：左面板保持「图层」、画布 3 色保留、状态栏「偏置完成：9 条轮廓」；「自动排料完成：5 个零件已落位」；勾选框 15×15 单行不换行；8 个数字输入 `validity.valid` 全为 true、`stepMismatch` 全为 false。

### 23.6 仍然存在的小问题（不阻塞交付）

1. 窗口宽度小于 1080 px 时（`body{min-width:1080px}`），模态框会按 `min(1000px, 100vw-72px)` 收缩，日志面板内容被挤成竖排；Tauri 默认窗口远大于此，浏览器小窗下才会遇到
2. 日志面板打开期间每 2 s 轮询一次 `GET /api/logs`；改为 WS 推送可省请求，但面板关闭后就不该推，权衡后保持轮询
3. 未装 Rust，`cargo tauri dev/build` 与 NSIS/MSI 安装包仍未在本机跑过（sidecar 打包已实测）
4. 自动排料在未配置 `BTCAM_DEEPNEST_CMD` 时回退内置扫描，会记一条 warning 并弹一次告警（符合设计，不是错误）

---

## 二十四、故障排查

| 现象 | 处理 |
| --- | --- |
| 状态栏一直"重连中" | 后端未启动：`scripts\run_backend.ps1`；或端口被占用 |
| `health` 显示 `geometryEngine: fallback` | 连 pyclipper 都没装：重跑 `install_backend.ps1` |
| 打包报找不到 `backend/cam-backend.exe` | 先跑 `scripts\build_sidecar.ps1` |
| 打包报“本次构建未内嵌后端” | 忘了先打 sidecar：跑 `scripts\build_sidecar.ps1`，或直接用 `scripts\build.ps1` |
| 双击便携版后一直“重连中” | 后端被杀软拦了：看 `%LOCALAPPDATA%\BtCAM\logs\`，把 `%LOCALAPPDATA%\BtCAM\bin` 加白名单 |
| 关窗后 `cam-backend.exe` 还在 | 该进程不是本程序拉起的（`owned=false` 不回收）；任务管理器结束即可 |
| 打包报 `LNK1104` | 主程序或后端 exe 被占用：先关掉 `btcam.exe` / `cam-backend.exe` |
| 构建日志没有 `cargo:warning=已嵌入后端` | sidecar 不在 `frontend\src-tauri\backend\`，看 `build_sidecar.ps1` 第 5 步 |
| `npm install` 后 `.bin` 为空 | 安装被中断：删 `node_modules` 重装 |
| 图形载入了但画布空白 | 按 `F` 适配视图；或检查 `GEOMETRY_DOC` 帧是否收到 |
| 导入 DXF 报“未安装 ezdxf” | `backend\.venv\Scripts\pip install -r backend\requirements.txt` |
| 导入后轮廓被拆得很碎 | 一般是图纸断线间隙超过 `repair.closeGap`；连接处留缝请 ≤ 1mm，或调大后重导 |
| 导入后红色线段很多 | 看日志里的告警类型：`open_contour` 是首尾没接上，`self_intersection` 是轮廓自身交叉 |
| `geometry/info` 显示 `backend=clipper` | OCCT 未安装：`pip install cadquery-ocp`（Py3.8 锁 7.7.2），或用 conda 装 pythonocc-core |
| OCCT 偏移报 `BRepOffsetAPI` 相关错误 | 多半是轮廓有重复点或零长度段：先用 `/api/geometry/merge` 串接，引擎入口已做 `dedup_points` |
| 偏移结果为空列表 | `distance` 大于内孔半径时内孔会退掉，属正常；或 `distance` 不是 `> 0`（方向由 `side` 控制） |
| 删除刀具提示“还有 N 个工艺模板在使用该刀具” | 先到工艺模板页把模板改绑其他刀，或删除对应模板 |
| 选了刀具后直径/进给/方向改不动 | 属正常：刀具字段接管面板，下拉选“不绑定”即恢复手改 |
| 钼丝刀的主轴转速为 0 | 线切割不走主轴，后端对 `category=wire` 统一置 0 |
| 导出报“后处理 #xxx 不存在” | 配置被删或改名：打开「后处理…」刷新列表，或看 `%LOCALAPPDATA%\BtCAM\post\` 下 JSON |
| 保存后处理提示“使用了未知占位符” | 模板只能用 12 个白名单占位符：`{name} {date} {programNo} {tool} {spindle} {safeZ} {x} {y} {z} {feed} {speed} {force}` |
| 改内置后处理被拒 | 内置只读：先“另存为”生成自定义 id，再改再保存 |
| 勾了“全部板材”却只出一个文件 | 只有一块板有摆放；且批量只在 `source=layout` 时生效 |
| HPGL 图形上下颠倒 | 绘图仪原点在左下：导出时勾选「Y 轴翻转」 |
| 启动后自定义后处理“被还原” | 内置配置按 `version` 升级会覆盖同名文件；自定义请用非内置 id |
| 工单列表搜不到刚存的单 | 日期筛选用的是**创建时间**且 `dateTo` 不含次日；先「重置」再搜，或检查是否切到了别的工程 |
| 工单里没有加工文件 | 打开工单后再导出（导出请求需带 `workOrderId`）；导入新 DXF 会解除当前工单关联 |
| 打开工单提示“刀具/模板已不在库” | 快照存的是当时的引用：重建刀后需重新选刀再「更新选中工单快照」 |
| 打开工单后当前图形消失 | 属正常：打开是**整份替换**语义（与导图一致），先存为新工单再切换 |
| 旧工单占空间太大 | 快照按顶点存 float32，点越多越大；不再需要的工单及时删除（连带清文件记录） |
| 日志面板一直是空的 | 面板打开后才轮询；先点「测试写入」，若也失败说明连接断开（看状态栏） |
| 日志里的报错没弹窗 | warning 需业务显式推送，属于设计如此；error 及以上才会自动弹窗。看面板确认级别 |
| 想保留历史日志 | 面板只在内存（上限 1000 条），重启即清；历史看 `%LOCALAPPDATA%\BtCAM\logs\backend.log`（4MB×3 滚动） |
| 日志被进度刷屏 | 进度日志已按 1.5s / 8% 节流；`BTCAM_LOG_LEVEL=WARNING` 可只留警告以上 |
| 前端报错弹窗反复出现 | 同文案 3 秒内只推一次、前端 5 秒内只上报一次；若持续提示说明是重复触发的真实错误 |
| 中文出口乱码 | 后端已强制 UTF-8；控制台请用 Windows Terminal |
| 点「计算剩余区域」报“当前工程没有零件” | 空板没有“剩余”可言：先导入 DXF 或排料，再算余料 |
| 余料当板材后零件跑到板外上方 | 正常：换板后旧摆放失效，统一停靠板外；点「自动排料」重落位，或手动拖回轮廓内 |
| 拖零件总提示“超出余料轮廓” | 余料可能是 L 形/带缺口：面积够不等于轮廓里放得下，换小件或换规则料 |
| 余料列表里看不到刚存的料 | 余料按 `project_id` 隔离；确认当前工程，或清空搜索关键词再刷新 |
| 删了余料后板材变回矩形 | 设计如此：正在被板材引用的余料删除时自动解绑，避免幽灵轮廓 |
| 后处理改完预览没变化 | 校验未通过时预览不出文本；先看红色错误条目，改成 `ok` 再预览 |
| 后处理保存按钮点不动 | `errors` 未清零；黄色 warnings 只是工艺提醒，不拦保存 |
| 想改内置后处理 | 内置只读：在编辑器里换一个 id「另存为」，内置模板保持不动 |
| 预览与导出内容不一样 | 预览跑的是示例刀路（100×60 矩形），导出读真实刀路；两者共用同一渲染器，差异来自坐标/行数 |
| MES 测试连接报“（已尝试 N 次）” | 地址/端口不对或 MES 不可达；401/403 不重试，先去设置页核对地址与密钥 |
| MES 拉单报“缺少单号” | MES 返回里没有 `orderId/orderNo/code`，需 MES 侧确认接口字段；该条会被跳过并列入 `problems` |
| 导出后 MES 没收到回传 | 检查开关 + `autoPush` + 工单编号是否等于 MES 单号；到「MES… → 日志」页看失败原因 |
| 断网时还能用 CAM 吗 | 能：开关关闭时一条请求都不发；自动推送是 daemon 线程 + 全程 `try/except`，不影响导出返回与耗时 |

---

## 二十五、V0.1 全链路联调与上机测试用例

12 个模块全部完成后的一次整体联调：**自动化脚本跑一遍全链路 + 重启复核持久化 + Tauri 窗口内手工走查**。
所有验证都在软件窗口内完成，没有生成任何浏览器调试页面。

### 25.1 跑法

```powershell
# 1) 主流程：导入 → 排料 → 刀路 → 余料复用 → 自定义后处理 → 工单 → MES 闭环（38 项）
backend\.venv\Scripts\python.exe scripts\e2e_test.py --url http://127.0.0.1:8975

# 2) 重启软件后复核数据没丢（3 项，复核完自动删探针）
backend\.venv\Scripts\python.exe scripts\e2e_test.py --url http://127.0.0.1:8975 --verify

# 3) 13 个模块回归脚本（274 项）
foreach($s in @('smoke_test','dxf_import_test','geometry_ops_test','layout_test','auto_nest_test',
  'toolpath_test','tool_library_test','post_test','post_editor_test','workorder_test',
  'log_alert_test','scrap_test','mes_test')) {
  backend\.venv\Scripts\python.exe "scripts\$s.py" --url http://127.0.0.1:8975 }
```

`e2e_test.py` 自带一个**假 MES**（标准库 `ThreadingHTTPServer`，随机端口），不依赖外网；
结束时会恢复 MES 配置与板材参数，只留工单/余料/刀具模板三个探针给 `--verify` 复核，复核完自行删除。
重跑时会先按 `E2E` 名字前缀清掉上次的残留，不会污染用户库。

### 25.2 本轮修复清单

| # | 现象 | 根因 | 修复位置 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | 绑定余料后点「复位」，零件落进余料缺口；`overflowCount` 仍报 0 | `reset()` 只做矩形行排；`build_meta` 越界统计只看矩形板框 | `layout_service.reset/park_outside/_outside_boundary/build_meta` | e2e D4（轮廓内 0 / 越界 2 · overflowCount=2） |
| 2 | 换余料板与「复位」两套停靠口径，结果不一致 | 停靠算法在两处各写一份 | `scrap_service._park_outside` 委托 `layout_service.park_outside(check_gap=True)`，`PARK_OFFSET=10` 统一板外间距 | e2e D3/D4 + scrap 38 项 |
| 3 | 连续排料/刀路后任务表只增不减 | `TaskQueue._tasks` 无上限，完成记录永不淘汰 | `core/tasks.py`：`MAX_TASK_RECORDS=400` + `_prune_tasks()`（排队/运行中不淘汰） | smoke / log_alert 回归 |
| 4 | 拔网线/休眠后 WS 连接挂在 hub 里越积越多 | 半开连接收不到 FIN，`receive_bytes` 永久阻塞 | 服务端 `IDLE_TIMEOUT_SEC=90` 空闲断开；客户端 `DEAD_AFTER_MS=45` 无回帧主动重连 | 手工断网 + `hub.client_count` 回归 |
| 5 | MES 单与本地工单是否关联看不出来，直接回传会没有用料与文件 | 关联只存在于 `work_order.code == remoteId` 的隐式约定 | `mes/service` 输出 `localWorkOrder`/`linkedCount`；前端列表/详情加关联徽标 + 「按单号新建工单」 | e2e F2/F3 |
| 6 | 导出/文件登记失败时日志没有上下文，跨模排查困难 | 异常日志只带异常本身 | `routes_cam` 日志补 工程 / 后处理 / 工单 / 板 号 | e2e G1~G3（按 `source` 分类可查） |
| 7 | 凹形余料上点「自动排料」整批 0 落位（P-1） | `ScanPacker._candidates()` 只给「板内左下角 + 已放件右上角」；余料板 `margin=0` 时 `(0,0)` 落在缺口里，候选集只剩一个无效点 | `nest_service.ScanPacker._boundary_candidates()`：把轮廓顶点及其「顶左 / 压底」投影加进候选（网格只当去重键，坐标不四舍五入），规则板不产生额外候选 | e2e D5（轮廓内 2 / 越界 0） |
| 8 | 未排下件的停靠位置两套口径（P-2） | `nest_service` 用 `height+margin` 停靠，`layout_service.park_outside` 用 `height+PARK_OFFSET` | 统一走 `PARK_OFFSET`（左端贴 0、上方留 10mm），与复位/换板完全一致 | auto_nest A6 + scrap E3 + e2e D4/D5 |

### 25.3 已验证无问题的项

| 检查项 | 验证方式 | 结论 |
| --- | --- | --- |
| OCCT 内存泄漏 | 200 次偏移后看进程 RSS | 无增长（`ReleaseScope` + 显式 `gc`） |
| DeepNest 子进程残留 | 排料中途取消 × 多次后查进程表 | 无残留（`finally: kill/wait/join`，轮询里感知取消） |
| WS 长连接堆积 | 反复开关窗口 + 强杀后端 | `hub` 用 set + 广播失败剔除，`finally` 摘除；空闲超时兜底 |
| 后处理改模板是否实时生效 | 改模板 → 立刻导出对比文本 | 生效（registry 每次读盘，无缓存） |
| 坏 DXF 是否拖垮进程 | 上传 1 个坏文件 | 任务 `failed`，进程存活，已有图形原样保留 |
| MES 离线是否影响 CAM | 关闭开关后复位/导出 | 零网络请求，`400` 明确提示，CAM 功能正常 |
| 关重启是否丢数据 | 重启后端后按 id 回读工单/余料/模板 | 全部一字不差（e2e `--verify` 3/3） |

### 25.4 余料排料的两个遗留问题（本轮已修）

原先记录为"不阻塞交付、需产品决策"的两项，在 V0.1 收尾时已修复并回归：

**P-1 凹形余料上自动排料排不上件（已修复）**

- 现象：把带缺口/L 形余料设为板材后点「自动排料」，零件全部停靠到板外（0 落位）。
- 根因：`nest_service.ScanPacker._candidates()` 只生成「板内左下角 `(margin,margin)` + 已放件右上角」两类候选点。
  换到余料板时 `margin=0`，而 `(0,0)` 正好落在余料缺口里，候选集就只剩这一个无效点，搜索无从落脚。
- 修复：新增 `_boundary_candidates()`，把余料轮廓顶点以及「顶到左边 `(margin,by)`」「压到底边 `(bx,margin)`」两条投影加入候选集。
  每个候选仍由 `LayoutService._check` 复验，所以多给候选不会误放；`boundary is None` 的规则板完全不产生额外候选，
  排序结果与改造前逐字一致（同一组零件仍落在 `(10,10) / (615,10) / (870,10)`）。
- 实现细节（踩过的坑）：布尔差生成的轮廓是圆弧近似（一次剩余区域就是 173 个点），必须去重，
  但**网格只能当去重键，坐标不能四舍五入** —— 把 `(600.038, 402.5)` 挪成 `(600, 402)` 会落进缺口，
  产生一个看着贴边、实际越界 1.35 mm² 的"合法"候选（正好压在 `_inside` 的 1 mm² 容差内，肉眼与统计都看不出来）。
  现状：网格按 1 mm 去重（不够再放到 4/10 mm），保留每组首个点的原始坐标，候选数上限 600。
- 验证：e2e D5 由"0 落位 + 安全不变式"升级为"至少 1 件进轮廓 + 安全不变式"，实测 2 件全部落在轮廓内
  （`(0,402.5)` 与 `(605,402.5)`，正好压在缺口上沿）；离线单测另覆盖 L 形缺角 300×300、93 点多顶点、
  100×100 放不下四种场景，越界数均为 0。

**P-2 未排下件的停靠间距两套口径（已修复）**

`nest_service` 原来用 `sheet.height + margin` 停靠（余料 `margin=0` 时贴着板边），`layout_service.park_outside` 用 `PARK_OFFSET=10`。
现已统一为 `PARK_OFFSET`：左端贴 0、上方留 10 mm，换板自动停靠与自动排料"未排下"落在同一位置，用户看到的是同一个口径。

**仍然存在的小事（不影响功能）**

- `_inside` 的 1 mm² 面积容差是有意保留的（圆角外扩与浮点让"刚好贴边"差几个 0.01 mm²）：
  理论上允许约 1 mm² 的越界，肉眼与切缝都无影响；收紧容差会让大量贴边摆放被误拒，故不动。

### 25.5 上机测试用例

前置：`scripts\run_backend.ps1` 已启动后端（状态栏「已连接」），数据目录干净或已备份。
「预期」列即判定标准，任一项不符就停下取证（截图 + 日志面板 + `%LOCALAPPDATA%\BtCAM\logs\backend.log`）。

#### A 组 · DXF 导入（模块 1）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| A-1 | 导入一张正常板件 DXF（含内孔） | 任务面板 `DXF 导入 完成`；画布出现轮廓、内孔为不同颜色；零件数 = 图形中闭合外框数 |
| A-2 | 同一张图再导一次（勾「替换」） | 图形不重复累加，零件数不变；旧摆放被清理 |
| A-3 | 导入一个改名成 `.dxf` 的文本文件 | 任务「失败」并给出中文原因；软件不崩、已有图形不变 |
| A-4 | 一次选 5 个 DXF 批量导入 | 5 个文件都进任务列表，逐个出结果；单个失败不影响其余 |
| A-5 | 导入含断线/自交的脏图 | 能自动修复的修复并给告警；修不了的记 `open_contour` / `self_intersection` 告警，不阻塞入库 |

#### B 组 · 手动排料 + 自动排料（模块 3/4）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| B-1 | 进入排料模式 | 零件自动行排，每个都有位置；状态栏显示板材尺寸与零件数 |
| B-2 | 拖动零件松手 | 合法落位后画布不弹回；越界/间距不足则弹回并提示具体原因（左/右/上/下/与某件间距不足） |
| B-3 | 旋转 90°、锁定、锁定木纹各一次 | 旋转后包围盒中心不偏移；锁定后拖不动；木纹锁定后自动排料也不再转它 |
| B-4 | 改板材为 1220×2440 / 余量 10 / 间距 5 | 越界件被夹回可用区域，`overflowCount` 与实际越界件数一致 |
| B-5 | 点「自动排料」，再点「取消」 | 任务进入 `cancelled` 终态且没有 `running` 残留；取消后画布仍是最后一次落库的位置 |
| B-6 | 连续自动排料 5 次 | 每次耗时稳定、结果不越界；任务列表不会无限增长（上限 400 条） |
| B-7 | 未配置 `BTCAM_DEEPNEST_CMD` 时点自动排料 | 回退内置扫描并记一条 warning（符合设计，不是错误） |

#### C 组 · 刀路与后处理（模块 5/6/7）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| C-1 | 生成刀路（外轮廓 + 内孔，带微连接） | 任务完成；画布绿实线切削、虚线空走、起割点可见；点数/层数合理 |
| C-2 | 绑定刀具库里的刀再生成 | 直径/进给/方向由刀具接管，面板对应字段变灰 |
| C-3 | 套用工艺模板再生成 | 补偿方向、微连接宽度按模板生效 |
| C-4 | 导出 NC / PLT / CUT 各一次 | 文件落盘、日志记录字节数；NC 首尾是 `%`、`M30`；PLT 首尾 `IN;`/`SP0;` |
| C-5 | 勾「全部板材」导出 | 一块板一个文件，文件名带板序号 |
| C-6 | 切割深度 18 / 每层 3 | 刀路分 6 层下刀，每层 Z 递减、最后到 −18 |

#### D 组 · 余料复用（模块 10）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| D-1 | 排完一块板后点「计算剩余区域」 | 列出可用碎块（按面积从大到小），勾选后能存进余料库 |
| D-2 | 把余料设为当前板材 | 板材尺寸=余料包围盒、余量归 0、画布边界画成余料轮廓而非矩形 |
| D-3 | **绑定余料后点「复位」** | 放不进轮廓的零件停靠到板外上方；`overflowCount` = 越界件数（本轮修复点） |
| D-4 | 在余料轮廓内拖一个零件到缺口位置 | 被拒绝并提示「超出余料轮廓」 |
| D-5 | 余料板上点「自动排料」 | 排得上的整块落在轮廓内、排不上的整块停靠板外；**不会出现跨在缺口上的件**（见 P-1 遗留项） |
| D-6 | 删掉正在当板材的余料 | 自动退回矩形整板，不会拿着幽灵轮廓继续判定 |
| D-7 | 解绑余料 | 退回矩形板并恢复原余量，`scrapId=0` |

#### E 组 · 自定义后处理编辑器（模块 11）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| E-1 | 「后处理…」里复制内置模板另存为自定义 id | 保存成功，列表出现新模板且标记为自定义 |
| E-2 | 在头部加一行 `( TEST-1 )` 后**直接导出** | 导出的文件里立刻出现 `TEST-1`（改模板即时生效，无需重启） |
| E-3 | 把 `cutXY` 改成 `G01 X{x} Y{y} {nope}` 再保存 | 保存被拦：字段 `gcode.cutXY` 报「未知占位符 {nope}」 |
| E-4 | 把 `cutXY` 清空 | 报「cutXY 不能为空」（槽位必填，否则刀路会静默少一半） |
| E-5 | 预览与导出对比 | 预览文本与导出文件结构一致（共用同一渲染器，仅坐标不同） |
| E-6 | 删除自定义模板后再导出 | 导出报「后处理 #xxx 不存在」并提示刷新列表；内置模板不可删 |

#### F 组 · 工单与持久化（模块 8）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| F-1 | 保存工单（当前工程带余料绑定） | 工单详情里零件数/轮廓数/利用率正确，余料绑定一并存进快照 |
| F-2 | 打开工单后再导出，导出时带工单 | 加工文件自动登记到该工单（详情「文件」页可见，含格式与字节） |
| F-3 | 先解绑余料，再「打开」这张工单 | 余料绑定被恢复，画布边界回到余料轮廓 |
| F-4 | 另存为 / 改名 / 删除 | 另存为生成新单不影响原单；删除连带清文件记录 |
| F-5 | **关闭软件再启动，重新打开该工单** | 零件、摆放、板材、余料、刀具与模板引用、后处理选择全部原样恢复 |

#### G 组 · MES 工单闭环（模块 12）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| G-1 | 设置页填地址/密钥 → 测试连接 | 连通返回服务器信息；地址错报「已尝试 N 次」；401/403 不重试 |
| G-2 | 点「拉取工单」 | 列表出现 MES 单；重复拉取只有一条（按单号幂等），本地状态不丢 |
| G-3 | 未建本地工单时看列表 | 关联徽标显示「未建本地工单」；顶部统计 `linkedCount` 为 0 |
| G-4 | 点「按单号新建工单」 | 生成 `code = MES 单号` 的本地工单，徽标变「已关联工单 #id」 |
| G-5 | 排料 + 刀路 + 导出后点「回传」 | MES 收到 `usage`（零件数/利用率）+ 加工文件；本地单标记 `archived`，日志页有成功记录 |
| G-6 | 关掉 MES 开关再点回传 | 返回 400「MES 对接未启用」，且**一个网络请求都不发** |
| G-7 | 开关关闭状态下做导入/排料/刀路/导出 | 全部正常，无告警弹窗，日志里没有 MES 相关报错 |
| G-8 | 回传时 MES 宕机 | 报 502 并带尝试次数；本地工单、文件、数据都不受影响（可稍后重试） |

#### H 组 · 异常容错与资源（模块 2/4/9）

| 编号 | 操作 | 预期 |
| --- | --- | --- |
| H-1 | 排料/刀路进行中强杀后端 | 前端状态栏 4 s 内转「已断开」，指数退避重连；后端拉起后自动恢复并重新同步图形 |
| H-2 | 拔网线/让机器休眠 2 分钟后唤醒 | 服务端 90 s 空闲断开半开连接；客户端 45 s 无回帧主动重连，`clients` 计数不虚高 |
| H-3 | 反复开关窗口 10 次 | 后端 `clients` 归零，任务队列无线程泄漏 |
| H-4 | 排料中途取消，再立刻排料 | 无残留子进程（任务管理器看不到 DeepNest），CPU 回落 |
| H-5 | 任务失败（坏 DXF / 非法模板） | 前端弹窗为中文业务原因；日志面板能按 `level`/`source` 区分 业务 / 几何 / 外部服务 |
| H-6 | 看日志来源分类 | `app.services.*`=业务，`app.geometry*`/`occt`=几何，`app.mes*`=外部 MES，`app.core.tasks`=任务，`frontend`=前端异常 |

### 25.6 本轮实测结果

| 项 | 结果 |
| --- | --- |
| `e2e_test.py` 主流程 | **38/38 通过**（P/A/B/C/D/E/F/G 七段，用时 5.4 s） |
| `e2e_test.py --verify`（重启后） | **3/3 通过**（工单 #99 / 余料 #56 · 173 点 / 模板 #59 全部原样读回，探针已清理） |
| 13 个模块回归脚本 | **274/274 通过**，0 失败（含 auto_nest 19 项、scrap 38 项复跑） |
| 前端 `tsc --noEmit` / `vite build` | 0 错误；786 modules，7.1 s |
| 联调后清理 | 临时日志/探针脚本已删；工单、余料、模板、MES 缓存中 `E2E` 残留均为 0 |
