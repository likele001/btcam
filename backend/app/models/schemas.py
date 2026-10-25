"""REST 请求/响应模型。

命名直接使用前端 camelCase，省掉一层字段映射，减少低级错误。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class OffsetRequest(BaseModel):
    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list, description="为空表示全部轮廓")
    distance: float = 1.0
    join: Literal["round", "miter", "square"] = "round"


class NestRequest(BaseModel):
    projectId: Optional[int] = None
    sheetWidth: float = 1220.0
    sheetHeight: float = 2440.0
    gap: float = 5.0
    rotation: bool = True
    engine: Literal["auto", "deepnest", "shelf"] = "auto"
    layerIds: List[int] = Field(default_factory=list)
    partIds: List[int] = Field(default_factory=list, description="为空表示按图层取全部闭合轮廓")


class ToolpathStartPoint(BaseModel):
    """手工起割点：画布上点一下即可，后端自动吸附到最近轮廓。

    contourId 可选：接口直调时用于指定轮廓；前端点选只传 x/y，
    后端按“离哪个轮廓最近”自动分配，不需要前端理解几何。
    """

    x: float
    y: float
    contourId: Optional[int] = None


class ToolpathRequest(BaseModel):
    """刀路生成（模块 5）：2D 轮廓切割。

    source 决定轮廓来源：
      - doc    原始图纸（按 ids/layerIds 筛选，空表示全部）
      - layout 当前排料板（整板合并，所有零件统一生成一套刀路）
    cutType 只负责“切哪些轮廓”，side 只负责“往哪边补偿”，两者独立。
    """

    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list)
    layerIds: List[int] = Field(default_factory=list)
    contours: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="可选，直接指定轮廓（前端临时图形）")
    # --- 模块 5：刀路类型与工艺
    source: Literal["doc", "layout"] = Field("doc", description="轮廓来源")
    sheetIndex: int = Field(0, ge=0, description="排料板序号（多板时用）")
    cutType: Literal["auto", "outer", "inner"] = Field(
        "auto", description="auto=外轮廓+内孔，outer=只切外轮廓，inner=只切内孔")
    direction: Literal["ccw", "cw", "climb", "conventional"] = Field(
        "ccw", description="ccw/cw 强制方向；climb=顺铣（外CCW/内CW）；conventional=逆铣")
    optimize: bool = Field(True, description="自动排序以缩短空程")
    startPoints: List[ToolpathStartPoint] = Field(
        default_factory=list, description="手工起割点（可多个）")
    microJoint: bool = Field(False, description="是否添加微连接（桥接）")
    microJointWidth: float = Field(2.0, ge=0.0, le=100.0, description="微连接宽度 mm")
    microJointCount: int = Field(2, ge=0, le=20, description="每条轮廓的微连接数量")
    toolDiameter: float = Field(6.0, ge=0.0, le=1000.0)
    toolId: Optional[int] = Field(None, description="刀具库 id；提供时刀具参数优先")
    templateId: Optional[int] = Field(None, description="工艺模板 id；提供时补偿/微连接按模板")
    side: Literal["auto", "outside", "inside", "on"] = Field(
        "auto", description="auto=外轮廓向外、内孔向内")
    depth: float = Field(18.0, gt=0.0, le=10000.0)
    passDepth: float = Field(3.0, gt=0.0, le=10000.0)
    feed: float = Field(3000.0, gt=0.0)
    plungeFeed: float = Field(800.0, gt=0.0)
    safeZ: float = Field(5.0, ge=0.0)
    liftZ: float = Field(5.0, ge=0.0, description="抬刀高度 mm（退刀到该 Z）")
    spindle: float = Field(18000.0, ge=0.0)
    leadIn: bool = True
    leadRadius: float = Field(2.0, ge=0.0, le=500.0)


class ExportRequest(ToolpathRequest):
    """导出（模块 7）：刀路文本 / DXF 图纸，支持单板与多板批量。

    processorId 优先；只给 format 时按旧路径映射到内置后处理：
      nc -> weihong_nc，plt -> plotter_plt，cut -> cutting_cut。
    allSheets 仅在 source=layout 时生效，每块板一个文件。
    """

    format: Literal["nc", "plt", "cut", "dxf"] = "nc"
    processorId: Optional[str] = Field(None, description="后处理配置 id（优先于 format）")
    name: str = "BT_CAM"
    flipY: bool = False
    speed: int = Field(30, ge=0, le=100)
    force: int = Field(20, ge=0, le=100)
    allSheets: bool = Field(False, description="导出排料板上的全部板材")
    workOrderId: Optional[int] = Field(
        None, description="工单 id：导出成功后自动登记加工文件路径")


class WorkOrderPostConfig(BaseModel):
    """工单里固化的后处理选择（打开工单时回填导出面板）。"""

    processorId: Optional[str] = None
    format: Literal["nc", "plt", "cut", "dxf"] = "nc"
    name: str = "BT_CAM"
    flipY: bool = False
    speed: int = Field(30, ge=0, le=100)
    force: int = Field(20, ge=0, le=100)


class WorkOrderSaveRequest(BaseModel):
    """保存/修改工单（模块 8）。

    POST 总是重新采集当前工程快照；PUT 默认只改元信息，
    recapture=True 时才把当前工程重新拍一份写回。
    """

    projectId: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=64)
    code: str = Field("", max_length=64, description="工件编号/订单号")
    note: str = Field("", max_length=500)
    toolpath: Dict[str, Any] = Field(default_factory=dict)
    post: Optional[WorkOrderPostConfig] = Field(
        None, description="不传则沿用存档中的后处理选择")
    recapture: bool = False


class WorkOrderDuplicateRequest(BaseModel):
    name: str = Field("", max_length=64, description="不传则用“原名 + 副本”")



class PostProcessorRequest(BaseModel):
    """后处理配置（模块 7）：一份 JSON = 一台机床。

    gcode/hpgl/dxf 只需填对应 kind 的那一段；其余字段忽略。
    """

    id: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=64)
    kind: Literal["gcode", "hpgl", "dxf"] = "gcode"
    ext: str = Field(..., min_length=1, max_length=8)
    description: str = Field("", max_length=500)
    gcode: Optional[Dict[str, Any]] = None
    hpgl: Optional[Dict[str, Any]] = None
    dxf: Optional[Dict[str, Any]] = None


class PostDraftRequest(BaseModel):
    """后处理草稿（模块 11）：可视化编辑器提交的整份配置。

    processor 原样收下，不在 pydantic 层做限制，交给 post.editor 逐字段校验，
    这样错误能带上 `gcode.cutXY` 这类字段名，前端才能高亮对应输入框。
    """

    processor: Dict[str, Any]
    processorId: Optional[str] = None


class PostPreviewRequest(PostDraftRequest):
    """预览请求：options 为假数据（尺寸/主轴/是否翻转等），不影响真实刀路。"""

    options: Dict[str, Any] = Field(default_factory=dict)


class TaskAccepted(BaseModel):
    taskId: str
    kind: str
    status: str = "queued"


# ---------------------------------------------------------------- 几何基础服务（模块 2）
class GeometryOffsetRequest(BaseModel):
    """轮廓偏移（刀具补偿）。distance 必须 > 0，方向由 side 决定。"""

    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list, description="为空表示全部轮廓")
    layerIds: List[int] = Field(default_factory=list)
    contours: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="可选，直接指定轮廓（前端临时图形）")
    distance: float = Field(1.0, gt=0, le=10000.0, description="补偿量 mm，必须 > 0")
    join: Literal["round", "miter", "square"] = "round"
    side: Literal["outside", "inside", "auto"] = Field(
        "auto", description="auto=外轮廓向外、内孔向内（刀具走材料外侧）")


class GeometryIntersectRequest(BaseModel):
    """轮廓求交：subject（主轮廓）与 clip（切割轮廓）的公共区域。"""

    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list, description="主体轮廓 id")
    clipIds: List[int] = Field(default_factory=list, description="切割轮廓 id")
    contours: Optional[List[Dict[str, Any]]] = None
    clip: Optional[List[Dict[str, Any]]] = None


class GeometryQueryRequest(BaseModel):
    """查询类接口（包围盒 / 闭合校验 / 自相交），同步返回。"""

    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list)
    layerIds: List[int] = Field(default_factory=list)
    contours: Optional[List[Dict[str, Any]]] = None
    tol: float = Field(0.05, ge=0.0, le=100.0, description="闭合判定容差 mm")


class GeometryMergeRequest(BaseModel):
    """断线合并。tol 是端点吸附容差。"""

    projectId: Optional[int] = None
    ids: List[int] = Field(default_factory=list)
    layerIds: List[int] = Field(default_factory=list)
    contours: Optional[List[Dict[str, Any]]] = None
    tol: float = Field(0.2, gt=0.0, le=20.0, description="端点吸附容差 mm")


# ---------------------------------------------------------------- 排料（模块 3）
class LayoutSheetRequest(BaseModel):
    """板材参数：宽高 + 四边预留余量 + 零件最小间距。"""

    projectId: Optional[int] = None
    width: float = Field(1220.0, gt=0.0, le=100000.0)
    height: float = Field(2440.0, gt=0.0, le=100000.0)
    margin: float = Field(10.0, ge=0.0, le=5000.0, description="四边预留余量 mm")
    gap: float = Field(5.0, ge=0.0, le=5000.0, description="零件最小间距 mm")


class LayoutPlaceRequest(BaseModel):
    """移动零件：tx/ty 是旋转后包围盒左下角在板材上的位置。"""

    projectId: Optional[int] = None
    partId: int
    tx: float
    ty: float
    rot: Optional[float] = Field(None, description="不传则保持当前角度")


class LayoutRotateRequest(BaseModel):
    projectId: Optional[int] = None
    partId: int
    rot: float = Field(..., description="目标角度（逆时针，度），绕包围盒中心旋转")


class LayoutLockRequest(BaseModel):
    projectId: Optional[int] = None
    partId: int
    locked: bool = True


class LayoutFixedRotRequest(BaseModel):
    """木纹方向锁定：锁定后自动排料与手动旋转都不再改变该零件角度。"""

    projectId: Optional[int] = None
    partId: int
    fixedRot: bool = True


class LayoutDuplicateRequest(BaseModel):
    """复制零件（排料板增件）：份数上限防误输入一次造出上千个零件拖垮渲染。"""

    projectId: Optional[int] = None
    partId: int
    count: int = Field(1, ge=1, le=99, description="复制份数（1~99）")


class LayoutNestRequest(BaseModel):
    """自动排料（模块 4）：在排料板上重排零件。

    板材参数与零件列表均可选：
      - 不传板材参数 -> 沿用当前排料板（面板/工具栏交互路径）；
      - 传了         -> 先按 set_sheet 落库，再重排（脚本/接口直调路径）；
      - 不传 partIds -> 全部未锁定零件参与；
      - 传了 partIds  -> 只重排这些零件，其余零件原地不动并作为障碍物。
    """

    projectId: Optional[int] = None
    engine: str = Field("auto", pattern="^(auto|deepnest|scan)$",
                        description="auto=有 DeepNest 桥接则用否则内置扫描；scan=内置扫描")
    rotation: bool = Field(True, description="是否允许 90° 旋转（木纹锁定的零件始终不转）")
    width: Optional[float] = Field(None, gt=0.0, le=100000.0, description="板材宽 mm")
    height: Optional[float] = Field(None, gt=0.0, le=100000.0, description="板材高 mm")
    margin: Optional[float] = Field(None, ge=0.0, le=5000.0, description="四边余量 mm")
    gap: Optional[float] = Field(None, ge=0.0, le=5000.0, description="零件最小间距 mm")
    partIds: Optional[List[int]] = Field(
        None, description="只重排这些零件；为空/不传表示全部未锁定零件")


# ---------------------------------------------------------------- 刀具库 / 工艺模板（模块 6）
class ToolRequest(BaseModel):
    """刀具：铣刀（平底/锥刀/球刀）或线切割钼丝。

    铣刀用 diameter + tipRadius；钼丝用 wireDiameter + dischargeGap，
    实际补偿半径 = 丝径/2 + 放电间隙（由服务层换算成等效直径）。
    """

    projectId: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=64)
    category: Literal["mill", "wire"] = "mill"
    toolType: Literal["flat", "ball", "v"] = "flat"
    diameter: float = Field(0.0, ge=0.0, le=1000.0)
    tipRadius: float = Field(0.0, ge=0.0, le=500.0)
    wireDiameter: float = Field(0.0, ge=0.0, le=100.0)
    dischargeGap: float = Field(0.0, ge=0.0, le=100.0)
    feed: float = Field(3000.0, gt=0.0)
    plungeFeed: float = Field(800.0, gt=0.0)
    spindle: float = Field(18000.0, ge=0.0)
    passDepth: float = Field(3.0, gt=0.0, le=10000.0)
    safeZ: float = Field(5.0, ge=0.0, le=10000.0)
    liftZ: float = Field(5.0, ge=0.0, le=10000.0)
    direction: Literal["climb", "conventional"] = "climb"
    note: str = ""


class TemplateParams(BaseModel):
    """模板里的补偿/微连接参数（不含刀具参数与切深）。"""

    cutType: Literal["auto", "outer", "inner"] = "auto"
    side: Literal["auto", "outside", "inside", "on"] = "auto"
    optimize: bool = True
    microJoint: bool = False
    microJointWidth: float = Field(2.0, ge=0.0, le=100.0)
    microJointCount: int = Field(2, ge=0, le=20)
    leadIn: bool = True
    leadRadius: float = Field(2.0, ge=0.0, le=500.0)


class TemplateRequest(BaseModel):
    """工艺模板：一把刀 + 一组补偿参数，按物料分类。"""

    projectId: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=64)
    material: str = Field("", max_length=64)
    toolId: Optional[int] = None
    params: TemplateParams = Field(default_factory=TemplateParams)
    note: str = ""


# ---------------------------------------------------------------- 余料库（模块 10）
class ScrapRequest(BaseModel):
    """新建/修改余料。

    outline 为空时用 width/height 生成矩形轮廓（手工录一块方料）；
    宽高与面积最终由后端从轮廓算出，前端传的值只作参考。
    """

    projectId: Optional[int] = None
    name: str = Field("", max_length=64)
    code: str = Field("", max_length=64, description="余料编号（贴标/扫码）")
    material: str = Field("", max_length=64)
    thickness: float = Field(0.0, ge=0.0, le=1000.0)
    width: Optional[float] = Field(None, gt=0.0, le=100000.0)
    height: Optional[float] = Field(None, gt=0.0, le=100000.0)
    outline: List[List[float]] = Field(
        default_factory=list, description="轮廓点 [[x,y],...]，坐标系与板材一致")
    note: str = Field("", max_length=500)


class ScrapRemainingRequest(BaseModel):
    """计算当前板材的剩余区域（预览，不落库）。"""

    projectId: Optional[int] = None
    gap: Optional[float] = Field(None, ge=0.0, le=5000.0,
                                 description="不传则用板材当前最小间距")
    minArea: float = Field(10000.0, ge=0.0, le=1e9, description="小于该面积(mm²)的碎料不列出")


class ScrapSaveRequest(ScrapRemainingRequest):
    """把选中的剩余区域存进余料库（index 来自 /api/scrap/remaining 的返回）。"""

    indices: List[int] = Field(default_factory=list)
    namePrefix: str = Field("", max_length=40)
    material: str = Field("", max_length=64)
    thickness: float = Field(0.0, ge=0.0, le=1000.0)
    note: str = Field("", max_length=500)


class ScrapApplyRequest(BaseModel):
    """把余料设为当前排料板（复用排料接口，轮廓作为边界）。"""

    projectId: Optional[int] = None
    gap: Optional[float] = Field(None, ge=0.0, le=5000.0)
    resetPlacements: bool = Field(True, description="换板后按新尺寸重新初始行排")


class ScrapClearRequest(BaseModel):
    """解绑余料，退回规则矩形板（可选同时套用新尺寸）。"""

    projectId: Optional[int] = None
    width: Optional[float] = Field(None, gt=0.0, le=100000.0)
    height: Optional[float] = Field(None, gt=0.0, le=100000.0)
    margin: Optional[float] = Field(None, ge=0.0, le=5000.0)
    gap: Optional[float] = Field(None, ge=0.0, le=5000.0)


# ---------------------------------------------------------------- MES 对接（模块 12）

class MesSettingsRequest(BaseModel):
    """保存 MES 配置。

    这里不做范围校验（pydantic 报 422 会盖掉 settings.py 里的中文提示），
    统一交给 settings.build()，路由层再把 MesConfigError 翻成 400。
    """

    enabled: bool = False
    vendor: str = "cenkor"
    baseUrl: str = ""
    apiKey: Optional[str] = None      # 留空 = 沿用已保存的密钥
    timeout: float = 8.0
    retries: int = 2
    autoPush: bool = False
    includeFiles: bool = True
    paths: Dict[str, str] = Field(default_factory=dict)


class MesPullRequest(BaseModel):
    """从 MES 拉单。"""

    projectId: Optional[int] = None
    limit: int = Field(50, ge=1, le=200)


class MesPushRequest(BaseModel):
    """回传归档；workOrderId 不传时后端按 MES 单号自动找同编号本地工单。"""

    orderId: int
    workOrderId: Optional[int] = None
    includeFiles: Optional[bool] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    geometryEngine: str
    nestingEngine: str
    clients: int
    workers: int
    dataDir: str


# ---------------------------------------------------------------- 日志与告警（模块 9）
class ClientLogRequest(BaseModel):
    """前端异常上报：渲染报错/未捕获的 Promise 拒绝统一落到后端日志。"""

    level: Literal["info", "warning", "error"] = "error"
    message: str = Field(..., min_length=1, max_length=500)
    detail: str = Field("", max_length=2000, description="堆栈或上下文")
    kind: str = Field("frontend", max_length=40, description="错误分类标签")
    source: str = Field("frontend", max_length=60, description="组件/页面名")
