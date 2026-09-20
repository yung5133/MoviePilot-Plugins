"""移动云盘自描述驱动规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec
from .client import Yun139Client
from .constants import PROVIDER_KEY, PROVIDER_NAME
from .provider import Yun139Drive, create_yun139_provider


class Yun139DriverDefinition(DriverDefinition):
    """移动云盘驱动规范。"""

    # 驱动键与显示名单点定义在 constants.py。
    id = PROVIDER_KEY
    name = PROVIDER_NAME
    icon = "mdi-cellphone-link"
    order = 70

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="yun139",
                title="移动云盘账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="yun139_account_info",
                        type="account",
                        account_key=f"drive:{PROVIDER_KEY}",
                        cols=12,
                    ),
                    FieldSpec(
                        key="yun139_authorization",
                        label="Authorization",
                        type="password",
                        cols=12,
                        hint="抓包 yun.139.com 请求头中的 Authorization "
                             "（Basic base64(手机号:secret)），"
                             "手机号可自动解析，也可单独填写。",
                    ),
                    FieldSpec(
                        key="yun139_phone",
                        label="手机号",
                        type="text",
                        cols=6,
                        placeholder="留空则从 Authorization 自动解析",
                        hint="用于分享转存接口的账号标识。",
                    ),
                    FieldSpec(
                        key="yun139_cookie",
                        label="登录 Cookie",
                        type="password",
                        cols=6,
                        hint="可选，无 Authorization 时回退使用浏览器 Cookie；"
                             "Cookie 中若含 ud_id，会自动用于查询网盘容量。",
                    ),
                    FieldSpec(
                        key="yun139_user_domain_id",
                        label="用户域 ID（ud_id）",
                        type="text",
                        cols=6,
                        placeholder="留空则尝试从登录 Cookie 提取",
                        hint="浏览器 Cookie 中的 ud_id，纯数字 ID，用于查询网盘容量；"
                             "留空时容量栏显示为空，不影响列目录与转存。",
                    ),
                    FieldSpec(
                        key="yun139_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        drive_provider=PROVIDER_KEY,
                        hint="移动云盘订阅任务先保存到此路径，之后按规则整理。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="yun139_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        drive_provider=PROVIDER_KEY,
                        hint="最终媒体从此目录开始按规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="yun139",
                title="请求超时",
                icon="mdi-timer-cog-outline",
                hint="作用于移动云盘 HTTP 请求，范围 10-300 秒。",
                fields=[
                    FieldSpec(
                        key="yun139_request_timeout",
                        label="请求超时（秒）",
                        type="number",
                        min=10,
                        max=300,
                        cols=6,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        authorization = str(
            config.get("_yun139_authorization")
            or config.get("yun139_authorization") or ""
        ).strip()
        cookie = str(
            config.get("_yun139_cookie") or config.get("yun139_cookie") or ""
        ).strip()
        phone = str(
            config.get("_yun139_phone") or config.get("yun139_phone") or ""
        ).strip()
        # 容量查询所需的用户域 ID；留空时客户端会再尝试从 Cookie 的 ud_id 提取。
        user_domain_id = str(
            config.get("_yun139_user_domain_id")
            or config.get("yun139_user_domain_id") or ""
        ).strip()
        if not authorization and not cookie:
            return None
        timeout = float(
            config.get("_yun139_request_timeout")
            or config.get("yun139_request_timeout") or 30
        )
        return Yun139Client(
            authorization=authorization,
            cookie=cookie,
            phone=phone,
            timeout=timeout,
            user_domain_id=user_domain_id,
        )

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any],
            context: Optional[Any] = None,
    ) -> Any:
        if not client:
            return None
        return create_yun139_provider(Yun139Drive(client=client))
