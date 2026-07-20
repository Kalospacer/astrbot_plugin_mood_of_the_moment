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
    status: "",
    sortBy: "created_at",
    sortOrder: "desc",
    page: 1,
    pageSize: 48,
    total: 0,
  },
  config: null,
  providers: [],
  formatJob: null,
  formatTimer: null,
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
  state.formatJob = state.overview?.format_job || null;
}

async function loadStickers() {
  const params = new URLSearchParams();
  const { q, tag, status, sortBy, sortOrder, page, pageSize } = state.filters;
  if (q) params.set("q", q);
  if (tag) params.set("tag", tag);
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
  const formatJob = state.formatJob || {};
  const formatActive = ["preparing", "ready"].includes(formatJob.status);
  statsEl.innerHTML = `
    <span><b>${state.filters.total}</b> 张贴纸</span>
    ${overview.missing ? `<span><b>${overview.missing}</b> 个文件丢失</span>` : ""}
    ${formatActive ? `<span class="format-badge">旧库格式化进行中：${escapeHtml(formatJob.status)}（${formatJob.processed || 0}/${formatJob.total || 0}）</span>` : ""}
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
        ${asset.exists ? `<img alt="${escapeHtml(asset.meme_def)}" loading="lazy" data-thumb-src="${escapeHtml(asset.thumbnail_endpoint || "")}" />` : `<span class="missing-text">文件缺失</span>`}
      </div>
      <div class="sticker-meta">
        <b title="${escapeHtml(asset.meme_def)}">:${escapeHtml(asset.meme_def)}:</b>
        <small>${escapeHtml((asset.tags || []).join(" · "))} · ${escapeHtml(asset.usage_count)} 次</small>
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

function hydrateImages(root = document) {
  // 列表缩略图与详情原图都直接赋 endpoint，浏览器原生 HTTP 缓存，无需 base64。
  root.querySelectorAll("img[data-thumb-src]").forEach((img) => {
    if (!img.src && img.dataset.thumbSrc) img.src = img.dataset.thumbSrc;
  });
  root.querySelectorAll("img[data-image-src]").forEach((img) => {
    if (!img.src && img.dataset.imageSrc) img.src = img.dataset.imageSrc;
  });
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
      ${asset.exists ? `<img alt="${escapeHtml(asset.meme_def)}" data-image-src="${escapeHtml(asset.image_endpoint || "")}" />` : `<span class="missing-text">文件缺失</span>`}
    </div>
    <form id="detailForm" class="detail-form">
      <label><span>meme_def（唯一名称）</span><input name="meme_def" value="${escapeHtml(asset.meme_def)}" required /></label>
      <label><span>tags（逗号分隔）</span><input name="tags" value="${escapeHtml((asset.tags || []).join(", "))}" required /></label>
      <label><span>描述</span><textarea name="description" required>${escapeHtml(asset.description || "")}</textarea></label>
      <label><span>来源</span><input name="source" value="${escapeHtml(asset.source || "")}" /></label>
      <label><span>发送标记</span><code>:${escapeHtml(asset.meme_def)}:</code></label>
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
    ...providers.map(p => `<option value="${escapeHtml(p.id)}" ${c.meme_review_provider_id === p.id ? "selected" : ""}>${escapeHtml(p.name || p.id)}</option>`)
  ].join("");

  body.innerHTML = `
    <label>
      <span>审查 LLM Provider</span>
      <select name="meme_review_provider_id">${providerOptions}</select>
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
        <span>注入 meme_def 数量上限</span>
        <input name="max_prompt_meme_defs" type="number" min="0" max="200" value="${escapeHtml(c.max_prompt_meme_defs ?? 30)}" />
      </label>
      <label>
        <span>注入 tag 数量上限</span>
        <input name="max_prompt_tags" type="number" min="0" max="200" value="${escapeHtml(c.max_prompt_tags ?? 30)}" />
      </label>
    </div>
    <div class="form-grid">
      <label>
        <span>清理间隔（小时）</span>
        <input name="cleanup_interval_hours" type="number" min="1" value="${escapeHtml(c.cleanup_interval_hours ?? 1)}" />
      </label>
      <label>
        <span>每次清理数量</span>
        <input name="cleanup_count" type="number" min="1" value="${escapeHtml(c.cleanup_count ?? 5)}" />
      </label>
    </div>
    <div class="form-grid">
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
    meme_review_provider_id: String(fd.get("meme_review_provider_id") || ""),
    review_system_prompt: String(fd.get("review_system_prompt") || ""),
    max_stickers: parseInt(fd.get("max_stickers"), 10) || 0,
    max_stickers_per_message: parseInt(fd.get("max_stickers_per_message"), 10) || 0,
    max_prompt_meme_defs: parseInt(fd.get("max_prompt_meme_defs"), 10) || 0,
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

// ---------- 格式化旧库 ----------

function stopFormatPolling() {
  if (state.formatTimer) {
    clearInterval(state.formatTimer);
    state.formatTimer = null;
  }
}

async function refreshFormatStatus(render = true) {
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/status");
  } catch (err) {
    state.formatJob = { status: "error", error: err.message };
  }
  if (render) renderFormatBody();
}

async function openFormatModal() {
  const modal = $("#formatModal");
  if (!modal) return;
  modal.hidden = false;
  $("#formatBody").innerHTML = `<div class="empty">加载中...</div>`;
  await refreshFormatStatus();
}

function closeFormatModal() {
  stopFormatPolling();
  $("#formatModal").hidden = true;
}

function formatItemStatusBadge(item) {
  if (item.committed) return `<span class="badge done">已入库</span>`;
  if (item.status === "success") return `<span class="badge ok">成功</span>`;
  if (item.status === "duplicate") return `<span class="badge dup">重复</span>`;
  return `<span class="badge fail">失败</span>`;
}

function renderFormatBody() {
  const body = $("#formatBody");
  if (!body) return;
  const job = state.formatJob || { status: "idle" };
  const status = job.status || "idle";

  if (status === "idle" || status === "committed" || status === "cancelled") {
    const note = status === "committed"
      ? `<p class="format-note ok-text">上一次任务已提交完成。</p>`
      : status === "cancelled"
        ? `<p class="format-note">上一次任务已取消。</p>`
        : "";
    body.innerHTML = `
      <div class="format-intro">
        <p>使用视觉模型重新识别图片，为每张图生成新的 <b>meme_def</b>、描述和 tags 并合并入库。支持断点续传与识别中部分提交。</p>
        ${note}
        <div class="format-source">
          <div class="gtitle">选择图片来源</div>
          <label class="source-option">
            <input type="radio" name="format_source" value="legacy" checked />
            <span><b>本插件旧库</b><small>迁移 stickers.sqlite3 中的旧表情包，成功后删除旧库</small></span>
          </label>
          <label class="source-option">
            <input type="radio" name="format_source" value="plugin_scan" />
            <span><b>其他插件目录</b><small>扫描 data/plugin_data 下其他插件的图片并导入，源图保留</small></span>
          </label>
          <div id="pluginDirPicker" class="plugin-dir-picker" hidden>
            <div class="gtitle" style="margin-top:8px">选择插件目录</div>
            <div id="pluginDirList" class="plugin-dir-list"><div class="empty">加载中...</div></div>
          </div>
        </div>
        <button id="startFormatBtn" type="button" class="primary">开始</button>
      </div>
    `;
    stopFormatPolling();
    // 绑定来源切换
    $$('input[name="format_source"]', body).forEach((radio) => {
      radio.addEventListener("change", async (e) => {
        const picker = $("#pluginDirPicker");
        if (e.target.value === "plugin_scan") {
          picker.hidden = false;
          await loadPluginDirs();
        } else {
          picker.hidden = true;
        }
      });
    });
    return;
  }

  if (status === "failed" || status === "error") {
    body.innerHTML = `
      <div class="format-intro">
        <p class="format-note fail-text">格式化分析失败：${escapeHtml(job.error || "未知错误")}</p>
        <div class="modal-actions" style="padding:0;border:none;">
          <button id="cancelFormatBtn" type="button" class="ghost" data-job-id="${escapeHtml(job.job_id || "")}">清理任务</button>
          <button id="resumeFormatBtn" type="button" class="primary">重试识别</button>
        </div>
      </div>
    `;
    stopFormatPolling();
    return;
  }

  const processed = job.processed || 0;
  const total = job.total || 0;
  const succeeded = job.succeeded || 0;
  const failed = job.failed || 0;
  const pendingCommit = job.pending_commit || 0;
  const items = job.items || [];
  const progressPct = total ? Math.round((processed / total) * 100) : 0;

  const rows = items.map((item) => `
    <tr class="${item.committed ? "row-committed" : ""}">
      <td>${formatItemStatusBadge(item)}</td>
      <td><code>${escapeHtml(item.old_storage_key || "")}</code></td>
      <td>${item.meme_def ? `<b>:${escapeHtml(item.meme_def)}:</b>` : "-"}</td>
      <td>${escapeHtml((item.tags || []).join(", ") || "-")}</td>
      <td class="desc-cell" title="${escapeHtml(item.description || item.reason || "")}">${escapeHtml(item.description || item.reason || "")}</td>
    </tr>
  `).join("");

  let actionBar = "";
  const isScan = job.source === "plugin_scan";
  if (status === "ready") {
    const confirmText = isScan
      ? `确认导入？仅成功项合并入库，源图片保留不动。`
      : `⚠️ 提交后：仅成功项进入新库；<b>${failed} 个失败项将被永久删除</b>（随旧库一起清除），且不可恢复。`;
    const confirmBtn = isScan ? "确认导入成功项" : "确认提交（删除失败项）";
    actionBar = `
      <div class="format-confirm">
        <p class="${isScan ? "format-note" : "format-warning"}">${confirmText}</p>
        <div class="modal-actions" style="padding:0;border:none;">
          <button id="cancelFormatBtn" type="button" class="ghost" data-job-id="${escapeHtml(job.job_id)}">取消任务</button>
          <button id="commitFormatBtn" type="button" class="primary ${isScan ? "" : "danger"}" data-job-id="${escapeHtml(job.job_id)}" data-source="${escapeHtml(job.source || "legacy")}">${confirmBtn}</button>
        </div>
      </div>
    `;
  } else {
    // preparing：识别进行中，可部分提交或继续
    actionBar = `
      <div class="format-confirm">
        <p class="format-note">正在逐张识图。已识别成功的 <b>${pendingCommit}</b> 项可先部分提交入库，剩余项继续识别。</p>
        <div class="modal-actions" style="padding:0;border:none;">
          <button id="cancelFormatBtn" type="button" class="ghost" data-job-id="${escapeHtml(job.job_id)}">取消任务</button>
          <button id="partialCommitFormatBtn" type="button" class="primary" data-job-id="${escapeHtml(job.job_id)}" ${pendingCommit ? "" : "disabled"}>部分提交已成功的 ${pendingCommit} 项</button>
        </div>
      </div>
    `;
  }

  body.innerHTML = `
    <div class="format-summary">
      <span>旧资产总数 <b>${total}</b></span>
      <span>已处理 <b>${processed}</b>（${progressPct}%）</span>
      <span class="ok-text">识图成功 <b>${succeeded}</b></span>
      <span class="fail-text">识图失败 <b>${failed}</b></span>
      <span>待提交 <b>${pendingCommit}</b></span>
    </div>
    <div class="format-progress"><div class="format-progress-bar" style="width:${progressPct}%"></div></div>
    <div class="format-table-wrap">
      <table class="format-table">
        <thead><tr><th>结果</th><th>旧文件</th><th>新 meme_def</th><th>新 tags</th><th>新描述 / 失败原因</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="empty">暂无条目</td></tr>`}</tbody>
      </table>
    </div>
    ${actionBar}
  `;

  if (status === "preparing") {
    if (!state.formatTimer) {
      state.formatTimer = setInterval(() => refreshFormatStatus(), 1500);
    }
  } else {
    stopFormatPolling();
  }
}

async function loadPluginDirs() {
  const list = $("#pluginDirList");
  if (!list) return;
  list.innerHTML = `<div class="empty">加载中...</div>`;
  try {
    const result = await fetchJson("/maintenance/format_old_library/plugin_dirs");
    const items = result.items || [];
    if (!items.length) {
      list.innerHTML = `<div class="empty">data/plugin_data 下没有找到含图片的其他插件目录</div>`;
      return;
    }
    list.innerHTML = items.map((d, i) => `
      <label class="plugin-dir-item">
        <input type="radio" name="plugin_dir" value="${escapeHtml(d.name)}" ${i === 0 ? "checked" : ""} />
        <span class="dir-name">${escapeHtml(d.name)}</span>
        <span class="dir-count">${escapeHtml(d.image_count)} 张图片</span>
      </label>
    `).join("");
  } catch (err) {
    list.innerHTML = `<div class="empty">${escapeHtml(err.message || "加载失败")}</div>`;
  }
}

async function startFormatJob() {
  const source = (document.querySelector('input[name="format_source"]:checked') || {}).value || "legacy";
  let pluginDir = null;
  if (source === "plugin_scan") {
    pluginDir = (document.querySelector('input[name="plugin_dir"]:checked') || {}).value;
    if (!pluginDir) {
      showToast("请先选择一个插件目录", "error");
      return;
    }
  }
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/prepare", {
      method: "POST",
      body: { source, plugin_dir: pluginDir },
    });
    renderFormatBody();
  } catch (err) {
    showToast(err.message || "无法启动", "error");
    await refreshFormatStatus();
  }
}

async function commitFormatJob(jobId) {
  const job = state.formatJob || {};
  const failed = job.failed || 0;
  const isScan = job.source === "plugin_scan";
  const ok = await customConfirm(
    isScan
      ? "确认导入识图成功的图片？成功项将合并入库，源图片保留不动。"
      : `确认提交格式化结果？成功项进入新库，${failed} 个失败项将随旧库被永久删除！`
  );
  if (!ok) return;
  const btn = $("#commitFormatBtn");
  if (btn) btn.disabled = true;
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/commit", {
      method: "POST",
      body: { job_id: jobId, confirm: true, discard_failed: true },
    });
    showToast(isScan ? "已导入成功项" : "格式化已提交，旧库已清除");
    closeFormatModal();
    await reloadAfterMutation("");
  } catch (err) {
    showToast(err.message || "提交失败", "error");
    await refreshFormatStatus();
  }
}

async function partialCommitFormatJob(jobId) {
  const job = state.formatJob || {};
  const pending = job.pending_commit || 0;
  if (!pending) {
    showToast("暂无可提交的成功项", "error");
    return;
  }
  const ok = await customConfirm(`先把已识别成功的 ${pending} 项提交入库？剩余项将继续识别，旧库在全部完成后才删除。`);
  if (!ok) return;
  const btn = $("#partialCommitFormatBtn");
  if (btn) btn.disabled = true;
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/commit", {
      method: "POST",
      body: { job_id: jobId, confirm: true, discard_failed: true, partial: true },
    });
    showToast(`已部分提交 ${pending} 项入库`);
    renderFormatBody();
    await loadOverview();
  } catch (err) {
    showToast(err.message || "部分提交失败", "error");
    await refreshFormatStatus();
  }
}

async function resumeFormatJob() {
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/resume", { method: "POST", body: {} });
    showToast("已继续识别");
    renderFormatBody();
  } catch (err) {
    showToast(err.message || "无法继续识别", "error");
    await refreshFormatStatus();
  }
}

async function cancelFormatJob(jobId) {
  const ok = await customConfirm("取消并清理本次格式化任务？staging 临时文件将被删除。");
  if (!ok) return;
  try {
    state.formatJob = await fetchJson("/maintenance/format_old_library/cancel", {
      method: "POST",
      body: { job_id: jobId },
    });
    showToast("格式化任务已取消");
    await refreshFormatStatus();
  } catch (err) {
    showToast(err.message || "取消失败", "error");
    await refreshFormatStatus();
  }
}

// ---------- 全局事件 ----------

// 文档视图切换
let docsMode = false;
function toggleDocsView(on) {
  docsMode = on;
  const main = document.querySelector(".shell");
  ["#stats", ".filter-panel", "#mainView"].forEach((sel) => {
    const el = document.querySelector(sel);
    if (el) el.hidden = on;
  });
  const docs = document.querySelector("#docsView");
  if (docs) docs.hidden = !on;
  const btn = document.querySelector("#docsViewBtn");
  if (btn) {
    btn.textContent = on ? "返回" : "文档";
    btn.classList.toggle("primary", on);
    btn.classList.toggle("ghost", !on);
  }
  // 文档模式下隐藏其他操作按钮
  ["#refreshBtn", "#selectModeBtn", "#openFormatBtn", "#openSettingsBtn", "#openImportBtn"].forEach((sel) => {
    const el = document.querySelector(sel);
    if (el) el.hidden = on;
  });
}

document.addEventListener("click", async (event) => {
  const target = event.target;

  if (target.id === "docsViewBtn") {
    toggleDocsView(!docsMode);
    return;
  }

  if (target.id === "closeDetailBtn") {
    $("#detailDrawer").setAttribute("aria-hidden", "true");
    return;
  }

  if (target.id === "openImportBtn") {
    $("#importModal").hidden = false;
    return;
  }
  if (target.id === "closeImportBtn" || target.matches("[data-close-import]")) {
    $("#importModal").hidden = true;
    return;
  }

  if (target.id === "openSettingsBtn") {
    await openSettings();
    return;
  }
  if (target.id === "closeSettingsBtn" || target.matches("[data-close-settings]")) {
    $("#settingsModal").hidden = true;
    return;
  }

  if (target.id === "openFormatBtn") {
    await openFormatModal();
    return;
  }
  if (target.id === "closeFormatBtn" || target.matches("[data-close-format]")) {
    closeFormatModal();
    await loadOverview();
    renderStats();
    return;
  }
  if (target.id === "startFormatBtn") {
    await startFormatJob();
    return;
  }
  if (target.id === "commitFormatBtn") {
    await commitFormatJob(target.dataset.jobId || "");
    return;
  }
  if (target.id === "partialCommitFormatBtn") {
    await partialCommitFormatJob(target.dataset.jobId || "");
    return;
  }
  if (target.id === "resumeFormatBtn") {
    await resumeFormatJob();
    return;
  }
  if (target.id === "cancelFormatBtn") {
    await cancelFormatJob(target.dataset.jobId || "");
    return;
  }

  if (target.id === "selectModeBtn") {
    toggleSelectMode(true);
    return;
  }
  if (target.id === "cancelSelectBtn") {
    toggleSelectMode(false);
    return;
  }

  const openTarget = target.closest?.("[data-open-asset]");
  if (openTarget) {
    const assetId = openTarget.dataset.openAsset;
    if (state.isSelectMode) {
      if (state.selectedIds.has(assetId)) {
        state.selectedIds.delete(assetId);
      } else {
        state.selectedIds.add(assetId);
      }
      renderStickerWall();
      renderSelectionBar();
    } else {
      await selectAssetForEdit(assetId);
    }
    return;
  }

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

document.addEventListener("change", async (event) => {
  const target = event.target;
  if (target.matches("[data-select-asset]")) {
    if (target.checked) state.selectedIds.add(target.dataset.selectAsset);
    else state.selectedIds.delete(target.dataset.selectAsset);
    renderSelectionBar();

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

$("#searchInput")?.addEventListener("input", debounce(async (event) => {
  state.filters.q = event.target.value.trim();
  state.filters.page = 1;
  await loadStickers();
  renderAll();
}, 220));

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

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

$("#importForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;

  try {
    const file = form.elements.file.files[0];
    const memeDef = formValue(form, "meme_def");
    const tags = formValue(form, "tags");
    const description = formValue(form, "description");
    let result;

    if (file) {
      const data_url = await readFileAsDataURL(file);
      result = await fetchJson("/sticker/upload", {
        method: "POST",
        body: { data_url, meme_def: memeDef, tags, description },
      });
    } else {
      result = await fetchJson("/sticker/import", {
        method: "POST",
        body: {
          image_source: formValue(form, "image_source"),
          meme_def: memeDef,
          tags,
          description,
        },
      });
    }

    form.reset();
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
        meme_def: formValue(form, "meme_def"),
        tags: formValue(form, "tags"),
        description: formValue(form, "description"),
        source: formValue(form, "source"),
      },
    });
    state.selectedAsset = result;
    await reloadAfterMutation("修改已保存");
  } catch (err) {
    showToast(err.message || "保存失败", "error");
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

loadAll().catch((error) => {
  showToast(error.message || "加载失败", "error");
  const wall = $("#stickerWall");
  if (wall) wall.innerHTML = `<div class="empty">${escapeHtml(error.message || "加载失败")}</div>`;
});
