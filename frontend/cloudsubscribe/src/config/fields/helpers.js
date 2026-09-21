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

export function createResourceTypeItems(cloudDriveItems, config) {
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
  const activeDrive = cloudDriveItems.find((item) => item.value === (config.cloud_drive || "115"));
  const declaredTypes = activeDrive?.resource_types;
  if (!Array.isArray(declaredTypes) || !declaredTypes.length) {
    // 数据缺失时应当放大候选集而不是缩小：拿不到能力声明就交出全量，
    // 由后端在保存时按真实能力收敛，避免候选塌缩成 115/ed2k/magnet 三项。
    return resourceTypes;
  }
  const supportedTypes = new Set(declaredTypes);
  const targetCanUpload = activeDrive?.capabilities?.includes("local_upload");
  if (config.cross_transfer_enabled && targetCanUpload) {
    cloudDriveItems.forEach((drive) => {
      const capabilities = new Set(drive.capabilities || []);
      if (
        drive.value === activeDrive?.value ||
        !capabilities.has("share_transfer") ||
        !capabilities.has("file_download")
      ) {
        return;
      }
      ;(drive.resource_types || []).forEach((value) => {
        if (!["ed2k", "magnet"].includes(value)) supportedTypes.add(value);
      })
    })
  }
  return resourceTypes.filter((item) => supportedTypes.has(item.value));
}
