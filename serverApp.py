#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 主服务端与 REST API (serverApp.py)
100% 独立原创代码，零受限开源协议依赖。
提供：项目管理、剧本首尾帧拆解、Flux3 双关键帧合成、MiniMax-H3 FL2VA / Wan2.2 视频生成、帧锁连续性接力与母带合成。
"""

import os
import re
import sys
import json
import time
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import closing

from core.studioStore import (
    BASE_DIR,
    DATA_DIR,
    initDatabase,
    getDbConnection,
    importFromHuobaoDatabase,
    upsertExtractedAssets,
    seedBuiltinDemoProject,
)
from core.directorPlanner import (
    rewriteNovelToSceneLedger,
    breakScriptIntoKeyframedShots,
    compileFl2vaVideoPrompt,
    compileWan22VideoPrompt,
    compileLockedKeyframePrompt,
)
from core.continuityManager import (
    syncEpisodeFrameLocks,
    extractHighResLastFrameFromVideo,
    mergeEpisodeMasterVideo,
)
from core.comfyBridge import (
    checkComfyStatus,
    enqueueKeyframeTask,
    enqueueShotVideoTask,
    enqueueAssetImageTask,
)

WEB_DIR = BASE_DIR / "web"
SERVER_PORT = int(os.environ.get("DRAMA_STUDIO_PORT", "8688"))


class StudioRequestHandler(BaseHTTPRequestHandler):
    """工业级短剧工作台 HTTP 处理器（支持 JSON API 与视频 Range 流式播放）"""

    def sendJson(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def readJsonBody(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def serveStaticFile(self, filePath):
        """支持 Range 分段流式响应的静态文件服务"""
        if not filePath.exists() or not filePath.is_file():
            self.sendJson({"error": f"文件不存在: {filePath.name}"}, 404)
            return

        mimeType, _ = mimetypes.guess_type(str(filePath))
        if not mimeType:
            mimeType = "application/octet-stream"
        if filePath.suffix == ".js":
            mimeType = "application/javascript; charset=utf-8"
        elif filePath.suffix == ".css":
            mimeType = "text/css; charset=utf-8"
        elif filePath.suffix == ".html":
            mimeType = "text/html; charset=utf-8"

        fileSize = filePath.stat().st_size
        rangeHeader = self.headers.get("Range")
        if rangeHeader and rangeHeader.startswith("bytes="):
            try:
                byteRange = rangeHeader.replace("bytes=", "").split("-")
                start = int(byteRange[0]) if byteRange[0] else 0
                end = int(byteRange[1]) if (len(byteRange) > 1 and byteRange[1]) else fileSize - 1
                end = min(end, fileSize - 1)
                chunkLen = end - start + 1
                with open(filePath, "rb") as f:
                    f.seek(start)
                    data = f.read(chunkLen)
                self.send_response(206)
                self.send_header("Content-Type", mimeType)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{fileSize}")
                self.send_header("Content-Length", str(chunkLen))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception:
                pass

        data = filePath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimeType)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/status":
                with closing(getDbConnection()) as conn:
                    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
                    # 隐藏 API Key 明文但保留是否已配置状态
                    maskedSettings = dict(settings)
                    if maskedSettings.get("llmApiKey"):
                        k = maskedSettings["llmApiKey"]
                        maskedSettings["llmApiKeyMasked"] = k[:6] + "****" + k[-4:] if len(k) > 10 else "****"
                    projCount = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
                    shotCount = conn.execute("SELECT count(*) FROM shots").fetchone()[0]
                comfy = checkComfyStatus()
                return self.sendJson({
                    "ok": True,
                    "comfy": comfy,
                    "settings": maskedSettings,
                    "stats": {"projects": projCount, "shots": shotCount},
                })

            if path == "/api/projects":
                with closing(getDbConnection()) as conn:
                    rows = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY updatedAt DESC")]
                    for r in rows:
                        r["episodeCount"] = conn.execute("SELECT count(*) FROM episodes WHERE projectId=?", (r["id"],)).fetchone()[0]
                        r["shotCount"] = conn.execute("SELECT count(*) FROM shots WHERE projectId=?", (r["id"],)).fetchone()[0]
                        r["assetCount"] = conn.execute("SELECT count(*) FROM assets WHERE projectId=?", (r["id"],)).fetchone()[0]
                return self.sendJson({"ok": True, "projects": rows})

            mWorkspace = re.match(r"^/api/projects/(\d+)/workspace$", path)
            if mWorkspace:
                projectId = int(mWorkspace.group(1))
                qs = parse_qs(parsed.query)
                reqEpId = int(qs["episodeId"][0]) if "episodeId" in qs else None
                with closing(getDbConnection()) as conn:
                    proj = conn.execute("SELECT * FROM projects WHERE id=?", (projectId,)).fetchone()
                    if not proj:
                        return self.sendJson({"error": "项目不存在"}, 404)
                    episodes = [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE projectId=? ORDER BY episodeNumber ASC", (projectId,))]
                    assets = [dict(r) for r in conn.execute("SELECT * FROM assets WHERE projectId=? ORDER BY id ASC", (projectId,))]

                    activeEpisode = None
                    if reqEpId:
                        activeEpisode = next((ep for ep in episodes if ep["id"] == reqEpId), None)
                    if not activeEpisode and episodes:
                        activeEpisode = episodes[0]

                    shots = []
                    if activeEpisode:
                        try:
                            activeEpisode["sceneLedger"] = json.loads(activeEpisode.get("sceneLedgerJson") or "[]")
                        except Exception:
                            activeEpisode["sceneLedger"] = []
                        rawShots = [
                            dict(r)
                            for r in conn.execute(
                                "SELECT * FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
                                (activeEpisode["id"],),
                            )
                        ]
                        for s in rawShots:
                            s["dialogue"] = json.loads(s.get("dialogueJson") or "[]")
                            s["references"] = json.loads(s.get("referenceAssetsJson") or "[]")
                            s["checks"] = json.loads(s.get("checksJson") or "[]")
                            s["qcReport"] = json.loads(s.get("qcReportJson") or "{}")
                            lockedKfPreview = compileLockedKeyframePrompt(s, assets, proj["visualStyle"], frameRole="first")
                            vidPreview = (
                                compileWan22VideoPrompt(s)
                                if s.get("videoEngine") == "wan22_moe"
                                else compileFl2vaVideoPrompt(s, hasLastFrame=(s.get("shotCategory") == "action_fl2va"))
                            )
                            s["compiledPreview"] = f"=== [1] 已注入角色/场景DNA的首帧生成指令 ===\n{lockedKfPreview}\n\n=== [2] 底层视频扩散模型渲染指令 ===\n{vidPreview}"
                            shots.append(s)

                    tasks = [
                        dict(t)
                        for t in conn.execute(
                            """
                            SELECT r.*, s.shotNumber, s.title AS shotTitle
                            FROM renderTasks r
                            LEFT JOIN shots s ON r.shotId = s.id
                            WHERE r.projectId=?
                            ORDER BY r.createdAt DESC LIMIT 30
                            """,
                            (projectId,),
                        )
                    ]

                return self.sendJson({
                    "ok": True,
                    "project": dict(proj),
                    "episodes": episodes,
                    "activeEpisode": activeEpisode,
                    "assets": assets,
                    "shots": shots,
                    "tasks": tasks,
                })

            if path == "/api/tasks":
                with closing(getDbConnection()) as conn:
                    tasks = [
                        dict(t)
                        for t in conn.execute(
                            """
                            SELECT r.*, s.shotNumber, s.title AS shotTitle
                            FROM renderTasks r
                            LEFT JOIN shots s ON r.shotId = s.id
                            ORDER BY r.createdAt DESC LIMIT 40
                            """
                        )
                    ]
                return self.sendJson({"ok": True, "tasks": tasks})

            # 静态媒体文件
            if path.startswith("/media/"):
                rel = path.lstrip("/")
                target = DATA_DIR / rel
                return self.serveStaticFile(target)

            # 前端工作台静态文件
            if path == "/" or path == "/index.html":
                return self.serveStaticFile(WEB_DIR / "index.html")
            webFile = WEB_DIR / path.lstrip("/")
            if webFile.exists() and webFile.is_file():
                return self.serveStaticFile(webFile)

            return self.sendJson({"error": f"未知路径: {path}"}, 404)
        except Exception as err:
            return self.sendJson({"ok": False, "error": str(err)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            body = self.readJsonBody()

            if path == "/api/settings":
                with closing(getDbConnection()) as conn:
                    for key in ("comfyEndpoint", "llmBaseUrl", "llmApiKey", "llmModel", "defaultVideoEngine", "enableUpscale3d", "h3SamplingSteps"):
                        if key in body and body[key] is not None:
                            if key == "llmApiKey" and "****" in str(body[key]):
                                continue
                            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(body[key])))
                    conn.commit()
                return self.sendJson({"ok": True, "comfy": checkComfyStatus()})

            if path == "/api/import/huobao":
                summary = importFromHuobaoDatabase(body.get("sourceDbPath"))
                return self.sendJson({"ok": True, "summary": summary})

            if path == "/api/projects":
                now = time.time()
                title = str(body.get("title") or "未命名短剧项目").strip()
                genre = str(body.get("genre") or "修仙/古风/逆袭").strip()
                style = str(body.get("visualStyle") or "3D国漫电影级写实质感 (High-end Chinese 3D CG & live-action realism)").strip()
                ratio = str(body.get("aspectRatio") or "16:9").strip()
                desc = str(body.get("description") or "").strip()
                novelText = str(body.get("novelContent") or "").strip()

                with closing(getDbConnection()) as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO projects (title, genre, visualStyle, aspectRatio, description, defaultVideoEngine, enableUpscale3d, createdAt, updatedAt)
                        VALUES (?, ?, ?, ?, ?, 'h3_fl2va', 1, ?, ?)
                        """,
                        (title, genre, style, ratio, desc, now, now),
                    )
                    projectId = cur.lastrowid
                    epCur = conn.execute(
                        """
                        INSERT INTO episodes (projectId, episodeNumber, title, novelContent, scriptContent, createdAt, updatedAt)
                        VALUES (?, 1, '第 1 集', ?, ?, ?, ?)
                        """,
                        (projectId, novelText, novelText, now, now),
                    )
                    episodeId = epCur.lastrowid
                    conn.commit()
                return self.sendJson({"ok": True, "projectId": projectId, "episodeId": episodeId})

            mRewrite = re.match(r"^/api/episodes/(\d+)/rewriteScript$", path)
            if mRewrite:
                episodeId = int(mRewrite.group(1))
                now = time.time()
                with closing(getDbConnection()) as conn:
                    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episodeId,)).fetchone()
                    if not ep:
                        return self.sendJson({"error": "剧集不存在"}, 404)
                    proj = conn.execute("SELECT * FROM projects WHERE id=?", (ep["projectId"],)).fetchone()
                    assets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (ep["projectId"],))]
                    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

                novelText = str(body.get("novelContent") or ep["novelContent"] or ep["scriptContent"] or "").strip()
                llmConfig = {
                    "baseUrl": settings.get("llmBaseUrl", ""),
                    "apiKey": settings.get("llmApiKey", ""),
                    "model": settings.get("llmModel", "deepseek-chat"),
                }
                rewritten = rewriteNovelToSceneLedger(novelText, assets, proj["visualStyle"], llmConfig)
                extractedList = rewritten.get("extractedAssets") or []
                if extractedList:
                    # 清理尚未生成定妆图的旧草稿资产，防止多次点击改写时产生同义不同名的重复资产
                    with closing(getDbConnection()) as conn:
                        conn.execute(
                            "DELETE FROM assets WHERE projectId=? AND (imageUrl IS NULL OR imageUrl='')",
                            (ep["projectId"],),
                        )
                        conn.commit()
                assetCountChanged = upsertExtractedAssets(ep["projectId"], extractedList)
                sceneLedger = rewritten.get("sceneLedger") or []
                formattedScript = rewritten.get("formattedScript") or ""

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE episodes SET novelContent=?, scriptContent=?, sceneLedgerJson=?, updatedAt=? WHERE id=?",
                        (novelText, formattedScript, json.dumps(sceneLedger, ensure_ascii=False), now, episodeId),
                    )
                    conn.commit()
                return self.sendJson({
                    "ok": True,
                    "sceneCount": len(sceneLedger),
                    "assetCountChanged": assetCountChanged,
                    "sceneLedger": sceneLedger,
                    "formattedScript": formattedScript,
                })

            mBreak = re.match(r"^/api/episodes/(\d+)/breakScript$", path)
            if mBreak:
                episodeId = int(mBreak.group(1))
                now = time.time()
                with closing(getDbConnection()) as conn:
                    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episodeId,)).fetchone()
                    if not ep:
                        return self.sendJson({"error": "剧集不存在"}, 404)
                    proj = conn.execute("SELECT * FROM projects WHERE id=?", (ep["projectId"],)).fetchone()
                    assets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (ep["projectId"],))]
                    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

                novelOrScript = body.get("novelContent") or body.get("scriptContent") or ep["novelContent"] or ep["scriptContent"]
                llmConfig = {
                    "baseUrl": settings.get("llmBaseUrl", ""),
                    "apiKey": settings.get("llmApiKey", ""),
                    "model": settings.get("llmModel", "deepseek-chat"),
                }

                # 若尚未生成场次空间调度账本，或用户要求先同步执行三阶改写，自动先生成账本与补全资产
                sceneLedger = []
                if not body.get("forceRewrite"):
                    try:
                        sceneLedger = json.loads(ep["sceneLedgerJson"] or "[]")
                    except Exception:
                        sceneLedger = []

                formattedScript = body.get("scriptContent") or ep["scriptContent"] or ""
                if not sceneLedger:
                    rewritten = rewriteNovelToSceneLedger(novelOrScript, assets, proj["visualStyle"], llmConfig)
                    upsertExtractedAssets(ep["projectId"], rewritten.get("extractedAssets") or [])
                    sceneLedger = rewritten.get("sceneLedger") or []
                    formattedScript = rewritten.get("formattedScript") or formattedScript
                    with closing(getDbConnection()) as conn:
                        assets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (ep["projectId"],))]

                plannedShots = breakScriptIntoKeyframedShots(
                    formattedScript or novelOrScript,
                    assets,
                    proj["visualStyle"],
                    llmConfig,
                    sceneLedger=sceneLedger,
                )

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE episodes SET scriptContent=?, sceneLedgerJson=?, updatedAt=? WHERE id=?",
                        (formattedScript or novelOrScript, json.dumps(sceneLedger, ensure_ascii=False), now, episodeId),
                    )
                    conn.execute("DELETE FROM shots WHERE episodeId=?", (episodeId,))
                    for s in plannedShots:
                        conn.execute(
                            """
                            INSERT INTO shots (
                                projectId, episodeId, shotNumber, title, shotCategory, videoEngine,
                                durationSec, continuityMode, lockFromPrevEnd, axisRule, intentText,
                                cameraFraming, cameraMovement, firstFramePrompt, actionArcPrompt,
                                lastFramePrompt, soundscapePrompt, dialogueJson, referenceAssetsJson,
                                checksJson, status, createdAt, updatedAt
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
                            """,
                            (
                                ep["projectId"],
                                episodeId,
                                int(s.get("shotNumber") or 1),
                                s.get("title") or "新镜头",
                                s.get("shotCategory") or "action_fl2va",
                                proj["defaultVideoEngine"] or "h3_fl2va",
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
                return self.sendJson({
                    "ok": True,
                    "shotCount": len(plannedShots),
                    "sceneCount": len(sceneLedger),
                })

            mShotUpdate = re.match(r"^/api/shots/(\d+)/update$", path)
            if mShotUpdate:
                shotId = int(mShotUpdate.group(1))
                now = time.time()
                with closing(getDbConnection()) as conn:
                    shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
                    if not shot:
                        return self.sendJson({"error": "镜头不存在"}, 404)

                    fields = {
                        "title": body.get("title", shot["title"]),
                        "shotCategory": body.get("shotCategory", shot["shotCategory"]),
                        "videoEngine": body.get("videoEngine", shot["videoEngine"]),
                        "durationSec": float(body.get("durationSec", shot["durationSec"])),
                        "continuityMode": body.get("continuityMode", shot["continuityMode"]),
                        "lockFromPrevEnd": int(body.get("lockFromPrevEnd", shot["lockFromPrevEnd"])),
                        "axisRule": body.get("axisRule", shot["axisRule"]),
                        "intentText": body.get("intentText", shot["intentText"]),
                        "cameraFraming": body.get("cameraFraming", shot["cameraFraming"]),
                        "cameraMovement": body.get("cameraMovement", shot["cameraMovement"]),
                        "firstFramePrompt": body.get("firstFramePrompt", shot["firstFramePrompt"]),
                        "actionArcPrompt": body.get("actionArcPrompt", shot["actionArcPrompt"]),
                        "lastFramePrompt": body.get("lastFramePrompt", shot["lastFramePrompt"]),
                        "soundscapePrompt": body.get("soundscapePrompt", shot["soundscapePrompt"]),
                        "dialogueJson": json.dumps(body["dialogue"], ensure_ascii=False) if "dialogue" in body else shot["dialogueJson"],
                        "updatedAt": now,
                    }
                    setClause = ", ".join(f"{k}=?" for k in fields.keys())
                    conn.execute(f"UPDATE shots SET {setClause} WHERE id=?", list(fields.values()) + [shotId])
                    conn.commit()

                syncEpisodeFrameLocks(shot["episodeId"])
                return self.sendJson({"ok": True})

            mGenKf = re.match(r"^/api/shots/(\d+)/generateKeyframes$", path)
            if mGenKf:
                shotId = int(mGenKf.group(1))
                frameRole = str(body.get("frameRole") or "both")
                taskId = enqueueKeyframeTask(shotId, frameRole=frameRole)
                return self.sendJson({"ok": True, "taskId": taskId})

            mGenVid = re.match(r"^/api/shots/(\d+)/generateVideo$", path)
            if mGenVid:
                shotId = int(mGenVid.group(1))
                engineOverride = body.get("videoEngine")
                taskId = enqueueShotVideoTask(shotId, engineOverride=engineOverride)
                return self.sendJson({"ok": True, "taskId": taskId})

            mExtractLast = re.match(r"^/api/shots/(\d+)/extractLastFrame$", path)
            if mExtractLast:
                shotId = int(mExtractLast.group(1))
                lastUrl = extractHighResLastFrameFromVideo(shotId)
                return self.sendJson({"ok": True, "lastFrameUrl": lastUrl})

            mSyncLocks = re.match(r"^/api/episodes/(\d+)/syncFrameLocks$", path)
            if mSyncLocks:
                episodeId = int(mSyncLocks.group(1))
                synced = syncEpisodeFrameLocks(episodeId)
                return self.sendJson({"ok": True, "syncedCount": synced})

            mBatchKf = re.match(r"^/api/episodes/(\d+)/batchKeyframes$", path)
            if mBatchKf:
                episodeId = int(mBatchKf.group(1))
                limitCount = int(body.get("limit") or 50)
                onlyMissing = bool(body.get("onlyMissing", True))
                with closing(getDbConnection()) as conn:
                    if onlyMissing:
                        rows = conn.execute(
                            """
                            SELECT id FROM shots
                            WHERE episodeId=? AND (firstFrameUrl IS NULL OR firstFrameUrl='' OR (shotCategory='action_fl2va' AND (lastFrameUrl IS NULL OR lastFrameUrl='')))
                            ORDER BY shotNumber ASC LIMIT ?
                            """,
                            (episodeId, limitCount),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id FROM shots WHERE episodeId=? ORDER BY shotNumber ASC LIMIT ?",
                            (episodeId, limitCount),
                        ).fetchall()
                taskIds = [enqueueKeyframeTask(r["id"], frameRole="both") for r in rows]
                return self.sendJson({"ok": True, "taskIds": taskIds, "count": len(taskIds)})

            mBatchVid = re.match(r"^/api/episodes/(\d+)/batchVideos$", path)
            if mBatchVid:
                episodeId = int(mBatchVid.group(1))
                onlyPending = bool(body.get("onlyPending", True))
                with closing(getDbConnection()) as conn:
                    if onlyPending:
                        rows = conn.execute(
                            """
                            SELECT id FROM shots
                            WHERE episodeId=? AND (videoUrl IS NULL OR videoUrl='' OR status='failed')
                            ORDER BY shotNumber ASC
                            """,
                            (episodeId,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
                            (episodeId,),
                        ).fetchall()
                taskIds = [enqueueShotVideoTask(r["id"]) for r in rows]
                return self.sendJson({"ok": True, "taskIds": taskIds, "count": len(taskIds)})

            mClearFailed = re.match(r"^/api/projects/(\d+)/clearFailedTasks$", path)
            if mClearFailed:
                projectId = int(mClearFailed.group(1))
                with closing(getDbConnection()) as conn:
                    conn.execute("DELETE FROM renderTasks WHERE projectId=? AND status='failed'", (projectId,))
                    conn.execute(
                        "UPDATE shots SET status=CASE WHEN videoUrl!='' THEN 'completed' WHEN firstFrameUrl!='' THEN 'keyframes_ready' ELSE 'planned' END WHERE projectId=? AND status='failed'",
                        (projectId,),
                    )
                    conn.commit()
                return self.sendJson({"ok": True})

            if path == "/api/assets/create":
                now = time.time()
                projectId = int(body.get("projectId") or 0)
                if not projectId:
                    return self.sendJson({"error": "缺少 projectId"}, 400)
                assetType = str(body.get("assetType") or "character").strip()
                name = str(body.get("name") or "新设定").lstrip("@").strip()
                eraTag = str(body.get("eraTag") or "").strip()
                roleTag = str(body.get("roleTag") or "").strip()
                visualPrompt = str(body.get("visualPrompt") or "").strip()
                with closing(getDbConnection()) as conn:
                    cur = conn.execute(
                        """
                        INSERT INTO assets (projectId, assetType, name, eraTag, roleTag, visualPrompt, imageUrl, createdAt, updatedAt)
                        VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
                        """,
                        (projectId, assetType, name, eraTag, roleTag, visualPrompt, now, now),
                    )
                    conn.commit()
                    newAssetId = cur.lastrowid
                return self.sendJson({"ok": True, "assetId": newAssetId})

            mAssetUpdate = re.match(r"^/api/assets/(\d+)/update$", path)
            if mAssetUpdate:
                assetId = int(mAssetUpdate.group(1))
                now = time.time()
                with closing(getDbConnection()) as conn:
                    row = conn.execute("SELECT * FROM assets WHERE id=?", (assetId,)).fetchone()
                    if not row:
                        return self.sendJson({"error": "资产不存在"}, 404)
                    name = str(body.get("name", row["name"])).lstrip("@").strip()
                    assetType = str(body.get("assetType", row["assetType"])).strip()
                    eraTag = str(body.get("eraTag", row["eraTag"])).strip()
                    roleTag = str(body.get("roleTag", row["roleTag"])).strip()
                    visualPrompt = str(body.get("visualPrompt", row["visualPrompt"])).strip()
                    conn.execute(
                        "UPDATE assets SET name=?, assetType=?, eraTag=?, roleTag=?, visualPrompt=?, updatedAt=? WHERE id=?",
                        (name, assetType, eraTag, roleTag, visualPrompt, now, assetId),
                    )
                    conn.commit()
                return self.sendJson({"ok": True})

            mAssetDelete = re.match(r"^/api/assets/(\d+)/delete$", path)
            if mAssetDelete:
                assetId = int(mAssetDelete.group(1))
                with closing(getDbConnection()) as conn:
                    conn.execute("DELETE FROM assets WHERE id=?", (assetId,))
                    conn.commit()
                return self.sendJson({"ok": True})

            mAssetGenImg = re.match(r"^/api/assets/(\d+)/generateImage$", path)
            if mAssetGenImg:
                assetId = int(mAssetGenImg.group(1))
                taskId = enqueueAssetImageTask(assetId)
                return self.sendJson({"ok": True, "taskId": taskId})

            mBatchAssetImgs = re.match(r"^/api/projects/(\d+)/batchAssetImages$", path)
            if mBatchAssetImgs:
                projectId = int(mBatchAssetImgs.group(1))
                onlyMissing = bool(body.get("onlyMissing", True))
                with closing(getDbConnection()) as conn:
                    if onlyMissing:
                        rows = conn.execute(
                            "SELECT id FROM assets WHERE projectId=? AND (imageUrl IS NULL OR imageUrl='') ORDER BY id ASC",
                            (projectId,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id FROM assets WHERE projectId=? ORDER BY id ASC",
                            (projectId,),
                        ).fetchall()
                taskIds = [enqueueAssetImageTask(r["id"]) for r in rows]
                return self.sendJson({"ok": True, "taskIds": taskIds, "count": len(taskIds)})

            mMerge = re.match(r"^/api/episodes/(\d+)/mergeMaster$", path)
            if mMerge:
                episodeId = int(mMerge.group(1))
                res = mergeEpisodeMasterVideo(episodeId)
                return self.sendJson({"ok": True, **res})

            return self.sendJson({"error": f"未知接口: {path}"}, 404)
        except Exception as err:
            return self.sendJson({"ok": False, "error": str(err)}, 500)

    def log_message(self, fmt, *args):
        pass


def main():
    initDatabase()
    # 若数据库从未初始化过演示项目且为空，首次启动自动初始化公共演示项目
    with closing(getDbConnection()) as conn:
        cnt = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
        seededRow = conn.execute("SELECT value FROM settings WHERE key='demoSeeded'").fetchone()
        alreadySeeded = bool(seededRow and seededRow["value"] == "1")
    if cnt == 0 and not alreadySeeded:
        try:
            demoId = seedBuiltinDemoProject()
            with closing(getDbConnection()) as conn:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demoSeeded', '1')")
                conn.commit()
            print(f"[dramaStudio] 已自动创建内置公共演示项目 (ID={demoId})")
        except Exception as err:
            print(f"[dramaStudio] 创建演示项目跳过: {err}")

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", SERVER_PORT), StudioRequestHandler)
    print(f"============================================================")
    print(f"🎬 DramaForge Studio (自主版权 AI 短剧工业化工作台) 已启动")
    print(f"👉 导演工作台地址: http://127.0.0.1:{SERVER_PORT}")
    print(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dramaStudio] 服务已停止")


if __name__ == "__main__":
    main()
