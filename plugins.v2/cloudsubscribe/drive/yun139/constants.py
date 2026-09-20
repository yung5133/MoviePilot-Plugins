"""移动云盘（139）驱动的共享常量。

本模块是这些常量的**唯一定义处**：驱动包内的 ``client.py`` / ``files.py`` /
``provider.py`` / ``definition.py`` 都从这里取值，避免同一语义在多处重复书写、
修改时漏改其中一处。

背景：v1.5.4 引入驱动、v1.5.5「对齐 OpenList」时，接入点、根标识与驱动标识分别
散落在多个模块的字面量中，修复时极易只改一处。收敛到本模块后，改一个值即可全量生效。

**收敛边界**：只收敛「语义相同、值必须一致」的量。以下两类刻意保留字面量——
  * ``definition.py`` 中的表单字段名（``yun139_authorization`` 等）与页签 id：
    它们是配置持久化与前端状态契约，当前恰好同名纯属命名巧合；若绑定到
    ``PROVIDER_KEY``，将来改驱动键会连带使已保存的用户配置失效。
  * ``resource_types`` 别名集合与包外映射表（``search/types.py``、
    ``core/drive_manager.py`` 等）：前者含多个不同值，后者在驱动包之外，
    反向依赖本模块会引入循环导入。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 驱动标识
# --------------------------------------------------------------------------- #

# 驱动注册键。以下位置的取值必须与本常量严格一致，否则会出现隐性故障：
#   client.py     —— DriveRateLimiter 命名空间（不一致则限流不共享）
#   files.py      —— provider_key、目录短缓存命名空间（不一致则缓存不共享）
#   provider.py   —— CloudDriveProvider.key / config_prefix
#   definition.py —— DriverDefinition.id / drive_provider / account_key
PROVIDER_KEY = "yun139"

# 驱动显示名，用于日志与界面展示。
PROVIDER_NAME = "移动云盘"


# --------------------------------------------------------------------------- #
# 根目录标识
# --------------------------------------------------------------------------- #

# 139 个人云（personal_new）的根目录标识是 "/"。
# 不要与其他网盘的约定混淆：夸克/123 云盘用 "0"，天翼云盘用 "-11"。
ROOT_CATALOG_ID = "/"

# 历史版本可能出现在配置、缓存或调用参数中的根标识写法。
# normalize_catalog_id() 会把它们统一归一为 ROOT_CATALOG_ID。
LEGACY_ROOT_CATALOG_IDS = frozenset({"/", "0", "-1", "-11"})


# --------------------------------------------------------------------------- #
# 接口主机
# --------------------------------------------------------------------------- #

# 用户态接口，同时用于路由策略查询。
USER_API = "https://user-njs.yun.139.com"

# 分享接口自带 /yun-share/ 前缀，与 OpenList 一致，无需改动。
SHARE_API = "https://share-kd-njs.yun.139.com"

# 个人云接入点的兜底地址。正常路径由 /user/route/qryRoutePolicy 按账号动态下发
# （返回值形如 https://personal-xxx.yun.139.com/hcy），此处仅在路由解析失败时使用，
# 保证新增依赖出问题时不会把原本可用的链路变成不可用。
#
# 关键：个人云接口的真实前缀是 "/hcy"（网关内层路由）。缺少该前缀时 /file/*
# 一律无法路由，表现为「授权能识别手机号，但列不出任何目录」。
FALLBACK_PERSONAL_API = "https://personal-kd-njs.yun.139.com/hcy"


# --------------------------------------------------------------------------- #
# 路由策略
# --------------------------------------------------------------------------- #

# 动态解析个人云接入点，避免 139 更换地域接入点后写死的地址失效。
ROUTE_POLICY_PATH = "/user/route/qryRoutePolicy"

# routePolicyList 中个人云对应的 modName。
ROUTE_POLICY_MOD_PERSONAL = "personal"

# 该请求头让网关在 routePolicyList 中下发内层路由地址（即带 /hcy 前缀的 httpsUrl）。
# 缺失此头时返回的是外层地址，拼接 /file/* 仍会路由失败。
ROUTE_POLICY_INNER_HCY_HEADER = "Inner-Hcy-Router-Https"
