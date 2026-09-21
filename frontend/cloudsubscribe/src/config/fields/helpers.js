export const enabled = (key) => (config) => Boolean(config[key]);

export function createCloudDriveItems(options) {
  return options.cloudDrives?.length ? options.cloudDrives : [{title: "115网盘", value: "115"}];
}

/**
 * 合并后端回传的网盘列表。
 *
 * ui_options 有多个作用域，其中 subscriptions 语境下的"可作为转存来源的网盘"
 * 只是全集的一个子集。若按覆盖式写入，先加载 base 再访问转存类 Tab 就会把
 * 全量替换成子集，导致未入选的网盘从「当前转存网盘」下拉与资源类型候选中消失。
 * 因此按 value 做并集：同一网盘以新数据为准，已见过的网盘不因窄列表而丢失。
 */
export function mergeCloudDriveOptions(current, incoming) {
  const existing = Array.isArray(current) ? current : [];
  const next = Array.isArray(incoming) ? incoming : [];
  if (!existing.length) return next;
  if (!next.length) return existing;
  const merged = new Map();
  existing.forEach((item) => {
    if (item && item.value !== undefined) merged.set(item.value, item);
  });
  next.forEach((item) => {
    if (item && item.value !== undefined) merged.set(item.value, item);
  });
  return [...merged.values()];
}

/**
 * 资源类型候选列表。
 *
 * 候选集只由「已配置哪些网盘」决定，**与「当前转存网盘」无关**。
 * 后者是运行时选择；曾用它收窄候选，配合 Config.vue 里按候选集剪枝的 watcher，
 * 会在切换转存网盘时把用户已保存的 resource_type_order 静默删掉
 * （选移动云盘 → 115/夸克/ed2k 被删；选夸克 → 移动云盘被删）。
 * 能否真正转存由后端在转存阶段按能力判断，不应反过来改写用户的搜索范围配置。
 *
 * 第 2 个参数仅为兼容既有调用方保留，本函数刻意不读取它。
 */
export function createResourceTypeItems(cloudDriveItems, _config) {
  const resourceTypes = [
    {title: "115分享", value: "115"},
    {title: "123分享", value: "123"},
    {title: "夸克分享", value: "quark"},
    {title: "光鸭分享", value: "guangya"},
    {title: "天翼云盘", value: "tianyi"},
    {title: "阿里云盘", value: "alipan"},
    {title: "移动云盘", value: "yun139"},
    {title: "ED2K", value: "ed2k"},
    {title: "Magnet", value: "magnet"},
  ]
  const supportedTypes = new Set();
  (Array.isArray(cloudDriveItems) ? cloudDriveItems : []).forEach((drive) => {
    const declared = drive?.resource_types;
    if (Array.isArray(declared)) {
      declared.forEach((value) => supportedTypes.add(value));
    }
  });
  if (!supportedTypes.size) {
    // 数据缺失时应当放大候选集而不是缩小：拿不到任何能力声明就交出全量，
    // 由后端在保存/转存阶段按真实能力收敛。
    return resourceTypes;
  }
  return resourceTypes.filter((item) => supportedTypes.has(item.value));
}
