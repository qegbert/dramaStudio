#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 核心数据存储与无损迁移层 (studioStore.py)
完全独立于任何第三方受限开源代码，100% 原创数据结构。
支持管理：项目(projects)、剧集(episodes)、设定资产(assets)、首尾帧结构化分镜(shots)、渲染任务(renderTasks)。
"""

import os
import re
import json
import time
import shutil
import sqlite3
from pathlib import Path
from contextlib import closing

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
KEYFRAMES_DIR = MEDIA_DIR / "keyframes"
VIDEOS_DIR = MEDIA_DIR / "videos"
ASSETS_DIR = MEDIA_DIR / "assets"
EXPORTS_DIR = MEDIA_DIR / "exports"
DB_PATH = DATA_DIR / "studioStore.sqlite3"

for folder in (DATA_DIR, MEDIA_DIR, KEYFRAMES_DIR, VIDEOS_DIR, ASSETS_DIR, EXPORTS_DIR):
    folder.mkdir(parents=True, exist_ok=True)


def getDbConnection():
    """获取带有字典行工厂的 SQLite 连接"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initDatabase():
    """初始化独立数据库表结构"""
    with closing(getDbConnection()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                genre TEXT DEFAULT '古风/修仙/剧情',
                visualStyle TEXT DEFAULT '3D国漫电影级写实质感',
                aspectRatio TEXT DEFAULT '16:9',
                description TEXT DEFAULT '',
                defaultVideoEngine TEXT DEFAULT 'h3_fl2va',
                enableUpscale3d INTEGER DEFAULT 1,
                createdAt REAL NOT NULL,
                updatedAt REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                projectId INTEGER NOT NULL,
                episodeNumber INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                novelContent TEXT DEFAULT '',
                scriptContent TEXT DEFAULT '',
                sceneLedgerJson TEXT DEFAULT '[]',
                masterVideoUrl TEXT DEFAULT '',
                createdAt REAL NOT NULL,
                updatedAt REAL NOT NULL,
                FOREIGN KEY(projectId) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                projectId INTEGER NOT NULL,
                assetType TEXT NOT NULL, -- 'character' | 'scene' | 'prop'
                name TEXT NOT NULL,
                roleTag TEXT DEFAULT '',
                eraTag TEXT DEFAULT '',  -- 区分前世/今生/不同时期状态，防止同名串台
                visualPrompt TEXT DEFAULT '',
                voiceProfile TEXT DEFAULT '',
                imageUrl TEXT DEFAULT '',
                createdAt REAL NOT NULL,
                updatedAt REAL NOT NULL,
                FOREIGN KEY(projectId) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                projectId INTEGER NOT NULL,
                episodeId INTEGER NOT NULL,
                shotNumber INTEGER NOT NULL,
                title TEXT NOT NULL,
                shotCategory TEXT NOT NULL DEFAULT 'action_fl2va', -- 'action_fl2va'(动作首尾帧) | 'dialogue_i2v'(台词单首帧)
                videoEngine TEXT NOT NULL DEFAULT 'h3_fl2va',      -- 'h3_fl2va' | 'wan22_moe' | 'h3_ref2va'
                durationSec REAL NOT NULL DEFAULT 5.0,
                continuityMode TEXT NOT NULL DEFAULT 'establish',  -- 'establish' | 'continue_action' | 'same_scene_cut' | 'insert' | 'world_cut'
                lockFromPrevEnd INTEGER NOT NULL DEFAULT 0,        -- 1=本镜首帧直接锁定复用上一镜高清尾帧原图
                axisRule TEXT DEFAULT '',
                intentText TEXT DEFAULT '',
                cameraFraming TEXT DEFAULT 'Medium shot',
                cameraMovement TEXT DEFAULT 'Steady camera',
                firstFramePrompt TEXT DEFAULT '',
                actionArcPrompt TEXT DEFAULT '',
                lastFramePrompt TEXT DEFAULT '',
                soundscapePrompt TEXT DEFAULT '',
                dialogueJson TEXT DEFAULT '[]',
                referenceAssetsJson TEXT DEFAULT '[]',
                checksJson TEXT DEFAULT '[]',
                firstFrameUrl TEXT DEFAULT '',
                lastFrameUrl TEXT DEFAULT '',
                videoUrl TEXT DEFAULT '',
                qcReportJson TEXT DEFAULT '{}',
                status TEXT DEFAULT 'planned', -- 'planned' | 'keyframes_ready' | 'rendering' | 'completed' | 'failed'
                createdAt REAL NOT NULL,
                updatedAt REAL NOT NULL,
                FOREIGN KEY(projectId) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(episodeId) REFERENCES episodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS renderTasks (
                id TEXT PRIMARY KEY,
                projectId INTEGER NOT NULL,
                episodeId INTEGER NOT NULL,
                shotId INTEGER NOT NULL,
                taskType TEXT NOT NULL, -- 'first_frame' | 'last_frame' | 'both_keyframes' | 'shot_video' | 'episode_merge'
                engineName TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', -- 'queued' | 'running' | 'succeeded' | 'failed'
                phaseText TEXT DEFAULT '排队中',
                comfyPromptId TEXT DEFAULT '',
                outputUrl TEXT DEFAULT '',
                errorMessage TEXT DEFAULT '',
                auditJson TEXT DEFAULT '{}',
                createdAt REAL NOT NULL,
                updatedAt REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # 写入默认配置
        defaults = {
            "comfyEndpoint": os.environ.get("WUYING_COMFY_ENDPOINT", "http://127.0.0.1:8188"),
            "llmBaseUrl": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "llmApiKey": os.environ.get("LLM_API_KEY", ""),
            "llmModel": os.environ.get("LLM_MODEL", "deepseek-chat"),
            "defaultVideoEngine": "h3_fl2va",
            "enableUpscale3d": "0",
            "h3SamplingSteps": "12",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, str(v)),
            )
        conn.execute("UPDATE settings SET value='12' WHERE key='h3SamplingSteps' AND value='20'")
        # 清理上次进程退出遗留的僵尸 running 任务，并恢复对应镜头状态
        nowTs = time.time()
        conn.execute(
            """
            UPDATE renderTasks
            SET status='failed', phaseText='上次服务重启中断，请重新点击生成', updatedAt=?
            WHERE status IN ('running', 'queued')
            """,
            (nowTs,),
        )
        conn.execute(
            """
            UPDATE shots
            SET status = CASE WHEN firstFrameUrl != '' THEN 'keyframes_ready' ELSE 'planned' END,
                updatedAt = ?
            WHERE status = 'rendering'
            """,
            (nowTs,),
        )
        # 兼容已有数据库平滑增加 sceneLedgerJson 字段
        epCols = [r[1] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()]
        if "sceneLedgerJson" not in epCols:
            conn.execute("ALTER TABLE episodes ADD COLUMN sceneLedgerJson TEXT DEFAULT '[]'")
        conn.commit()


def upsertExtractedAssets(projectId, extractedList):
    """
    将剧本改写阶段自动识别出的角色（含前世/今生时空标签）、场景、关键道具合并入库：
    - 若同名同类型资产尚未存在，则自动新增；
    - 若已存在但缺少 visualPrompt 或 eraTag，则自动补全。
    返回新增或补全的资产数量。
    """
    if not isinstance(extractedList, list):
        return 0
    now = time.time()
    changed = 0
    with closing(getDbConnection()) as conn:
        for item in extractedList:
            if not isinstance(item, dict):
                continue
            aType = str(item.get("assetType") or "character").strip()
            if aType not in ("character", "scene", "prop"):
                continue
            name = str(item.get("name") or "").lstrip("@").strip()
            if not name:
                continue
            roleTag = str(item.get("roleTag") or "").strip()
            eraTag = str(item.get("eraTag") or "").strip()
            visualPrompt = str(item.get("visualPrompt") or "").strip()
            voiceProfile = str(item.get("voiceProfile") or "").strip()

            existing = conn.execute(
                "SELECT * FROM assets WHERE projectId=? AND assetType=? AND name=?",
                (projectId, aType, name),
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO assets (projectId, assetType, name, roleTag, eraTag, visualPrompt, voiceProfile, imageUrl, createdAt, updatedAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (projectId, aType, name, roleTag, eraTag, visualPrompt, voiceProfile, now, now),
                )
                changed += 1
            else:
                newPrompt = existing["visualPrompt"] or visualPrompt
                newEra = existing["eraTag"] or eraTag
                newRole = existing["roleTag"] or roleTag
                newVoice = existing["voiceProfile"] or voiceProfile
                if (newPrompt != existing["visualPrompt"]) or (newEra != existing["eraTag"]) or (newRole != existing["roleTag"]):
                    conn.execute(
                        "UPDATE assets SET roleTag=?, eraTag=?, visualPrompt=?, voiceProfile=?, updatedAt=? WHERE id=?",
                        (newRole, newEra, newPrompt, newVoice, now, existing["id"]),
                    )
                    changed += 1
        conn.commit()
    return changed


def classifyShotCategory(planDict, actionSummary=""):
    """
    智能判定镜头类型：
    - 若动作幅度大、包含明显空间位移或关键物理状态变化，归为 'action_fl2va'（强制使用首尾帧双锚点驱动，杜绝动作演一半停住）；
    - 若以长句台词对白博弈、微表情特写为主，且始末姿态基本一致，归为 'dialogue_i2v'（使用单首帧高精度驱动）。
    """
    dialogues = planDict.get("dialogue") or []
    spokenChars = sum(len(re.findall(r"[\u4e00-\u9fff]", d.get("text", ""))) for d in dialogues if isinstance(d, dict))
    startState = str(planDict.get("start_state") or "").lower()
    endState = str(planDict.get("end_state") or "").lower()
    actionText = (str(planDict.get("action") or "") + " " + str(actionSummary or "")).lower()

    heavyActionKeywords = [
        "grab", "push", "pull", "run", "jump", "throw", "strike", "hit", "open", "close",
        "stand up", "sit down", "fall", "turn around", "walk", "reach", "slam", "lift",
        "wrench", "valve", "bolt", "shovel", "hammer", "pour", "explode", "blast",
        "拧", "拉", "推", "跑", "冲", "砸", "摔", "拔", "刺", "站起", "蹲下", "转身", "伸手", "掀", "泼"
    ]
    hasHeavyAction = any(kw in actionText for kw in heavyActionKeywords)
    stateChanged = startState != endState and len(endState) > 15

    if spokenChars >= 14 and not hasHeavyAction:
        return "dialogue_i2v"
    if hasHeavyAction or stateChanged:
        return "action_fl2va"
    return "dialogue_i2v" if spokenChars > 0 else "action_fl2va"


def buildKeyframePromptsFromPlan(planDict, shotRow, visualStyle="Cinematic realistic 3D animation"):
    """
    将分镜计划中的 start_state 和 end_state 编译为专用于 Flux3-Klein 静态关键帧生成的精确视觉提示词。
    确保首帧与尾帧具有相同的场景光照、镜头焦段、轴线方向和人物服装锚点，只改变动作起点与终点姿态！
    """
    refs = planDict.get("references") or []
    charDescs = []
    sceneDesc = ""
    propDescs = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        role = ref.get("role", "")
        assetName = str(ref.get("asset", "")).lstrip("@")
        desc = str(ref.get("description", "")).strip()
        if role == "character":
            charDescs.append(f"{assetName} ({desc})")
        elif role == "scene":
            sceneDesc = f"{assetName}: {desc}"
        elif role == "prop":
            propDescs.append(f"{assetName} ({desc})")

    camera = planDict.get("camera") or {}
    framing = camera.get("framing") or shotRow.get("shot_type") or "Medium shot"
    continuity = planDict.get("continuity") or {}
    axisRule = continuity.get("axis") or "Maintain strict left-to-right spatial axis and consistent screen direction."

    startState = planDict.get("start_state") or shotRow.get("description") or "Character in opening pose."
    actionArc = planDict.get("action") or shotRow.get("description") or "Performs the planned dramatic action."
    endState = planDict.get("end_state") or shotRow.get("result") or "Character completes the action in final pose."

    baseHeader = (
        f"[{visualStyle}], {framing}. "
        f"Environment: {sceneDesc or shotRow.get('location') or 'Consistent interior scene'}. "
        f"Subjects: {'; '.join(charDescs) if charDescs else 'Main protagonist'}. "
    )
    if propDescs:
        baseHeader += f"Key props: {'; '.join(propDescs)}. "
    baseHeader += f"Spatial axis: {axisRule} "

    firstFramePrompt = (
        f"{baseHeader}"
        f"OPENING KEYFRAME POSE (0.0s): {startState} "
        f"Sharp facial features, realistic fabric texture, unified cinematic lighting, no text or subtitles."
    )
    lastFramePrompt = (
        f"{baseHeader}"
        f"ENDING KEYFRAME POSE (Final frame after action): {endState} "
        f"Exact same camera lens, environment lighting, and character identity/costume as opening frame, "
        f"showing the completed physical payoff of: {actionArc} No text or subtitles."
    )
    return firstFramePrompt, actionArc, lastFramePrompt


def importFromHuobaoDatabase(sourceDbPath=None):
    """
    从外部兼容的 SQLite 短剧数据库导入用户剧目、角色/场景/道具图片素材与分镜数据，
    并自动升级为“首帧 + 动作弧线 + 尾帧 + 连续性帧锁”结构。
    """
    if not sourceDbPath:
        sourceDbPath = os.environ.get("EXTERNAL_DRAMA_DB_PATH", "")
    if not sourceDbPath:
        raise ValueError("未指定外部数据库路径 (sourceDbPath)")
    sourceDb = Path(sourceDbPath).expanduser()
    if not sourceDb.exists():
        raise FileNotFoundError(f"未找到源数据库: {sourceDb}")

    sourceDataDir = sourceDb.parent
    now = time.time()
    importedSummary = {"projects": 0, "episodes": 0, "assets": 0, "shots": 0}

    with closing(sqlite3.connect(str(sourceDb))) as srcConn, closing(getDbConnection()) as dstConn:
        srcConn.row_factory = sqlite3.Row

        # 同步读取 LLM API Key（如果源数据库中有配置且当前未设置）
        try:
            cfgRows = srcConn.execute(
                "SELECT service_type, base_url, api_key, model FROM ai_service_configs WHERE is_active=1"
            ).fetchall()
            for cfg in cfgRows:
                if cfg["service_type"] == "text" and cfg["api_key"]:
                    models = json.loads(cfg["model"]) if cfg["model"] and cfg["model"].startswith("[") else [cfg["model"]]
                    dstConn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('llmBaseUrl', ?)", (cfg["base_url"],))
                    dstConn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('llmApiKey', ?)", (cfg["api_key"],))
                    if models and models[0]:
                        dstConn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('llmModel', ?)", (models[0],))
        except Exception:
            pass

        dramas = srcConn.execute("SELECT * FROM dramas WHERE deleted_at IS NULL").fetchall()
        for drama in dramas:
            # 检查是否已经导入过同名项目
            existing = dstConn.execute("SELECT id FROM projects WHERE title=?", (drama["title"],)).fetchone()
            if existing:
                projectId = existing["id"]
                dstConn.execute("DELETE FROM shots WHERE projectId=?", (projectId,))
                dstConn.execute("DELETE FROM assets WHERE projectId=?", (projectId,))
                dstConn.execute("DELETE FROM episodes WHERE projectId=?", (projectId,))
            else:
                cur = dstConn.execute(
                    """
                    INSERT INTO projects (title, genre, visualStyle, aspectRatio, description, defaultVideoEngine, enableUpscale3d, createdAt, updatedAt)
                    VALUES (?, ?, ?, ?, ?, 'h3_fl2va', 1, ?, ?)
                    """,
                    (
                        drama["title"],
                        drama["genre"] or "修仙/古风/剧情",
                        "3D国漫电影级写实质感 (High-end Chinese 3D CG & live-action realism)" if drama["style"] == "3d" else (drama["style"] or "电影级写实质感"),
                        drama["aspect_ratio"] or "16:9",
                        drama["description"] or "",
                        now,
                        now,
                    ),
                )
                projectId = cur.lastrowid
            importedSummary["projects"] += 1

            def copyMediaFile(relOrAbsPath, targetSubdir):
                if not relOrAbsPath:
                    return ""
                relClean = str(relOrAbsPath).lstrip("/")
                candidate = sourceDataDir / relClean
                if not candidate.exists():
                    candidate = Path(relOrAbsPath)
                if candidate.exists() and candidate.is_file():
                    ext = candidate.suffix or ".png"
                    newName = f"imp_{projectId}_{int(time.time()*1000)}_{candidate.name}"
                    destPath = targetSubdir / newName
                    shutil.copy2(str(candidate), str(destPath))
                    return f"/media/{targetSubdir.name}/{newName}"
                return ""

            # 导入角色资产
            chars = srcConn.execute("SELECT * FROM characters WHERE drama_id=? AND deleted_at IS NULL", (drama["id"],)).fetchall()
            for ch in chars:
                imgUrl = copyMediaFile(ch["image_url"] or ch["local_path"], ASSETS_DIR)
                eraTag = "前世" if "前世" in (ch["name"] or "") or "前世" in (ch["role"] or "") else "今生"
                dstConn.execute(
                    """
                    INSERT INTO assets (projectId, assetType, name, roleTag, eraTag, visualPrompt, voiceProfile, imageUrl, createdAt, updatedAt)
                    VALUES (?, 'character', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        projectId,
                        ch["name"],
                        ch["role"] or "",
                        eraTag,
                        ch["appearance"] or ch["final_prompt"] or ch["description"] or "",
                        ch["personality"] or "",
                        imgUrl,
                        now,
                        now,
                    ),
                )
                importedSummary["assets"] += 1

            # 导入场景资产
            scenes = srcConn.execute("SELECT * FROM scenes WHERE drama_id=? AND deleted_at IS NULL", (drama["id"],)).fetchall()
            for sc in scenes:
                imgUrl = copyMediaFile(sc["image_url"] or sc["local_path"], ASSETS_DIR)
                dstConn.execute(
                    """
                    INSERT INTO assets (projectId, assetType, name, roleTag, eraTag, visualPrompt, voiceProfile, imageUrl, createdAt, updatedAt)
                    VALUES (?, 'scene', ?, ?, '', ?, '', ?, ?, ?)
                    """,
                    (
                        projectId,
                        sc["location"],
                        sc["time"] or "",
                        sc["prompt"] or sc["final_prompt"] or "",
                        imgUrl,
                        now,
                        now,
                    ),
                )
                importedSummary["assets"] += 1

            # 导入道具资产
            props = srcConn.execute("SELECT * FROM props WHERE drama_id=? AND deleted_at IS NULL", (drama["id"],)).fetchall()
            for pr in props:
                imgUrl = copyMediaFile(pr["image_url"] or pr["local_path"], ASSETS_DIR)
                dstConn.execute(
                    """
                    INSERT INTO assets (projectId, assetType, name, roleTag, eraTag, visualPrompt, voiceProfile, imageUrl, createdAt, updatedAt)
                    VALUES (?, 'prop', ?, ?, '', ?, '', ?, ?, ?)
                    """,
                    (
                        projectId,
                        pr["name"],
                        pr["type"] or "",
                        pr["prompt"] or pr["final_prompt"] or pr["description"] or "",
                        imgUrl,
                        now,
                        now,
                    ),
                )
                importedSummary["assets"] += 1

            # 导入剧集与分镜
            episodes = srcConn.execute("SELECT * FROM episodes WHERE drama_id=? AND deleted_at IS NULL ORDER BY episode_number", (drama["id"],)).fetchall()
            for ep in episodes:
                epCur = dstConn.execute(
                    """
                    INSERT INTO episodes (projectId, episodeNumber, title, novelContent, scriptContent, masterVideoUrl, createdAt, updatedAt)
                    VALUES (?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        projectId,
                        ep["episode_number"] or 1,
                        ep["title"] or f"第 {ep['episode_number']} 集",
                        ep["content"] or "",
                        ep["script_content"] or "",
                        now,
                        now,
                    ),
                )
                episodeId = epCur.lastrowid
                importedSummary["episodes"] += 1

                storyboards = srcConn.execute(
                    "SELECT * FROM storyboards WHERE episode_id=? AND deleted_at IS NULL ORDER BY storyboard_number",
                    (ep["id"],),
                ).fetchall()

                for sb in storyboards:
                    sbDict = dict(sb)
                    rawPlan = sbDict.get("shot_plan") or ""
                    planObj = {}
                    if rawPlan:
                        try:
                            planObj = json.loads(rawPlan)
                        except Exception:
                            planObj = {}
                    if not planObj and sbDict.get("video_prompt") and "H3_SHOT_PLAN_V1" in sbDict["video_prompt"]:
                        try:
                            tail = sbDict["video_prompt"].split("H3_SHOT_PLAN_V1", 1)[1].strip()
                            planObj = json.loads(tail)
                        except Exception:
                            planObj = {}

                    continuity = planObj.get("continuity") or {}
                    contMode = continuity.get("mode") or ("establish" if sbDict["storyboard_number"] == 1 else "same_scene_cut")
                    lockFromPrevEnd = 1 if contMode == "continue_action" else 0
                    axisRule = continuity.get("axis") or "Maintain consistent left-right character axis."
                    intentText = planObj.get("intent") or sbDict.get("description") or ""
                    cameraObj = planObj.get("camera") or {}
                    cameraFraming = cameraObj.get("framing") or sbDict.get("shot_type") or "Medium shot"
                    cameraMovement = cameraObj.get("movement") or sbDict.get("movement") or "Steady camera"

                    firstPrompt, actionArc, lastPrompt = buildKeyframePromptsFromPlan(planObj, sbDict)
                    shotCategory = classifyShotCategory(planObj, sbDict.get("description") or "")
                    soundscape = planObj.get("soundscape") or sbDict.get("sound_effect") or "Realistic physical room tone and movement Foley."
                    dialogues = planObj.get("dialogue") or []
                    references = planObj.get("references") or []
                    checks = planObj.get("checks") or ["主体人物五官与服装一致", "首尾帧动作结果完整达成", "声源与口型准确对齐"]

                    firstImgUrl = copyMediaFile(sbDict.get("first_frame_image") or sbDict.get("composed_image"), KEYFRAMES_DIR)
                    lastImgUrl = copyMediaFile(sbDict.get("last_frame_image"), KEYFRAMES_DIR)
                    videoUrl = copyMediaFile(sbDict.get("video_url"), VIDEOS_DIR)

                    status = "planned"
                    if videoUrl:
                        status = "completed"
                    elif firstImgUrl or lastImgUrl:
                        status = "keyframes_ready"

                    dstConn.execute(
                        """
                        INSERT INTO shots (
                            projectId, episodeId, shotNumber, title, shotCategory, videoEngine,
                            durationSec, continuityMode, lockFromPrevEnd, axisRule, intentText,
                            cameraFraming, cameraMovement, firstFramePrompt, actionArcPrompt,
                            lastFramePrompt, soundscapePrompt, dialogueJson, referenceAssetsJson,
                            checksJson, firstFrameUrl, lastFrameUrl, videoUrl, status, createdAt, updatedAt
                        ) VALUES (?, ?, ?, ?, ?, 'h3_fl2va', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            projectId,
                            episodeId,
                            sbDict["storyboard_number"],
                            sbDict.get("title") or f"镜头 #{sbDict['storyboard_number']}",
                            shotCategory,
                            float(planObj.get("duration_s") or sbDict.get("duration") or 5.0),
                            contMode,
                            lockFromPrevEnd,
                            axisRule,
                            intentText,
                            cameraFraming,
                            cameraMovement,
                            firstPrompt,
                            actionArc,
                            lastPrompt,
                            soundscape,
                            json.dumps(dialogues, ensure_ascii=False),
                            json.dumps(references, ensure_ascii=False),
                            json.dumps(checks, ensure_ascii=False),
                            firstImgUrl,
                            lastImgUrl,
                            videoUrl,
                            status,
                            now,
                            now,
                        ),
                    )
                    importedSummary["shots"] += 1

        dstConn.commit()
    return importedSummary


def seedBuiltinDemoProject():
    """
    当新用户克隆开源仓库首次启动且没有任何项目时，自动创建一个开箱即用的公共演示项目
    《风雪孤驿 (Demo)》，展示 S01 场次空间调度账本、设定资产与 FL2VA 首尾关键帧分镜。
    """
    from .directorPlanner import (
        _deterministicNovelToSceneLedger,
        formatSceneLedgerToMarkdown,
        breakScriptIntoKeyframedShots,
    )

    demoNovel = (
        "边关大雪封山，夜色如墨。巡边刀客沈夜顶着凛冽风雪推开孤驿沉重的木门，大堂火盆噼啪作响，老掌柜在柜台后警惕地举起油灯。\n\n"
        "沈夜反手合拢木门，抖落肩头积雪，一言不发地走到木案前，从怀中取出一枚玄铁青铜虎符重重拍在桌面：“前路封山，这枚虎符够不够换两匹快马？”"
    )
    visualStyle = "3D国漫电影级写实质感 (High-end Chinese 3D CG & live-action realism)"
    now = time.time()

    with closing(getDbConnection()) as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (title, genre, visualStyle, aspectRatio, description, defaultVideoEngine, enableUpscale3d, createdAt, updatedAt)
            VALUES (?, ?, ?, '16:9', ?, 'h3_fl2va', 1, ?, ?)
            """,
            (
                "风雪孤驿 (Demo)",
                "古风/武侠/悬疑",
                visualStyle,
                "内置公共演示项目：展示三阶影视剧本改写、180°空间轴线账本与 FL2VA 首尾关键帧视频管线。",
                now,
                now,
            ),
        )
        projectId = cur.lastrowid

    ledgerRes = _deterministicNovelToSceneLedger(demoNovel, [], visualStyle)
    upsertExtractedAssets(projectId, ledgerRes.get("extractedAssets") or [])
    sceneLedger = ledgerRes.get("sceneLedger") or []
    formattedScript = formatSceneLedgerToMarkdown(sceneLedger)

    with closing(getDbConnection()) as conn:
        assets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (projectId,))]
        epCur = conn.execute(
            """
            INSERT INTO episodes (projectId, episodeNumber, title, novelContent, scriptContent, sceneLedgerJson, masterVideoUrl, createdAt, updatedAt)
            VALUES (?, 1, '第 1 集：风雪叩门', ?, ?, ?, '', ?, ?)
            """,
            (projectId, demoNovel, formattedScript, json.dumps(sceneLedger, ensure_ascii=False), now, now),
        )
        episodeId = epCur.lastrowid

        plannedShots = breakScriptIntoKeyframedShots(formattedScript, assets, visualStyle, sceneLedger=sceneLedger)
        for s in plannedShots:
            conn.execute(
                """
                INSERT INTO shots (
                    projectId, episodeId, shotNumber, title, shotCategory, videoEngine,
                    durationSec, continuityMode, lockFromPrevEnd, axisRule, intentText,
                    cameraFraming, cameraMovement, firstFramePrompt, actionArcPrompt,
                    lastFramePrompt, soundscapePrompt, dialogueJson, referenceAssetsJson,
                    checksJson, status, createdAt, updatedAt
                ) VALUES (?, ?, ?, ?, ?, 'h3_fl2va', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
                """,
                (
                    projectId,
                    episodeId,
                    int(s.get("shotNumber") or 1),
                    s.get("title") or "新镜头",
                    s.get("shotCategory") or "action_fl2va",
                    float(s.get("durationSec") or 5.0),
                    s.get("continuityMode") or "establish",
                    int(s.get("lockFromPrevEnd") or 0),
                    s.get("axisRule") or "",
                    s.get("intentText") or "",
                    s.get("cameraFraming") or "Medium shot",
                    s.get("cameraMovement") or "Steady camera",
                    s.get("firstFramePrompt") or "",
                    s.get("actionArcPrompt") or "",
                    s.get("lastFramePrompt") or "",
                    s.get("soundscapePrompt") or "",
                    json.dumps(s.get("dialogue") or [], ensure_ascii=False),
                    json.dumps(s.get("references") or [], ensure_ascii=False),
                    json.dumps(s.get("checks") or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
        conn.commit()
    return projectId

