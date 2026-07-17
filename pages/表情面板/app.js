const HTTP_API = "/astrbot_plugin_mood_of_the_moment/page";
const PAGE_ENDPOINT_PREFIX = "page";

const state = {
  overview: null,
  stickers: [],
  selectedAssetId: "",
  selectedAsset: null,
  selectedIds: new Set(),
  isSelectMode: false,
  filters: {
    q: "",
    tag: "",
    group: "",
    status: "",
    sortBy: "created_at",
    sortOrder: "desc",
    page: 1,
    pageSize: 48,
    total: 0,
  },
  imageCache: new Map(),
  config: null,
  providers: [],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, tone = "") {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show ${tone}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.className = "toast";
  }, 2600);
}

async function customConfirm(message) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.style.zIndex = "9999";
    overlay.innerHTML = `
      <div class="modal-backdrop"></div>
      <div class="modal-panel" style="max-width: 320px; padding: 24px;">
        <h3 style="margin: 0 0 16px 0; font-size: 16px;">确认操作</h3>
        <p style="margin: 0 0 24px 0; color: var(--ink-muted);">${escapeHtml(message)}</p>
        <div class="modal-actions">
          <button type="button" class="ghost" id="customConfirmCancel">取消</button>
          <button type="button" class="primary danger" id="customConfirmOk">确认</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    
    const cleanUp = () => {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    };
    
    overlay.querySelector("#customConfirmCancel").onclick = () => {
      cleanUp();
      resolve(false);
    };
    overlay.querySelector("#customConfirmOk").onclick = () => {
      cleanUp();
      resolve(true);
    };
    overlay.querySelector(".modal-backdrop").onclick = () => {
      cleanUp();
      resolve(false);
    };
  });
}

function getBridge() {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  try {
    if (window.parent && window.parent !== window && window.parent.AstrBotPluginPage) {
      return window.parent.AstrBotPluginPage;
    }
  } catch (error) {
    return null;
  }
  return null;
}

async function waitForBridge(timeoutMs = 2500) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const bridge = getBridge();
    if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
      return bridge;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  return getBridge();
}

async function bridgeRequest(bridge, path, method, body) {
  const url = new URL(path, "https://mood-panel.local/");
  const endpoint = `${PAGE_ENDPOINT_PREFIX}/${url.pathname.replace(/^\/+/, "")}`.replace(/\/+/g, "/");
  if (method === "GET") {
    const params = Object.fromEntries(url.searchParams.entries());
    return bridge.apiGet(endpoint, Object.keys(params).length ? params : undefined);
  }
  return bridge.apiPost(endpoint, body || {});
}

function normalizePayload(payload) {
  if (payload && typeof payload === "object" && "success" in payload) {
    if (!payload.success) throw new Error(payload.error || "请求失败");
    return payload.data;
  }
  return payload;
}

async function fetchJson(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const bridge = await waitForBridge();
  let payload;
  if (bridge && typeof bridge.apiGet === "function" && typeof bridge.apiPost === "function") {
    payload = await bridgeRequest(bridge, path, method, options.body);
  } else if (new URLSearchParams(window.location.search).get("debug_http") === "1" || window.location.protocol.startsWith("http")) {
    const response = await fetch(`${HTTP_API}${path}`, {
      method,
      cache: "no-store",
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    const text = await response.text();
    payload = text ? JSON.parse(text) : {};
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  } else {
    throw new Error("请从 AstrBot 后台的插件拓展页打开此面板");
  }
  return normalizePayload(payload);
}

async function loadAll() {
  await Promise.all([loadOverview(), loadStickers()]);
  renderAll();
}

async function loadOverview() {
  state.overview = await fetchJson("/overview");
}

async function loadStickers() {
  const params = new URLSearchParams();
  const { q, tag, group, status, sortBy, sortOrder, page, pageSize } = state.filters;
  if (q) params.set("q", q);
  if (tag) params.set("tag", tag);
  if (group) params.set("group", group);
  if (status) params.set("status", status);
  params.set("sort_by", sortBy);
  params.set("sort_order", sortOrder);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  
  const result = await fetchJson(`/stickers?${params.toString()}`);
  state.stickers = result.items || [];
  state.filters.total = Number(result.total || 0);
  state.filters.page = Number(result.page || page);
  state.filters.sortBy = result.sort_by || sortBy;
  state.filters.sortOrder = result.sort_order || sortOrder;
  
  // Selected IDs are preserved across pages to support multi-page bulk operations.
}

function renderAll() {
  renderStats();
  renderTags();
  renderStickerWall();
  renderSelectionBar();
  renderDetailDrawer();
  renderPager();
}

function renderStats() {
  const statsEl = $("#stats");
  if (!statsEl) return;
  const overview = state.overview || {};
  statsEl.innerHTML = `
    <span><b>${state.filters.total}</b> 张贴纸</span>
    ${overview.missing ? `<span><b>${overview.missing}</b> 个文件丢失</span>` : ""}
  `;
}

function renderTags() {
  const rail = $("#tagRail");
  if (!rail) return;
  const overview = state.overview || {};
  const tags = overview.tags || [];
  
  const clearBtn = $("#clearFilterBtn");
  if (clearBtn) {
    clearBtn.className = `tag-chip ${!state.filters.tag ? "is-active" : ""}`;
  }

  rail.innerHTML = tags.map((t) => `
    <button type="button" class="tag-chip ${t.name === state.filters.tag ? "is-active" : ""}" data-filter-tag="${escapeHtml(t.name)}">
      ${escapeHtml(t.name)} (${escapeHtml(t.count)})
    </button>
  `).join("");
}

function renderStickerWall() {
  const wall = $("#stickerWall");
  if (!wall) return;
  
  document.body.classList.toggle("is-select-mode", state.isSelectMode);
  
  if (!state.stickers.length) {
    wall.innerHTML = `<div class="empty">没有找到贴纸</div>`;
    return;
  }
  
  wall.innerHTML = state.stickers.map((asset) => `
    <article class="sticker ${state.selectedIds.has(asset.asset_id) ? "is-selected" : ""} ${asset.exists ? "" : "missing"}" data-asset-id="${escapeHtml(asset.asset_id)}">
      <input class="sticker-select" type="checkbox" data-select-asset="${escapeHtml(asset.asset_id)}" ${state.selectedIds.has(asset.asset_id) ? "checked" : ""} aria-label="选择" />
      <div class="thumb" data-open-asset="${escapeHtml(asset.asset_id)}">
        ${asset.exists ? `<img alt="${escapeHtml(asset.original_name)}" data-image-asset="${escapeHtml(asset.asset_id)}" />` : `<span class="missing-text">文件缺失</span>`}
      </div>
      <div class="sticker-meta">
        <b title="${escapeHtml(asset.original_name)}">${escapeHtml(asset.original_name)}</b>
        <small>${escapeHtml(asset.group_name)} · ${escapeHtml(asset.usage_count)} 次</small>
      </div>
    </article>
  `).join("");
  hydrateImages(wall);
}

function renderSelectionBar() {
  const bar = $("#selectionBar");
  if (!bar) return;
  if (state.isSelectMode) {
    bar.hidden = false;
    $("#selectionCount").textContent = `已选 ${state.selectedIds.size} 张`;
  } else {
    bar.hidden = true;
  }
}

async function hydrateImages(root = document) {
  const imgs = [...root.querySelectorAll("img[data-image-asset]")];
  await Promise.all(imgs.map(async (img) => {
    const assetId = img.dataset.imageAsset;
    if (!assetId) return;
    if (state.imageCache.has(assetId)) {
      img.src = state.imageCache.get(assetId);
      return;
    }
    const asset = state.stickers.find((item) => item.asset_id === assetId) || state.selectedAsset;
    const endpoint = asset?.image_endpoint;
    if (!endpoint) return;
    try {
      const result = await fetchJson(endpoint);
      if (result?.data_url) {
        state.imageCache.set(assetId, result.data_url);
        img.src = result.data_url;
      }
    } catch (error) {
      img.alt = "图片加载失败";
    }
  }));
}

function renderDetailDrawer() {
  const panel = $("#detailPanel");
  if (!panel) return;
  const asset = state.selectedAsset;
  if (!asset) {
    panel.innerHTML = `<div class="empty">未选择贴纸</div>`;
    return;
  }
  panel.innerHTML = `
    <div class="detail-image">
      ${asset.exists ? `<img alt="${escapeHtml(asset.original_name)}" data-image-asset="${escapeHtml(asset.asset_id)}" />` : `<span class="missing-text">文件缺失</span>`}
    </div>
    <form id="detailForm" class="detail-form">
      <label><span>分组</span><input name="group_name" value="${escapeHtml(asset.group_name)}" /></label>
      <label><span>标签 (逗号分隔)</span><input name="labels" value="${escapeHtml((asset.labels || []).join(", "))}" /></label>
      <label><span>描述</span><textarea name="description">${escapeHtml(asset.description || "")}</textarea></label>
      <label><span>来源</span><input name="source" value="${escapeHtml(asset.source || "")}" /></label>
      <label><span>asset_id</span><code>${escapeHtml(asset.asset_id)}</code></label>
      <label><span>文件</span><code>${escapeHtml(asset.storage_key || asset.file_path || "")}</code></label>
      <div class="detail-actions">
        <button type="submit" class="primary">保存修改</button>
        <button id="deleteAssetBtn" type="button" class="ghost danger-text">删除</button>
      </div>
    </form>
  `;
  hydrateImages(panel);
}

function renderPager() {
  const { page, pageSize, total } = state.filters;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  $("#pageInfo").textContent = `${page} / ${pages}`;
  $("#prevPageBtn").disabled = page <= 1;
  $("#nextPageBtn").disabled = page >= pages;
}

async function selectAssetForEdit(assetId) {
  state.selectedAssetId = assetId;
  state.selectedAsset = await fetchJson(`/sticker?asset_id=${encodeURIComponent(assetId)}`);
  renderDetailDrawer();
  $("#detailDrawer").setAttribute("aria-hidden", "false");
}

function toggleSelectMode(on) {
  state.isSelectMode = on;
  if (!on) state.selectedIds.clear();
  renderStickerWall();
  renderSelectionBar();
  $("#selectModeBtn").hidden = on;
  $("#openImportBtn").hidden = on;
  $("#refreshBtn").hidden = on;
}

async function reloadAfterMutation(message) {
  await loadAll();
  if (state.selectedAssetId) {
    try {
      state.selectedAsset = await fetchJson(`/sticker?asset_id=${encodeURIComponent(state.selectedAssetId)}`);
    } catch (error) {
      state.selectedAssetId = "";
      state.selectedAsset = null;
      $("#detailDrawer").setAttribute("aria-hidden", "true");
    }
  }
  renderAll();
  if (message) showToast(message);
}

function formValue(form, name) {
  return String(new FormData(form).get(name) || "").trim();
}

function renderSettingsForm() {
  const body = $("#settingsBody");
  if (!body) return;
  if (!state.config) {
    body.innerHTML = `<div class="empty">加载失败</div>`;
    return;
  }
  const c = state.config;
  const providers = state.providers || [];
  const providerOptions = [
    `<option value="">默认（留空）</option>`,
    ...providers.map(p => `<option value="${escapeHtml(p.id)}" ${c.tag_provider_id === p.id ? "selected" : ""}>${escapeHtml(p.name || p.id)}</option>`)
  ].join("");

  body.innerHTML = `
    <label>
      <span>审查 LLM Provider</span>
      <select name="tag_provider_id">${providerOptions}</select>
    </label>
    <label>
      <span>审查 System Prompt</span>
      <textarea name="review_system_prompt" rows="6">${escapeHtml(c.review_system_prompt || "")}</textarea>
    </label>
    <div class="form-grid">
      <label>
        <span>最大表情包数量</span>
        <input name="max_stickers" type="number" min="0" value="${escapeHtml(c.max_stickers ?? 100)}" />
      </label>
      <label>
        <span>每条消息最多贴纸</span>
        <input name="max_stickers_per_message" type="number" min="0" max="10" value="${escapeHtml(c.max_stickers_per_message ?? 1)}" />
      </label>
    </div>
    <div class="form-grid">
      <label>
        <span>提示标签数量上限</span>
        <input name="max_prompt_tags" type="number" min="0" max="100" value="${escapeHtml(c.max_prompt_tags ?? 30)}" />
      </label>
      <label>
        <span>清理间隔（小时）</span>
        <input name="cleanup_interval_hours" type="number" min="1" value="${escapeHtml(c.cleanup_interval_hours ?? 1)}" />
      </label>
    </div>
    <div class="form-grid">
      <label>
        <span>每次清理数量</span>
        <input name="cleanup_count" type="number" min="1" value="${escapeHtml(c.cleanup_count ?? 5)}" />
      </label>
      <label>
        <span>最小保留数量</span>
        <input name="min_stickers_to_keep" type="number" min="0" value="${escapeHtml(c.min_stickers_to_keep ?? 0)}" />
      </label>
    </div>
    <label class="checkbox-row">
      <input name="enable_auto_steal" type="checkbox" ${c.enable_auto_steal ? "checked" : ""} />
      <span>启用自动偷图</span>
    </label>
    <label class="checkbox-row">
      <input name="enable_auto_cleanup" type="checkbox" ${c.enable_auto_cleanup ? "checked" : ""} />
      <span>启用自动清理</span>
    </label>
    <label class="checkbox-row">
      <input name="steal_all_images" type="checkbox" ${c.steal_all_images ? "checked" : ""} />
      <span>偷取所有图片</span>
    </label>
    <label class="checkbox-row">
      <input name="only_store_emojis" type="checkbox" ${c.only_store_emojis ? "checked" : ""} />
      <span>仅偷取商城表情</span>
    </label>
  `;
}

async function openSettings() {
  const modal = $("#settingsModal");
  if (!modal) return;
  modal.hidden = false;
  const body = $("#settingsBody");
  if (body) body.innerHTML = `<div class="empty">加载中...</div>`;
  try {
    const result = await fetchJson("/config");
    state.config = result.config || {};
    state.providers = result.providers || [];
    renderSettingsForm();
  } catch (err) {
    if (body) body.innerHTML = `<div class="empty">${escapeHtml(err.message || "加载配置失败")}</div>`;
  }
}

function collectSettingsPayload() {
  const form = $("#settingsForm");
  if (!form) return null;
  const fd = new FormData(form);
  return {
    tag_provider_id: String(fd.get("tag_provider_id") || ""),
    review_system_prompt: String(fd.get("review_system_prompt") || ""),
    max_stickers: parseInt(fd.get("max_stickers"), 10) || 0,
    max_stickers_per_message: parseInt(fd.get("max_stickers_per_message"), 10) || 0,
    max_prompt_tags: parseInt(fd.get("max_prompt_tags"), 10) || 0,
    cleanup_interval_hours: parseInt(fd.get("cleanup_interval_hours"), 10) || 1,
    cleanup_count: parseInt(fd.get("cleanup_count"), 10) || 1,
    min_stickers_to_keep: parseInt(fd.get("min_stickers_to_keep"), 10) || 0,
    enable_auto_steal: fd.get("enable_auto_steal") === "on",
    enable_auto_cleanup: fd.get("enable_auto_cleanup") === "on",
    steal_all_images: fd.get("steal_all_images") === "on",
    only_store_emojis: fd.get("only_store_emojis") === "on",
  };
}

// Global click handler
document.addEventListener("click", async (event) => {
  const target = event.target;

  // Drawer close
  if (target.id === "closeDetailBtn") {
    $("#detailDrawer").setAttribute("aria-hidden", "true");
    return;
  }

  // Modal open/close
  if (target.id === "openImportBtn") {
    $("#importModal").hidden = false;
    return;
  }
  if (target.id === "closeImportBtn" || target.matches("[data-close-import]")) {
    $("#importModal").hidden = true;
    return;
  }

  // Settings modal
  if (target.id === "openSettingsBtn") {
    await openSettings();
    return;
  }
  if (target.id === "closeSettingsBtn" || target.matches("[data-close-settings]")) {
    $("#settingsModal").hidden = true;
    return;
  }

  // Select mode toggle
  if (target.id === "selectModeBtn") {
    toggleSelectMode(true);
    return;
  }
  if (target.id === "cancelSelectBtn") {
    toggleSelectMode(false);
    return;
  }

  // Click on a sticker
  const openTarget = target.closest?.("[data-open-asset]");
  if (openTarget) {
    const assetId = openTarget.dataset.openAsset;
    if (state.isSelectMode) {
      // Toggle selection
      if (state.selectedIds.has(assetId)) {
        state.selectedIds.delete(assetId);
      } else {
        state.selectedIds.add(assetId);
      }
      renderStickerWall();
      renderSelectionBar();
    } else {
      // Open drawer
      await selectAssetForEdit(assetId);
    }
    return;
  }

  // Tags filter
  const tagBtn = target.closest?.("[data-filter-tag]");
  if (tagBtn) {
    const value = tagBtn.dataset.filterTag;
    state.filters.page = 1;
    state.filters.tag = state.filters.tag === value ? "" : value;
    await loadStickers();
    renderAll();
    return;
  }

  if (target.id === "clearFilterBtn") {
    state.filters.tag = "";
    state.filters.page = 1;
    await loadStickers();
    renderAll();
    return;
  }

  // Other actions
  if (target.id === "refreshBtn") {
    await reloadAfterMutation("已刷新");
    return;
  }
  if (target.id === "prevPageBtn") {
    state.filters.page = Math.max(1, state.filters.page - 1);
    await loadStickers();
    renderAll();
    return;
  }
  if (target.id === "nextPageBtn") {
    state.filters.page += 1;
    await loadStickers();
    renderAll();
    return;
  }

  if (target.id === "deleteAssetBtn" && state.selectedAssetId) {
    if (!(await customConfirm("确认删除这张贴纸？"))) return;
    try {
      await fetchJson("/sticker/delete", {
        method: "POST",
        body: { asset_id: state.selectedAssetId, confirm: true },
      });
      state.imageCache.delete(state.selectedAssetId);
      state.selectedIds.delete(state.selectedAssetId);
      state.selectedAssetId = "";
      state.selectedAsset = null;
      $("#detailDrawer").setAttribute("aria-hidden", "true");
      await reloadAfterMutation("已删除");
    } catch (err) {
      showToast(err.message || "删除失败", "error");
    }
    return;
  }

  if (target.id === "bulkDeleteBtn") {
    const assetIds = [...state.selectedIds];
    if (!assetIds.length) return;
    if (!(await customConfirm(`确认删除选中的 ${assetIds.length} 张贴纸？`))) return;
    try {
      await fetchJson("/sticker/bulk_delete", {
        method: "POST",
        body: { asset_ids: assetIds, confirm: true },
      });
      assetIds.forEach((assetId) => state.imageCache.delete(assetId));
      toggleSelectMode(false);
      await reloadAfterMutation("已批量删除");
    } catch (err) {
      showToast(err.message || "批量删除失败", "error");
    }
    return;
  }

  if (target.id === "pruneBtn") {
    if (!(await customConfirm("清理所有文件缺失的数据库记录？"))) return;
    try {
      const result = await fetchJson("/maintenance/prune_missing", {
        method: "POST",
        body: { confirm: true },
      });
      await reloadAfterMutation(`已清理 ${result.removed?.length || 0} 条记录`);
    } catch (err) {
      showToast(err.message || "清理失败", "error");
    }
    return;
  }
});

// Checkbox selection
document.addEventListener("change", async (event) => {
  const target = event.target;
  if (target.matches("[data-select-asset]")) {
    if (target.checked) state.selectedIds.add(target.dataset.selectAsset);
    else state.selectedIds.delete(target.dataset.selectAsset);
    renderSelectionBar();
    
    // Update the parent article class
    const article = target.closest("article.sticker");
    if (article) article.classList.toggle("is-selected", target.checked);
    return;
  }
  
  if (target.id === "statusFilter") {
    state.filters.status = target.value;
    state.filters.page = 1;
    await loadStickers();
    renderAll();
    return;
  }
  if (target.id === "sortBySelect") {
    state.filters.sortBy = target.value || "created_at";
    state.filters.page = 1;
    await loadStickers();
    renderAll();
    return;
  }
  if (target.id === "sortOrderSelect") {
    state.filters.sortOrder = target.value;
    state.filters.page = 1;
    await loadStickers();
    renderAll();
  }
});

// Search input
$("#searchInput")?.addEventListener("input", debounce(async (event) => {
  state.filters.q = event.target.value.trim();
  state.filters.page = 1;
  await loadStickers();
  renderAll();
}, 220));

// File input display
const fileInput = $("#fileInput");
if (fileInput) {
  fileInput.addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) {
      $("#fileName").textContent = `已选择: ${file.name}`;
    } else {
      $("#fileName").textContent = "也可以在下面粘贴 http/https 图片地址或服务器本地路径";
    }
  });
}

// Read file as base64
function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Import form submit
$("#importForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  
  try {
    const file = form.elements.file.files[0];
    let result;
    
    if (file) {
      // POST to /sticker/upload
      const data_url = await readFileAsDataURL(file);
      result = await fetchJson("/sticker/upload", {
        method: "POST",
        body: {
          data_url,
          filename: file.name,
          group_name: formValue(form, "group_name") || "unsorted",
          labels: formValue(form, "labels"),
          description: formValue(form, "description")
        },
      });
    } else {
      // POST to /sticker/import
      result = await fetchJson("/sticker/import", {
        method: "POST",
        body: {
          image_source: formValue(form, "image_source"),
          group_name: formValue(form, "group_name") || "unsorted",
          labels: formValue(form, "labels"),
          description: formValue(form, "description")
        },
      });
    }
    
    form.reset();
    form.elements.group_name.value = "unsorted";
    $("#fileName").textContent = "也可以在下面粘贴 http/https 图片地址或服务器本地路径";
    $("#importModal").hidden = true;
    
    await reloadAfterMutation("已添加表情");
    if (result.asset) {
      await selectAssetForEdit(result.asset.asset_id);
    }
  } catch (err) {
    showToast(err.message || "上传失败", "error");
  } finally {
    button.disabled = false;
  }
});

// Settings form submit
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.id !== "settingsForm") return;
  event.preventDefault();
  const button = form.querySelector("button[type='submit']");
  if (button) button.disabled = true;
  try {
    const payload = collectSettingsPayload();
    if (!payload) return;
    await fetchJson("/config/update", {
      method: "POST",
      body: payload,
    });
    $("#settingsModal").hidden = true;
    showToast("配置已保存");
    await loadOverview();
    renderStats();
  } catch (err) {
    showToast(err.message || "保存失败", "error");
  } finally {
    if (button) button.disabled = false;
  }
});

// Detail form submit
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.id !== "detailForm") return;
  event.preventDefault();
  if (!state.selectedAssetId) return;
  const button = form.querySelector("button[type='submit']");
  if (button) button.disabled = true;
  try {
    const result = await fetchJson("/sticker/update", {
      method: "POST",
      body: {
        asset_id: state.selectedAssetId,
        group_name: formValue(form, "group_name"),
        labels: formValue(form, "labels"),
        description: formValue(form, "description"),
        source: formValue(form, "source"),
      },
    });
    state.selectedAsset = result;
    state.imageCache.delete(state.selectedAssetId);
    await reloadAfterMutation("修改已保存");
  } finally {
    if (button) button.disabled = false;
  }
});

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

// Initial load
loadAll().catch((error) => {
  showToast(error.message || "加载失败", "error");
  const wall = $("#stickerWall");
  if (wall) wall.innerHTML = `<div class="empty">${escapeHtml(error.message || "加载失败")}</div>`;
});
