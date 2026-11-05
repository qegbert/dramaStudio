/**
 * DramaForge Studio - 导演工作台前端核心控制器 (appMain.js)
 * 负责管理：项目/剧集切换、设定资产展示、四联导演监视器分镜编辑、
 * 首尾关键帧生成、FL2VA/Wan2.2 视频生成、帧锁接力与整集母带合成。
 */

let currentState = {
  projects: [],
  activeProjectId: null,
  activeEpisodeId: null,
  workspace: null,
  assetFilter: "all",
  settings: {},
  pollTimer: null,
};

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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
  try {
    await refreshStatus();
    await loadProjects();
    // 启动后台任务轮询（当存在 running 任务时自动刷新工作台）
    currentState.pollTimer = setInterval(pollRunningTasks, 3000);
  } catch (err) {
    console.error("初始化失败:", err);
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
    txt.textContent = `无影云未开机 (已启用本地高清 Previz 预演模式)`;
  }
}

async function loadProjects(preferredId = null) {
  const data = await apiFetch("/api/projects");
  currentState.projects = data.projects || [];
  const sel = document.getElementById("projectSelect");
  if (currentState.projects.length === 0) {
    sel.innerHTML = `<option value="">暂无项目，请点击上方导入或新建</option>`;
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
  renderMasterBanner(ws.activeEpisode);
  renderSceneLedger(ws.activeEpisode);
  renderShotCards(ws.shots || []);
  renderTaskList(ws.tasks || []);
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
  panel.style.display = "flex";
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
            <span class="modeBadge action_fl2va">${escapeHtml(sc.eraTag || "时空锁定")} · ${beatCount} 个节拍</span>
          </div>
          <div><strong style="color:var(--text-secondary);">允许入镜角色池：</strong>${castHtml || "无"}</div>
          <div style="color:var(--amber-gold); background:rgba(245,158,11,0.08); padding:6px 8px; border-radius:4px;">
            📐 <strong>180° 空间站位轴线：</strong>${escapeHtml(sc.spatialAxisCn || sc.spatialAxis || "")}
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
      const thumbHtml = a.imageUrl
        ? `<img class="assetThumb" src="${escapeHtml(a.imageUrl)}" alt="${escapeHtml(a.name)}" />`
        : `<div class="assetThumb" style="display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--text-muted);">${a.assetType === "character" ? "角色" : a.assetType === "scene" ? "场景" : "道具"}</div>`;
      const eraBadge = a.eraTag
        ? `<span class="eraBadge">${escapeHtml(a.eraTag)}</span>`
        : "";
      return `
        <div class="assetItem">
          ${thumbHtml}
          <div style="min-width:0;">
            <div class="assetName">@${escapeHtml(a.name)} ${eraBadge}</div>
            <div style="font-size:11px; color:var(--amber-gold);">${escapeHtml(a.roleTag || a.assetType)}</div>
            <div class="assetDesc" title="${escapeHtml(a.visualPrompt)}">${escapeHtml(a.visualPrompt || "暂无视觉描述")}</div>
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
  document.getElementById("masterMetaText").textContent = `当前剧集《${activeEpisode.title}》已完成多镜无缝拼接，支持直接下载成片或导入剪映微调。`;
}

function renderShotCards(shots) {
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

  const container = document.getElementById("shotListContainer");
  if (!shots.length) {
    container.innerHTML = `
      <div style="background:var(--bg-panel); border:1px dashed var(--border-subtle); border-radius:10px; padding:40px; text-align:center; color:var(--text-secondary);">
        当前剧集尚无分镜。请点击上方 <strong>“📜 剧本拆解 / 重排分镜”</strong> 自动生成首尾帧分镜表。
      </div>
    `;
    return;
  }

  container.innerHTML = shots
    .map((s) => {
      const isAction = s.shotCategory === "action_fl2va";
      const modeClass = isAction ? "actionMode" : "dialogueMode";
      const modeLabel = isAction
        ? "⚡ 动作推进镜 (FL2VA 首尾帧双锚点)"
        : "💬 台词博弈镜 (I2V 单首帧声学锁)";

      const firstImgHtml = s.firstFrameUrl
        ? `<img src="${escapeHtml(s.firstFrameUrl)}" alt="首帧" />`
        : `<div class="emptyViewportHint">尚未生成首帧 (0.0s 起点构图)<br/>点击下方按钮生成或从前镜尾帧锁定</div>`;

      const lastImgHtml = s.lastFrameUrl
        ? `<img src="${escapeHtml(s.lastFrameUrl)}" alt="尾帧" />`
        : `<div class="emptyViewportHint">${
            isAction
              ? "尚未生成尾帧 (动作结果锚点)<br/>锁定尾帧可100%防止情节演一半停住"
              : "台词镜可选尾帧 (默认单首帧驱动)"
          }</div>`;

      const videoHtml = s.videoUrl
        ? `<video src="${escapeHtml(s.videoUrl)}" controls preload="metadata"></video>`
        : `<div class="emptyViewportHint">待渲染镜头成片<br/>(${escapeHtml(s.videoEngine)})</div>`;

      const dialogues = Array.isArray(s.dialogue) ? s.dialogue : [];
      const dialogueHtml = dialogues.length
        ? dialogues
            .map(
              (d) => `
              <div class="dialogueItem">
                <span class="speechKindTag ${d.kind === "internal" ? "internal" : ""}">${
                d.kind === "internal" ? "内心独白·闭口" : d.kind === "offscreen" ? "画外音" : "画内对口型"
              }</span>
                <strong>${escapeHtml(d.speaker || "角色")}:</strong>
                <span style="flex:1;">「${escapeHtml(d.text || "")}」</span>
              </div>
            `
            )
            .join("")
        : `<div style="font-size:11.5px; color:var(--text-muted);">纯动作/氛围镜头（无角色说话对白，杜绝乱动嘴皮）</div>`;

      const qc = s.qcReport || {};
      const qcBadge = qc.grade
        ? `<span class="eraBadge" style="background:rgba(16,185,129,0.2);color:var(--emerald-ok);">QC:${escapeHtml(qc.grade)} · ${qc.duration || s.durationSec}s</span>`
        : `<span style="font-size:11px;color:var(--text-muted);">${escapeHtml(s.status)}</span>`;

      return `
        <article class="shotCard ${modeClass}" id="shot-card-${s.id}">
          <div class="shotHeader">
            <div class="shotMetaLeft">
              <span class="shotNumBadge">SHOT #${s.shotNumber}</span>
              <input class="shotTitleInput" value="${escapeHtml(s.title)}" onchange="saveShotField(${s.id}, {title: this.value})" />
              <span class="modeBadge ${s.shotCategory}">${modeLabel}</span>
              <select class="formSelect" style="padding:3px 8px; font-size:11.5px;" onchange="saveShotField(${s.id}, {shotCategory: this.value})">
                <option value="action_fl2va" ${isAction ? "selected" : ""}>切换为：动作首尾帧镜 (FL2VA)</option>
                <option value="dialogue_i2v" ${!isAction ? "selected" : ""}>切换为：台词单首帧镜 (I2V)</option>
              </select>
              <select class="formSelect" style="padding:3px 8px; font-size:11.5px;" onchange="saveShotField(${s.id}, {videoEngine: this.value})">
                <option value="h3_fl2va" ${s.videoEngine === "h3_fl2va" ? "selected" : ""}>引擎：MiniMax-H3 FL2VA (首尾帧音画)</option>
                <option value="wan22_moe" ${s.videoEngine === "wan22_moe" ? "selected" : ""}>引擎：Wan 2.2 14B MoE (双专家高清)</option>
              </select>
              ${
                s.shotNumber > 1
                  ? `<label class="frameLockLabel" title="开启后，本镜首帧直接无损复用上一镜的高清静态尾帧 (.png)，彻底解决前后断联与视频截帧模糊">
                      <input type="checkbox" ${Number(s.lockFromPrevEnd) === 1 ? "checked" : ""} onchange="saveShotField(${s.id}, {lockFromPrevEnd: this.checked ? 1 : 0})" />
                      🔒 帧锁：承接上镜尾帧
                    </label>`
                  : ""
              }
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-size:12px; font-family:var(--font-mono); color:var(--text-secondary);">${s.durationSec}s · ${escapeHtml(s.cameraFraming)}</span>
              <button class="btn btnSmall" onclick="showCompiledPrompt(${s.id})">🔍 编译指令</button>
              <button class="btn btnSmall btnCyan" onclick="triggerKeyframes(${s.id}, 'both')">🎨 生成首尾关键帧</button>
              <button class="btn btnSmall btnPrimary" onclick="triggerShotVideo(${s.id})">🎬 渲染本镜视频</button>
            </div>
          </div>

          <!-- 四联导演监视器画板 -->
          <div class="quadMonitorGrid">
            <!-- [1] 首帧锚点 -->
            <div class="frameSlot">
              <div class="slotHeader">
                <span>[1] 首帧锚点 (0.0s 起点)</span>
                <button class="btn btnSmall" onclick="triggerKeyframes(${s.id}, 'first')">重绘首帧</button>
              </div>
              <div class="mediaViewport">${firstImgHtml}</div>
              <textarea class="promptTextarea" placeholder="首帧静态构图提示词..." onchange="saveShotField(${s.id}, {firstFramePrompt: this.value})">${escapeHtml(s.firstFramePrompt)}</textarea>
            </div>

            <!-- [2] 戏剧动作弧线与逐字台词轨 -->
            <div class="arcCenterColumn">
              <div class="slotHeader">
                <span>[2] 戏剧目的、动作推进弧线 & 轴线契约</span>
                <span style="font-family:var(--font-mono); color:var(--amber-gold);">${escapeHtml(s.continuityMode)}</span>
              </div>
              <div class="intentBanner">🎯 叙事目的：${escapeHtml(s.intentText || "交代本镜核心情节变化")}</div>
              <div class="formGroup">
                <label>中间物理运动轨迹 (Motion Progression Arc)：</label>
                <textarea class="promptTextarea" style="min-height:54px;" onchange="saveShotField(${s.id}, {actionArcPrompt: this.value})">${escapeHtml(s.actionArcPrompt)}</textarea>
              </div>
              <div class="formGroup">
                <label>空间轴线与机位运动 (${escapeHtml(s.cameraMovement)})：</label>
                <input class="formInput" style="padding:5px 8px; font-size:11.5px;" value="${escapeHtml(s.axisRule)}" onchange="saveShotField(${s.id}, {axisRule: this.value})" />
              </div>
              <div class="dialogueTrackBox">
                <div style="font-size:11px; font-weight:700; color:var(--text-secondary);">🎙️ 声学与台词轨道 (画内口型 / 画外闭口硬约束)：</div>
                ${dialogueHtml}
              </div>
            </div>

            <!-- [3] 尾帧结果锚点 -->
            <div class="frameSlot">
              <div class="slotHeader">
                <span>[3] 尾帧锚点 (${s.durationSec}s 结果落点)</span>
                <button class="btn btnSmall" onclick="triggerKeyframes(${s.id}, 'last')">重绘尾帧</button>
              </div>
              <div class="mediaViewport">${lastImgHtml}</div>
              <textarea class="promptTextarea" placeholder="尾帧动作完成态构图提示词..." onchange="saveShotField(${s.id}, {lastFramePrompt: this.value})">${escapeHtml(s.lastFramePrompt)}</textarea>
            </div>

            <!-- [4] 镜头成片监视器 -->
            <div class="frameSlot">
              <div class="slotHeader">
                <span>[4] 镜头成片监视器</span>
                ${qcBadge}
              </div>
              <div class="mediaViewport">${videoHtml}</div>
              <div style="font-size:11px; color:var(--text-secondary); display:flex; flex-direction:column; gap:4px;">
                <div>🔊 声景: ${escapeHtml((s.soundscapePrompt || "").slice(0, 68))}</div>
                ${
                  s.videoUrl
                    ? `<button class="btn btnSmall" style="width:100%; margin-top:4px;" onclick="extractLastFrameFromShot(${s.id})">📸 提取此视频末帧作为下镜首帧</button>`
                    : `<div style="color:var(--text-muted);">点击右上角“🎬 渲染本镜视频”启动生成</div>`
                }
              </div>
            </div>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderTaskList(tasks) {
  const container = document.getElementById("taskListContainer");
  if (!tasks.length) {
    container.innerHTML = `<div style="color:var(--text-muted); font-size:12px;">暂无渲染任务记录</div>`;
    return;
  }
  container.innerHTML = tasks
    .map((t) => {
      const badgeColor =
        t.status === "succeeded"
          ? "var(--emerald-ok)"
          : t.status === "failed"
          ? "var(--rose-err)"
          : "var(--amber-gold)";
      return `
        <div class="taskCard ${escapeHtml(t.status)}">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <strong>镜头 ID #${t.shotId} · ${escapeHtml(t.taskType)}</strong>
            <span style="font-family:var(--font-mono); font-size:11px; color:${badgeColor};">${escapeHtml(t.status)}</span>
          </div>
          <div style="color:var(--text-secondary);">${escapeHtml(t.phaseText || "")}</div>
          ${
            t.errorMessage
              ? `<div style="color:var(--rose-err); font-size:11px;">${escapeHtml(t.errorMessage)}</div>`
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
    const tasks = data.tasks || [];
    const hadRunning =
      currentState.workspace &&
      (currentState.workspace.tasks || []).some((t) => t.status === "running" || t.status === "queued");
    const nowRunning = tasks.some((t) => t.status === "running" || t.status === "queued");

    renderTaskList(tasks.filter((t) => t.projectId === currentState.activeProjectId));
    if (hadRunning || nowRunning) {
      await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    }
  } catch (err) {
    // 忽略静默轮询异常
  }
}

async function refreshTasksOnly() {
  if (!currentState.activeProjectId) return;
  await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
}

async function saveShotField(shotId, payload) {
  try {
    await apiFetch(`/api/shots/${shotId}/update`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    alert(`保存镜头修改失败: ${err.message}`);
  }
}

async function triggerKeyframes(shotId, frameRole = "both") {
  try {
    await apiFetch(`/api/shots/${shotId}/generateKeyframes`, {
      method: "POST",
      body: JSON.stringify({ frameRole }),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    alert(`提交关键帧生成失败: ${err.message}`);
  }
}

async function triggerShotVideo(shotId) {
  try {
    await apiFetch(`/api/shots/${shotId}/generateVideo`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    alert(`提交镜头视频渲染失败: ${err.message}`);
  }
}

async function extractLastFrameFromShot(shotId) {
  try {
    await apiFetch(`/api/shots/${shotId}/extractLastFrame`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    alert(`提取视频末尾帧失败: ${err.message}`);
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
    alert(`已同步全集连续性帧锁（本次更新 ${res.syncedCount} 个镜头的首帧锚点）！`);
  } catch (err) {
    alert(`同步帧锁失败: ${err.message}`);
  }
}

async function batchGenerateKeyframes(limit = 3) {
  if (!currentState.activeEpisodeId) return;
  try {
    await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/batchKeyframes`, {
      method: "POST",
      body: JSON.stringify({ limit }),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    alert(`批量生成关键帧失败: ${err.message}`);
  }
}

async function mergeMasterEpisode() {
  if (!currentState.activeEpisodeId) return;
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/mergeMaster`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    const tlLink = document.getElementById("timelineDownloadLink");
    if (tlLink && res.timelineUrl) {
      tlLink.href = res.timelineUrl;
    }
  } catch (err) {
    alert(`合成整集母带提示: ${err.message}`);
  }
}

async function importHuobaoData() {
  try {
    const res = await apiFetch("/api/import/huobao", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadProjects();
    alert(
      `成功从本机历史库导入并升级为首尾帧结构：\n项目 ${res.summary.projects} 个，剧集 ${res.summary.episodes} 集，设定资产 ${res.summary.assets} 项，分镜 ${res.summary.shots} 条！`
    );
  } catch (err) {
    alert(`导入失败: ${err.message}`);
  }
}

function showCompiledPrompt(shotId) {
  if (!currentState.workspace) return;
  const shot = (currentState.workspace.shots || []).find((s) => s.id === shotId);
  if (!shot) return;
  document.getElementById("compiledPromptPre").textContent = shot.compiledPreview || "";
  document.getElementById("promptPreviewModal").classList.add("open");
}

function openScriptModal() {
  if (!currentState.workspace || !currentState.workspace.activeEpisode) {
    alert("请先选择或创建一个短剧项目");
    return;
  }
  const ep = currentState.workspace.activeEpisode;
  document.getElementById("novelInputArea").value = ep.novelContent || ep.scriptContent || "";
  document.getElementById("scriptInputArea").value = ep.scriptContent || "";
  document.getElementById("rewriteStatusBadge").textContent =
    ep.sceneLedger && ep.sceneLedger.length
      ? `✅ 已建立 ${ep.sceneLedger.length} 场空间调度账本`
      : "尚未生成场次空间调度账本";
  document.getElementById("scriptModal").classList.add("open");
}

async function submitNovelRewrite() {
  if (!currentState.activeEpisodeId) return;
  const novelText = document.getElementById("novelInputArea").value.trim();
  if (!novelText) {
    alert("请先在左栏输入小说原文");
    return;
  }
  const badge = document.getElementById("rewriteStatusBadge");
  badge.textContent = "⏳ 正在改写分场剧本、建立 180° 空间轴线与提取资产...";
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/rewriteScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText }),
    });
    document.getElementById("scriptInputArea").value = res.formattedScript || "";
    badge.textContent = `✅ 已生成 ${res.sceneCount} 个场次账本，新增/补全 ${res.assetCountChanged} 项设定资产！`;
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
  } catch (err) {
    badge.textContent = "❌ 改写失败";
    alert(`小说改写分场剧本失败: ${err.message}`);
  }
}

async function submitScriptBreakdown() {
  if (!currentState.activeEpisodeId) return;
  const novelText = document.getElementById("novelInputArea").value.trim();
  const scriptText = document.getElementById("scriptInputArea").value.trim();
  if (!novelText && !scriptText) {
    alert("请输入小说原文或剧本内容");
    return;
  }
  try {
    const res = await apiFetch(`/api/episodes/${currentState.activeEpisodeId}/breakScript`, {
      method: "POST",
      body: JSON.stringify({ novelContent: novelText, scriptContent: scriptText }),
    });
    closeModal("scriptModal");
    await loadWorkspace(currentState.activeProjectId, currentState.activeEpisodeId);
    alert(`成功按 ${res.sceneCount || 1} 场空间调度账本生成 ${res.shotCount} 条首尾帧分镜！`);
  } catch (err) {
    alert(`剧本拆解失败: ${err.message}`);
  }
}

function openNewProjectModal() {
  document.getElementById("newProjectModal").classList.add("open");
}

async function submitNewProject() {
  const title = document.getElementById("newProjTitle").value.trim();
  if (!title) {
    alert("请输入短剧名称");
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
    await loadProjects(res.projectId);
  } catch (err) {
    alert(`创建项目失败: ${err.message}`);
  }
}

function openSettingsModal() {
  const s = currentState.settings || {};
  document.getElementById("cfgComfyEndpoint").value = s.comfyEndpoint || "http://127.0.0.1:8188";
  document.getElementById("cfgH3Steps").value = s.h3SamplingSteps || "20";
  document.getElementById("cfgLlmBaseUrl").value = s.llmBaseUrl || "https://api.deepseek.com/v1";
  document.getElementById("cfgLlmApiKey").value = s.llmApiKeyMasked || "";
  document.getElementById("cfgLlmModel").value = s.llmModel || "deepseek-chat";
  document.getElementById("settingsModal").classList.add("open");
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
    await refreshStatus();
  } catch (err) {
    alert(`保存设置失败: ${err.message}`);
  }
}

function closeModal(modalId) {
  document.getElementById(modalId).classList.remove("open");
}

window.addEventListener("DOMContentLoaded", initApp);
