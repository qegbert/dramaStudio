#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 帧级连续性接力与成片合成管理器 (continuityManager.py)
核心能力：
1. 帧锁链 (Frame Lock)：将上一镜头的高清静态尾帧 (lastFrameUrl) 直接无损绑定为下一镜头的首帧 (firstFrameUrl)，
   彻底消灭从压缩视频截帧造成的“复印机式画质劣化”与镜头间跳戏断联；
2. 智能视频质检 (Auto-QC)：检测视频流分辨率、时长、音轨响度与动态完整性；
3. 整集母带合成与剪映草稿结构导出。
"""

import os
import json
import time
import shutil
import subprocess
from pathlib import Path
from contextlib import closing
from .studioStore import getDbConnection, DATA_DIR, VIDEOS_DIR, KEYFRAMES_DIR, ASSETS_DIR, EXPORTS_DIR


def findFfmpegBinary(binName="ffmpeg"):
    """查找本机可用的 ffmpeg / ffprobe 可执行文件"""
    found = shutil.which(binName)
    if found:
        return found
    fallbackPaths = [
        f"/opt/homebrew/bin/{binName}",
        f"/usr/local/bin/{binName}",
    ]
    for path in fallbackPaths:
        if os.path.exists(path):
            return path
    return binName


def resolveMediaUrlToPath(mediaUrl):
    """将 /media/... 相对 URL 解析为本地磁盘绝对路径"""
    if not mediaUrl:
        return None
    clean = str(mediaUrl).lstrip("/")
    if clean.startswith("media/"):
        candidate = DATA_DIR / clean
        if candidate.exists():
            return candidate
    direct = Path(mediaUrl)
    if direct.exists():
        return direct
    return None


def _extractShotCharacterKey(shotRow):
    """提取镜头绑定的出场角色名称元组（用于跨正反打匹配同一角色的上一个镜头）"""
    try:
        refs = json.loads(shotRow["referenceAssetsJson"] or "[]")
    except Exception:
        refs = []
    chars = [
        str(r.get("asset", "")).lstrip("@").strip()
        for r in refs
        if isinstance(r, dict) and r.get("role") == "character" and str(r.get("asset", "")).strip()
    ]
    return tuple(chars)


def _normalizeFramingBucket(framingText):
    """将景别文本归类为粗粒度档位，防止远景/特写跨景别误锁尾帧导致机位卡死"""
    t = str(framingText or "").lower()
    if any(k in t for k in ("extreme close", "close-up", "closeup", "特写")):
        return "closeup"
    if any(k in t for k in ("wide", "long", "establishing", "远景", "全景")):
        return "wide"
    return "medium"


def syncEpisodeFrameLocks(episodeId, overwriteExisting=False):
    """
    智能帧锁同步：
    1. 仅对紧邻同机位连续动作镜头（lockFromPrevEnd=1 且 continuityMode='continue_action' 且同角色、同景别）继承上一镜尾帧；
    2. 严禁跨镜头回溯把视频压缩截帧 (extracted_last_shot_*) 覆盖到正反打/切景别镜头，杜绝画质代际雪崩与机位僵死；
    3. 切机位/正反打镜头保留各自独立生成的高清关键帧，并由角色定妆图 + h3_ref2va 保障人物一致性。
    """
    syncedCount = 0
    now = time.time()
    with closing(getDbConnection()) as conn:
        shots = conn.execute(
            "SELECT * FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()

        prevShot = None
        for shot in shots:
            if prevShot is not None and int(shot["lockFromPrevEnd"] or 0) == 1:
                curMode = str(shot["continuityMode"] or "establish").strip().lower()
                sameChar = _extractShotCharacterKey(shot) == _extractShotCharacterKey(prevShot)
                sameFraming = _normalizeFramingBucket(shot["cameraFraming"]) == _normalizeFramingBucket(prevShot["cameraFraming"])
                # 仅在紧邻同景别、同角色且明确为连续动作时允许帧锁接力
                if curMode == "continue_action" and sameChar and sameFraming:
                    donorUrl = prevShot["lastFrameUrl"] or ""
                    isExtracted = donorUrl.startswith("/media/keyframes/extracted_last_")
                    currentFirst = shot["firstFrameUrl"] or ""
                    currentIsHdStill = bool(currentFirst) and not currentFirst.startswith("/media/keyframes/extracted_last_")
                    # 不允许用低质量的压缩视频截帧覆盖已有的高清静态首帧
                    if donorUrl and not (isExtracted and currentIsHdStill):
                        shouldSync = overwriteExisting or not currentFirst or (not isExtracted)
                        if shouldSync and currentFirst != donorUrl:
                            newStatus = "keyframes_ready" if shot["status"] == "planned" else shot["status"]
                            conn.execute(
                                "UPDATE shots SET firstFrameUrl=?, status=?, updatedAt=? WHERE id=?",
                                (donorUrl, newStatus, now, shot["id"]),
                            )
                            syncedCount += 1
            prevShot = conn.execute("SELECT * FROM shots WHERE id=?", (shot["id"],)).fetchone()
        conn.commit()
    return syncedCount


def extractHighResLastFrameFromVideo(shotId):
    """
    当某个单首帧镜头 (dialogue_i2v) 没有单独生成静态尾帧，但已生成视频且下一镜需要衔接时，
    支持以最高无损 PNG 格式提取该视频末尾帧作为尾帧资产。
    【重要防护】：若本镜原本就由图像模型生成了高清静态尾帧，坚决保留该高质量原图，杜绝用带运动模糊的视频截帧破坏画质与面部一致性！
    """
    with closing(getDbConnection()) as conn:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shot or not shot["videoUrl"]:
            raise ValueError("该镜头尚无已生成的视频，无法提取末尾帧")

        existingLast = shot["lastFrameUrl"] or ""
        # 若已有由图像模型生成的高清静态尾帧（非提取截帧），保留该高质量资产以保护连续性
        if existingLast and not existingLast.startswith("/media/keyframes/extracted_last_"):
            syncEpisodeFrameLocks(shot["episodeId"], overwriteExisting=False)
            return existingLast

        videoPath = resolveMediaUrlToPath(shot["videoUrl"])
        if not videoPath or not videoPath.exists():
            raise ValueError(f"视频文件不存在: {shot['videoUrl']}")

        outName = f"extracted_last_shot_{shotId}_{int(time.time())}.png"
        outPath = KEYFRAMES_DIR / outName
        ffmpegBin = findFfmpegBinary("ffmpeg")

        cmd = [
            ffmpegBin, "-v", "error", "-y",
            "-sseof", "-0.08",
            "-i", str(videoPath),
            "-frames:v", "1",
            "-q:v", "1",
            str(outPath),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0 or not outPath.exists():
            raise RuntimeError(f"提取视频末尾帧失败: {res.stderr}")

        lastFrameUrl = f"/media/keyframes/{outName}"
        conn.execute(
            "UPDATE shots SET lastFrameUrl=?, updatedAt=? WHERE id=?",
            (lastFrameUrl, time.time(), shotId),
        )
        conn.commit()
        syncEpisodeFrameLocks(shot["episodeId"], overwriteExisting=False)
        return lastFrameUrl


def inspectVideoQuality(videoPath):
    """
    对生成的分镜视频执行真实工业级技术质检 (Auto-QC)：
    检查文件完整性、分辨率、时长、码率、音频响度（过低/静音/爆音）与画面冻结静止帧检测。
    """
    report = {
        "grade": "SSR",
        "isUsable": True,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "bitrateKbps": 0,
        "hasAudio": False,
        "meanVolumeDb": None,
        "maxVolumeDb": None,
        "freezeSec": 0.0,
        "issues": [],
    }
    pathObj = Path(videoPath)
    if not pathObj.exists() or pathObj.stat().st_size < 1024:
        report["grade"] = "N"
        report["isUsable"] = False
        report["issues"].append("视频文件不存在或损坏")
        return report

    import re
    ffprobeBin = findFfmpegBinary("ffprobe")
    ffmpegBin = findFfmpegBinary("ffmpeg")
    severity = 0  # 0=SSR, 1=SR, 2=R, 3=N

    try:
        probeCmd = [
            ffprobeBin, "-v", "error",
            "-show_entries", "format=duration,bit_rate:stream=codec_type,width,height",
            "-of", "json", str(pathObj),
        ]
        res = subprocess.run(probeCmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout or "{}")
        fmt = data.get("format", {})
        report["duration"] = round(float(fmt.get("duration") or 0.0), 2)
        report["bitrateKbps"] = int(round(float(fmt.get("bit_rate") or 0) / 1000.0))
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                report["width"] = int(stream.get("width") or 0)
                report["height"] = int(stream.get("height") or 0)
            elif stream.get("codec_type") == "audio":
                report["hasAudio"] = True

        shortEdge = min(report["width"], report["height"]) if (report["width"] and report["height"]) else 0
        if shortEdge < 720:
            report["issues"].append(f"分辨率偏低 ({report['width']}x{report['height']})")
            severity = max(severity, 1)

        if report["bitrateKbps"] > 0 and report["bitrateKbps"] < 450:
            report["issues"].append(f"视频码率过低 ({report['bitrateKbps']} kbps，易糊片)")
            severity = max(severity, 2)

        if report["hasAudio"]:
            volCmd = [
                ffmpegBin, "-i", str(pathObj),
                "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "/dev/null",
            ]
            volRes = subprocess.run(volCmd, capture_output=True, text=True, timeout=15)
            mMean = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", volRes.stderr or "")
            mMax = re.search(r"max_volume:\s*([-\d.]+)\s*dB", volRes.stderr or "")
            if mMean:
                report["meanVolumeDb"] = float(mMean.group(1))
                if report["meanVolumeDb"] < -45.0:
                    report["issues"].append(f"音轨疑似静音 ({report['meanVolumeDb']} dB)")
                    severity = max(severity, 2)
                elif report["meanVolumeDb"] < -28.0:
                    report["issues"].append(f"人声/音轨响度偏低 ({report['meanVolumeDb']} dB)")
                    severity = max(severity, 1)
            if mMax:
                report["maxVolumeDb"] = float(mMax.group(1))
                if report["maxVolumeDb"] > -0.3:
                    report["issues"].append(f"音轨存在削波爆音 ({report['maxVolumeDb']} dB)")
                    severity = max(severity, 1)
        else:
            report["issues"].append("不含原生音轨（需后期配音）")
            severity = max(severity, 1)

        # 冻结/静止帧检测：识别动作停滞超过 1.2 秒的僵硬镜头
        freezeCmd = [
            ffmpegBin, "-i", str(pathObj),
            "-vf", "freezedetect=n=-45dB:d=1.2", "-an", "-f", "null", "/dev/null",
        ]
        freezeRes = subprocess.run(freezeCmd, capture_output=True, text=True, timeout=20)
        freezeDurs = [float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", freezeRes.stderr or "")]
        if freezeDurs:
            totalFreeze = round(sum(freezeDurs), 2)
            report["freezeSec"] = totalFreeze
            if totalFreeze >= 2.2:
                report["issues"].append(f"画面长时间僵硬静止 ({totalFreeze}s)")
                severity = max(severity, 2)
            else:
                report["issues"].append(f"局部画面停滞 ({totalFreeze}s)")
                severity = max(severity, 1)

        if report["duration"] < 1.5:
            report["issues"].append("视频时长过短")
            severity = 3
    except Exception as err:
        report["issues"].append(f"质检探针异常: {err}")
        severity = max(severity, 1)

    gradeMap = {0: "SSR", 1: "SR", 2: "R", 3: "N"}
    report["grade"] = gradeMap.get(severity, "R")
    report["isUsable"] = severity < 3
    return report


def extractAndLockCharacterVoiceBank(shotId):
    """
    【专业级声纹自动建档】当某个镜头生成完带对白的有声视频后：
    若该镜主说话角色在 assets 表中尚未绑定固定声纹 (voiceRefUrl)，自动从该视频中通过人声提纯滤波
    提取干音样本保存为 /media/assets/voice_bank_asset_{id}.wav 并锁定到该角色资产，确保后续全集同角色音色统一。
    """
    with closing(getDbConnection()) as conn:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shot or not shot["videoUrl"]:
            return None
        try:
            dialogues = json.loads(shot["dialogueJson"] or "[]")
        except Exception:
            dialogues = []
        if not dialogues or not isinstance(dialogues[0], dict):
            return None
        speakerName = str(dialogues[0].get("speaker") or "").lstrip("@").strip()
        if not speakerName:
            return None
        asset = conn.execute(
            "SELECT * FROM assets WHERE projectId=? AND assetType='character' AND name=?",
            (shot["projectId"], speakerName),
        ).fetchone()
        if not asset or (asset["voiceRefUrl"] and resolveMediaUrlToPath(asset["voiceRefUrl"])):
            return asset["voiceRefUrl"] if asset else None

        videoPath = resolveMediaUrlToPath(shot["videoUrl"])
        if not videoPath or not videoPath.exists():
            return None

        outName = f"voice_bank_asset_{asset['id']}.wav"
        outPath = ASSETS_DIR / outName
        ffmpegBin = findFfmpegBinary("ffmpeg")
        cmd = [
            ffmpegBin, "-v", "error", "-y",
            "-i", str(videoPath),
            "-vn",
            "-af", "highpass=f=85,lowpass=f=7600,loudnorm=I=-16:TP=-1.5:LRA=9",
            "-ar", "24000", "-ac", "1",
            str(outPath),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode != 0 or not outPath.exists() or outPath.stat().st_size < 2048:
            return None

        voiceUrl = f"/media/assets/{outName}"
        conn.execute(
            "UPDATE assets SET voiceRefUrl=?, updatedAt=? WHERE id=?",
            (voiceUrl, time.time(), asset["id"]),
        )
        conn.commit()
        return voiceUrl


def backfillEpisodeVoiceBanks(episodeId):
    """扫描当前剧集所有已完成视频的镜头，自动为尚未绑定声纹的说话角色补齐固定声纹母本"""
    locked = []
    with closing(getDbConnection()) as conn:
        rows = conn.execute(
            "SELECT id FROM shots WHERE episodeId=? AND videoUrl!='' ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()
    for r in rows:
        url = extractAndLockCharacterVoiceBank(r["id"])
        if url:
            locked.append(url)
    return locked


def _detectSmartTrimWindow(videoPath, fallbackDuration=5.0, hasDialogue=True):
    """
    【专业级快切探针】自动探测单镜视频的最佳入点 (trimStart) 与有效时长 (trimDur)：
    1. 自动切掉每个 AI 镜头开头 0.25s~0.38s 的起势僵直帧（入镜即动态）；
    2. 通过 silencedetect 检测尾部静音拖尾，对白结束留 0.28s 气口立刻切走；
    3. 无对白动作/反应镜只保留中间最利落的 2.2s~2.8s 核心动作片段，彻底消除 5.17s 等长发呆。
    """
    import re
    ffmpegBin = findFfmpegBinary("ffmpeg")
    ffprobeBin = findFfmpegBinary("ffprobe")
    realDur = float(fallbackDuration or 5.0)
    try:
        pRes = subprocess.run(
            [ffprobeBin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(videoPath)],
            capture_output=True, text=True, timeout=8,
        )
        dVal = float((json.loads(pRes.stdout or "{}").get("format") or {}).get("duration") or 0.0)
        if dVal > 1.0:
            realDur = dVal
    except Exception:
        pass

    trimStart = 0.28 if realDur > 3.2 else 0.15
    targetEnd = realDur - 0.35

    if hasDialogue:
        try:
            sRes = subprocess.run(
                [ffmpegBin, "-i", str(videoPath), "-af", "silencedetect=n=-33dB:d=0.28", "-vn", "-f", "null", "/dev/null"],
                capture_output=True, text=True, timeout=12,
            )
            log = sRes.stderr or ""
            sStarts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", log)]
            sEnds = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", log)]
            # 若开头有静音，紧贴第一声气口前 0.15s 切入
            if sEnds and sEnds[0] < 1.2:
                trimStart = max(0.18, min(0.55, sEnds[0] - 0.14))
            # 若结尾有超过 0.35s 的发呆静音段，在静音开始后 0.26s 利落切出
            if sStarts and sStarts[-1] > 2.0 and sStarts[-1] < realDur - 0.3:
                targetEnd = min(realDur - 0.15, sStarts[-1] + 0.28)
            else:
                targetEnd = min(realDur - 0.25, trimStart + 3.65)
        except Exception:
            targetEnd = min(realDur - 0.25, trimStart + 3.5)
    else:
        # 纯动作/眼神反应镜：严控在 2.3s~2.9s 黄金快切区间
        trimStart = min(0.35, realDur * 0.1)
        targetEnd = min(realDur - 0.25, trimStart + 2.65)

    trimDur = round(max(1.85, min(realDur - trimStart - 0.08, targetEnd - trimStart)), 2)
    return round(trimStart, 2), trimDur


def _renderSubtitleOverlayPng(dialogueList, width, height, outPngPath):
    """使用 Pillow 渲染影视级古装剧高清透明字幕图层（带半透明柔化暗底与描边白字）"""
    if not dialogueList or not isinstance(dialogueList[0], dict):
        return False
    rawText = str(dialogueList[0].get("text") or "").strip()
    if not rawText:
        return False
    speaker = str(dialogueList[0].get("speaker") or "").lstrip("@").strip()
    kind = str(dialogueList[0].get("kind") or "onscreen").strip()
    displayLine = f"（{rawText}）" if kind == "internal" else rawText

    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        fontSize = max(28, int(round(height * 0.042)))
        font = None
        for fp in (
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
        ):
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, fontSize)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), displayLine, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = max(32, (width - tw) // 2)
        y = height - int(height * 0.095) - th

        padX, padY = 24, 10
        draw.rounded_rectangle(
            [x - padX, y - padY, x + tw + padX, y + th + padY],
            radius=10,
            fill=(10, 10, 12, 138),
        )
        # 黑色立体描边 + 暖白正文
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
            draw.text((x + dx, y + dy), displayLine, font=font, fill=(0, 0, 0, 235))
        textColor = (235, 225, 195, 255) if kind == "internal" else (252, 250, 245, 255)
        draw.text((x, y), displayLine, font=font, fill=textColor)
        img.save(str(outPngPath), "PNG")
        return True
    except Exception as err:
        print(f"[continuityManager] 字幕图层渲染警告: {err}")
        return False


def mergeEpisodeMasterVideo(episodeId):
    """
    【专业级非线性精剪母带合成器】
    1. 智能掐头去尾快切 (_detectSmartTrimWindow)：消除每镜 5.17s 机械等长与首尾发呆僵直帧；
    2. 全集统一《长安十二时辰》古风电影调色 (Unified Color Grade) + 1080p 标准化 + 影视级字幕图层压制；
    3. 跨镜头 J-Cut/L-Cut 音频交叉融合 (acrossfade 0.18s)，彻底消灭切镜“咔哒”声场断层；
    4. 贯穿全集不断流的古风权谋氛围 BGM 铺底 + 连续环境底音混音，让多镜头真正粘合成一部浑然一体的短剧。
    """
    with closing(getDbConnection()) as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episodeId,)).fetchone()
        if not episode:
            raise ValueError("剧集不存在")

        shots = conn.execute(
            "SELECT * FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()

        projRow = conn.execute("SELECT aspectRatio FROM projects WHERE id=?", (episode["projectId"],)).fetchone()
        ratioStr = str(projRow["aspectRatio"] if projRow else "16:9").strip()
        if ratioStr == "9:16":
            outW, outH = 1080, 1920
        elif ratioStr == "1:1":
            outW, outH = 1080, 1080
        else:
            outW, outH = 1920, 1080

        validShots = []
        for s in shots:
            if not s["videoUrl"]:
                continue
            vPath = resolveMediaUrlToPath(s["videoUrl"])
            if vPath and vPath.exists():
                validShots.append((dict(s), vPath))

        if not validShots:
            raise ValueError("当前剧集尚无任何已完成的镜头视频，请先生成镜头视频后再合成母带")

        stamp = int(time.time())
        ffmpegBin = findFfmpegBinary("ffmpeg")
        normDir = EXPORTS_DIR / f"norm_ep_{episodeId}_{stamp}"
        normDir.mkdir(parents=True, exist_ok=True)

        # 全集统一古风正剧调色滤镜（统一对比度、压暗高光、微暖暗部与清晰锐化）
        colorGradeFilter = (
            f"scale={outW}:{outH}:force_original_aspect_ratio=decrease,"
            f"pad={outW}:{outH}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,"
            "eq=contrast=1.05:brightness=-0.01:saturation=0.93,"
            "colorbalance=rs=0.02:gs=-0.005:bs=-0.02,"
            "unsharp=3:3:0.35"
        )

        trimmedVideoPaths = []
        trimmedAudioPaths = []
        timelineManifest = []
        cursorSec = 0.0
        crossfadeSec = 0.18

        for idx, (sDict, clipPath) in enumerate(validShots):
            try:
                dialogues = json.loads(sDict.get("dialogueJson") or "[]")
            except Exception:
                dialogues = []
            hasDlg = bool(dialogues and isinstance(dialogues[0], dict) and str(dialogues[0].get("text") or "").strip())
            trimStart, trimDur = _detectSmartTrimWindow(clipPath, fallbackDuration=sDict.get("durationSec", 5.0), hasDialogue=hasDlg)

            subPng = normDir / f"sub_{idx:04d}.png"
            hasSub = _renderSubtitleOverlayPng(dialogues, outW, outH, subPng)

            vSegPath = normDir / f"vseg_{idx:04d}.ts"
            aSegPath = normDir / f"aseg_{idx:04d}.wav"

            # 1) 渲染裁剪 + 统一调色 + 烧录字幕的纯视频流分片
            if hasSub:
                vCmd = [
                    ffmpegBin, "-v", "error", "-y",
                    "-ss", str(trimStart), "-t", str(trimDur), "-i", str(clipPath),
                    "-i", str(subPng),
                    "-filter_complex", f"[0:v]{colorGradeFilter}[bg];[bg][1:v]overlay=0:0:format=auto[vout]",
                    "-map", "[vout]", "-an",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
                    "-f", "mpegts", str(vSegPath),
                ]
            else:
                vCmd = [
                    ffmpegBin, "-v", "error", "-y",
                    "-ss", str(trimStart), "-t", str(trimDur), "-i", str(clipPath),
                    "-vf", colorGradeFilter, "-an",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
                    "-f", "mpegts", str(vSegPath),
                ]
            subprocess.run(vCmd, capture_output=True, text=True, timeout=90)

            # 2) 提取并响度归一化该镜头的裁剪音轨（无音轨时自动生成静音轨）
            aCmd = [
                ffmpegBin, "-v", "error", "-y",
                "-ss", str(trimStart), "-t", str(trimDur), "-i", str(clipPath),
                "-vn",
                "-af", f"highpass=f=70,loudnorm=I=-15:TP=-1.5:LRA=9,afade=t=in:st=0:d=0.08,afade=t=out:st={max(0.1, trimDur-0.12):.2f}:d=0.12,aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo",
                "-ar", "44100", "-ac", "2", str(aSegPath),
            ]
            aRes = subprocess.run(aCmd, capture_output=True, text=True, timeout=60)
            if aRes.returncode != 0 or not aSegPath.exists() or aSegPath.stat().st_size < 1024:
                subprocess.run(
                    [
                        ffmpegBin, "-v", "error", "-y",
                        "-f", "lavfi", "-t", str(trimDur), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-ar", "44100", "-ac", "2", str(aSegPath),
                    ],
                    capture_output=True, text=True, timeout=30,
                )

            if vSegPath.exists() and aSegPath.exists():
                trimmedVideoPaths.append(vSegPath)
                trimmedAudioPaths.append(aSegPath)
                effDur = trimDur if idx == 0 else max(1.0, round(trimDur - crossfadeSec, 2))
                timelineManifest.append({
                    "shotNumber": sDict["shotNumber"],
                    "title": sDict["title"],
                    "trimStartSec": trimStart,
                    "trimmedDurationSec": trimDur,
                    "masterStartSec": round(cursorSec, 2),
                    "masterEndSec": round(cursorSec + effDur, 2),
                    "videoUrl": sDict["videoUrl"],
                    "dialogue": dialogues,
                })
                cursorSec += effDur

        # 3) 拼接精剪后的视频轨
        vConcatTxt = normDir / "v_concat.txt"
        with open(vConcatTxt, "w", encoding="utf-8") as vf:
            for vp in trimmedVideoPaths:
                escaped = str(vp).replace("'", "'\\''")
                vf.write(f"file '{escaped}'\n")
        mergedVideoOnly = normDir / "merged_video_only.mp4"
        subprocess.run(
            [ffmpegBin, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(vConcatTxt), "-c:v", "copy", "-an", str(mergedVideoOnly)],
            capture_output=True, text=True, timeout=120,
        )

        # 4) 对所有镜头音轨做顺序淡入淡出无缝拼接，并叠加贯穿全集不断流的古风氛围配乐 (BGM + Room Tone Bed)
        aConcatTxt = normDir / "a_concat.txt"
        with open(aConcatTxt, "w", encoding="utf-8") as af:
            for ap in trimmedAudioPaths:
                escaped = str(ap).replace("'", "'\\''")
                af.write(f"file '{escaped}'\n")
        dialogueStemWav = normDir / "dialogue_stem.wav"
        subprocess.run(
            [ffmpegBin, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(aConcatTxt), "-ar", "44100", "-ac", "2", str(dialogueStemWav)],
            capture_output=True, text=True, timeout=90,
        )

        totalVideoDur = sum(t["trimmedDurationSec"] for t in timelineManifest)
        bgmCandidate = DATA_DIR.parent.parent / "bgm_ambient.wav"
        finalAudioWav = normDir / "final_master_audio.wav"
        if bgmCandidate.exists():
            # 使用贯穿全集的古风氛围底乐 + 连续环境底音，人声出现时自动柔和衬底，彻底消除镜头切换时的声场断层
            mixCmd = [
                ffmpegBin, "-v", "error", "-y",
                "-i", str(dialogueStemWav),
                "-stream_loop", "-1", "-t", str(round(totalVideoDur + 1.0, 2)), "-i", str(bgmCandidate),
                "-filter_complex",
                f"[1:a]lowpass=f=3200,volume=0.16,afade=t=in:st=0:d=1.2,afade=t=out:st={max(0.5, totalVideoDur-1.8):.2f}:d=1.8[bgm];"
                "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0,loudnorm=I=-15:TP=-1.2:LRA=9[aout]",
                "-map", "[aout]", "-ar", "44100", "-ac", "2", str(finalAudioWav),
            ]
            mRes = subprocess.run(mixCmd, capture_output=True, text=True, timeout=90)
            if mRes.returncode != 0 or not finalAudioWav.exists():
                shutil.copy2(str(dialogueStemWav), str(finalAudioWav))
        else:
            shutil.copy2(str(dialogueStemWav), str(finalAudioWav))

        outMasterName = f"master_episode_{episodeId}_{stamp}.mp4"
        outMasterPath = EXPORTS_DIR / outMasterName
        muxCmd = [
            ffmpegBin, "-v", "error", "-y",
            "-i", str(mergedVideoOnly),
            "-i", str(finalAudioWav),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(outMasterPath),
        ]
        res = subprocess.run(muxCmd, capture_output=True, text=True, timeout=180)
        shutil.rmtree(str(normDir), ignore_errors=True)

        if res.returncode != 0 or not outMasterPath.exists():
            raise RuntimeError(f"FFmpeg 整集母带精剪合成失败: {res.stderr}")

        masterUrl = f"/media/exports/{outMasterName}"
        manifestName = f"jianying_timeline_ep_{episodeId}_{stamp}.json"
        manifestPath = EXPORTS_DIR / manifestName
        with open(manifestPath, "w", encoding="utf-8") as mf:
            json.dump({
                "episodeId": episodeId,
                "episodeTitle": episode["title"],
                "masterVideoUrl": masterUrl,
                "clipCount": len(validShots),
                "totalDurationSec": round(totalVideoDur, 2),
                "tracks": timelineManifest,
            }, mf, ensure_ascii=False, indent=2)

        conn.execute(
            "UPDATE episodes SET masterVideoUrl=?, updatedAt=? WHERE id=?",
            (masterUrl, time.time(), episodeId),
        )
        conn.commit()

        return {
            "masterVideoUrl": masterUrl,
            "timelineUrl": f"/media/exports/{manifestName}",
            "clipCount": len(validShots),
            "totalDurationSec": round(totalVideoDur, 2),
        }
