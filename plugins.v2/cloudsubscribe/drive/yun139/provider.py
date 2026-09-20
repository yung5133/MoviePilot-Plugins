"""移动云盘能力适配。"""

from dataclasses import dataclass

from .client import Yun139Client
from .constants import PROVIDER_KEY, PROVIDER_NAME
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
        key=PROVIDER_KEY,
        name=PROVIDER_NAME,
        config_prefix=PROVIDER_KEY,
        # 别名集合保留字面量：它含 139 / mobile 等多个不同值，属分享链接识别契约，
        # 与驱动键并非同一语义，收敛无意义。
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
