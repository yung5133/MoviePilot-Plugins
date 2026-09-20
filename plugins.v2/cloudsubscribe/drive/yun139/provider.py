"""移动云盘能力适配。"""

from dataclasses import dataclass

from .client import Yun139Client
from .files import Yun139FileService
from .share import Yun139ShareService
from ...core.cloud import CloudDriveCapability, CloudDrivePolicy, CloudDriveProvider


@dataclass
class Yun139Drive:
    client: Yun139Client

    def close(self):
        self.client.close()


def create_yun139_provider(drive: Yun139Drive) -> CloudDriveProvider:
    files = Yun139FileService(drive.client)
    share = Yun139ShareService(drive.client, files)
    return CloudDriveProvider(
        key="yun139",
        name="移动云盘",
        config_prefix="yun139",
        resource_types=frozenset({"yun139", "139", "mobile"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.SHARE_TRANSFER: share,
        },
        policy=CloudDrivePolicy(
            pagination_mode="cursor",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=100,
            max_concurrency=2,
        ),
    )
