# BtCAM 部署文档（V0.4 完整版）

> 面向：现场安装、工艺员、技术支持。打包出包流程见 [PACKAGING.md](PACKAGING.md)。

---

## 1. 交付形态与系统要求

| 形态 | 文件 | 适用 |
| --- | --- | --- |
| 便携版（推荐） | `BtCAM-0.4.0-portable.exe` | 拷到任意目录双击运行，免安装、免管理员 |
| NSIS 安装包 | `BtCAM-0.4.0-setup.exe` | 需要开始菜单快捷方式/固定目录；**装到当前用户**，不写 Program Files |
| MSI | `BtCAM-0.4.0.msi` | 域环境批量部署：`msiexec /i BtCAM-0.4.0.msi /qn` |

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 1809+ / Windows 11，64 位 |
| 运行时 | WebView2 Runtime（安装包已内置静默引导程序，缺了会自动补装；完全离线机请先手动装 Evergreen Runtime） |
| 内存 | 建议 8 GB 以上（排料/刀路吃 CPU，不是内存） |
| 磁盘 | 程序 ~30 MB；数据目录随图纸/工单增长，建议预留 2 GB |
| 端口 | 本机 `127.0.0.1:8975`（仅回环，不对外监听） |
| 权限 | 普通用户即可；不需要管理员、不写注册表（NSIS 只写当前用户项） |

---

## 2. 安装

**便携版**：把 exe 放到一个可写目录（例如 `D:\BtCAM\`），双击。不要放在 `C:\Program Files` 下
（那里普通用户不可写，虽然程序本身不写安装目录，但现场常会顺手放导出文件）。

**安装包**：双击 `BtCAM-0.4.0-setup.exe` → 选择目录 → 完成。安装模式是 `currentUser`，
不会弹 UAC；卸载走「设置 → 应用」。

**静默部署**

```powershell
# NSIS
Start-Process .\BtCAM-0.4.0-setup.exe -ArgumentList '/S' -Wait
# MSI
msiexec /i .\BtCAM-0.4.0.msi /qn /norestart
```

---

## 3. 首次启动注意事项（★ 现场最容易踩的坑）

1. **第一次启动慢 2~5 秒是正常的**。主程序要把内嵌的后端（约 25 MB）解压到
   `%LOCALAPPDATA%\com.btsoft.btcam\bin\cam-backend-<hash>.exe`，同一版本只解压一次，
   之后启动直接复用。杀毒软件首次扫描这个 exe 会更慢。
2. **状态栏必须是「已连接」**。出现「已断开」先看软件内日志面板（工具栏「日志…」），
   90% 是 8975 端口被上一份残留进程占着：任务管理器结束 `cam-backend.exe` 后重启软件。
3. **不会弹控制台黑窗**。生产版主程序与后端都隐藏控制台；这是刻意的，不是启动失败。
4. **右键没有「检查/Inspect」**。生产版关闭了开发者工具，这是刻意的。
   所有排障信息看「日志…」面板和「关于…」弹窗。
5. **首次启动自动建库并补齐默认数据**（无需任何手工操作）：
   - 数据目录 `%LOCALAPPDATA%\BtCAM\`、库文件 `btcam.db`；
   - 默认工程「未命名工程」+ 板材 1220×2440×18 + 图层（切割/划槽/定位孔）；
   - 刀具库 3 把：T1 平底 6mm、T2 平底 3mm、W1 钼丝 0.2mm；
   - 工艺模板 3 条：密度板 18mm 标准、多层板 18mm 微连接、无纺布 钼丝；
   - 后处理模板 6 份：`weihong_nc` / `syntec_nc` / `shanlong_nc` / `plotter_plt` / `cutting_cut` / `generic_dxf`。
6. **不要给 `%LOCALAPPDATA%\BtCAM` 设只读/禁止写入**：后处理模板与日志都要写在这里。
7. **多用户共用一台电脑**：数据按 Windows 用户隔离，A 用户看不到 B 用户的工单。
   需要共享数据时导出工单文件交接，或统一用一个 Windows 账号。
8. **同时开两份程序**是允许的：第二个实例检测到 8975 已有后端会直接复用，
   但两边共用同一个库，**不建议**两个窗口同时改同一个工单。
9. **防火墙提示**可以全部拒绝：端口只监听 `127.0.0.1`，MES 对接是**主动外连**，不需要入站规则。
10. **首次启动先验一遍「关于…」**：确认 `V0.4 完整版`、版本号 `0.4.0`、后端版本 `0.4.0`、
    数据目录路径正确。这一步能把「装错包/装错版本」当场发现。

---

## 4. 数据目录、备份与还原

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\BtCAM\btcam.db` | SQLite 库：工程、图形、零件、排料、刀路参数、工单、余料、MES 配置与工单缓存 |
| `%LOCALAPPDATA%\BtCAM\btcam.db-wal` / `-shm` | WAL 日志，**备份时一并拷走**（或先关软件） |
| `%LOCALAPPDATA%\BtCAM\post\<id>.json` | 后处理模板（一台机床一份） |
| `%LOCALAPPDATA%\BtCAM\logs\backend.log` | 后端日志，单文件 4 MB，滚动保留 3 份 |
| `%LOCALAPPDATA%\com.btsoft.btcam\bin\` | 后端 exe 解压缓存（可随时删除，下次启动自动重建） |

**备份**（建议每天/每次批量加工前）：关闭软件 → 复制整个 `%LOCALAPPDATA%\BtCAM` 目录。

**还原**：关闭软件 → 用备份覆盖 `%LOCALAPPDATA%\BtCAM` → 启动。

**数据库自检/离线初始化**（可选，运维用）：

```powershell
# 只读自检：表、旧版升级所需列、默认刀具/模板、内置后处理是否齐全
python scripts\init_db.py --check

# 离线建库（不启动软件），不会覆盖任何已有业务数据
python scripts\init_db.py

# 拿一个旧库副本演练升级，不动线上库
python scripts\init_db.py --db D:\backup\btcam-old.db
```

退出码：`0` 通过 / `1` 缺表缺列或种子缺失 / `2` 库文件不存在（全新机器正常）。
便携版用户机器上没有 Python 时，可把 `btcam.db` 拷回开发机执行上述命令。

---

## 5. 升级（覆盖安装）

1. 关闭软件（确认任务管理器里没有 `cam-backend.exe`）；
2. 备份 `%LOCALAPPDATA%\BtCAM`；
3. 用新包覆盖安装（或用新便携版替换旧 exe）；
4. 启动 → 打开「关于…」核对版本号；
5. 老工单点开验证一份，再开始生产。

升级时自动发生的事（**不需要手工写 SQL**）：

| 场景 | 行为 |
| --- | --- |
| 老库缺新列（工单 `code/note/stats/updated_at`、归档文件 `format/processor/bytes/sheet_index`、余料 `outline`、刀具多列…） | 启动时 `db._migrate()` 用 `PRAGMA table_info` 探测后 `ALTER TABLE` 补列，旧数据原样保留 |
| 老工单的件数/利用率只存在于快照 JSON 里 | 首次补 `stats` 列时从快照回填到列上，列表页直接显示件数 |
| `updated_at` 为空/0 的老行 | 用 `created_at` 回填，避免列表显示 1970 年 |
| 余料表还是旧「多环 rings」结构 | 整表按 id 重建为「单轮廓 outline」结构并迁移数据 |
| 内置后处理模板有更新 | 按模板自带 `version` 覆盖内置文件；**用户自建/改过的模板（`builtin=false`）永远不动** |
| 刀具库为空 | 补 3 把默认刀 + 3 条工艺模板；已有刀具一律不覆盖 |

库结构版本写在 `app_setting['schema.version']`（V0.4 = `4`），排障时可一眼看出这个库被哪版程序迁移过。

> 从 V0.1/V0.2/V0.3 升级到 V0.4 是**原地升级**，不需要导出导入。
> 反向降级（V0.4 → 旧版）**不支持**：旧版程序不认识新列/新表，会报错。

---

## 6. MES 配置说明（可选件，模块 12）

**不配也能全功能使用**：MES 只是拉单/回传的通道，关掉后导入、排料、刀路、导出照常。

### 6.1 配置步骤

1. 工具栏「MES…」→ 打开设置页；
2. 打开「启用 MES 对接」开关；
3. 选择厂商模板（`CenkorMes` / `LightMes` / 自定义）；
4. 填服务地址，例如 `http://192.168.1.20:8080`（**只填到端口或前缀，不要带具体接口路径**）；
5. 填 API Key（首次必填；之后再保存时输入框留空 = 沿用旧密钥）；
6. 点「测试连接」，看到成功再保存；
7. 「拉取工单」→ 选单 → 自动关联本地工单 → 排料/刀路 → 导出时勾选回传。

### 6.2 字段与默认值

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 关 | 关闭时所有 `/api/mes/*` 只回「未启用」，不发任何网络请求 |
| `vendor` | `cenkor` | 三家模板，路径不同；`custom` 时三个路径都由你填 |
| `baseUrl` | 空 | 必须是 `http(s)://主机[:端口][/前缀]`；尾部 `/` 会被去掉；启用后必填 |
| `apiKey` | 空 | 最长 256 字符；落库明文（本机单用户工具），接口只回掩码 `sk****abcd` |
| `timeout` | `8` 秒 | 允许 0.5~60 |
| `retries` | `2` | 允许 0~5，用于测试连接与推送重试 |
| `autoPush` | 关 | 导出完成后自动回传，不必再点一次 |
| `includeFiles` | 开 | 回传时带上 NC/PLT/CUT 文件 |
| `paths.ping` | 厂商内置 | 测试连接用 |
| `paths.orders` | 厂商内置 | 拉单列表用 |
| `paths.archive` | 厂商内置 | 回传用，含 `{remoteId}` 占位符，发送前替换成 URL 编码后的单号 |

内置路径：

| vendor | ping | orders | archive |
| --- | --- | --- | --- |
| `cenkor` | `/api/v1/ping` | `/api/v1/workorders` | `/api/v1/workorders/{remoteId}/archive` |
| `lightmes` | `/api/ping` | `/api/workorders` | `/api/workorders/{remoteId}/archive` |
| `custom` | `/api/ping` | `/api/workorders` | `/api/workorders/{remoteId}/archive` |

### 6.3 关联规则与常见问题

- **本地工单按 `code`（工件编号）= MES 单号 自动关联**。现场务必让 `code` 与 MES 单号一致，
  否则回传时找不到本地工单，需要在下拉里手工指定。
- 「未启用」：`enabled` 没开，或服务地址为空（地址为空时保存会被拒绝）。
- 「MES 侧不可达 / 已尝试 N 次」：网络不通、地址写成了内网域名而 DNS 不通、或防火墙拦了出站。
  先用浏览器/`curl` 在同一台机器上访问 `baseUrl + ping` 验证。
- 「密钥不对」：留空保存不会改密钥；密钥填错只能粘对的那一把覆盖。
- 回传失败不影响导出文件本身：文件已经在本地磁盘，重新点回传即可。
- 所有交互（测试/拉单/回传，含状态码与尝试次数）都记录在 MES 面板的日志里。

---

## 7. DeepNest 依赖部署说明（排料增强）

### 7.1 先搞清楚现状

DeepNest **不是 Python 包**，程序不内置它，而是通过**外部进程桥接**：

- 没配置 `BTCAM_DEEPNEST_CMD` → 自动用内置行排算法（`shelf`），功能完整、结果稳定，
  只是利用率不如 DeepNest；
- 配了 → 自动排料改走 DeepNest，失败/超时仍会退回行排。

装不装 DeepNest 只影响**排料利用率**，不影响其它任何功能。

### 7.2 桥接协议（自己写封装脚本时必须严格照这个）

- 程序执行 `BTCAM_DEEPNEST_CMD` 指向的命令（`shlex` 切分，Windows 路径含空格要加引号）；
- 把**一个 JSON 写到子进程 stdin**（写完即关闭）；
- 从 **stdout 读一个 JSON**，退出码必须是 `0`，stderr 只在报错时用；
- 单次超时 180 秒，超时会杀掉子进程并退回行排。

请求：

```json
{
  "sheet": { "w": 2440, "h": 1220 },
  "gap": 5.0,
  "rotation": true,
  "parts": [
    { "id": 17, "pts": [[0,0],[100,0],[100,50],[0,50]], "rotation": true }
  ]
}
```

`pts` 是零件轮廓（mm，已归一化到包围盒左下角），`rotation=false` 表示这件**锁定角度**
（木纹方向），不能旋转。

响应：

```json
{
  "placements": [
    { "partId": 17, "sheet": 0, "tx": 10.0, "ty": 10.0, "rot": 0 }
  ],
  "sheetCount": 1,
  "fitness": 0.82
}
```

`tx/ty` 是零件包围盒左下角在板材上的位置，`rot` 为逆时针角度。

### 7.3 最小桥接脚本示例（Node.js）

```js
// deepnest-bridge.js —— 读 stdin 的 JSON，交给你的 DeepNest 调用，回写协议规定的 JSON
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => {
  const req = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  // 这里换成真实调用：spawn DeepNest / 调它的 HTTP 服务 / 读它的结果文件
  const placements = req.parts.map((p, i) => ({
    partId: p.id, sheet: 0, tx: 10 + i * 120, ty: 10, rot: 0,
  }));
  process.stdout.write(JSON.stringify({ placements, sheetCount: 1, fitness: 0 }));
});
```

> 上面的坐标只是占位，没有排料逻辑。真实部署时把「计算」换成：
> 把 `req` 转成 DeepNest 的输入、起 DeepNest、读它输出的每一件 `(x, y, rotation)`，
> 再按上面的字段回写。

### 7.4 配置环境变量

桥接命令由**后端进程**读取，所以要设在「后端能看到的」环境里：

```powershell
# 当前用户永久生效（推荐）：重开软件后生效
[Environment]::SetEnvironmentVariable('BTCAM_DEEPNEST_CMD', 'node D:\tools\deepnest\deepnest-bridge.js', 'User')

# 只对当前 PowerShell 会话生效（调试用）
$env:BTCAM_DEEPNEST_CMD = 'node D:\tools\deepnest\deepnest-bridge.js'
```

注意：

- 便携版/安装版都是从主程序拉起后端，**主程序会继承你的用户环境变量**，所以设 User 级就行；
- 命令里如果路径带空格，整体用双引号包住：`'"C:\Program Files\nodejs\node.exe" D:\dn\bridge.js'`；
- 改了环境变量要**完全退出软件再启动**（不是最小化）。

### 7.5 验证与回退

| 检查点 | 期望 |
| --- | --- |
| 启动后状态栏「排料」 | 显示 `deepnest`（未配置时是 `shelf`） |
| 工具栏「自动排料」 | 任务面板进度到 90% 后返回结果，利用率高于行排 |
| 日志面板 | 出现「排料引擎: DeepNest 桥接」，无「回退行排」字样 |
| 手工单测 | `echo {...} | node bridge.js` 能直接吐出合法 JSON，且有 `placements` |

常见失败：命令写错 → 日志「DeepNest 桥接进程启动失败」；输出不是 JSON → 「返回不是合法 JSON」；
超时 → 「DeepNest 计算超时（180s）」。以上任一情况都会**自动回退行排**，不会卡死界面。

回退到行排：删掉 `BTCAM_DEEPNEST_CMD` 变量，重启软件。

---

## 8. 几何引擎说明（与打包选项相关）

| 包 | 几何引擎 | 说明 |
| --- | --- | --- |
| 默认生产包（约 25 MB 后端） | `clipper` | 轮廓按 0.02 mm 弦高离散后做偏置/布尔，功能完整 |
| `-WithOcct` 重量包（约 130 MB 后端） | `occt` | 精确圆弧/NURBS，客户指定 OCCT 时用 |

看当前实际生效的引擎：状态栏「几何 …」、或「关于…」弹窗的「几何引擎」。
想强制指定可用环境变量 `BTCAM_GEOM_ENGINE`（`auto` / `occt` / `clipper`）。

---

## 9. 环境变量一览（全部可选）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `BTCAM_PORT` | `8975` | 后端端口；改这里前端也要改，**不建议动** |
| `BTCAM_HOST` | `127.0.0.1` | 监听地址；**不要改成 0.0.0.0**（会把计算服务暴露到局域网） |
| `BTCAM_WORKERS` | CPU 核数/2 | 计算并发度 |
| `BTCAM_PROCESS_POOL` | `0` | `1` = 用进程池隔离 C++ 崩溃（代价是任务参数要可 pickle） |
| `BTCAM_GEOM_ENGINE` | `auto` | `auto` / `occt` / `clipper` |
| `BTCAM_NEST_ENGINE` | `auto` | `auto` / `deepnest` / `shelf` |
| `BTCAM_LOG_LEVEL` | `INFO` | 排障时临时设 `DEBUG` |
| `BTCAM_LOG_BUFFER` | `1000` | 日志面板内存缓冲条数 |
| `BTCAM_DEEPNEST_CMD` | 空 | DeepNest 桥接命令，见 §7 |
| `BTCAM_PARENT_PID` | 由主程序注入 | 父进程守护；**不要手工设** |
| `BTCAM_CHUNK_BYTES` | 1 MB | 大数据分片阈值，一般不用动 |

---

## 10. 排障入口与卸载

**排障顺序**：状态栏连接状态 → 工具栏「日志…」（面板内可复制）→
`%LOCALAPPDATA%\BtCAM\logs\backend.log` → 「关于…」里的版本/数据目录/引擎。

| 现象 | 先看这里 | 常见原因 |
| --- | --- | --- |
| 一直「连接中 / 已断开」 | 日志面板 | 8975 被占、杀软拦了 `cam-backend-*.exe` |
| 导入 DXF 失败 | 日志面板 + 告警弹窗 | 图纸缺闭合轮廓/自相交，按告警提示修；单文件 ≤ 200 MB |
| 排料结果不如预期 | 状态栏「排料」 | 用的是 `shelf` 行排；按 §7 配 DeepNest |
| 导出文件机床不认 | 后处理面板 | 换对应机床模板（weihong/syntec/shanlong），或自建模板 |
| 打开旧工单报错 | 日志面板 | 先升级到本版（会自动补列）；仍报错时把 `btcam.db` 交回开发 |

**卸载**：安装版走「设置 → 应用 → BtCAM → 卸载」。卸载**不会**删数据目录；
彻底清理需手工删除 `%LOCALAPPDATA%\BtCAM`（数据/模板/日志）与
`%LOCALAPPDATA%\com.btsoft.btcam`（后端 exe 缓存）。删之前务必先备份。
