"""中国移动云盘（139 网盘）客户端。

鉴权使用抓包获得的 Authorization（Basic base64(...)），
请求需携带完整的 mcloud-* / X-Yun-* 头并按真机规则生成 mcloud-sign 签名。
接口路径与签名规则对齐 OpenList drivers/139（personal_new 变体）。
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
from urllib.parse import quote

import requests
from app.log import logger

from ..common import DriveRateLimiter


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
    PERSONAL_API = "https://personal-kd-njs.yun.139.com"
    USER_API = "https://user-njs.yun.139.com"
    SHARE_API = "https://share-kd-njs.yun.139.com"
    ROOT_CATALOG_ID = "0"
    MCLOUD_VERSION = "7.14.0"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
            self, authorization: str = "", cookie: str = "",
            phone: str = "", timeout: int = 30,
    ):
        self.authorization = self._strip_basic_prefix(authorization)
        self.cookie = str(cookie or "").strip()
        self.phone = str(phone or "").strip() or self._account_from_authorization(
            self.authorization
        )
        self.timeout = max(10, int(timeout or 30))
        self.rate_limiter = DriveRateLimiter.shared(
            "yun139",
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
            personal: bool = False,
    ) -> dict:
        payload = dict(body or {})
        # 签名基于紧凑 JSON 原文，必须与请求体逐字节一致，不能用 json= 参数。
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._personal_headers() if personal else self._common_headers()
        headers["mcloud-sign"] = self._mcloud_sign(content)
        response = self.rate_limiter.call(
            self.session.post,
            base + path,
            data=content.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        return self._response_data(response)

    def personal_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.PERSONAL_API, path, body, personal=True)

    def user_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.USER_API, path, body)

    def share_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.SHARE_API, path, body)

    @classmethod
    def normalize_catalog_id(cls, directory_id: Any) -> str:
        value = str(directory_id if directory_id is not None else "").strip()
        return value if value and value not in {"/", "-11"} else cls.ROOT_CATALOG_ID

    # ------------------------------------------------------------------ #
    # 账号信息
    # ------------------------------------------------------------------ #

    def check_login(self) -> bool:
        try:
            self.list_files(self.ROOT_CATALOG_ID)
            return True
        except Exception as error:
            logger.debug(f"移动云盘登录校验失败：{error}")
            return False

    def get_account_info(self) -> dict:
        try:
            self.list_files(self.ROOT_CATALOG_ID)
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
            "storage": {"total": "", "used": "", "remaining": ""},
        }

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
