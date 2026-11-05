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
    sel.innerHTML = `<option value="">暂无项目，请点击右上角新建短剧</option>`;
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
  updateWorkflowStepper(ws.shots || [], ws.activeEpisode);
  renderMasterBanner(ws.activeEpisode);
  renderSceneLedger(ws.activeEpisode);
  renderFilmstripRibbon(ws.shots || []);
  renderShotCards(ws.shots || [], ws.tasks || []);
  renderTaskList(ws.tasks || [], ws.shots || []);
}

/**
 * 更新顶部 4 步短剧制作向导条，自动高亮用户当前应该操作的下一步
 */
function updateWorkflowStepper(shots, activeEpisode) {
  const total = shots.length;
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

  if (s1Text) s1Text.textContent = total > 0 ? `已拆解 ${total} 镜` : "待拆解";
  if (s2Text) s2Text.textContent = `${kfReadyCount} / ${total} 已就绪`;
  if (s3Text) s3Text.textContent = `${vidReadyCount} / ${total} 已出片`;
  if (s4Text) s4Text.textContent = hasMaster ? "✅ 已合成成片" : vidReadyCount > 0 ? `可合成 (${vidReadyCount}镜)` : "待生成视频";

  // 判断当前推荐哪一步高亮
  let recommendedStep = 1;
  if (total === 0) {
    recommendedStep = 1;
  } else if (kfReadyCount < total) {
    recommendedStep = 2;
  } else if (vidReadyCount < total) {
    recommendedStep = 3;
  } else {
    recommendedStep = 4;
  }

  [1, 2, 3, 4].forEach((num) => {
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
    container.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:12px 0;">当前分类下暂无资产</div>`;
    return;
  }
  container.innerHTML = filtered
    .map((a) => {
      const typeCn =
        a.assetType === "character" ? "角色" : a.assetType === "scene" ? "场景" : "道具";
      const thumbHtml = a.imageUrl
        ? `<img class="assetThumb" src="${escapeHtml(a.imageUrl)}" alt="${escapeHtml(a.name)}" onclick="openLightbox('${escapeHtml(a.imageUrl)}', '@${escapeHtml(a.name)} · 设定原图')" style="cursor:zoom-in;" />`
        : `<div class="assetThumb" style="display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--text-muted);">${typeCn}</div>`;
      const eraBadge = a.eraTag
        ? `<span class="eraBadge">${escapeHtml(a.eraTag)}</span>`
        : "";
      return `
        <div class="assetItem">
          ${thumbHtml}
          <div style="min-width:0;">
            <div class="assetName">@${escapeHtml(a.name)} ${eraBadge}</div>
            <div style="font-size:11px; color:var(--amber-gold);">${escapeHtml(a.roleTag || typeCn)}</div>
            <div class="assetDesc" title="${escapeHtml(a.visualPrompt)}">${escapeHtml(a.visualPrompt || "暂无外貌/环境描述")}</div>
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
    renderShotCards(currentState.workspace.shots || [], currentState.workspace.tasks || []);
  }
}

function onShotSearchInput(keyword) {
  currentState.shotSearchKeyword = (keyword || "").trim().toLowerCase();
  if (currentState.workspace) {
    renderShotCards(currentState.workspace.shots || [], currentState.workspace.tasks || []);
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

function renderShotCards(shots, tasks = []) {
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
    container.innerHTML = `
      <div style="background:var(--bg-panel); border:1px dashed var(--border-subtle); border-radius:10px; padding:40px; text-align:center; color:var(--text-secondary);">
        当前剧集还没有分镜。请点击上方 <strong>“第 ① 步：打开剧本与分镜拆解”</strong> 自动生成每一镜的分镜卡片。
      </div>
    `;
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
      const titleDisplay = shotTitle
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
        <div class="taskCard ${escapeHtml(t.status)}" onclick="scrollToShot(${t.shotId})" title="点击定位到${titleDisplay}">
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

function openScriptModal() {
  if (!currentState.workspace || !currentState.workspace.activeEpisode) {
    showToast("请先选择或创建一个短剧项目", "error");
    return;
  }
  const ep = currentState.workspace.activeEpisode;
  document.getElementById("novelInputArea").value = ep.novelContent || ep.scriptContent || "";
  document.getElementById("scriptInputArea").value = ep.scriptContent || "";
  document.getElementById("rewriteStatusBadge").textContent =
    ep.sceneLedger && ep.sceneLedger.length
      ? `✅ 已建立 ${ep.sceneLedger.length} 场调度账本`
      : "";
  openDialog("scriptModal");
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
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/rewriteScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText }),
    });
    document.getElementById("scriptInputArea").value = res.formattedScript || "";
    badge.textContent = `✅ 已生成 ${res.sceneCount} 场剧本，提取/更新 ${res.assetCountChanged} 项资产！`;
    showToast(`已完成剧本改写并自动入库 ${res.assetCountChanged} 项角色/场景/道具`, "success");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    badge.textContent = "❌ 改写失败";
    showToast(`小说改写失败: ${err.message}`, "error");
  }
}

async function submitScriptBreakdown() {
  if (!currentState.activeEpisodeId) return;
  const novelText = document.getElementById("novelInputArea").value.trim();
  const scriptText = document.getElementById("scriptInputArea").value.trim();
  if (!novelText && !scriptText) {
    showToast("请输入小说原文或剧本内容", "error");
    return;
  }
  try {
    showToast("正在为您拆解全集分镜表，请稍候...", "info");
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/breakScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText, scriptContent: scriptText }),
    });
    closeModal("scriptModal");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    showToast(`🎉 成功生成 ${res.shotCount} 个分镜！接下来请点击顶部「第 ② 步」或「第 ③ 步」`, "success");
  } catch (err) {
    showToast(`剧本拆解失败: ${err.message}`, "error");
  }
}

function openNewProjectModal() {
  openDialog("newProjectModal");
}

async function submitNewProject() {
  const title = document.getElementById("newProjTitle").value.trim();
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
        novelContent: document.getElementById("newProjNovel").value.trim(),
      }),
    });
    closeModal("newProjectModal");
    showToast(`已创建短剧项目《${title}》`, "success");
    await loadProjects(res.projectId);
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
