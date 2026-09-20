"""移动云盘目录读取与文件查询。"""

from __future__ import annotations

from typing import Optional

from app.core.cache import TTLCache

from ..common import (
    CloudDriveFileServiceBase,
    create_directory_cache,
    safe_int,
)
from ...core.cloud import CloudFile


class Yun139FileService(CloudDriveFileServiceBase):
    provider_name = "移动云盘"
    provider_key = "yun139"
    root_directory_id = "0"

    def __init__(self, client):
        self.client = client
        self._directory_cache: TTLCache = create_directory_cache("yun139", client)

    @staticmethod
    def _to_cloud_file(item: dict) -> CloudFile:
        file_id = str(item.get("fileId") or item.get("id") or "")
        name = str(item.get("name") or item.get("fileName") or "")
        type_value = str(
            item.get("type") or item.get("contentType") or ""
        ).strip().lower()
        is_directory = type_value in {"folder", "dir", "catalog"} or bool(
            item.get("isDir")
        )
        return CloudFile(
            file_id,
            name,
            is_directory,
            size=0 if is_directory else safe_int(item.get("size")),
            sha1=str(item.get("sha1") or item.get("contentHash") or ""),
            native=item,
        )

    def _list(self, directory_id: str) -> list[CloudFile]:
        key = str(directory_id or self.root_directory_id)
        cached = self._directory_cache.get(key)
        if cached is not None:
            return list(cached)
        result: list[CloudFile] = []
        cursor = ""
        for _ in range(500):
            data = self.client.list_files(key, page_cursor=cursor)
            items = data.get("items")
            if not isinstance(items, list):
                break
            for item in items:
                if isinstance(item, dict):
                    file = self._to_cloud_file(item)
                    if file.id and file.name:
                        result.append(file)
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor or not items:
                break
        self._directory_cache.set(key, tuple(result))
        return result

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        data = self.client.create_folder(name, parent_id)
        self._invalidate_directory_cache()
        folder_id = str(data.get("fileId") or data.get("id") or "")
        if not folder_id:
            return None
        return CloudFile(folder_id, name, True, native=data)

    def _is_success(self, response) -> bool:
        # 客户端层已对接口错误抛出异常，返回内容即视为成功。
        return response is not False and response is not None
