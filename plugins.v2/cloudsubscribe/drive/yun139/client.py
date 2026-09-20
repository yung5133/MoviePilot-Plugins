"""中国移动云盘（139 网盘）客户端。

鉴权使用抓包获得的 Authorization（Basic base64(手机号:secret)），
兼容浏览器 Cookie；请求需携带 mcloud-sign 签名头。
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
from app.utils.string import StringUtils

from ..common import DriveRateLimiter, safe_int


class Yun139ApiError(RuntimeError):
    pass


class Yun139Client:
    USER_API = "https://user-njs.yun.139.com"
    PERSONAL_API = "https://personal-kd-njs.yun.139.com"
    SHARE_API = "https://share-kd-njs.yun.139.com"
    ROOT_CATALOG_ID = "0"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(
            self, authorization: str = "", cookie: str = "",
            phone: str = "", timeout: int = 30,
    ):
        self.authorization = self._normalize_authorization(authorization)
        self.cookie = str(cookie or "").strip()
        self.phone = str(phone or "").strip() or self._phone_from_authorization(
            self.authorization
        )
        self.timeout = max(10, int(timeout or 30))
        self.user_domain_id = ""
        self.nick_name = ""
        self.rate_limiter = DriveRateLimiter.shared(
            "yun139",
            self.authorization or self.phone or self.cookie,
            min_interval=0.5,
        )
        self.session = requests.Session()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": self.USER_AGENT,
            "Referer": "https://yun.139.com/",
            "Origin": "https://yun.139.com",
        }
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.cookie:
            headers["Cookie"] = self.cookie
        self.session.headers.update(headers)

    def close(self) -> None:
        self.session.close()

    # ------------------------------------------------------------------ #
    # 鉴权辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_authorization(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if value.lower().startswith(("basic ", "bearer ")):
            return value
        return f"Basic {value}"

    @staticmethod
    def _phone_from_authorization(authorization: str) -> str:
        """Basic 凭据为 base64(手机号:secret)，可反解出手机号。"""
        match = re.match(r"(?i)^basic\s+([A-Za-z0-9+/=]+)$", authorization or "")
        if not match:
            return ""
        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8", "ignore")
        except Exception:
            return ""
        account = decoded.split(":", 1)[0].strip()
        return account if re.fullmatch(r"\d{6,15}", account) else ""

    @staticmethod
    def _mcloud_sign(body: Dict[str, Any]) -> str:
        """按 mcloud 规则生成 mcloud-sign 头：对紧凑 JSON 序列化后排序签名。"""
        datetime_cst = (
            datetime.now(timezone.utc) + timedelta(hours=8)
        ).strftime("%Y-%m-%d %H:%M:%S")
        random_str = "".join(
            random.choices(string.ascii_letters + string.digits, k=16)
        )
        raw = quote(
            json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
        )
        raw = "".join(sorted(raw))
        encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        first = hashlib.md5(encoded.encode("utf-8")).hexdigest()
        second = hashlib.md5(
            f"{datetime_cst}:{random_str}".encode("utf-8")
        ).hexdigest()
        sign = hashlib.md5((first + second).encode("utf-8")).hexdigest().upper()
        return f"{datetime_cst},{random_str},{sign}"

    # ------------------------------------------------------------------ #
    # HTTP 底座
    # ------------------------------------------------------------------ #

    @staticmethod
    def _response_data(response: requests.Response) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise Yun139ApiError(f"移动云盘鉴权失败（HTTP {status}），凭据可能已过期")
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
        resp_code = data.get("respCode")
        if isinstance(resp_code, dict):
            return_code = str(resp_code.get("returnCode") or "")
            if return_code and return_code.upper() not in {"0", "SUCCESS"}:
                raise Yun139ApiError(
                    f"移动云盘接口错误 {return_code}："
                    f"{resp_code.get('returnMsg') or return_code}"
                )
        return data

    def _post(self, base: str, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        payload = dict(body or {})
        # 签名基于紧凑 JSON 原文，必须与请求体逐字节一致，不能用 json= 参数。
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        response = self.rate_limiter.call(
            self.session.post,
            base + path,
            data=content.encode("utf-8"),
            headers={"mcloud-sign": self._mcloud_sign(payload)},
            timeout=self.timeout,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        return self._response_data(response)

    def user_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.USER_API, path, body)

    def personal_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.PERSONAL_API, path, body)

    def share_request(self, path: str, body: Optional[Dict[str, Any]] = None) -> dict:
        return self._post(self.SHARE_API, path, body)

    @staticmethod
    def _unwrap(data: dict) -> dict:
        """兼容 {"data": {...}} 与 {"respData": {...}} 两类响应包装。"""
        if not isinstance(data, dict):
            return {}
        for key in ("data", "respData", "respDataInfo"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return data

    @classmethod
    def normalize_catalog_id(cls, directory_id: Any) -> str:
        value = str(directory_id if directory_id is not None else "").strip()
        return value if value and value not in {"/", "-11"} else cls.ROOT_CATALOG_ID

    # ------------------------------------------------------------------ #
    # 账号信息
    # ------------------------------------------------------------------ #

    def ensure_user(self) -> str:
        if self.user_domain_id:
            return self.user_domain_id
        data = self._unwrap(
            self.user_request("/user/getUser", {"account": self.phone})
        )
        self.user_domain_id = str(
            data.get("userDomainId") or data.get("userDomain") or ""
        )
        self.nick_name = str(data.get("nickName") or data.get("name") or "")
        self.phone = self.phone or str(data.get("account") or "")
        if not self.user_domain_id:
            raise Yun139ApiError("移动云盘未返回用户标识，凭据可能无效")
        return self.user_domain_id

    def check_login(self) -> bool:
        try:
            return bool(self.ensure_user())
        except Exception as error:
            logger.debug(f"移动云盘登录校验失败：{error}")
            return False

    def get_account_info(self) -> dict:
        try:
            user_domain_id = self.ensure_user()
            disk: Dict[str, Any] = {}
            try:
                disk = self._unwrap(
                    self.user_request(
                        "/user/disk/getPersonalDiskInfo",
                        {"userDomainId": user_domain_id},
                    )
                )
            except Yun139ApiError as error:
                logger.debug(f"移动云盘容量信息获取失败：{error}")
            # 容量字段按 MB 返回，兼容个别以字节返回的字段命名。
            total = max(
                safe_int(disk.get("diskSize")) * 1024 * 1024,
                safe_int(disk.get("totalSize")),
            )
            free = max(
                safe_int(disk.get("freeDiskSize")) * 1024 * 1024,
                safe_int(disk.get("freeSize")),
            )
            used = max(0, total - free) if total else 0
            name = self.nick_name or (
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
                "storage": {
                    "total": StringUtils.str_filesize(total),
                    "used": StringUtils.str_filesize(used),
                    "remaining": StringUtils.str_filesize(free),
                },
            }
        except Exception as error:
            return {"connected": False, "error": str(error)}

    # ------------------------------------------------------------------ #
    # 文件接口（/hcy/*）
    # ------------------------------------------------------------------ #

    def list_files(
            self, parent_file_id: str = "", page_cursor: str = "",
            page_size: int = 100,
    ) -> dict:
        return self._unwrap(
            self.personal_request(
                "/hcy/file/list",
                {
                    "pageInfo": {
                        "pageSize": max(1, min(int(page_size or 100), 200)),
                        "pageCursor": str(page_cursor or ""),
                    },
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "parentFileId": self.normalize_catalog_id(parent_file_id),
                },
            )
        )

    def create_folder(self, name: str, parent_file_id: str = "") -> dict:
        return self._unwrap(
            self.personal_request(
                "/hcy/file/create",
                {
                    "parentFileId": self.normalize_catalog_id(parent_file_id),
                    "name": name,
                    "type": "folder",
                },
            )
        )

    def rename_file(self, file_id: str, new_name: str) -> dict:
        return self._unwrap(
            self.personal_request(
                "/hcy/file/update",
                {"fileId": str(file_id), "name": new_name},
            )
        )

    def move_files(self, file_ids: List[str], to_directory_id: str) -> dict:
        # 移动接口未见于公开实现，按 hcy 网关惯例推断；失败时上层有兜底日志。
        return self._unwrap(
            self.personal_request(
                "/hcy/file/move",
                {
                    "fileIds": [str(value) for value in file_ids],
                    "targetFileId": self.normalize_catalog_id(to_directory_id),
                },
            )
        )

    def delete_files(self, file_ids: List[str]) -> bool:
        data = self._unwrap(
            self.personal_request(
                "/hcy/recyclebin/batchTrash",
                {"fileIds": [str(value) for value in file_ids]},
            )
        )
        task_id = str(data.get("taskId") or data.get("taskID") or "")
        return self.wait_task(task_id) if task_id else True

    def get_task_status(self, task_id: str) -> dict:
        try:
            return self._unwrap(
                self.personal_request("/hcy/task/get", {"taskId": str(task_id)})
            )
        except Yun139ApiError as error:
            logger.debug(f"移动云盘任务状态查询失败：{error}")
            return {}

    def wait_task(self, task_id: str, timeout: float = 60.0) -> bool:
        """轮询异步任务；查询接口不可用时按已受理处理。"""
        if not task_id:
            return True
        deadline = time.monotonic() + max(5.0, float(timeout or 60))
        queried = False
        while time.monotonic() < deadline:
            data = self.get_task_status(task_id)
            if not data:
                return queried or True
            queried = True
            status = str(data.get("status") or data.get("taskStatus") or "").lower()
            if status in {"success", "finished", "2", "done"}:
                return True
            if status in {"failed", "error", "3"}:
                return False
            time.sleep(0.5)
        logger.debug(f"移动云盘任务 {task_id} 等待超时，按已提交处理")
        return True
