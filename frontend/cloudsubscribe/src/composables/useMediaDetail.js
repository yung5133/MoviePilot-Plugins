import {computed, ref, watch} from "vue";
import {
  DEFAULT_CHANNELS,
  getChannelDefaultIcon,
  getNormalizedResourceType,
  getResourceTabIcon,
  getResourceTypeName,
  getSourceName,
  isPointUnlockResource,
  responseItems,
  unwrapApiResponse,
} from "./resourceUtils";

// 媒体渠道搜索结果的前端内存缓存（15分钟有效）
const mediaSearchMemoryCache = new Map();
const MEDIA_SEARCH_CACHE_TTL = 15 * 60 * 1000;

function getMediaCacheKey(media) {
  if (!media) return "";
  const id = media.tmdb_id || media.douban_id || media.media_id || media.imdb_id || "";
  const title = String(media.title || "").trim().toLowerCase();
  const year = String(media.year || "").trim();
  const type = String(media.media_type || "movie").trim();
  return `${type}:${id}:${title}:${year}`;
}


/** 管理详情弹窗的数据补全、并发取消和生命周期。 */
export function useMediaDetail({api, pluginId, pluginConfig, showMessage}) {
  const detailVisible = ref(false);
  const detailLoading = ref(false);
  const activeMedia = ref(null);
  const activeDetailSeason = ref(1);

  function normalizeChannels(sources) {
    if (!Array.isArray(sources)) return [];
    return sources
      .map((source) => {
        const key = (typeof source === "object" && source?.key ? source.key : String(source)).toLowerCase();
        const name = typeof source === "object" && source?.name ? source.name : getSourceName(key);
        const icon = (typeof source === "object" && source?.icon) ? source.icon : getChannelDefaultIcon(key);
        return { key, name, icon };
      })
      .filter((channel) => Boolean(channel.key));
  }

  const availableChannels = ref(normalizeChannels(DEFAULT_CHANNELS));
  const availableDrives = ref([
    {key: "115", name: "115网盘"},
    {key: "quark", name: "夸克网盘"},
    {key: "alipan", name: "阿里云盘"},
    {key: "123", name: "123云盘"},
    {key: "tianyi", name: "天翼云盘"},
    {key: "guangya", name: "光鸭网盘"},
    {key: "yun139", name: "移动云盘"},
  ]);
  const activeChannelTab = ref("pansou");
  const activeResourceTab = ref("");
  const resourceSearchQuery = ref("");
  const selectedResourceSpecs = ref([]);
  const channelResults = ref({});
  const channelLoading = ref({});
  const channelSearched = ref({});
  const channelElapsed = ref({});
  let requestToken = 0;


  function getItemFansub(item) {
    if (item?.fansub) return String(item.fansub).trim();
    const title = String(item?.title || "").trim();
    const match = title.match(/^[\[【]([^\]】]+)[\]】]/);
    return match ? match[1].trim() : "其他";
  }

  const currentChannelResources = computed(() => channelResults.value[activeChannelTab.value] || []);
  const currentChannelResourceTabs = computed(() => {
    const list = currentChannelResources.value;
    const channel = String(activeChannelTab.value || "").toLowerCase();
    const isAnimeBtChannel = channel === "mikan" || channel === "animegarden";

    if (isAnimeBtChannel) {
      if (!list.length) return [];
      const counts = {};
      for (const item of list) {
        const fs = getItemFansub(item);
        counts[fs] = (counts[fs] || 0) + 1;
      }
      const sorted = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
      return [
        {
          value: "all",
          title: "全部",
          count: list.length,
          icon: "mdi-account-group-outline",
        },
        ...sorted.map((fs) => ({
          value: fs,
          title: fs,
          count: counts[fs],
          icon: "mdi-subtitles-outline",
        })),
      ];
    }

    const counts = {};
    for (const item of list) {
      const type = getNormalizedResourceType(item);
      counts[type] = (counts[type] || 0) + 1;
    }
    const order = [
      "115",
      "quark",
      "alipan",
      "uc",
      "guangya",
      "tianyi",
      "yun139",
      "123",
      "xunlei",
      "baidu",
      "magnet",
      "ed2k",
      "other",
    ];
    return Object.keys(counts)
      .sort((a, b) => {
        const aIndex = order.indexOf(a);
        const bIndex = order.indexOf(b);
        if (aIndex >= 0 && bIndex >= 0) return aIndex - bIndex;
        if (aIndex >= 0) return -1;
        if (bIndex >= 0) return 1;
        return counts[b] - counts[a];
      })
      .map((type) => ({
        value: type,
        title: getResourceTypeName(type),
        count: counts[type],
        icon: getResourceTabIcon(type),
      }));
  });

  const activeResourceFilterCount = computed(() => {
    let count = 0;
    if (String(resourceSearchQuery.value || "").trim()) count += 1;
    if (Array.isArray(selectedResourceSpecs.value)) count += selectedResourceSpecs.value.length;
    return count;
  });

  function resetResourceFilters() {
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  const currentChannelFilteredResources = computed(() => {
    const list = currentChannelResources.value;
    const selected = activeResourceTab.value;
    // 1. 过滤当前选中的子 tab
    const availableTabs = currentChannelResourceTabs.value;
    const effectiveSelected = availableTabs.some((tab) => tab.value === selected)
      ? selected
      : (availableTabs[0]?.value || "");

    const channel = String(activeChannelTab.value || "").toLowerCase();
    const isAnimeBtChannel = channel === "mikan" || channel === "animegarden";

    let tabFiltered = list;
    if (isAnimeBtChannel) {
      if (effectiveSelected && effectiveSelected !== "all") {
        tabFiltered = list.filter((item) => getItemFansub(item) === effectiveSelected);
      }
    } else {
      tabFiltered = !effectiveSelected
        ? list
        : list.filter((item) => getNormalizedResourceType(item) === effectiveSelected);
    }

    // 2. 搜索当前子 tab 列表的文本
    const query = String(resourceSearchQuery.value || "").trim().toLowerCase();
    let searched = tabFiltered;
    if (query) {
      searched = searched.filter((item) => {
        const title = String(item?.title || "").toLowerCase();
        const desc = String(item?.description || "").toLowerCase();
        const fileName = String(item?.file_name || "").toLowerCase();
        const tags = (item?.tags || []).map((t) => String(t || "").toLowerCase()).join(" ");
        const fansub = String(item?.fansub || "").toLowerCase();
        return title.includes(query) || desc.includes(query) || fileName.includes(query) || tags.includes(query) || fansub.includes(query);
      });
    }

    // 3. 规格快筛过滤
    const specs = selectedResourceSpecs.value || [];
    if (specs.length > 0) {
      searched = searched.filter((item) => {
        const itemTags = (item?.tags || []).map((t) => String(t).toUpperCase());
        const titleUpper = String(item?.title || "").toUpperCase();

        return specs.every((spec) => {
          const specUpper = String(spec).toUpperCase();
          if (specUpper === "免费") {
            return isPointUnlockResource(item) && Number(item?.unlock_points || 0) === 0;
          }
          if (specUpper === "4K") {
            return itemTags.includes("4K") || titleUpper.includes("4K") || titleUpper.includes("2160P");
          }
          if (specUpper === "1080P") {
            return itemTags.includes("1080P") || titleUpper.includes("1080P");
          }
          if (specUpper === "原盘") {
            return itemTags.some((t) => t.includes("原盘") || t.includes("REMUX") || t.includes("BLURAY") || t.includes("BDMV"))
              || /原盘|REMUX|BLURAY|BDMV/i.test(titleUpper);
          }
          if (specUpper === "HDR") {
            return itemTags.some((t) => t.includes("HDR")) || /HDR/i.test(titleUpper);
          }
          if (specUpper === "杜比视界" || specUpper === "DV") {
            return itemTags.some((t) => t.includes("杜比") || t.includes("DV") || t.includes("DOVI"))
              || /杜比视界|\bDV\b|DOVI|DOLBY\s*VISION/i.test(titleUpper);
          }
          return itemTags.includes(specUpper) || titleUpper.includes(specUpper);
        });
      });
    }

    return searched
      .map((item, index) => ({item, index}))
      .sort((a, b) => resourceTagCount(b.item) - resourceTagCount(a.item) || a.index - b.index)
      .map(({item}) => item);
  });

  function resourceTagCount(resource) {
    return Array.isArray(resource?.tags) ? resource.tags.length : 0;
  }

  async function loadMediaDetail(item, token = requestToken) {
    const response = await api.value.post(`plugin/${pluginId.value}/resource/detail`, item);
    const result = unwrapApiResponse(response);
    const detail = result?.data?.item || result?.item;
    if (result?.success === false || !detail || typeof detail !== "object") {
      throw new Error(result?.message || "获取媒体详情失败");
    }
    if (token !== requestToken) return false;
    activeMedia.value = {...activeMedia.value, ...detail};
    return true;
  }

  async function openMedia(item) {
    const token = ++requestToken;
    activeMedia.value = {...item};
    detailLoading.value = true;
    detailVisible.value = true;
    try {
      await loadMediaDetail(item, token);
    } finally {
      if (token === requestToken) detailLoading.value = false;
    }
    return token === requestToken && detailVisible.value;
  }

  function resetChannelState() {
    channelResults.value = {};
    channelLoading.value = {};
    channelSearched.value = {};
    channelElapsed.value = {};
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  function syncAvailableChannels(sources) {
    const list = normalizeChannels(sources);
    if (!list.length) return;
    availableChannels.value = list;
  }

  async function searchChannel(channelKey, force = false) {
    if (!channelKey || !activeMedia.value || (!force && channelSearched.value[channelKey])) return;
    if (force) {
      const mKey = getMediaCacheKey(activeMedia.value);
      if (mKey && mediaSearchMemoryCache.has(mKey)) {
        const entry = mediaSearchMemoryCache.get(mKey);
        delete entry.results?.[channelKey];
        delete entry.searched?.[channelKey];
        delete entry.elapsed?.[channelKey];
      }
    }
    channelLoading.value = {...channelLoading.value, [channelKey]: true};
    try {
      const media = activeMedia.value;
      const response = await api.value.post(`plugin/${pluginId.value}/resource/search_resources`, {
        source: channelKey,
        force: Boolean(force),
        force_refresh: Boolean(force),
        title: media.title,
        original_title: media.original_title || "",
        year: media.year || "",
        media_type: media.media_type || "movie",
        tmdb_id: media.tmdb_id || 0,
        imdb_id: media.imdb_id || "",
        tvdb_id: media.tvdb_id || 0,
        douban_id: media.douban_id || 0,
        bangumi_id: media.bangumi_id || 0,
        anilist_id: media.anilist_id || 0,
        anidb_id: media.anidb_id || 0,
        media_source: media.media_source || "",
        media_id: media.media_id || "",
      });
      const result = unwrapApiResponse(response);
      channelResults.value = {...channelResults.value, [channelKey]: result?.success ? responseItems(result) : []};
      channelElapsed.value = {...channelElapsed.value, [channelKey]: result?.data?.elapsed ?? null};
      if (result?.success) {
        syncAvailableChannels(result.data?.available_sources);
        if (Array.isArray(result.data?.available_drives) && result.data.available_drives.length) {
          availableDrives.value = result.data.available_drives;
        }
        if (result.data?.main_cloud_drive && pluginConfig?.value)
          pluginConfig.value.cloud_drive = result.data.main_cloud_drive;

        // 缓存该媒体的渠道搜索结果
        const mKey = getMediaCacheKey(activeMedia.value);
        if (mKey) {
          const entry = mediaSearchMemoryCache.get(mKey) || {results: {}, searched: {}, elapsed: {}, time: Date.now()};
          entry.results[channelKey] = channelResults.value[channelKey];
          entry.searched[channelKey] = true;
          entry.elapsed[channelKey] = channelElapsed.value[channelKey];
          entry.time = Date.now();
          mediaSearchMemoryCache.set(mKey, entry);
        }
      } else {
        showMessage?.(result?.message || `${getSourceName(channelKey)} 检索失败`, "warning");
      }
    } catch (error) {
      channelResults.value = {...channelResults.value, [channelKey]: []};
      const unavailable = error?.response?.status === 502 || String(error?.message || "").includes("502");
      showMessage?.(
        unavailable
          ? `${getSourceName(channelKey)} 渠道服务暂不可用 (502)，请稍后重试`
          : `${getSourceName(channelKey)} 搜索异常: ${error?.message || error}`,
        "warning",
      );
    } finally {
      channelLoading.value = {...channelLoading.value, [channelKey]: false};
      channelSearched.value = {...channelSearched.value, [channelKey]: true};
    }
  }

  async function openMediaDetail(item) {
    const key = getMediaCacheKey(item);
    const cached = mediaSearchMemoryCache.get(key);
    if (cached && Date.now() - cached.time < MEDIA_SEARCH_CACHE_TTL) {
      channelResults.value = {...(cached.results || {})};
      channelLoading.value = {};
      channelSearched.value = {...(cached.searched || {})};
      channelElapsed.value = {...(cached.elapsed || {})};
      activeResourceTab.value = "";
      resourceSearchQuery.value = "";
      selectedResourceSpecs.value = [];
    } else {
      resetChannelState();
    }
    try {
      const stillOpen = await openMedia(item);
      if (!stillOpen) return;
    } catch (error) {
      console.warn("[网盘资源] 获取媒体详情失败，使用榜单媒体信息继续", error);
    }
    if (!detailVisible.value) return;
    const seasons = activeMedia.value?.seasons || [];
    const defaultSeason =
      seasons.find((season) => {
        const episodes = Array.isArray(season?.episodes) ? season.episodes : [];
        return !episodes.length || episodes.some((episode) => !episode?.in_library);
      }) || seasons[0];
    activeDetailSeason.value = defaultSeason?.season_number || 1;
    const firstChannel = availableChannels.value[0]?.key || "";
    activeChannelTab.value = firstChannel;
    if (firstChannel && !channelSearched.value[firstChannel]) {
      await searchChannel(firstChannel);
    }
  }

  function onChannelTabChange(channelKey) {
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    if (!channelSearched.value[channelKey] && !channelLoading.value[channelKey]) searchChannel(channelKey);
  }

  function getChannelCount(channelKey) {
    return channelKey === activeChannelTab.value
      ? currentChannelResources.value.length
      : (channelResults.value[channelKey] || []).length;
  }

  function closeMediaDetail() {
    detailVisible.value = false;
  }

  watch(detailVisible, (visible) => {
    if (visible) return;
    requestToken += 1;
    detailLoading.value = false;
  });

  watch(
    currentChannelResourceTabs,
    (tabs) => {
      const first = tabs[0]?.value || "";
      if (!tabs.length) {
        activeResourceTab.value = "";
      } else if (!tabs.some((tab) => tab.value === activeResourceTab.value)) {
        activeResourceTab.value = first;
      }
    },
    {immediate: true},
  );

  watch(
    availableChannels,
    (channels) => {
      const first = channels[0]?.key || "";
      if (!channels.some((channel) => channel.key === activeChannelTab.value)) {
        activeChannelTab.value = first;
      }
    },
    {immediate: true},
  );

  return {
    detailVisible,
    detailLoading,
    activeMedia,
    activeDetailSeason,
    availableChannels,
    availableDrives,
    activeChannelTab,
    activeResourceTab,
    resourceSearchQuery,
    selectedResourceSpecs,
    activeResourceFilterCount,
    resetResourceFilters,
    channelResults,
    channelLoading,
    channelSearched,
    channelElapsed,
    currentChannelResources,
    currentChannelResourceTabs,
    currentChannelFilteredResources,
    openMediaDetail,
    searchChannel,
    onChannelTabChange,
    getChannelCount,
    closeMediaDetail,
    syncAvailableChannels,
  };
}
