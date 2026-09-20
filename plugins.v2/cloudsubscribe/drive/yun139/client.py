"""中国移动云盘（139 网盘）客户端。

鉴权使用抓包获得的 Authorization（Basic base64(...)），
请求需携带完整的 mcloud-* / X-Yun-* 头并按真机规则生成 mcloud-sign 签名。
接口路径与签名规则对齐 OpenList drivers/139（personal_new 变体）。

个人云接入点不写死：首次使用时调用 ``/user/route/qryRoutePolicy`` 按账号解析，
结果形如 ``https://personal-xxx.yun.139.com/hcy``；解析失败回退到
``constants.FALLBACK_PERSONAL_API``。``/hcy`` 是网关内层路由前缀，属于路径的一部分，
``/file/*`` 必须拼在其之后，否则网关无法路由，表现为「授权可识别手机号但列不出目录」。

网盘容量（存储配额）走的是**另一个主机另一个接口**：``POST {USER_API}/user/disk/quota/detail``，
以 ``userDomainId`` 定位用户，响应 ``data.diskSize`` / ``data.freeDiskSize`` 单位为 MB。
该字段取自浏览器 Cookie 的 ``ud_id``，也可由用户在配置中直接填写；两者都缺失时容量栏
留空，但**不影响列目录、下载与转存**——容量查询的任何失败都被收敛为「不展示」。

主机、根目录标识、路由策略、容量接口等常量统一在 ``constants.py`` 定义，本文件不重复字面量。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import requests
from app.log import logger

from . import constants
from ..common import DriveRateLimiter, format_size, safe_int

# 预先把配置里声明的 Cookie 键名小写化，避免每次解析都重建集合。
_USER_DOMAIN_ID_COOKIE_KEYS = frozenset(
    str(key).lower() for key in constants.USER_DOMAIN_ID_COOKIE_KEYS
)


class Yun139ApiError(RuntimeError):
    pass


def _encode_uri_component(value: str) -> str:
    """复刻 JS encodeURIComponent：Go url.QueryEscape 后再还原 !'()* 并将 + 转为 %20。"""
    encoded = quote(value, safe="")
    for escaped, plain in (
        ("%21", "!"), ("%27", "'"), ("%28", "("), ("%29", ")"), ("%2A", "*"),
    ):
        encoded = encoded.replace(escaped, plain)
    return encoded


class Yun139Client:
    MCLOUD_VERSION = "7.14.0"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
            self, authorization: str = "", cookie: str = "",
            phone: str = "", timeout: int = 30, user_domain_id: str = "",
    ):
        self.authorization = self._strip_basic_prefix(authorization)
        self.cookie = str(cookie or "").strip()
        self.phone = str(phone or "").strip() or self._account_from_authorization(
            self.authorization
        )
        self.timeout = max(10, int(timeout or 30))
        # 容量查询所需的用户域 ID：配置显式填写优先，其次从 Cookie 的 ud_id 提取。
        self._user_domain_id = str(user_domain_id or "").strip()
        # 首次成功的 userDomainId；命中后优先复用，避免每轮都重新试探。
        self._quota_identity: str = ""
        # 容量查询失败提示只记一次，避免每次刷新账号卡片都刷一条 warning。
        self._quota_warned = False
        # 个人云接入点：首次请求时按账号解析路由策略，之后复用。
        self._personal_api: Optional[str] = None
        self.rate_limiter = DriveRateLimiter.shared(
            constants.PROVIDER_KEY,
            self.authorization or self.phone or self.cookie,
            min_interval=0.5,
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
        })
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie

    def close(self) -> None:
        self.session.close()

    # ------------------------------------------------------------------ #
    # 鉴权辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_basic_prefix(value: str) -> str:
        value = str(value or "").strip()
        if value.lower().startswith("basic "):
            value = value[6:].strip()
        return value

    @staticmethod
    def _account_from_authorization(authorization_b64: str) -> str:
        """解码 base64 后结构为 ``xxx:账号:token``，账号即手机号（第 2 段）。"""
        if not authorization_b64:
            return ""
        try:
            decoded = base64.b64decode(authorization_b64).decode("utf-8", "ignore")
        except Exception:
            return ""
        parts = decoded.split(":")
        if len(parts) < 3:
            return ""
        account = parts[1].strip()
        return account if re.fullmatch(r"\d{6,15}", account) else ""

    @staticmethod
    def parse_user_domain_id(cookie: str) -> str:
        """从浏览器 Cookie 串中取出 ``ud_id``，取不到时返回空串。

        兼容实际粘贴中的多种形态：``a=1; ud_id=123; b=2``、以换行分隔、
        键或值被引号包裹、带 ``Cookie:`` 前缀、以及值被 URL 编码。
        """
        text = str(cookie or "")
        if not text:
            return ""
        # 分号与换行都可能是分隔符；同时剥掉可能残留的 "Cookie:" 前缀。
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1]
        for chunk in re.split(r"[;\n\r]+", text):
            key, separator, value = chunk.partition("=")
            if not separator:
                continue
            key = key.strip().strip("\"'")
            if key.lower() not in _USER_DOMAIN_ID_COOKIE_KEYS:
                continue
            value = unquote(value.strip().strip("\"'"))
            if value:
                return value
        return ""

    def resolve_user_domain_id(self) -> str:
        """按优先级解析容量查询所需的 userDomainId：配置项 > Cookie 的 ud_id。"""
        for value in (self._user_domain_id, self.parse_user_domain_id(self.cookie)):
            value = str(value or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _mcloud_sign(content: str) -> str:
        """按真机规则生成 mcloud-sign 头值：``时间,随机串,签名``。

        签名 = md5( base64( 排序后的 encodeURIComponent(body) ) ) 拼接
        md5( "时间:随机串" ) 再取 md5 并大写。
        """
        ts = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        sorted_body = "".join(sorted(_encode_uri_component(content)))
        encoded = base64.b64encode(sorted_body.encode("utf-8")).decode("utf-8")
        first = hashlib.md5(encoded.encode("utf-8")).hexdigest()
        second = hashlib.md5(f"{ts}:{rand}".encode("utf-8")).hexdigest()
        sign = hashlib.md5((first + second).encode("utf-8")).hexdigest().upper()
        return f"{ts},{rand},{sign}"

    def _auth_header(self) -> str:
        return f"Basic {self.authorization}" if self.authorization else ""

    def _common_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": self._auth_header(),
            "CMS-DEVICE": "default",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-version": self.MCLOUD_VERSION,
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
        }

    def _personal_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": self._auth_header(),
            "Caller": "web",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Version": self.MCLOUD_VERSION,
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": (
                "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||"
            ),
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        }
        return headers

    def _route_headers(self) -> Dict[str, str]:
        """路由策略查询专用请求头（对齐 OpenList ``requestRoute``）。

        与个人云头部有两处必需差异：

        1. 改用 OpenList 实测可用的这一组头，不携带 x-yun-* 系列；
        2. 必须携带 ``Inner-Hcy-Router-Https: 1``。缺此头时网关只下发外层地址，
           拿到后拼接 ``/file/*`` 仍会路由失败。
        """
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": self._auth_header(),
            "CMS-DEVICE": "default",
            "mcloud-channel": "1000101",
            "mcloud-client": "10701",
            "mcloud-version": self.MCLOUD_VERSION,
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            constants.ROUTE_POLICY_INNER_HCY_HEADER: "1",
        }

    # ------------------------------------------------------------------ #
    # HTTP 底座
    # ------------------------------------------------------------------ #

    @staticmethod
    def _response_data(response: requests.Response) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise Yun139ApiError(
                f"移动云盘鉴权失败（HTTP {status}），凭据可能已过期或请求头不完整"
            )
        try:
            data = response.json()
        except ValueError:
            if status >= 400:
                raise Yun139ApiError(f"移动云盘请求失败（HTTP {status}）")
            raise Yun139ApiError("移动云盘返回了无法识别的响应")
        if not isinstance(data, dict):
            raise Yun139ApiError("移动云盘返回了无效响应")
        if data.get("success") is False:
            raise Yun139ApiError(
                str(data.get("message") or data.get("msg") or "移动云盘接口返回失败")
            )
        code = str(
            data.get("code") or data.get("resultCode") or data.get("resCode") or ""
        )
        if code and code.upper() not in {"0", "CS0000", "0000", "SUCCESS"}:
            message = (
                data.get("message") or data.get("msg") or data.get("error")
                or str(data)[:200]
            )
            raise Yun139ApiError(f"移动云盘接口错误 {code}：{message}")
        return data

    @staticmethod
    def _unwrap(data: dict) -> dict:
        for key in ("data", "respData", "respDataInfo"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data

    def _post(
            self, base: str, path: str, body: Optional[Dict[str, Any]],
            headers: Optional[Dict[str, str]] = None,
    ) -> dict:
        payload = dict(body or {})
        # 签名基于紧凑 JSON 原文，必须与请求体逐字节一致，不能用 json= 参数。
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        # 复制一份，避免调用方传入的头部字典被 mcloud-sign 就地污染、跨请求复用。
        final_headers = dict(
            headers if headers is not None else self._common_headers()
        )
        final_headers["mcloud-sign"] = self._mcloud_sign(content)
        response = self.rate_limiter.call(
            self.session.post,
            base + path,
            data=content.encode("utf-8"),
            headers=final_headers,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        return self._response_data(response)

    def personal_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(
            self.personal_api, path, body, headers=self._personal_headers()
        )

    def user_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(constants.USER_API, path, body)

    def share_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(constants.SHARE_API, path, body)

    # ------------------------------------------------------------------ #
    # 个人云接入点解析
    # ------------------------------------------------------------------ #

    @property
    def personal_api(self) -> str:
        """当前账号的个人云基址。首次访问时解析并缓存。"""
        if self._personal_api is None:
            self._personal_api = self._resolve_personal_api()
        return self._personal_api

    def _resolve_personal_api(self) -> str:
        """解析个人云接入点；任何异常都回退到内置地址，不向上抛出。

        回退是刻意设计：路由策略是新增依赖，接口变更、网络抖动或头部校验失败
        都不应把原本可用的链路变成不可用。回退值与 v1.5.6 的硬编码值一致。
        """
        try:
            host = self.query_route_host(constants.ROUTE_POLICY_MOD_PERSONAL)
        except Exception as error:
            logger.warning(f"移动云盘路由策略解析失败，回退内置接入点：{error}")
            return constants.FALLBACK_PERSONAL_API
        if not host:
            logger.warning("移动云盘路由策略未下发 personal 接入点，回退内置接入点")
            return constants.FALLBACK_PERSONAL_API
        host = host.rstrip("/")
        if "/hcy" not in host:
            # 正常应下发内层 /hcy 路由地址；不含时仍按返回值使用，仅记录以便排查。
            logger.warning(f"移动云盘路由策略返回的接入点不含 /hcy：{host}")
        logger.info(f"移动云盘个人云接入点：{host}")
        return host

    def query_route_host(
            self, mod_name: str = constants.ROUTE_POLICY_MOD_PERSONAL,
    ) -> str:
        """查询路由策略，返回指定模块的 https 基址；未命中时返回空串。"""
        data = self._post(
            constants.USER_API,
            constants.ROUTE_POLICY_PATH,
            {
                "userInfo": {
                    "userType": 1,
                    "accountType": 1,
                    "accountName": self.phone or self.authorization,
                },
                "modAddrType": 1,
            },
            headers=self._route_headers(),
        )
        policy_list = self._unwrap(data).get("routePolicyList") or []
        if not isinstance(policy_list, list):
            return ""
        for item in policy_list:
            if not isinstance(item, dict):
                continue
            if str(item.get("modName") or "") == mod_name:
                return str(item.get("httpsUrl") or "").strip()
        return ""

    @classmethod
    def normalize_catalog_id(cls, directory_id: Any) -> str:
        """把空值及历史根标识统一归一为 constants.ROOT_CATALOG_ID。"""
        value = str(directory_id if directory_id is not None else "").strip()
        if not value or value in constants.LEGACY_ROOT_CATALOG_IDS:
            return constants.ROOT_CATALOG_ID
        return value

    # ------------------------------------------------------------------ #
    # 账号信息
    # ------------------------------------------------------------------ #

    def check_login(self) -> bool:
        try:
            self.list_files(constants.ROOT_CATALOG_ID)
            return True
        except Exception as error:
            logger.debug(f"移动云盘登录校验失败：{error}")
            return False

    def get_account_info(self) -> dict:
        try:
            self.list_files(constants.ROOT_CATALOG_ID)
        except Exception as error:
            return {"connected": False, "error": str(error)}
        name = (
            f"{self.phone[:3]}****{self.phone[-4:]}"
            if len(self.phone) >= 7 else self.phone
        ) or "移动云盘用户"
        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": "",
                "membership_supported": False,
                "is_vip": False,
                "is_forever_vip": False,
                "vip_expire_date": "",
            },
            "storage": self.get_storage_info(),
        }

    # ------------------------------------------------------------------ #
    # 容量（存储配额）
    # ------------------------------------------------------------------ #

    def get_storage_info(self) -> Dict[str, str]:
        """配置页展示用的容量字符串；任何失败都返回空串。

        刻意吞掉所有异常：容量属附加信息，查询失败不应把账号卡片从
        「已连接」降级为错误——那会把本来可用的列目录/转存链路一起赔进去。
        """
        try:
            quota = self.query_disk_quota()
        except Exception as error:
            logger.debug(f"移动云盘容量查询异常：{error}")
            quota = None
        if not quota:
            return {"total": "", "used": "", "remaining": ""}
        return {
            "total": format_size(quota["total"]),
            "used": format_size(quota["used"]),
            "remaining": format_size(quota["remaining"]),
        }

    def query_disk_quota(self) -> Optional[Dict[str, int]]:
        """查询容量，成功时返回 {total, used, remaining}（字节），失败返回 None。"""
        candidates = self._quota_identity_candidates()
        if not candidates:
            self._warn_quota_once(
                "移动云盘容量未查询：无法确定 userDomainId"
                "（配置项、Cookie 的 ud_id、Authorization 中的手机号均不可用）"
            )
            return None
        for identity in candidates:
            try:
                data = self.user_request(
                    constants.USER_DISK_QUOTA_PATH,
                    {"userDomainId": identity},
                )
            except Exception as error:
                logger.debug(f"移动云盘容量查询未命中：{error}")
                continue
            quota = self._parse_disk_quota(data)
            if quota:
                if identity != self._quota_identity:
                    self._quota_identity = identity
                    logger.info("移动云盘容量查询已可用")
                return quota
        self._warn_quota_once(
            "移动云盘容量查询失败：候选标识均未被服务端接受（常见于凭据已过期）；"
            "可重新抓包，或在插件配置中填写「用户域 ID（ud_id）」/ 含 ud_id 的登录 Cookie"
        )
        return None

    def _quota_identity_candidates(self) -> List[str]:
        """容量查询的候选标识，按命中概率排序并去重。

        手机号排在最后仅作兜底：**已实测确认服务端接受手机号充当 userDomainId**
        （返回 success=true 且 diskSize>0），因此大多数情况下用户无需额外配置。
        """
        candidates: List[str] = []
        for value in (
            self._quota_identity, self.resolve_user_domain_id(), self.phone,
        ):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    @staticmethod
    def _parse_disk_quota(data: dict) -> Optional[Dict[str, int]]:
        """解析容量响应；diskSize 缺失或非正时视为失败。"""
        payload = Yun139Client._unwrap(data)
        if not isinstance(payload, dict):
            return None
        total_mb = safe_int(payload.get("diskSize"))
        free_mb = safe_int(payload.get("freeDiskSize"))
        if total_mb <= 0:
            return None
        # 防御上游把 freeDiskSize 报成大于总量或负数。
        free_mb = max(0, min(free_mb, total_mb))
        unit = constants.QUOTA_UNIT_BYTES
        return {
            "total": total_mb * unit,
            "used": (total_mb - free_mb) * unit,
            "remaining": free_mb * unit,
        }

    def _warn_quota_once(self, message: str) -> None:
        """容量不可用只提示一次，避免每次刷新账号卡片都刷一条 warning。"""
        if self._quota_warned:
            return
        self._quota_warned = True
        logger.warning(message)

    # ------------------------------------------------------------------ #
    # 个人云盘文件接口（personal_new：/file/*）
    # ------------------------------------------------------------------ #

    def list_files(
            self, parent_file_id: str = "", page_cursor: str = "",
            page_size: int = 100,
    ) -> dict:
        return self._unwrap(
            self.personal_request(
                "/file/list",
                {
                    "imageThumbnailStyleList": ["Small", "Large"],
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {
                        "pageCursor": str(page_cursor or ""),
                        "pageSize": max(1, min(int(page_size or 100), 100)),
                    },
                    "parentFileId": self.normalize_catalog_id(parent_file_id),
                },
            )
        )

    def create_folder(self, name: str, parent_file_id: str = "") -> dict:
        return self._unwrap(
            self.personal_request(
                "/file/create",
                {
                    "parentFileId": self.normalize_catalog_id(parent_file_id),
                    "name": name,
                    "description": "",
                    "type": "folder",
                    "fileRenameMode": "force_rename",
                },
            )
        )

    def rename_file(self, file_id: str, new_name: str) -> dict:
        return self._unwrap(
            self.personal_request(
                "/file/update",
                {"fileId": str(file_id), "name": new_name, "description": ""},
            )
        )

    def move_files(self, file_ids: List[str], to_directory_id: str) -> dict:
        return self._unwrap(
            self.personal_request(
                "/file/batchMove",
                {
                    "fileIds": [str(value) for value in file_ids],
                    "toParentFileId": self.normalize_catalog_id(to_directory_id),
                },
            )
        )

    def delete_files(self, file_ids: List[str]) -> dict:
        return self._unwrap(
            self.personal_request(
                "/recyclebin/batchTrash",
                {"fileIds": [str(value) for value in file_ids]},
            )
        )

    def wait_task(self, task_id: str, timeout: float = 0.0) -> bool:
        # personal_new 的批量接口为同步返回，无异步任务需要等待。
        return True
