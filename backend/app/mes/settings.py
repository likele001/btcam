"""MES 连接配置（模块 12）。

存储位置
    SQLite `app_setting` 表里一条 JSON，key = `mes.config`。
    不单独建表的原因：配置就一份、不需要检索，app_setting 本来就是给这种场景留的。

密钥处理
    - 落库是明文（本机单用户工具，加密没有额外收益，反而不利于排障）；
    - 出接口一律掩码 `sk_****abcd`，前端输入框不显示原值，留空表示"不修改"；
    - 日志里永不打印 api_key，client 只打印 URL 与状态码。

地址与路径按厂商内置模板，`vendor=custom` 时可自己填三个路径。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.models import db

logger = logging.getLogger(__name__)

SETTING_KEY = "mes.config"

# 三家内置模板：ping 用于设置页"测试连接"，orders 拉单，archive 回传
# {remoteId} 是占位符，回传前替换成 URL 编码后的单号。
VENDORS: Dict[str, Dict[str, str]] = {
    "cenkor": {
        "label": "CenkorMes",
        "ping": "/api/v1/ping",
        "orders": "/api/v1/workorders",
        "archive": "/api/v1/workorders/{remoteId}/archive",
    },
    "lightmes": {
        "label": "LightMes",
        "ping": "/api/ping",
        "orders": "/api/workorders",
        "archive": "/api/workorders/{remoteId}/archive",
    },
    "custom": {
        "label": "自定义 / 其他 MES",
        "ping": "/api/ping",
        "orders": "/api/workorders",
        "archive": "/api/workorders/{remoteId}/archive",
    },
}

PATH_KEYS = ("ping", "orders", "archive")
MAX_KEY_LEN = 256


class MesConfigError(RuntimeError):
    """配置不合法；路由层翻译成 HTTP 400。"""


def mask_key(text: str) -> str:
    """密钥掩码：短密钥全遮，长密钥留头 2 尾 4，便于核对是哪一把。"""
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return "%s****%s" % (text[:2], text[-4:])


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class MesConfig:
    """一份 MES 连接配置；enabled=False 时所有网络动作都被拒绝。"""

    enabled: bool = False
    vendor: str = "cenkor"
    base_url: str = ""
    api_key: str = ""
    timeout: float = 8.0
    retries: int = 2
    auto_push: bool = False
    include_files: bool = True
    paths: Dict[str, str] = field(default_factory=dict)

    def to_dict(self, public: bool = True) -> Dict[str, Any]:
        """序列化：统一输出 **camelCase**（与其它模块一致）。

        注意不能用 `asdict()`——它输出 snake_case，读回来时 autoPush /
        includeFiles / baseUrl 全部落空，配置就变成"存了等于没存"。
        """
        data: Dict[str, Any] = {
            "enabled": bool(self.enabled),
            "vendor": self.vendor,
            "baseUrl": self.base_url,
            "timeout": float(self.timeout),
            "retries": int(self.retries),
            "autoPush": bool(self.auto_push),
            "includeFiles": bool(self.include_files),
            "paths": dict(self.paths),
        }
        if public:
            # 对外永远不给明文：只给掩码与"是否已设置"
            data["apiKey"] = ""
            data["apiKeyMask"] = mask_key(self.api_key)
            data["hasApiKey"] = bool(self.api_key)
        else:
            data["apiKey"] = self.api_key
        return data

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "MesConfig":
        """容错读取：坏字段退回默认值，不让一条脏配置卡死启动。"""
        data = raw if isinstance(raw, dict) else {}

        def pick(camel: str, snake: str, default: Any) -> Any:
            value = data.get(camel)
            if value is None:
                value = data.get(snake)
            return default if value is None else value

        vendor = str(pick("vendor", "vendor", "cenkor") or "cenkor").strip().lower()
        if vendor not in VENDORS:
            vendor = "cenkor"

        raw_paths = pick("paths", "paths", {})
        paths: Dict[str, str] = {}
        if isinstance(raw_paths, dict):
            for name in PATH_KEYS:
                value = str(raw_paths.get(name) or "").strip()
                if value:
                    paths[name] = value

        return cls(
            enabled=bool(pick("enabled", "enabled", False)),
            vendor=vendor,
            base_url=str(pick("baseUrl", "base_url", "") or "").strip(),
            api_key=str(pick("apiKey", "api_key", "") or ""),
            timeout=max(0.5, min(60.0, _to_float(pick("timeout", "timeout", 8.0), 8.0))),
            retries=max(0, min(5, _to_int(pick("retries", "retries", 2), 2))),
            auto_push=bool(pick("autoPush", "auto_push", False)),
            include_files=bool(pick("includeFiles", "include_files", True)),
            paths=paths,
        )

    def vendor_info(self) -> Dict[str, str]:
        return VENDORS.get(self.vendor, VENDORS["cenkor"])

    def path(self, name: str) -> str:
        """取路径：自定义配置优先，其次厂商默认。"""
        return self.paths.get(name) or self.vendor_info().get(name) or ""


def build(payload: Any, current: Optional[MesConfig] = None) -> MesConfig:
    """校验并生成配置；`apiKey` 留空且已有配置时沿用旧密钥。"""
    if not isinstance(payload, dict):
        raise MesConfigError("配置必须是对象")
    base = current or MesConfig()

    vendor = str(payload.get("vendor") or base.vendor or "cenkor").strip().lower()
    if vendor not in VENDORS:
        raise MesConfigError("vendor 必须是 %s 之一" % "/".join(sorted(VENDORS)))

    base_url = str(payload.get("baseUrl", payload.get("base_url", base.base_url)) or "")
    base_url = base_url.strip().rstrip("/")
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MesConfigError("MES 地址必须是 http(s)://主机[:端口][/前缀] 形式")

    enabled = bool(payload.get("enabled", base.enabled))
    if enabled and not base_url:
        raise MesConfigError("启用 MES 对接前必须填写服务地址")

    timeout = _to_float(payload.get("timeout", base.timeout), base.timeout)
    if not (0.5 <= timeout <= 60.0):
        raise MesConfigError("超时时间必须在 0.5~60 秒之间")

    retries = _to_int(payload.get("retries", base.retries), base.retries)
    if not (0 <= retries <= 5):
        raise MesConfigError("重试次数必须在 0~5 之间")

    # 密钥：显式传空串/不传都表示"沿用已保存的那一把"，
    # 前端拿到的是掩码，不可能把掩码当新密钥写回来。
    raw_key = payload.get("apiKey", payload.get("api_key"))
    api_key = str(raw_key).strip() if raw_key is not None else ""
    if not api_key:
        api_key = base.api_key
    if len(api_key) > MAX_KEY_LEN:
        raise MesConfigError("密钥过长（最多 %d 字符）" % MAX_KEY_LEN)

    paths = dict(base.paths)
    raw_paths = payload.get("paths")
    if isinstance(raw_paths, dict):
        for name in PATH_KEYS:
            if name not in raw_paths:
                continue
            value = str(raw_paths.get(name) or "").strip()
            if value:
                paths[name] = value
            else:
                paths.pop(name, None)

    return MesConfig(
        enabled=enabled,
        vendor=vendor,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        retries=retries,
        auto_push=bool(payload.get("autoPush", payload.get("auto_push", base.auto_push))),
        include_files=bool(payload.get("includeFiles", payload.get("include_files",
                                                               base.include_files))),
        paths=paths,
    )


def load() -> MesConfig:
    """读配置；坏 JSON 直接退回默认（未启用），离线可用优先。"""
    try:
        with db.get_conn() as conn:
            raw = db.get_setting(conn, SETTING_KEY)
    except Exception:  # 库还没建好等极端情况：MES 是可选件，不能拖垮调用方
        logger.warning("MES 配置读取失败，已退回默认（未启用）", exc_info=True)
        return MesConfig()
    if not raw:
        return MesConfig()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("MES 配置 JSON 解析失败，已退回默认（未启用）")
        return MesConfig()
    return MesConfig.from_dict(payload)


def save(payload: Any) -> MesConfig:
    """保存配置；密钥留空 = 不改。"""
    current = load()
    config = build(payload, current)
    with db.get_conn() as conn:
        db.set_setting(
            conn, SETTING_KEY, json.dumps(config.to_dict(public=False), ensure_ascii=False))
    logger.info("MES 配置已保存: enabled=%s vendor=%s url=%s",
                config.enabled, config.vendor, config.base_url)
    return config


def public_payload(config: MesConfig) -> Dict[str, Any]:
    """给前端的形状：camelCase + 掩码 + 厂商清单与默认路径。"""
    data = config.to_dict(public=True)
    data["vendors"] = [{"vendor": key, "label": item["label"]}
                       for key, item in VENDORS.items()]
    data["defaults"] = {
        key: {name: item.get(name, "") for name in PATH_KEYS}
        for key, item in VENDORS.items()
    }
    return data
