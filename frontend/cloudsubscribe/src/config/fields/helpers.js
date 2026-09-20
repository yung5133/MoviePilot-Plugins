export const enabled = (key) => (config) => Boolean(config[key]);

export function createCloudDriveItems(options) {
  return options.cloudDrives?.length ? options.cloudDrives : [{title: "115网盘", value: "115"}];
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
  // 只有当当前网盘确实声明了自己支持的类型时才按之过滤。
  // 历史教训：ui_options 各作用域回传的 cloud_drives 字段完整度不一致，曾有作用域
  // 缺 capabilities / resource_types，此时若按空集处理，候选会塌缩成 ["115","ed2k","magnet"]，
  // 表现为"某些网盘类型凭空消失"。数据缺失应当放大候选集，而不是缩小。
  const declaredTypes = activeDrive?.resource_types;
  if (!Array.isArray(declaredTypes) || !declaredTypes.length) return resourceTypes;
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
