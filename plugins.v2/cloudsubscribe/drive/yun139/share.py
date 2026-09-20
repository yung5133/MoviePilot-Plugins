"""移动云盘公开分享读取与转存。"""

from __future__ import annotations

import re
from threading import RLock
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from app.log import logger

from ..common import iter_transfer_batches
from ...core.cloud import ShareLinkStatus

SHARE_LINK_PATTERNS = (
    r"yun\.139\.com/shareweb/#/w/i/([A-Za-z0-9]+)",
    r"caiyun\.139\.com/w/i/([A-Za-z0-9]+)",
    r"caiyun\.139\.com/m/i\?([A-Za-z0-9]+)",
    r"caiyun\.feixin\.10086\.cn/([A-Za-z0-9]+)",
)
PASSWD_QUERY_PATTERN = r"[?&](?:passwd|password|pwd)=([A-Za-z0-9]+)"
PASSWD_TEXT_PATTERN = r"(?:提取码|访问码|密码|passwd|pwd)\s*[：:=\s]\s*([A-Za-z0-9]{4,8})"

ROOT_CATALOG = "root"
MAX_BATCH = 100
MAX_DEPTH = 20


def _find_value(data: Any, key: str) -> Any:
    """深度优先查找响应中首个指定字段，兼容 richlife 接口的多层包装。"""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _find_value(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_value(value, key)
            if found is not None:
                return found
    return None


class Yun139ShareService:
    def __init__(self, client, files):
        self.client = client
        self.files = files
        # share_key -> {条目路径: 条目字典}，转存接口以路径为标识。
        self._share_entries: Dict[str, Dict[str, dict]] = {}
        self._lock = RLock()

    # ------------------------------------------------------------------ #
    # 链接解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, str]:
        value = unquote(str(share_url or "").strip())
        link_id = ""
        for pattern in SHARE_LINK_PATTERNS:
            match = re.search(pattern, value, re.I)
            if match:
                link_id = match.group(1)
                break
        if not link_id:
            return {}
        access_code = ""
        match = re.search(PASSWD_QUERY_PATTERN, value, re.I)
        if not match:
            match = re.search(PASSWD_TEXT_PATTERN, value, re.I)
        if match:
            access_code = match.group(1)
        return {"share_code": link_id, "access_code": access_code}

    @staticmethod
    def _share_key(parsed: Dict[str, str]) -> str:
        return f"{parsed['share_code']}|{parsed.get('access_code') or ''}"

    # ------------------------------------------------------------------ #
    # 分享目录接口
    # ------------------------------------------------------------------ #

    def _get_outlink_info(
            self, link_id: str, passwd: str, catalog_id: str = ROOT_CATALOG
    ) -> tuple[List[dict], List[dict]]:
        data = self.client.share_request(
            "/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6",
            {
                "getOutLinkInfoReq": {
                    "account": self.client.phone,
                    "linkID": link_id,
                    "passwd": passwd,
                    "pCaID": catalog_id,
                    "caSrt": 0,
                    "coSrt": 0,
                    "srtDr": 1,
                    "bNum": 1,
                    "eNum": 200,
                }
            },
        )
        folders = _find_value(data, "caLst") or []
        contents = _find_value(data, "coLst") or []
        return (
            [item for item in folders if isinstance(item, dict)],
            [item for item in contents if isinstance(item, dict)],
        )

    @staticmethod
    def _folder_entry(item: dict) -> Optional[dict]:
        catalog_id = str(item.get("catalogID") or item.get("id") or "")
        name = str(item.get("catalogName") or item.get("name") or "")
        path = str(item.get("path") or "") or catalog_id
        if not catalog_id or not name:
            return None
        return {
            "id": path, "name": name, "is_dir": True, "size": 0,
            "catalog_id": catalog_id,
        }

    @staticmethod
    def _file_entry(item: dict) -> Optional[dict]:
        content_id = str(item.get("contentID") or item.get("id") or "")
        name = str(item.get("contentName") or item.get("name") or "")
        path = str(item.get("path") or "") or content_id
        if not content_id or not name:
            return None
        try:
            size = int(item.get("coSize") or item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {"id": path, "name": name, "is_dir": False, "size": size}

    def _parsed_info(self, share_url: str) -> Dict[str, str]:
        parsed = self.extract_share_info(share_url)
        if not parsed:
            raise ValueError("无效的移动云盘分享链接")
        return parsed

    def _root_listing(
            self, parsed: Dict[str, str]
    ) -> tuple[List[dict], List[dict]]:
        folders, files = self._get_outlink_info(
            parsed["share_code"], parsed.get("access_code") or ""
        )
        entries = [
            entry
            for entry in (
                *(self._folder_entry(item) for item in folders),
                *(self._file_entry(item) for item in files),
            )
            if entry
        ]
        cache_key = self._share_key(parsed)
        with self._lock:
            cached = self._share_entries.setdefault(cache_key, {})
            cached.update({entry["id"]: entry for entry in entries})
        return folders, entries

    def _remember_all(self, parsed: Dict[str, str]) -> Dict[str, dict]:
        """递归遍历分享目录，缓存全部条目供转存使用。"""
        cache_key = self._share_key(parsed)
        with self._lock:
            cached = self._share_entries.get(cache_key)
        if cached and any(not entry["is_dir"] for entry in cached.values()):
            return cached
        entries: Dict[str, dict] = {}
        stack = [ROOT_CATALOG]
        depth: Dict[str, int] = {ROOT_CATALOG: 0}
        while stack:
            catalog_id = stack.pop()
            try:
                folders, files = self._get_outlink_info(
                    parsed["share_code"],
                    parsed.get("access_code") or "",
                    catalog_id,
                )
            except Exception as error:
                logger.debug(f"移动云盘分享目录读取失败：{catalog_id} - {error}")
                continue
            for raw in folders:
                entry = self._folder_entry(raw)
                if not entry:
                    continue
                entries[entry["id"]] = entry
                current_depth = depth.get(catalog_id, 0)
                child_id = entry["catalog_id"]
                if current_depth < MAX_DEPTH:
                    depth[child_id] = current_depth + 1
                    stack.append(child_id)
            for raw in files:
                entry = self._file_entry(raw)
                if entry:
                    entries[entry["id"]] = entry
        with self._lock:
            existing = self._share_entries.get(cache_key) or {}
            existing.update(entries)
            self._share_entries[cache_key] = existing
            return existing

    # ------------------------------------------------------------------ #
    # ShareTransferOperations 协议实现
    # ------------------------------------------------------------------ #

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            parsed = self._parsed_info(share_url)
            folders, entries = self._root_listing(parsed)
            status.is_valid = bool(entries)
            status.file_count = len(entries)
            status.share_info = {**parsed, "items": entries}
            if not status.is_valid:
                status.error_message = "移动云盘分享内容为空或已失效"
        except Exception as error:
            message = str(error)
            status.error_message = message or "移动云盘分享不可用"
            lowered = message.lower()
            status.is_expired = (
                "过期" in message or "失效" in message or "expired" in lowered
            )
            status.is_cancelled = "取消" in message or "cancel" in lowered
            status.is_deleted = "不存在" in message or "删除" in message
        return status

    def list_share_files(self, share_url: str, **kwargs) -> list:
        try:
            parsed = self._parsed_info(share_url)
            entries = self._remember_all(parsed)
            return [
                {
                    "id": entry["id"],
                    "name": entry["name"],
                    "is_dir": False,
                    "size": entry["size"],
                }
                for entry in entries.values()
                if not entry["is_dir"]
            ]
        except Exception as error:
            logger.warning(f"读取移动云盘分享文件失败：{error}")
            return []

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        parsed = self._parsed_info(share_url)
        cache_key = self._share_key(parsed)
        if parent_id:
            with self._lock:
                parent = (self._share_entries.get(cache_key) or {}).get(str(parent_id))
            catalog_id = (parent or {}).get("catalog_id") or str(parent_id)
        else:
            catalog_id = ROOT_CATALOG
        folders, files = self._get_outlink_info(
            parsed["share_code"], parsed.get("access_code") or "", catalog_id
        )
        result = []
        with self._lock:
            cached = self._share_entries.setdefault(cache_key, {})
            for raw in folders:
                entry = self._folder_entry(raw)
                if entry:
                    cached[entry["id"]] = entry
                    result.append({
                        "id": entry["id"], "name": entry["name"],
                        "is_dir": True, "size": 0,
                    })
            for raw in files:
                entry = self._file_entry(raw)
                if entry:
                    cached[entry["id"]] = entry
                    result.append({
                        "id": entry["id"], "name": entry["name"],
                        "is_dir": False, "size": entry["size"],
                    })
        return result

    # ------------------------------------------------------------------ #
    # 转存
    # ------------------------------------------------------------------ #

    def _resolve_target(self, save_path: str) -> Optional[str]:
        lookup = self.files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        return self.client.normalize_catalog_id(lookup.directory_id)

    def _transfer_entries(
            self, parsed: Dict[str, str], entries: List[dict],
            target_catalog_id: str,
    ) -> bool:
        content_paths = [e["id"] for e in entries if not e["is_dir"]]
        catalog_paths = [e["id"] for e in entries if e["is_dir"]]
        if not content_paths and not catalog_paths:
            return False
        link_id = parsed["share_code"]
        passwd = parsed.get("access_code") or ""
        data = self.client.share_request(
            "/yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask",
            {
                "createOuterLinkBatchOprTaskReq": {
                    "msisdn": self.client.phone,
                    "ownerAccount": "",
                    "taskType": 1,
                    "linkID": link_id,
                    "needPassword": bool(passwd),
                    "taskInfo": {
                        "linkID": link_id,
                        "needPassword": bool(passwd),
                        "contentInfoList": content_paths,
                        "catalogInfoList": catalog_paths,
                        "newCatalogID": target_catalog_id,
                    },
                }
            },
        )
        task_id = str(_find_value(data, "taskID") or _find_value(data, "taskId") or "")
        if not task_id:
            return False
        return self.client.wait_task(task_id)

    def _lookup_entries(
            self, parsed: Dict[str, str], file_ids: List[str]
    ) -> List[dict]:
        cache_key = self._share_key(parsed)
        with self._lock:
            cached = dict(self._share_entries.get(cache_key) or {})
        if not all(str(value) in cached for value in file_ids):
            cached = self._remember_all(parsed)
        entries = []
        for file_id in file_ids:
            file_id = str(file_id)
            entry = cached.get(file_id)
            if entry:
                entries.append(entry)
            else:
                # 未命中缓存时按文件路径直接提交（转存失败由任务结果兜底）。
                entries.append(
                    {"id": file_id, "name": "", "is_dir": False, "size": 0}
                )
        return entries

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        try:
            parsed = self._parsed_info(share_url)
            folders, entries = self._root_listing(parsed)
            if not entries:
                return False
            target = self._resolve_target(save_path)
            if not target:
                return False
            # 转存顶层条目即可保留分享内的目录结构。
            return self._transfer_entries(parsed, entries, target)
        except Exception as error:
            logger.warning(f"移动云盘分享转存失败：{error}")
            return False

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str = "", **kwargs,
    ) -> bool:
        try:
            parsed = self._parsed_info(share_url)
            target = self._resolve_target(save_path)
            if not target:
                return False
            entries = self._lookup_entries(parsed, [str(file_id)])
            success = self._transfer_entries(parsed, entries, target)
            if not success and target_name:
                if self.files.find_file(save_path, target_name):
                    return True
            return success
        except Exception as error:
            logger.warning(f"移动云盘分享单文件转存失败：{error}")
            return False

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs,
    ) -> tuple:
        normalized = [str(value) for value in file_ids if str(value or "")]
        if not normalized:
            return [], []
        succeeded, failed = [], []
        try:
            parsed = self._parsed_info(share_url)
            target = self._resolve_target(save_path)
            if not target:
                return [], normalized
        except Exception as error:
            logger.warning(f"移动云盘批量转存准备失败：{error}")
            return [], normalized
        for batch in iter_transfer_batches(
                normalized,
                kwargs.get("batch_size", 20),
                kwargs.get("batch_interval", 3),
                MAX_BATCH,
        ):
            try:
                entries = self._lookup_entries(parsed, batch)
                if self._transfer_entries(parsed, entries, target):
                    succeeded.extend(batch)
                else:
                    failed.extend(batch)
            except Exception as error:
                logger.warning(f"移动云盘批量转存异常：{error}")
                failed.extend(batch)
        return succeeded, failed
