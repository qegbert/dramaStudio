/**
 * DramaForge Studio - 导演工作台前端核心控制器 (appMain.js)
 * 采用「4步傻瓜式向导 + 三栏直观故事板 + 折叠高级参数 + 人类可读任务队列」交互设计
 */

let currentState = {
  projects: [],
  activeProjectId: null,
  activeEpisodeId: null,
  activeShotId: null,
  workspace: null,
  assetFilter: "all",
  shotFilter: "all",
  shotSearchKeyword: "",
  settings: {},
  pollTimer: null,
  saveIndicatorTimer: null,
};

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(message, type = "info") {
  const stack = document.getElementById("toastStack");
  if (!stack) return;
  const item = document.createElement("div");
  item.className = `toastItem ${type}`;
  const icon = type === "success" ? "✅" : type === "error" ? "❌" : "🎬";
  item.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
  stack.appendChild(item);
  setTimeout(() => {
    item.style.opacity = "0";
    item.style.transform = "translateY(8px)";
    item.style.transition = "all 0.2s ease";
    setTimeout(() => item.remove(), 220);
  }, 3400);
}

function flashSaveIndicator(text = "✓ 已自动保存") {
  const el = document.getElementById("saveIndicator");
  if (!el) return;
  el.textContent = text;
  el.classList.add("visible");
  if (currentState.saveIndicatorTimer) {
    clearTimeout(currentState.saveIndicatorTimer);
  }
  currentState.saveIndicatorTimer = setTimeout(() => {
    el.classList.remove("visible");
  }, 1800);
}

function openDialog(dialogId) {
  const dialog = document.getElementById(dialogId);
  if (!dialog) return;
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) {
      dialog.showModal();
    }
  } else {
    dialog.setAttribute("open", "");
  }
}

function closeModal(dialogId) {
  const dialog = document.getElementById(dialogId);
  if (!dialog) return;
  if (typeof dialog.close === "function" && dialog.open) {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

function initDialogLightDismissFallback() {
  if (!("closedBy" in HTMLDialogElement.prototype)) {
    document.querySelectorAll('dialog[closedby="any"]').forEach((dialog) => {
      dialog.addEventListener("click", (e) => {
        const rect = dialog.getBoundingClientRect();
        const inDialog =
          e.clientX >= rect.left &&
          e.clientX <= rect.right &&
          e.clientY >= rect.top &&
          e.clientY <= rect.bottom;
        if (!inDialog) {
          dialog.close();
        }
      });
    });
  }
}

function openLightbox(imageUrl, captionText = "画面细节审片") {
  if (!imageUrl) return;
  const img = document.getElementById("lightboxImage");
  const cap = document.getElementById("lightboxCaption");
  if (img) img.src = imageUrl;
  if (cap) cap.textContent = captionText;
  openDialog("lightboxModal");
}

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json();
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || `请求失败 (${resp.status})`);
  }
  return data;
}

async function initApp() {
  initDialogLightDismissFallback();
  try {
    await refreshStatus();
    await loadProjects();
    currentState.pollTimer = setInterval(pollRunningTasks, 2500);
  } catch (err) {
    console.error("初始化失败:", err);
    showToast(`工作台初始化异常: ${err.message}`, "error");
  }
}

async function refreshStatus() {
  const data = await apiFetch("/api/status");
  currentState.settings = data.settings || {};
  const dot = document.getElementById("comfyStatusDot");
  const txt = document.getElementById("comfyStatusText");
  if (data.comfy && data.comfy.online) {
    dot.className = "statusDot online";
    txt.textContent = `无影云端在线 (${data.comfy.gpu || "RTX 5880 Ada"})`;
  } else {
    dot.className = "statusDot offline";
    txt.textContent = `无影云未连接 (当前为本地预演模式)`;
  }
}

async function loadProjects(preferredId = null) {
  const data = await apiFetch("/api/projects");
  currentState.projects = data.projects || [];
  const sel = document.getElementById("projectSelect");
  if (currentState.projects.length === 0) {
    currentState.activeProjectId = null;
    currentState.activeEpisodeId = null;
    currentState.workspace = null;
    sel.innerHTML = `<option value="">暂无项目，请点击右上角新建短剧</option>`;
    document.getElementById("episodeSelect").innerHTML = `<option value="">暂无剧集</option>`;
    document.getElementById("assetCountLabel").textContent = "0 项";
    document.getElementById("assetList").innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:12px 0;">新建短剧并拆解剧本后，角色/场景/道具将自动入库</div>`;
    document.getElementById("filmstripRibbon").style.display = "none";
    document.getElementById("masterBanner").style.display = "none";
    document.getElementById("sceneLedgerPanel").style.display = "none";
    updateWorkflowStepper([], null);
    document.getElementById("shotListContainer").innerHTML = `
      <div style="background:var(--bg-panel); border:1px dashed var(--border-subtle); border-radius:12px; padding:48px 24px; text-align:center; color:var(--text-secondary);">
        <div style="font-size:16px; font-weight:700; color:var(--text-primary); margin-bottom:8px;">🎬 工作台数据已清空，准备就绪</div>
        <div style="margin-bottom:16px;">请点击右上角或下方按钮创建一个新的短剧项目开始验证全流程。</div>
        <button class="btn btnPrimary" onclick="openNewProjectModal()">➕ 立即新建短剧项目</button>
      </div>
    `;
    document.getElementById("taskListContainer").innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:12px 0;">暂无生成任务</div>`;
    return;
  }
  sel.innerHTML = currentState.projects
    .map(
      (p) =>
        `<option value="${p.id}">${escapeHtml(p.title)} (${p.shotCount} 镜 · ${escapeHtml(p.aspectRatio)})</option>`
    )
    .join("");

  const targetId = preferredId || currentState.activeProjectId || currentState.projects[0].id;
  sel.value = String(targetId);
  await loadWorkspace(targetId);
}

async function onProjectChange(projectId) {
  if (!projectId) return;
  await loadWorkspace(Number(projectId));
}

async function onEpisodeChange(episodeId) {
  if (!episodeId || !currentState.activeProjectId) return;
  await loadWorkspace(currentState.activeProjectId, Number(episodeId));
}

async function loadWorkspace(projectId, episodeId = null) {
  currentState.activeProjectId = Number(projectId);
  const query = episodeId ? `?episodeId=${episodeId}` : "";
  const ws = await apiFetch(`/api/projects/${projectId}/workspace${query}`);
  currentState.workspace = ws;
  currentState.activeEpisodeId = ws.activeEpisode ? ws.activeEpisode.id : null;

  renderEpisodeSelector(ws.episodes || [], currentState.activeEpisodeId);
  renderAssetList(ws.assets || []);
  updateWorkflowStepper(ws.shots || [], ws.activeEpisode, ws.assets || []);
  renderMasterBanner(ws.activeEpisode);
  renderSceneLedger(ws.activeEpisode);
  renderFilmstripRibbon(ws.shots || []);
  renderShotCards(ws.shots || [], ws.tasks || [], ws.assets || []);
  renderTaskList(ws.tasks || [], ws.shots || []);
  if (document.getElementById("assetBibleModal") && document.getElementById("assetBibleModal").open) {
    renderAssetBibleModal();
  }
}

/**
 * 更新顶部 5 步黄金流水线向导条，严格遵循：
 * ① 剧本改写与提取设定 -> ② 确定角色/场景/道具定妆 -> ③ 拆解分镜 -> ④ 生成各镜首尾画面 -> ⑤ 生成视频与合成
 */
function updateWorkflowStepper(shots, activeEpisode, assets = []) {
  const totalShots = shots.length;
  const totalAssets = assets.length;
  const imagedAssets = assets.filter((a) => Boolean(a.imageUrl)).length;
  const hasSceneLedger = Boolean(activeEpisode && activeEpisode.sceneLedger && activeEpisode.sceneLedger.length > 0);

  const kfReadyCount = shots.filter((s) => {
    if (s.shotCategory === "action_fl2va") {
      return Boolean(s.firstFrameUrl && s.lastFrameUrl);
    }
    return Boolean(s.firstFrameUrl);
  }).length;
  const vidReadyCount = shots.filter((s) => Boolean(s.videoUrl)).length;
  const hasMaster = Boolean(activeEpisode && activeEpisode.masterVideoUrl);

  const s1Text = document.getElementById("step1StatText");
  const s2Text = document.getElementById("step2StatText");
  const s3Text = document.getElementById("step3StatText");
  const s4Text = document.getElementById("step4StatText");
  const s5Text = document.getElementById("step5StatText");

  if (s1Text) {
    s1Text.textContent = hasSceneLedger
      ? `✅ 已建 ${activeEpisode.sceneLedger.length} 场剧本`
      : "待改写提取";
  }
  if (s2Text) {
    s2Text.textContent =
      totalAssets > 0
        ? `共 ${totalAssets} 项 (${imagedAssets} 已定妆)`
        : "待提取设定";
  }
  if (s3Text) {
    s3Text.textContent = totalShots > 0 ? `已拆解 ${totalShots} 镜` : "待拆解分镜";
  }
  if (s4Text) {
    s4Text.textContent = `${kfReadyCount} / ${totalShots} 画面就绪`;
  }
  if (s5Text) {
    s5Text.textContent = hasMaster
      ? "✅ 已合成整集"
      : `${vidReadyCount} / ${totalShots} 已出视频`;
  }

  let recommendedStep = 1;
  if (!hasSceneLedger && totalAssets === 0 && totalShots === 0) {
    recommendedStep = 1;
  } else if (totalAssets > 0 && totalShots === 0) {
    recommendedStep = 2;
  } else if (totalShots === 0) {
    recommendedStep = 3;
  } else if (kfReadyCount < totalShots) {
    recommendedStep = 4;
  } else {
    recommendedStep = 5;
  }

  [1, 2, 3, 4, 5].forEach((num) => {
    const card = document.getElementById(`stepCard${num}`);
    if (card) {
      card.classList.toggle("recommended", num === recommendedStep);
    }
  });
}

function renderSceneLedger(activeEpisode) {
  const panel = document.getElementById("sceneLedgerPanel");
  const grid = document.getElementById("sceneLedgerGrid");
  if (!panel || !grid) return;
  const ledger = (activeEpisode && activeEpisode.sceneLedger) || [];
  if (!ledger.length) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "block";
  grid.innerHTML = ledger
    .map((sc) => {
      const castHtml = (sc.allowedCharacters || [])
        .map((c) => `<span class="eraBadge">@${escapeHtml(c)}</span>`)
        .join(" ");
      const propsHtml = (sc.propLedger || [])
        .map(
          (p) =>
            `<span class="propFlowTag">🔧 <strong>@${escapeHtml(p.propName)}</strong>: ${escapeHtml(p.initialState)} → ${escapeHtml(p.transitionAction)} → ${escapeHtml(p.finalState)}</span>`
        )
        .join("");
      const beatCount = (sc.beats || []).length;
      return `
        <div class="sceneLedgerCard">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong style="font-size:13px; color:var(--text-primary);">
              [${escapeHtml(sc.sceneCode)}] ${escapeHtml(sc.sceneTitle || sc.locationName)}
            </strong>
            <span class="modeBadge action_fl2va">${escapeHtml(sc.eraTag || "当前时空")} · ${beatCount} 个节拍</span>
          </div>
          <div><strong style="color:var(--text-secondary);">本场出场角色：</strong>${castHtml || "无"}</div>
          <div style="color:var(--amber-gold); background:rgba(245,158,11,0.08); padding:6px 8px; border-radius:4px;">
            📐 <strong>人物左右站位固定：</strong>${escapeHtml(sc.spatialAxisCn || sc.spatialAxis || "")}
          </div>
          ${propsHtml ? `<div>${propsHtml}</div>` : ""}
        </div>
      `;
    })
    .join("");
}

function renderEpisodeSelector(episodes, activeId) {
  const sel = document.getElementById("episodeSelect");
  if (!episodes.length) {
    sel.innerHTML = `<option value="">暂无剧集</option>`;
    return;
  }
  sel.innerHTML = episodes
    .map(
      (ep) =>
        `<option value="${ep.id}" ${ep.id === activeId ? "selected" : ""}>第 ${ep.episodeNumber} 集：${escapeHtml(ep.title)}</option>`
    )
    .join("");
}

function setAssetFilter(filterType) {
  currentState.assetFilter = filterType;
  document.querySelectorAll(".filterTab").forEach((el) => {
    el.classList.toggle("active", el.getAttribute("data-type") === filterType);
  });
  if (currentState.workspace) {
    renderAssetList(currentState.workspace.assets || []);
  }
}

function renderAssetList(assets) {
  const filtered =
    currentState.assetFilter === "all"
      ? assets
      : assets.filter((a) => a.assetType === currentState.assetFilter);
  document.getElementById("assetCountLabel").textContent = `${filtered.length} 项`;
  const container = document.getElementById("assetList");
  if (!filtered.length) {
    container.innerHTML = `
      <div style="color:var(--text-muted); font-size:12px; padding:12px 0; display:flex; flex-direction:column; gap:8px;">
        <span>当前分类下暂无设定资产。</span>
        <button class="btn btnSmall" onclick="openAssetBibleModal()">➕ 打开定妆台手动新增或从剧本提取</button>
      </div>
    `;
    return;
  }
  container.innerHTML = filtered
    .map((a) => {
      const typeCn =
        a.assetType === "character" ? "角色" : a.assetType === "scene" ? "场景" : "道具";
      const thumbHtml = a.imageUrl
        ? `<img class="assetThumb" src="${escapeHtml(a.imageUrl)}" alt="${escapeHtml(a.name)}" onclick="openLightbox('${escapeHtml(a.imageUrl)}', '@${escapeHtml(a.name)} · 锁定定妆设定图')" style="cursor:zoom-in;" title="点击放大查看定妆照" />`
        : `<div class="assetThumb" onclick="triggerAssetImage(${a.id})" style="display:flex;align-items:center;justify-content:center;font-size:10.5px;color:var(--amber-gold);cursor:pointer;text-align:center;line-height:1.2;" title="点击生成定妆设定图">🎨生成<br/>定妆图</div>`;
      const eraBadge = a.eraTag
        ? `<span class="eraBadge">${escapeHtml(a.eraTag)}</span>`
        : "";
      return `
        <div class="assetItem">
          ${thumbHtml}
          <div style="min-width:0;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:4px;">
              <div class="assetName">@${escapeHtml(a.name)} ${eraBadge}</div>
              <div style="display:flex; gap:3px; flex-shrink:0;">
                <button class="btn btnSmall" style="padding:1px 5px; font-size:10px;" onclick="triggerAssetImage(${a.id})" title="生成或重绘定妆参考图">🎨</button>
                <button class="btn btnSmall" style="padding:1px 5px; font-size:10px;" onclick="openAssetBibleModal(${a.id})" title="编辑外貌衣着或场景设定">✏️</button>
              </div>
            </div>
            <div style="font-size:11px; color:var(--amber-gold);">${escapeHtml(a.roleTag || typeCn)}</div>
            <div class="assetDesc" title="${escapeHtml(a.visualPrompt)}">${escapeHtml(a.visualPrompt || "暂无外貌/环境描述，点击右上角 ✏️ 补充")}</div>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderMasterBanner(activeEpisode) {
  const banner = document.getElementById("masterBanner");
  if (!activeEpisode || !activeEpisode.masterVideoUrl) {
    banner.style.display = "none";
    return;
  }
  banner.style.display = "flex";
  const player = document.getElementById("masterVideoPlayer");
  if (player.getAttribute("src") !== activeEpisode.masterVideoUrl) {
    player.src = activeEpisode.masterVideoUrl;
  }
  document.getElementById("masterDownloadLink").href = activeEpisode.masterVideoUrl;
  document.getElementById("masterMetaText").textContent = `当前剧集《${activeEpisode.title}》已完成多镜头无缝拼接，可直接右侧播放审片或下载 MP4 成片。`;
}

function renderFilmstripRibbon(shots) {
  const ribbon = document.getElementById("filmstripRibbon");
  if (!ribbon) return;
  if (!shots || !shots.length) {
    ribbon.style.display = "none";
    return;
  }
  ribbon.style.display = "flex";
  ribbon.innerHTML = shots
    .map((s, idx) => {
      const isAction = s.shotCategory === "action_fl2va";
      const isActive = currentState.activeShotId === s.id;
      const firstThumb = s.firstFrameUrl
        ? `<img src="${escapeHtml(s.firstFrameUrl)}" alt="第${s.shotNumber}镜起点" />`
        : `<div class="emptyMini">无起点图</div>`;
      const lastThumb = s.lastFrameUrl
        ? `<img src="${escapeHtml(s.lastFrameUrl)}" alt="第${s.shotNumber}镜落点" />`
        : `<div class="emptyMini">${isAction ? "无落点图" : "单图驱动"}</div>`;
      const statusDot = s.videoUrl
        ? `<span style="color:var(--emerald-ok);" title="已生成视频">✅</span>`
        : `<span style="color:var(--text-muted);" title="待生成视频">○</span>`;
      const lockConnector =
        idx > 0
          ? `<div class="filmLockConnector ${Number(s.lockFromPrevEnd) === 1 ? "locked" : ""}" title="${
              Number(s.lockFromPrevEnd) === 1
                ? "已开启画面衔接：本镜起点直接沿用上一镜结尾画面"
                : "切换新机位"
            }">${Number(s.lockFromPrevEnd) === 1 ? "🔗" : "·"}</div>`
          : "";
      return `
        ${lockConnector}
        <div class="filmCell ${isActive ? "activeShot" : ""}" onclick="scrollToShot(${s.id})">
          <div class="filmCellHeader">
            <span>第 ${s.shotNumber} 镜</span>
            <span>${s.durationSec}s ${statusDot}</span>
          </div>
          <div class="filmThumbPair">
            ${firstThumb}
            ${lastThumb}
          </div>
          <div class="filmCellTitle" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
        </div>
      `;
    })
    .join("");
}

function scrollToShot(shotId) {
  currentState.activeShotId = shotId;
  if (currentState.workspace) {
    renderFilmstripRibbon(currentState.workspace.shots || []);
  }
  const card = document.getElementById(`shot-card-${shotId}`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.style.boxShadow = "0 0 0 2px var(--amber-gold)";
    setTimeout(() => {
      card.style.boxShadow = "";
    }, 1400);
  }
}

function setShotFilter(filterMode) {
  currentState.shotFilter = filterMode;
  document.querySelectorAll(".statChip[data-shot-filter]").forEach((chip) => {
    chip.classList.toggle("activeFilter", chip.getAttribute("data-shot-filter") === filterMode);
  });
  if (currentState.workspace) {
    renderShotCards(currentState.workspace.shots || [], currentState.workspace.tasks || [], currentState.workspace.assets || []);
  }
}

function onShotSearchInput(keyword) {
  currentState.shotSearchKeyword = (keyword || "").trim().toLowerCase();
  if (currentState.workspace) {
    renderShotCards(currentState.workspace.shots || [], currentState.workspace.tasks || [], currentState.workspace.assets || []);
  }
}

function filterShotsList(shots) {
  return shots.filter((s) => {
    if (currentState.shotFilter === "action_fl2va" && s.shotCategory !== "action_fl2va") {
      return false;
    }
    if (currentState.shotFilter === "dialogue_i2v" && s.shotCategory !== "dialogue_i2v") {
      return false;
    }
    if (currentState.shotFilter === "locked" && Number(s.lockFromPrevEnd) !== 1) {
      return false;
    }
    if (currentState.shotFilter === "completed" && !s.videoUrl) {
      return false;
    }
    if (currentState.shotSearchKeyword) {
      const kw = currentState.shotSearchKeyword;
      const dlgText = (s.dialogue || []).map((d) => `${d.speaker || ""}:${d.text || ""}`).join(" ");
      const haystack = `第${s.shotNumber}镜 #${s.shotNumber} ${s.title || ""} ${s.intentText || ""} ${s.actionArcPrompt || ""} ${dlgText}`.toLowerCase();
      if (!haystack.includes(kw)) {
        return false;
      }
    }
    return true;
  });
}

function renderShotCards(shots, tasks = [], assets = []) {
  document.getElementById("statTotalShots").textContent = shots.length;
  document.getElementById("statActionShots").textContent = shots.filter(
    (s) => s.shotCategory === "action_fl2va"
  ).length;
  document.getElementById("statDialogueShots").textContent = shots.filter(
    (s) => s.shotCategory === "dialogue_i2v"
  ).length;
  document.getElementById("statLockedShots").textContent = shots.filter(
    (s) => Number(s.lockFromPrevEnd) === 1
  ).length;
  const completedCountEl = document.getElementById("statCompletedShots");
  if (completedCountEl) {
    completedCountEl.textContent = shots.filter((s) => Boolean(s.videoUrl)).length;
  }

  const container = document.getElementById("shotListContainer");
  if (!shots.length) {
    const ep = currentState.workspace && currentState.workspace.activeEpisode;
    const rawNovel = (ep && (ep.novelContent || ep.scriptContent)) || "";
    const hasLedger = Boolean(ep && ep.sceneLedger && ep.sceneLedger.length > 0);
    if (hasLedger || assets.length > 0) {
      container.innerHTML = `
        <div style="background:var(--bg-panel); border:1px solid rgba(6,182,212,0.45); border-radius:12px; padding:32px 28px; display:flex; flex-direction:column; gap:14px;">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <div style="font-size:16px; font-weight:700; color:var(--cyan-monitor);">
              🎭 已提取 ${assets.length} 项角色 / 场景 / 道具设定，请先确认定妆设定后再拆解分镜
            </div>
            <span class="eraBadge">当前处于第 ② 步 ➔ 第 ③ 步</span>
          </div>
          <div style="font-size:13px; color:var(--text-secondary);">
            💡 <strong>为什么先确定角色、场景与道具？</strong> 先在定妆台锁死每个角色的脸型发型、服装颜色与场景光影，后续生成的每一镜画面都会强制沿用同一套设定 DNA，彻底杜绝人物变脸与换装！
          </div>
          <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center;">
            <button class="btn btnPrimary" onclick="openAssetBibleModal()">
              🎭 打开第 ② 步：核对角色 / 场景 / 道具设定并生成定妆图
            </button>
            <button class="btn btnCyan" onclick="triggerDirectBreakdownFromMain()">
              🎬 设定已确认，直接执行第 ③ 步：生成全集分镜表
            </button>
          </div>
        </div>
      `;
    } else if (rawNovel.trim().length > 0) {
      const previewSnippet = escapeHtml(rawNovel.trim().slice(0, 140)) + (rawNovel.trim().length > 140 ? "..." : "");
      container.innerHTML = `
        <div style="background:var(--bg-panel); border:1px solid rgba(245,158,11,0.45); border-radius:12px; padding:32px 28px; display:flex; flex-direction:column; gap:14px;">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <div style="font-size:16px; font-weight:700; color:var(--amber-gold);">
              📖 已录入本集小说原文（共 ${rawNovel.trim().length} 字），请先提取角色/场景设定
            </div>
            <span class="eraBadge">待执行第 ① 步 ➔ 第 ② 步</span>
          </div>
          <div style="background:var(--bg-void); border:1px solid var(--border-subtle); border-radius:8px; padding:12px; font-size:12.5px; color:var(--text-secondary);">
            <strong>内容摘要：</strong>${previewSnippet}
          </div>
          <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center;">
            <button class="btn btnPrimary" onclick="openScriptModal(true)">
              📜 第 ① 步：改写分场剧本 & 自动提取角色/场景/道具
            </button>
          </div>
        </div>
      `;
    } else {
      container.innerHTML = `
        <div style="background:var(--bg-panel); border:1px dashed var(--border-subtle); border-radius:12px; padding:42px 24px; text-align:center; color:var(--text-secondary);">
          <div style="font-size:15px; font-weight:700; color:var(--text-primary); margin-bottom:8px;">📜 当前剧集还没有录入小说或分镜</div>
          <div style="margin-bottom:16px;">请先点击第 ① 步粘贴小说原文提取角色/场景/道具，确认定妆后再拆解分镜。</div>
          <button class="btn btnPrimary" onclick="openScriptModal()">📜 打开第 ① 步：剧本改写与设定提取</button>
        </div>
      `;
    }
    return;
  }

  const visibleShots = filterShotsList(shots);
  if (!visibleShots.length) {
    container.innerHTML = `
      <div style="background:var(--bg-panel); border:1px dashed var(--border-subtle); border-radius:10px; padding:32px; text-align:center; color:var(--text-secondary);">
        当前筛选或搜索条件下没有匹配的镜头。<button class="btn btnSmall" style="margin-left:8px;" onclick="setShotFilter('all')">显示全部镜头</button>
      </div>
    `;
    return;
  }

  // 建立每个镜头当前最新任务的映射，以便在镜头卡片内部直接显示进度条或错误提示
  const latestTaskByShot = {};
  tasks.forEach((t) => {
    if (!latestTaskByShot[t.shotId]) {
      latestTaskByShot[t.shotId] = t;
    }
  });

  container.innerHTML = visibleShots
    .map((s) => {
      const isAction = s.shotCategory === "action_fl2va";
      const modeClass = isAction ? "actionMode" : "dialogueMode";
      const modeLabel = isAction
        ? "⚡ 动作镜（首尾两张图锁死动作）"
        : "💬 台词镜（单张起点图说话表演）";

      const latestTask = latestTaskByShot[s.id];
      const isRunning = latestTask && (latestTask.status === "running" || latestTask.status === "queued");
      const isFailed = latestTask && latestTask.status === "failed" && s.status === "failed";

      let inlineBannerHtml = "";
      if (isRunning) {
        inlineBannerHtml = `
          <div class="shotInlineStatusBanner">
            <div style="display:flex; justify-content:space-between; align-items:center; font-size:12px;">
              <strong style="color:var(--amber-gold);">⏳ ${escapeHtml(latestTask.phaseText || "正在云端排队或生成中...")}</strong>
              <span style="color:var(--text-secondary); font-size:11px;">完成后将自动刷新画面</span>
            </div>
            <progress class="studioProgress" aria-label="第 ${s.shotNumber} 镜生成进度"></progress>
          </div>
        `;
      } else if (isFailed) {
        inlineBannerHtml = `
          <div class="shotInlineStatusBanner failedBanner">
            <div style="display:flex; justify-content:space-between; align-items:center; font-size:12px;">
              <span style="color:var(--rose-err);">⚠️ <strong>上次生成未成功：</strong>${escapeHtml(latestTask.errorMessage || "网络超时或云端繁忙")}</span>
              <button class="btn btnSmall btnPrimary" onclick="triggerShotVideo(${s.id})">🔄 立即重新生成视频</button>
            </div>
          </div>
        `;
      }

      const firstImgHtml = s.firstFrameUrl
        ? `<img src="${escapeHtml(s.firstFrameUrl)}" alt="起点画面" onclick="openLightbox('${escapeHtml(s.firstFrameUrl)}', '第 ${s.shotNumber} 镜 [起点画面] · ${escapeHtml(s.title)}')" title="点击放大查看起点画面" />`
        : `<div class="emptyViewportHint">尚未生成起点画面<br/>点击右上角「生成画面」</div>`;

      const lastImgHtml = s.lastFrameUrl
        ? `<img src="${escapeHtml(s.lastFrameUrl)}" alt="结束画面" onclick="openLightbox('${escapeHtml(s.lastFrameUrl)}', '第 ${s.shotNumber} 镜 [结果画面] · ${escapeHtml(s.title)}')" title="点击放大查看结果画面" />`
        : `<div class="emptyViewportHint">${
            isAction
              ? "尚未生成动作结果画面<br/>有结果图可防止动作演一半卡住"
              : "台词镜只需起点画面即可驱动"
          }</div>`;

      const videoHtml = s.videoUrl
        ? `<video src="${escapeHtml(s.videoUrl)}" controls preload="metadata"></video>`
        : `<div class="emptyViewportHint">尚未生成本镜视频<br/>点击右上角「🎬 生成本镜视频」即可渲染</div>`;

      const dialogues = Array.isArray(s.dialogue) ? s.dialogue : [];
      const dialogueHtml = dialogues.length
        ? dialogues
            .map(
              (d) => `
              <div class="dialogueItem">
                <span class="speechKindTag ${d.kind === "internal" ? "internal" : ""}">${
                d.kind === "internal" ? "内心独白(闭嘴)" : d.kind === "offscreen" ? "画外音" : "开口说话"
              }</span>
                <strong>${escapeHtml(d.speaker || "角色")}：</strong>
                <span style="flex:1;">「${escapeHtml(d.text || "")}」</span>
              </div>
            `
            )
            .join("")
        : `<div style="font-size:12px; color:var(--text-muted);">本镜为纯动作/神态表演（无开口台词）</div>`;

      // 构建本镜绑定的角色/场景/道具胶囊
      const assetByName = {};
      (assets || []).forEach((a) => {
        assetByName[String(a.name || "").lstrip ? a.name : String(a.name || "").replace(/^@/, "").trim()] = a;
      });
      const refs = Array.isArray(s.references) ? s.references : [];
      const boundNames = [];
      refs.forEach((r) => {
        const cleanName = String((r && r.asset) || "").replace(/^@/, "").trim();
        if (cleanName && !boundNames.includes(cleanName)) {
          boundNames.push(cleanName);
        }
      });
      // 同时扫描标题和提示词中出现的项目资产名
      const scanText = `${s.title || ""} ${s.intentText || ""} ${s.firstFramePrompt || ""}`;
      (assets || []).forEach((a) => {
        if (a.name && !boundNames.includes(a.name) && scanText.includes(a.name)) {
          boundNames.push(a.name);
        }
      });

      const boundPillsHtml = boundNames.length
        ? boundNames
            .map((nm) => {
              const aObj = assetByName[nm];
              const thumb = aObj && aObj.imageUrl ? `<img src="${escapeHtml(aObj.imageUrl)}" alt="${escapeHtml(nm)}" />` : "🔒";
              const aId = aObj ? aObj.id : null;
              return `<span class="boundAssetPill" onclick="${aId ? `openAssetBibleModal(${aId})` : "openAssetBibleModal()"}" title="点击查看或微调 @${escapeHtml(nm)} 的锁定外貌设定">${thumb} @${escapeHtml(nm)}</span>`;
            })
            .join("")
        : `<span style="color:var(--text-muted);">未绑定特定资产</span>`;

      const statusBadge = s.videoUrl
        ? `<span class="eraBadge" style="background:rgba(16,185,129,0.2);color:var(--emerald-ok);">✅ 视频已生成 (${s.durationSec}s)</span>`
        : s.firstFrameUrl
        ? `<span class="eraBadge" style="background:rgba(6,182,212,0.18);color:var(--cyan-monitor);">🖼️ 画面已就绪，可生成视频</span>`
        : `<span style="font-size:11.5px;color:var(--text-muted);">待生成画面</span>`;

      return `
        <article class="shotCard ${modeClass} ${isRunning ? "runningCard" : ""}" id="shot-card-${s.id}">
          <!-- 卡片顶部栏：清晰的镜号、标题、衔接开关与两大核心按钮 -->
          <div class="shotHeader">
            <div class="shotMetaLeft">
              <span class="shotNumBadge">第 ${s.shotNumber} 镜</span>
              <input class="shotTitleInput" aria-label="镜头标题" value="${escapeHtml(s.title)}" onchange="saveShotField(${s.id}, {title: this.value})" title="点击可直接修改镜头标题" />
              <span class="modeBadge ${s.shotCategory}">${modeLabel}</span>
              ${
                s.shotNumber > 1
                  ? `<label class="frameLockLabel" title="勾选后，本镜的起点画面直接沿用上一镜的结尾画面，保证前后镜头人物位置与光线无缝衔接">
                      <input type="checkbox" class="visually-hidden" ${Number(s.lockFromPrevEnd) === 1 ? "checked" : ""} onchange="saveShotField(${s.id}, {lockFromPrevEnd: this.checked ? 1 : 0})" />
                      <span>🔗 承接上镜结尾画面</span>
                    </label>`
                  : ""
              }
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              ${statusBadge}
              <button class="btn btnSmall btnCyan" ${isRunning ? "disabled" : ""} onclick="triggerKeyframes(${s.id}, 'both')">
                🎨 ${s.firstFrameUrl ? "重画本镜首尾图" : "① 生成本镜画面"}
              </button>
              <button class="btn btnSmall btnPrimary" ${isRunning ? "disabled" : ""} onclick="triggerShotVideo(${s.id})">
                🎬 ${isRunning ? "正在排队生成..." : s.videoUrl ? "重新生成视频" : "② 生成本镜视频"}
              </button>
            </div>
          </div>

          ${inlineBannerHtml}

          <!-- 清爽三栏故事板：[1. 首尾关键帧画面] -> [2. 本镜演什么与台词] -> [3. 本镜视频成片] -->
          <div class="storyBoardGrid">
            <!-- 左栏：镜头画面（起点 -> 落点） -->
            <div class="boardPanel">
              <div class="slotHeader">
                <span>🖼️ 第一步：镜头画面（起点 ➔ 落点）</span>
                <span style="font-size:11px; color:var(--text-muted);">点击图片可放大</span>
              </div>
              <div class="keyframePairGrid ${isAction ? "" : "singleMode"}">
                <div class="keyframeBox">
                  <div class="keyframeRoleTag">
                    <span>起点画面 (0.0s)</span>
                    <button class="btn btnSmall" style="padding:2px 6px; font-size:10.5px;" ${isRunning ? "disabled" : ""} onclick="triggerKeyframes(${s.id}, 'first')">重画起点</button>
                  </div>
                  <div class="mediaViewport">${firstImgHtml}</div>
                </div>
                ${
                  isAction
                    ? `
                    <div class="keyframeBox">
                      <div class="keyframeRoleTag">
                        <span>动作结果画面 (${s.durationSec}s)</span>
                        <button class="btn btnSmall" style="padding:2px 6px; font-size:10.5px;" ${isRunning ? "disabled" : ""} onclick="triggerKeyframes(${s.id}, 'last')">重画结果</button>
                      </div>
                      <div class="mediaViewport">${lastImgHtml}</div>
                    </div>
                  `
                    : ""
                }
              </div>
            </div>

            <!-- 中栏：本镜剧情、动作与角色台词 -->
            <div class="boardPanel">
              <div class="slotHeader">
                <span>📖 第二步：本镜剧情动作 & 角色台词</span>
                <span style="font-family:var(--font-mono); font-size:11px; color:var(--amber-gold);">${s.durationSec}秒 · ${escapeHtml(s.cameraFraming)}</span>
              </div>
              <div class="shotBoundAssetsRow">
                <strong style="color:var(--cyan-monitor);">🔒 已绑定角色/场景设定：</strong>
                ${boundPillsHtml}
              </div>
              <div class="intentBanner">🎯 <strong>剧情看点：</strong>${escapeHtml(s.intentText || s.title || "推进本场核心剧情")}</div>
              <div class="storyActionReadonly" title="可在下方折叠抽屉中修改详细动作描述">
                🏃 <strong>画面动作：</strong>${escapeHtml(s.actionArcPrompt || "角色按剧情进行自然表演")}
              </div>
              <div class="dialogueTrackBox">
                <div style="font-size:11px; font-weight:700; color:var(--text-secondary);">🎙️ 角色台词与声音：</div>
                ${dialogueHtml}
              </div>
            </div>

            <!-- 右栏：本镜生成视频成片 -->
            <div class="boardPanel">
              <div class="slotHeader">
                <span>🎬 第三步：本镜视频成片预览</span>
                ${
                  s.videoUrl
                    ? `<button class="btn btnSmall" style="padding:2px 7px; font-size:10.5px;" onclick="extractLastFrameFromShot(${s.id})" title="从当前视频最后一帧截取高清图传给下一镜">📸 截取末帧传给下镜</button>`
                    : ""
                }
              </div>
              <div class="mediaViewport">${videoHtml}</div>
              <div style="font-size:11.5px; color:var(--text-secondary);">
                🔊 <strong>环境音效：</strong>${escapeHtml((s.soundscapePrompt || "自然环境声效").slice(0, 72))}
              </div>
            </div>
          </div>

          <!-- 底部原生 <details> 高级参数折叠抽屉：默认收起不打扰，想微调提示词时随时展开 -->
          <details class="shotAdvancedDrawer">
            <summary>
              <span>🛠️ 微调本镜提示词、切换镜头类型或更换视频模型（默认已由 AI 自动配好，无需修改）</span>
              <span>展开设置 ▾</span>
            </summary>
            <div class="advancedDrawerBody">
              <div class="formGroup">
                <label>起点画面提示词 (First Frame Prompt)：</label>
                <textarea class="promptTextarea" onchange="saveShotField(${s.id}, {firstFramePrompt: this.value})">${escapeHtml(s.firstFramePrompt)}</textarea>
              </div>
              <div class="formGroup">
                <label>中间动作过程描述 (Action Progression)：</label>
                <textarea class="promptTextarea" onchange="saveShotField(${s.id}, {actionArcPrompt: this.value})">${escapeHtml(s.actionArcPrompt)}</textarea>
              </div>
              <div class="formGroup">
                <label>结束结果画面提示词 (Last Frame Prompt)：</label>
                <textarea class="promptTextarea" onchange="saveShotField(${s.id}, {lastFramePrompt: this.value})">${escapeHtml(s.lastFramePrompt)}</textarea>
              </div>
              <div class="formGroup">
                <label>镜头模式（动作双图锁死 vs 台词单图说话）：</label>
                <select class="formSelect" onchange="saveShotField(${s.id}, {shotCategory: this.value})">
                  <option value="action_fl2va" ${isAction ? "selected" : ""}>⚡ 动作推进镜（首尾两张图锁死动作）</option>
                  <option value="dialogue_i2v" ${!isAction ? "selected" : ""}>💬 台词博弈镜（单张起点图说话）</option>
                </select>
              </div>
              <div class="formGroup">
                <label>底层视频渲染模型：</label>
                <select class="formSelect" onchange="saveShotField(${s.id}, {videoEngine: this.value})">
                  <option value="h3_fl2va" ${s.videoEngine === "h3_fl2va" ? "selected" : ""}>MiniMax-H3 FL2VA（首尾帧音画同步）</option>
                  <option value="wan22_moe" ${s.videoEngine === "wan22_moe" ? "selected" : ""}>Wan 2.2 14B MoE（双专家电影级图生视频）</option>
                </select>
              </div>
              <div class="formGroup">
                <label>人物站位轴线与机位运动：</label>
                <div style="display:flex; gap:6px;">
                  <input class="formInput" style="flex:1;" value="${escapeHtml(s.axisRule)}" onchange="saveShotField(${s.id}, {axisRule: this.value})" />
                  <button class="btn btnSmall" onclick="showCompiledPrompt(${s.id})">🔍 查看最终模型指令</button>
                </div>
              </div>
            </div>
          </details>
        </article>
      `;
    })
    .join("");
}

/**
 * 渲染右侧任务队列：使用「第 X 镜《镜头名》」+ 中文任务状态，点击直接跳转到对应镜头
 */
function renderTaskList(tasks, shots = []) {
  const container = document.getElementById("taskListContainer");
  if (!tasks.length) {
    container.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:12px 0;">当前没有正在运行的任务。点击上方「第 ② 步」或「第 ③ 步」即可开始生成。</div>`;
    return;
  }

  const shotMap = {};
  shots.forEach((s) => {
    shotMap[s.id] = s;
  });

  const taskTypeCnMap = {
    shot_video: "🎬 生成镜头视频",
    both_keyframe: "🎨 生成首尾画面",
    first_keyframe: "🖼️ 重画起点画面",
    last_keyframe: "🖼️ 重画结果画面",
    asset_image: "🎭 生成角色/场景定妆图",
  };

  const statusCnMap = {
    running: "⏳ 进行中",
    queued: "⏳ 排队中",
    succeeded: "✅ 已完成",
    failed: "❌ 失败",
  };

  container.innerHTML = tasks
    .map((t) => {
      const matchedShot = shotMap[t.shotId];
      const shotNum = t.shotNumber || (matchedShot ? matchedShot.shotNumber : t.shotId);
      const shotTitle = t.shotTitle || (matchedShot ? matchedShot.title : "");
      const titleDisplay =
        t.taskType === "asset_image"
          ? "🎭 角色/场景定妆设定"
          : shotTitle
          ? `第 ${shotNum} 镜《${escapeHtml(shotTitle)}》`
          : `第 ${shotNum} 镜`;
      const typeDisplay = taskTypeCnMap[t.taskType] || t.taskType;
      const statusDisplay = statusCnMap[t.status] || t.status;

      const badgeColor =
        t.status === "succeeded"
          ? "var(--emerald-ok)"
          : t.status === "failed"
          ? "var(--rose-err)"
          : "var(--amber-gold)";

      const isRunning = t.status === "running" || t.status === "queued";

      return `
        <div class="taskCard ${escapeHtml(t.status)}" onclick="${t.taskType === "asset_image" ? "openAssetBibleModal()" : `scrollToShot(${t.shotId})`}" title="点击查看详情">
          <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
            <strong style="color:var(--text-primary);">${titleDisplay}</strong>
            <span style="font-size:11.5px; font-weight:700; color:${badgeColor};">${statusDisplay}</span>
          </div>
          <div style="font-size:11.5px; color:var(--cyan-monitor);">${escapeHtml(typeDisplay)}</div>
          <div style="color:var(--text-secondary); font-size:11.5px;">${escapeHtml(t.phaseText || "")}</div>
          ${isRunning ? `<progress class="studioProgress" aria-label="${titleDisplay}生成进度"></progress>` : ""}
          ${
            t.errorMessage
              ? `<div style="color:var(--rose-err); font-size:11px; display:flex; justify-content:space-between; align-items:center; margin-top:2px;">
                  <span>原因：${escapeHtml(t.errorMessage)}</span>
                  <button class="btn btnSmall btnPrimary" style="padding:2px 7px; font-size:10.5px;" onclick="event.stopPropagation(); triggerShotVideo(${t.shotId})">重试</button>
                </div>`
              : ""
          }
        </div>
      `;
    })
    .join("");
}

async function pollRunningTasks() {
  if (!currentState.activeProjectId) return;
  try {
    const data = await apiFetch("/api/tasks");
    const tasks = (data.tasks || []).filter((t) => t.projectId === currentState.activeProjectId);
    const hadRunning =
      currentState.workspace &&
      (currentState.workspace.tasks || []).some((t) => t.status === "running" || t.status === "queued");
    const nowRunning = tasks.some((t) => t.status === "running" || t.status === "queued");

    if (hadRunning || nowRunning) {
      await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    } else {
      renderTaskList(tasks, (currentState.workspace && currentState.workspace.shots) || []);
    }
  } catch (err) {
    // 忽略静默轮询异常
  }
}

async function saveShotField(shotId, payload) {
  try {
    await apiFetch(`/api/shots/${shotId}/update`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    flashSaveIndicator("✓ 已自动保存");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`保存镜头修改失败: ${err.message}`, "error");
  }
}

async function triggerKeyframes(shotId, frameRole = "both") {
  try {
    await apiFetch(`/api/shots/${shotId}/generateKeyframes`, {
      method: "POST",
      body: JSON.stringify({ frameRole }),
    });
    showToast("已加入画面生成队列，后台正在生成...", "info");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`提交关键帧生成失败: ${err.message}`, "error");
  }
}

async function triggerShotVideo(shotId) {
  try {
    await apiFetch(`/api/shots/${shotId}/generateVideo`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast("已加入视频渲染队列，后台将按顺序自动生成...", "info");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`提交镜头视频渲染失败: ${err.message}`, "error");
  }
}

async function extractLastFrameFromShot(shotId) {
  try {
    await apiFetch(`/api/shots/${shotId}/extractLastFrame`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast("已截取本镜视频末尾帧并同步为下一镜的起点画面！", "success");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`提取视频末尾帧失败: ${err.message}`, "error");
  }
}

async function syncAllFrameLocks() {
  if (!currentState.activeEpisodeId) return;
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/syncFrameLocks`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    showToast(`已同步前后镜头衔接画面（共更新 ${res.syncedCount} 个镜头）`, "success");
  } catch (err) {
    showToast(`同步失败: ${err.message}`, "error");
  }
}

async function batchGenerateKeyframes(limit = 50) {
  if (!currentState.activeEpisodeId) return;
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/batchKeyframes`, {
      method: "POST",
      body: JSON.stringify({ limit, onlyMissing: true }),
    });
    if (res.count === 0) {
      showToast("本集所有镜头的首尾画面都已经生成完毕啦！可以直接点「第 ③ 步」生成视频", "success");
    } else {
      showToast(`已将 ${res.count} 个缺失画面的镜头加入排队生成队列！`, "info");
    }
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`批量生成画面失败: ${err.message}`, "error");
  }
}

async function batchGenerateAllVideos() {
  if (!currentState.activeEpisodeId) return;
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/batchVideos`, {
      method: "POST",
      body: JSON.stringify({ onlyPending: true }),
    });
    if (res.count === 0) {
      showToast("本集所有镜头视频均已生成完毕！可以直接点「第 ④ 步」合成整集短剧", "success");
    } else {
      showToast(`已将 ${res.count} 个未完成镜头按顺序加入视频渲染队列，后台将一镜一镜自动完成！`, "success");
    }
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`批量排队生成视频失败: ${err.message}`, "error");
  }
}

async function clearFailedTasks() {
  if (!currentState.activeProjectId) return;
  try {
    await apiFetch(`/api/projects/${currentState.activeProjectId}/clearFailedTasks`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast("已清理所有失败任务记录", "success");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`清理失败记录异常: ${err.message}`, "error");
  }
}

async function mergeMasterEpisode() {
  if (!currentState.activeEpisodeId) return;
  try {
    showToast("正在将本集各镜头无缝拼接为完整短剧 MP4...", "info");
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/mergeMaster`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    const tlLink = document.getElementById("timelineDownloadLink");
    if (tlLink && res.timelineUrl) {
      tlLink.href = res.timelineUrl;
    }
    showToast("🎉 整集完整短剧 MP4 已合成完毕！请在顶部播放器直接观看或下载", "success");
  } catch (err) {
    showToast(`合成整集短剧提示: ${err.message}`, "error");
  }
}

function showCompiledPrompt(shotId) {
  if (!currentState.workspace) return;
  const shot = (currentState.workspace.shots || []).find((s) => s.id === shotId);
  if (!shot) return;
  document.getElementById("compiledPromptPre").textContent = shot.compiledPreview || "";
  openDialog("promptPreviewModal");
}

async function copyCompiledPrompt() {
  const pre = document.getElementById("compiledPromptPre");
  if (!pre || !pre.textContent) return;
  try {
    await navigator.clipboard.writeText(pre.textContent);
    showToast("已复制指令到剪贴板", "success");
  } catch (err) {
    showToast("复制失败，请手动选中复制", "error");
  }
}

let scriptProgressTimer = null;

function setScriptButtonsBusy(isBusy, activeMode = "") {
  const b1 = document.getElementById("btnStep1Rewrite");
  const b2 = document.getElementById("btnStep2Breakdown");
  const bGo = document.getElementById("btnGoToAssetBible");
  if (b1) {
    b1.disabled = isBusy;
    b1.textContent =
      isBusy && activeMode === "rewrite"
        ? "⏳ AI 正在改写分场剧本并提取角色..."
        : "✨ 让 AI 自动改写剧本 & 提取角色场景";
  }
  if (bGo) {
    bGo.disabled = isBusy;
  }
  if (b2) {
    b2.disabled = isBusy;
    b2.textContent =
      isBusy && activeMode === "breakdown"
        ? "⏳ AI 正在生成分镜表..."
        : "🎬 直接按剧本生成分镜表";
  }
}

function updateScriptStagePills(activeStageIndex, allDone = false) {
  [1, 2, 3, 4].forEach((idx) => {
    const pill = document.getElementById(`sPill${idx}`);
    if (!pill) return;
    pill.classList.remove("activeStage", "doneStage");
    if (allDone || idx < activeStageIndex) {
      pill.classList.add("doneStage");
    } else if (idx === activeStageIndex) {
      pill.classList.add("activeStage");
    }
  });
}

function startScriptProgressTracker(stagesConfig) {
  const box = document.getElementById("scriptProgressBox");
  const icon = document.getElementById("scriptPhaseIcon");
  const title = document.getElementById("scriptPhaseTitle");
  const sub = document.getElementById("scriptPhaseSub");
  const timerEl = document.getElementById("scriptTimerText");
  const bar = document.getElementById("scriptProgressBar");

  if (!box) return;
  box.style.display = "flex";
  box.className = "scriptProgressBox";
  if (icon) icon.textContent = "⏳";

  const startTime = Date.now();
  if (scriptProgressTimer) clearInterval(scriptProgressTimer);

  const tick = () => {
    const elapsedSec = (Date.now() - startTime) / 1000;
    if (timerEl) timerEl.textContent = `已耗时 ${elapsedSec.toFixed(1)}s`;

    // 根据耗时匹配当前阶段说明
    let current = stagesConfig[0];
    for (const st of stagesConfig) {
      if (elapsedSec >= st.afterSec) {
        current = st;
      }
    }
    if (title) title.textContent = current.title;
    if (sub) sub.textContent = current.sub;
    if (bar) {
      const pct = Math.min(94, Math.round(current.basePct + elapsedSec * 2.2));
      bar.value = pct;
    }
    updateScriptStagePills(current.pillIndex, false);
  };

  tick();
  scriptProgressTimer = setInterval(tick, 200);
}

function finishScriptProgressTracker(success, titleText, subText, pillDoneUntil = 4) {
  if (scriptProgressTimer) {
    clearInterval(scriptProgressTimer);
    scriptProgressTimer = null;
  }
  const box = document.getElementById("scriptProgressBox");
  const icon = document.getElementById("scriptPhaseIcon");
  const title = document.getElementById("scriptPhaseTitle");
  const sub = document.getElementById("scriptPhaseSub");
  const bar = document.getElementById("scriptProgressBar");

  if (!box) return;
  box.style.display = "flex";
  box.className = `scriptProgressBox ${success ? "doneState" : "errorState"}`;
  if (icon) icon.textContent = success ? "✅" : "❌";
  if (title) {
    title.style.color = success ? "var(--emerald-ok)" : "var(--rose-err)";
    title.textContent = titleText;
  }
  if (sub) sub.textContent = subText;
  if (bar) bar.value = success ? 100 : 0;

  [1, 2, 3, 4].forEach((idx) => {
    const pill = document.getElementById(`sPill${idx}`);
    if (!pill) return;
    pill.classList.remove("activeStage", "doneStage");
    if (success && idx <= pillDoneUntil) {
      pill.classList.add("doneStage");
    }
  });
}

function openScriptModal(showGuideBanner = false) {
  if (!currentState.workspace || !currentState.workspace.activeEpisode) {
    showToast("请先选择或创建一个短剧项目", "error");
    return;
  }
  const ep = currentState.workspace.activeEpisode;
  const rawNovel = ep.novelContent || ep.scriptContent || "";
  document.getElementById("novelInputArea").value = rawNovel;
  document.getElementById("scriptInputArea").value = ep.scriptContent && ep.sceneLedger && ep.sceneLedger.length ? ep.scriptContent : "";
  document.getElementById("rewriteStatusBadge").textContent =
    ep.sceneLedger && ep.sceneLedger.length
      ? `✅ 已建立 ${ep.sceneLedger.length} 场调度账本`
      : "尚未生成分场剧本";

  const box = document.getElementById("scriptProgressBox");
  if (showGuideBanner && rawNovel.trim().length > 0 && box) {
    box.style.display = "flex";
    box.className = "scriptProgressBox";
    document.getElementById("scriptPhaseIcon").textContent = "👋";
    const title = document.getElementById("scriptPhaseTitle");
    title.style.color = "var(--amber-gold)";
    title.textContent = `已自动载入您录入的小说原文（共 ${rawNovel.trim().length} 字）`;
    document.getElementById("scriptPhaseSub").textContent =
      "请点击右下角「🚀 一键全自动：改写剧本 + 生成分镜表」，或先点左上方「✨ 第一步」审阅剧本。";
    document.getElementById("scriptTimerText").textContent = "准备就绪";
    document.getElementById("scriptProgressBar").value = 8;
    updateScriptStagePills(1, false);
  } else if (box && !scriptProgressTimer) {
    box.style.display = "none";
  }

  openDialog("scriptModal");
}

async function openScriptModalAndRunAuto() {
  openScriptModal(false);
  await submitScriptBreakdown(true);
}

async function submitNovelRewrite() {
  if (!currentState.activeEpisodeId) return;
  const novelText = document.getElementById("novelInputArea").value.trim();
  if (!novelText) {
    showToast("请先在左栏粘贴小说原文", "error");
    return;
  }
  const badge = document.getElementById("rewriteStatusBadge");
  badge.textContent = "⏳ 正在改写分场剧本并提取角色场景...";

  setScriptButtonsBusy(true, "rewrite");
  startScriptProgressTracker([
    {
      afterSec: 0,
      pillIndex: 1,
      basePct: 12,
      title: "正在执行阶段 ①：解析小说原文与心理描写视觉外化...",
      sub: "正在调用大模型通读小说段落，将抽象心理活动转化为镜头可拍的物理动作与神态细节",
    },
    {
      afterSec: 2.5,
      pillIndex: 2,
      basePct: 38,
      title: "正在执行阶段 ②：自动识别并提取本集角色、场景与关键道具...",
      sub: "自动区分角色不同时期（前世/今生）外貌锚点，防止后续视频渲染串台",
    },
    {
      afterSec: 5.5,
      pillIndex: 3,
      basePct: 65,
      title: "正在执行阶段 ③：建立 180° 空间站位轴线与标准分场剧本...",
      sub: "锁定每一场戏的人物左右站位方向与道具状态流转，生成右侧影视级分场剧本",
    },
  ]);

  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/rewriteScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText }),
    });
    document.getElementById("scriptInputArea").value = res.formattedScript || "";
    badge.textContent = `✅ 已生成 ${res.sceneCount} 场剧本，提取/更新 ${res.assetCountChanged} 项资产！`;
    finishScriptProgressTracker(
      true,
      `第一步完成：已生成 ${res.sceneCount} 场标准影视剧本，并自动提取 ${res.assetCountChanged} 项角色/场景/道具！`,
      "👉 强烈建议：请点击右下角「🎭 去第 ② 步：确认角色 / 场景 / 道具设定 ➔」，先核对并锁死人物外貌衣着、生成定妆图，再进入第 ③ 步拆解分镜！",
      3
    );
    showToast(`已提取 ${res.assetCountChanged} 项角色/场景/道具，请进入第 ② 步确认定妆设定！`, "success");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    badge.textContent = "❌ 改写失败";
    finishScriptProgressTracker(false, "小说改写分场剧本失败", err.message, 0);
    showToast(`小说改写失败: ${err.message}`, "error");
  } finally {
    setScriptButtonsBusy(false);
  }
}

/* ============================================================================
 * 第 ② 步：角色 / 场景 / 道具定妆与设定锁定工作台 (Asset Bible Studio)
 * ============================================================================ */
let modalAssetFilter = "all";

function openAssetBibleModal(focusAssetId = null) {
  if (!currentState.workspace) {
    showToast("请先选择或新建一个短剧项目", "error");
    return;
  }
  renderAssetBibleModal();
  openDialog("assetBibleModal");
  if (focusAssetId) {
    setTimeout(() => {
      const card = document.getElementById(`bible-card-${focusAssetId}`);
      if (card) {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.style.borderColor = "var(--amber-gold)";
      }
    }, 120);
  }
}

function setModalAssetFilter(filterType) {
  modalAssetFilter = filterType;
  document.querySelectorAll("[data-modal-asset-filter]").forEach((el) => {
    el.classList.toggle("activeFilter", el.getAttribute("data-modal-asset-filter") === filterType);
  });
  renderAssetBibleModal();
}

function renderAssetBibleModal() {
  const assets = (currentState.workspace && currentState.workspace.assets) || [];
  const totalEl = document.getElementById("modalAssetTotal");
  const charEl = document.getElementById("modalAssetCharCount");
  const sceneEl = document.getElementById("modalAssetSceneCount");
  const propEl = document.getElementById("modalAssetPropCount");

  if (totalEl) totalEl.textContent = assets.length;
  if (charEl) charEl.textContent = assets.filter((a) => a.assetType === "character").length;
  if (sceneEl) sceneEl.textContent = assets.filter((a) => a.assetType === "scene").length;
  if (propEl) propEl.textContent = assets.filter((a) => a.assetType === "prop").length;

  const grid = document.getElementById("assetBibleGrid");
  if (!grid) return;

  const filtered =
    modalAssetFilter === "all"
      ? assets
      : assets.filter((a) => a.assetType === modalAssetFilter);

  if (!filtered.length) {
    grid.innerHTML = `
      <div style="grid-column:1 / -1; background:var(--bg-card); border:1px dashed var(--border-subtle); border-radius:10px; padding:36px; text-align:center; color:var(--text-secondary);">
        <div style="font-size:14px; font-weight:700; color:var(--text-primary); margin-bottom:6px;">当前分类下还没有角色 / 场景 / 道具设定</div>
        <div style="margin-bottom:12px;">您可以先在「第 ① 步」从小说自动提取，或者直接点击右上方「➕ 新增角色 / 场景 / 道具」手动添加。</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = filtered
    .map((a) => {
      const typeLabel =
        a.assetType === "character" ? "👤 人物角色" : a.assetType === "scene" ? "🏞️ 场景环境" : "🔧 关键道具";
      const thumbHtml = a.imageUrl
        ? `<img src="${escapeHtml(a.imageUrl)}" alt="${escapeHtml(a.name)}" onclick="openLightbox('${escapeHtml(a.imageUrl)}', '@${escapeHtml(a.name)} · 定妆设定大图')" />`
        : `<div class="emptyViewportHint" style="font-size:11px;">尚未生成定妆图<br/>点击下方按钮生成</div>`;

      return `
        <div class="assetBibleCard" id="bible-card-${a.id}">
          <div class="assetBibleThumbCol">
            <div class="assetBibleViewport">${thumbHtml}</div>
            <button class="btn btnSmall btnCyan" style="width:100%;" onclick="triggerAssetImage(${a.id})">
              🎨 ${a.imageUrl ? "重画定妆图" : "生成定妆图"}
            </button>
          </div>
          <div style="display:flex; flex-direction:column; gap:7px; min-width:0;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
              <div style="display:flex; align-items:center; gap:6px; flex:1;">
                <span class="eraBadge">${typeLabel}</span>
                <input
                  class="formInput"
                  style="padding:4px 8px; font-weight:700; width:130px;"
                  value="${escapeHtml(a.name)}"
                  placeholder="名称"
                  onchange="saveAssetField(${a.id}, {name: this.value})"
                />
                <input
                  class="formInput"
                  style="padding:4px 8px; font-size:11.5px; width:115px;"
                  value="${escapeHtml(a.eraTag || "")}"
                  placeholder="时期(如:今生/前世)"
                  onchange="saveAssetField(${a.id}, {eraTag: this.value})"
                />
              </div>
              <button class="btn btnSmall btnDanger" style="padding:3px 7px;" onclick="deleteAssetItem(${a.id})" title="删除此项设定">🗑️</button>
            </div>
            <div>
              <input
                class="formInput"
                style="padding:4px 8px; font-size:12px; width:100%;"
                value="${escapeHtml(a.roleTag || "")}"
                placeholder="身份定位 / 场景氛围标签（如：男主·青衫剑客 / 暴雨夜荒庙）"
                onchange="saveAssetField(${a.id}, {roleTag: this.value})"
              />
            </div>
            <div class="formGroup">
              <label style="font-size:11px; color:var(--amber-gold);">
                🔒 锁定外貌衣着 / 场景光影 / 道具特征描述（后续所有分镜将强制注入此描述）：
              </label>
              <textarea
                class="promptTextarea"
                style="min-height:66px; font-size:12px;"
                placeholder="详细填写固定面部特征、发型发色、服装款式颜色材质，或场景的建筑陈设与主光源..."
                onchange="saveAssetField(${a.id}, {visualPrompt: this.value})"
              >${escapeHtml(a.visualPrompt || "")}</textarea>
            </div>
          </div>
        </div>
      `;
    })
    .join("");
}

async function saveAssetField(assetId, payload) {
  try {
    await apiFetch(`/api/assets/${assetId}/update`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    flashSaveIndicator("✓ 设定已锁定保存");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`保存设定失败: ${err.message}`, "error");
  }
}

async function createNewAssetItem(assetType = "character") {
  if (!currentState.activeProjectId) return;
  const defaultName =
    assetType === "character" ? "新角色" : assetType === "scene" ? "新场景" : "新道具";
  try {
    const res = await apiFetch("/api/assets/create", {
      method: "POST",
      body: JSON.stringify({
        projectId: currentState.activeProjectId,
        assetType,
        name: defaultName,
        eraTag: "默认时期",
        roleTag: assetType === "character" ? "核心角色" : assetType === "scene" ? "核心场景" : "关键道具",
        visualPrompt: "",
      }),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    openAssetBibleModal(res.assetId);
    showToast(`已新增${defaultName}，请填写外貌/环境描述`, "success");
  } catch (err) {
    showToast(`新增设定失败: ${err.message}`, "error");
  }
}

async function deleteAssetItem(assetId) {
  try {
    await apiFetch(`/api/assets/${assetId}/delete`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast("已删除该项设定", "info");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`删除设定失败: ${err.message}`, "error");
  }
}

async function triggerAssetImage(assetId) {
  try {
    await apiFetch(`/api/assets/${assetId}/generateImage`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast("已加入定妆设定图生成队列，完成后将自动显示！", "info");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`提交定妆图生成失败: ${err.message}`, "error");
  }
}

async function batchGenerateAllAssetImages() {
  if (!currentState.activeProjectId) return;
  try {
    const res = await apiFetch(`/api/projects/${currentState.activeProjectId}/batchAssetImages`, {
      method: "POST",
      body: JSON.stringify({ onlyMissing: true }),
    });
    if (res.count === 0) {
      showToast("所有角色、场景与道具都已有定妆设定图！如需重画可点击单个卡片上的「重画定妆图」", "success");
    } else {
      showToast(`已将 ${res.count} 项角色/场景/道具加入定妆图生成队列！`, "success");
    }
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    showToast(`批量生成定妆图失败: ${err.message}`, "error");
  }
}

async function confirmAssetsAndGoToBreakdown() {
  closeModal("assetBibleModal");
  openScriptModal(false);
  await submitScriptBreakdown(false);
}

async function triggerDirectBreakdownFromMain() {
  openScriptModal(false);
  const scriptVal = (document.getElementById("scriptInputArea").value || "").trim();
  const novelVal = (document.getElementById("novelInputArea").value || "").trim();
  if (scriptVal || novelVal) {
    await submitScriptBreakdown(false);
  }
}

async function submitScriptBreakdown(autoRewriteFirst = false) {
  if (!currentState.activeEpisodeId) return;
  const novelText = document.getElementById("novelInputArea").value.trim();
  let scriptText = document.getElementById("scriptInputArea").value.trim();
  if (!novelText && !scriptText) {
    showToast("请输入小说原文或剧本内容", "error");
    return;
  }

  setScriptButtonsBusy(true, autoRewriteFirst ? "auto" : "breakdown");

  try {
    // 如果用户点击「一键全自动」或者右侧还没有分场剧本，先显式执行第一步改写并实时展示在右栏
    if (autoRewriteFirst || !scriptText) {
      startScriptProgressTracker([
        {
          afterSec: 0,
          pillIndex: 1,
          basePct: 10,
          title: "全自动拆解 (1/2)：正在解析小说原文并转化为可视动作...",
          sub: "将小说叙事改写为标准分场结构，剔除冗余旁白，提炼精炼对白",
        },
        {
          afterSec: 2.2,
          pillIndex: 2,
          basePct: 30,
          title: "全自动拆解 (1/2)：正在自动提取角色、场景与关键道具入库...",
          sub: "提取出的角色与场景将自动出现在主界面左侧「角色 / 场景 / 道具库」中",
        },
        {
          afterSec: 4.8,
          pillIndex: 3,
          basePct: 48,
          title: "全自动拆解 (1/2)：正在生成 180° 空间轴线与分场调度账本...",
          sub: "即将完成右侧分场剧本并自动进入逐镜首尾帧拆解...",
        },
      ]);

      const rewriteRes = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/rewriteScript`, {
        method: "POST",
        body: JSON.stringify({ novelContent: novelText || scriptText }),
      });
      scriptText = rewriteRes.formattedScript || scriptText;
      document.getElementById("scriptInputArea").value = scriptText;
      document.getElementById("rewriteStatusBadge").textContent = `✅ 已生成 ${rewriteRes.sceneCount} 场剧本（提取 ${rewriteRes.assetCountChanged} 项资产）`;
    }

    // 紧接着执行第二步：拆解首尾关键帧分镜表
    startScriptProgressTracker([
      {
        afterSec: 0,
        pillIndex: 4,
        basePct: 62,
        title: "全自动拆解 (2/2)：正在按分场剧本拆解逐镜「首尾关键帧」故事板...",
        sub: "正在为每一镜规划：0.0s 起点画面提示词、中间物理运动轨迹、动作结果落点画面与角色台词...",
      },
      {
        afterSec: 3.5,
        pillIndex: 4,
        basePct: 82,
        title: "全自动拆解 (2/2)：正在配置前后镜头衔接锁 (Frame Lock) 与声学约束...",
        sub: "自动区分「⚡ 动作推进镜 (双图锁死动作)」与「💬 台词博弈镜 (单图开口说话)」，即将生成完毕...",
      },
    ]);

    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/breakScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText, scriptContent: scriptText }),
    });

    finishScriptProgressTracker(
      true,
      `🎉 拆解全部完成！共生成 ${res.shotCount} 个首尾帧分镜（覆盖 ${res.sceneCount || 1} 个场次）`,
      "正在为您打开主工作台分镜列表...",
      4
    );
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);

    setTimeout(() => {
      closeModal("scriptModal");
      showToast(
        `🎉 成功生成 ${res.shotCount} 个分镜！现在可点击顶部「第 ② 步」生成画面或「第 ③ 步」生成视频`,
        "success"
      );
    }, 650);
  } catch (err) {
    finishScriptProgressTracker(false, "剧本拆解失败", err.message, 0);
    showToast(`剧本拆解失败: ${err.message}`, "error");
  } finally {
    setScriptButtonsBusy(false);
  }
}

function openNewProjectModal() {
  openDialog("newProjectModal");
}

async function submitNewProject() {
  const title = document.getElementById("newProjTitle").value.trim();
  const novelContent = document.getElementById("newProjNovel").value.trim();
  if (!title) {
    showToast("请输入短剧名称", "error");
    return;
  }
  try {
    const res = await apiFetch("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        title,
        genre: document.getElementById("newProjGenre").value.trim(),
        visualStyle: document.getElementById("newProjStyle").value.trim(),
        aspectRatio: document.getElementById("newProjRatio").value,
        novelContent,
      }),
    });
    closeModal("newProjectModal");
    await loadProjects(res.projectId);
    if (novelContent.length > 0) {
      showToast(`已创建《${title}》，正在由 AI 自动改写分场剧本并提取角色场景...`, "info");
      openScriptModal(false);
      await submitNovelRewrite();
    } else {
      showToast(`已创建短剧项目《${title}》，请点击「第 ① 步」粘贴小说并拆解分镜`, "success");
    }
  } catch (err) {
    showToast(`创建项目失败: ${err.message}`, "error");
  }
}

function openSettingsModal() {
  const s = currentState.settings || {};
  document.getElementById("cfgComfyEndpoint").value = s.comfyEndpoint || "http://127.0.0.1:8188";
  document.getElementById("cfgH3Steps").value = s.h3SamplingSteps || "20";
  document.getElementById("cfgLlmBaseUrl").value = s.llmBaseUrl || "https://api.deepseek.com/v1";
  document.getElementById("cfgLlmApiKey").value = s.llmApiKeyMasked || "";
  document.getElementById("cfgLlmModel").value = s.llmModel || "deepseek-chat";
  openDialog("settingsModal");
}

async function saveSettings() {
  try {
    await apiFetch("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        comfyEndpoint: document.getElementById("cfgComfyEndpoint").value.trim(),
        h3SamplingSteps: document.getElementById("cfgH3Steps").value.trim(),
        llmBaseUrl: document.getElementById("cfgLlmBaseUrl").value.trim(),
        llmApiKey: document.getElementById("cfgLlmApiKey").value.trim(),
        llmModel: document.getElementById("cfgLlmModel").value.trim(),
      }),
    });
    closeModal("settingsModal");
    showToast("引擎配置已保存并重新检测连接", "success");
    await refreshStatus();
  } catch (err) {
    showToast(`保存设置失败: ${err.message}`, "error");
  }
}

window.addEventListener("DOMContentLoaded", initApp);
