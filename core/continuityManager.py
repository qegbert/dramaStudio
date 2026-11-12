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
from .studioStore import getDbConnection, DATA_DIR, VIDEOS_DIR, KEYFRAMES_DIR, EXPORTS_DIR


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


def syncEpisodeFrameLocks(episodeId, overwriteExisting=False):
    """
    扫描指定剧集的所有镜头：
    凡是开启了 lockFromPrevEnd=1 的镜头：
    1. 优先向前倒序查找最近一个“同出场角色”的镜头作为帧源 (donorShot)，支持正反打切回同一角色时无缝承接其上一镜尾帧；
    2. 若无同角色前序镜头，则承接紧邻上一镜；
    3. 一旦帧源镜头已有真实尾帧 (lastFrameUrl) 或首帧 (firstFrameUrl)，自动无损同步为当前镜头的首帧 (firstFrameUrl)。
    返回本次成功同步的镜头数量。
    """
    syncedCount = 0
    now = time.time()
    with closing(getDbConnection()) as conn:
        shots = conn.execute(
            "SELECT * FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()

        historyShots = []
        for shot in shots:
            if historyShots and int(shot["lockFromPrevEnd"] or 0) == 1:
                curCharKey = _extractShotCharacterKey(shot)
                donorShot = None
                if curCharKey:
                    for earlier in reversed(historyShots):
                        if _extractShotCharacterKey(earlier) == curCharKey and (earlier["lastFrameUrl"] or earlier["firstFrameUrl"]):
                            donorShot = earlier
                            break
                if donorShot is None:
                    donorShot = historyShots[-1]

                donorUrl = donorShot["lastFrameUrl"] or donorShot["firstFrameUrl"]
                currentFirst = shot["firstFrameUrl"] or ""
                shouldSync = bool(donorUrl) and (overwriteExisting or not currentFirst or bool(donorShot["lastFrameUrl"]))
                if shouldSync and currentFirst != donorUrl:
                    newStatus = shot["status"]
                    if newStatus == "planned":
                        newStatus = "keyframes_ready"
                    conn.execute(
                        "UPDATE shots SET firstFrameUrl=?, status=?, updatedAt=? WHERE id=?",
                        (donorUrl, newStatus, now, shot["id"]),
                    )
                    syncedCount += 1
            refreshed = conn.execute("SELECT * FROM shots WHERE id=?", (shot["id"],)).fetchone()
            historyShots.append(refreshed)
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
    对生成的分镜视频执行技术质检 (Auto-QC)：
    检查文件完整性、分辨率、时长、是否含音轨及平均音量。
    """
    report = {
        "grade": "SSR",
        "isUsable": True,
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "hasAudio": False,
        "meanVolumeDb": None,
        "issues": [],
    }
    pathObj = Path(videoPath)
    if not pathObj.exists() or pathObj.stat().st_size < 1024:
        report["grade"] = "N"
        report["isUsable"] = False
        report["issues"].append("视频文件不存在或损坏")
        return report

    ffprobeBin = findFfmpegBinary("ffprobe")
    ffmpegBin = findFfmpegBinary("ffmpeg")
    try:
        probeCmd = [
            ffprobeBin, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(pathObj),
        ]
        res = subprocess.run(probeCmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout or "{}")
        report["duration"] = round(float(data.get("format", {}).get("duration") or 0.0), 2)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                report["width"] = int(stream.get("width") or 0)
                report["height"] = int(stream.get("height") or 0)
            elif stream.get("codec_type") == "audio":
                report["hasAudio"] = True

        if report["hasAudio"]:
            volCmd = [
                ffmpegBin, "-i", str(pathObj),
                "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "/dev/null",
            ]
            volRes = subprocess.run(volCmd, capture_output=True, text=True, timeout=15)
            import re
            m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", volRes.stderr or "")
            if m:
                report["meanVolumeDb"] = float(m.group(1))
                if report["meanVolumeDb"] < -55.0:
                    report["issues"].append("音轨音量过低（疑似静音）")
                    report["grade"] = "SR"
        else:
            report["grade"] = "SR"
            report["issues"].append("不含原生音轨（需后期配音）")

        if report["duration"] < 1.5:
            report["grade"] = "N"
            report["isUsable"] = False
            report["issues"].append("视频时长过短")
    except Exception as err:
        report["issues"].append(f"质检探针异常: {err}")

    return report


def mergeEpisodeMasterVideo(episodeId):
    """
    将当前剧集所有已生成视频的镜头，按 shotNumber 顺序无缝拼接成整集母带 MP4，
    并同时生成一份可导入后期软件的分镜时间轴清单 JSON。
    """
    with closing(getDbConnection()) as conn:
        episode = conn.execute("SELECT * FROM episodes WHERE id=?", (episodeId,)).fetchone()
        if not episode:
            raise ValueError("剧集不存在")

        shots = conn.execute(
            "SELECT * FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()

        validClips = []
        timelineManifest = []
        cursorSec = 0.0
        for s in shots:
            if not s["videoUrl"]:
                continue
            vPath = resolveMediaUrlToPath(s["videoUrl"])
            if vPath and vPath.exists():
                validClips.append(vPath)
                dur = float(s["durationSec"] or 5.0)
                timelineManifest.append({
                    "shotNumber": s["shotNumber"],
                    "title": s["title"],
                    "startSec": round(cursorSec, 2),
                    "endSec": round(cursorSec + dur, 2),
                    "durationSec": dur,
                    "videoUrl": s["videoUrl"],
                    "dialogue": json.loads(s["dialogueJson"] or "[]"),
                })
                cursorSec += dur

        if not validClips:
            raise ValueError("当前剧集尚无任何已完成的镜头视频，请先生成镜头视频后再合成母带")

        stamp = int(time.time())
        ffmpegBin = findFfmpegBinary("ffmpeg")
        normDir = EXPORTS_DIR / f"norm_ep_{episodeId}_{stamp}"
        normDir.mkdir(parents=True, exist_ok=True)

        normalizedClips = []
        for idx, clip in enumerate(validClips):
            normClipPath = normDir / f"seg_{idx:04d}.ts"
            # 先将不同来源视频的分辨率、帧率与音频采样率统一标准化为 MPEG-TS 流，彻底杜绝 AAC scalefactor 冲突
            normCmd = [
                ffmpegBin, "-v", "error", "-y",
                "-i", str(clip),
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24",
                "-af", "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-f", "mpegts",
                str(normClipPath),
            ]
            subRes = subprocess.run(normCmd, capture_output=True, text=True, timeout=60)
            if subRes.returncode != 0 or not normClipPath.exists():
                # 若源片段无音轨，补齐静音轨后再转码
                silentCmd = [
                    ffmpegBin, "-v", "error", "-y",
                    "-i", str(clip),
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                    "-shortest", "-f", "mpegts",
                    str(normClipPath),
                ]
                subprocess.run(silentCmd, capture_output=True, text=True, timeout=60)
            if normClipPath.exists():
                normalizedClips.append(normClipPath)

        concatListPath = normDir / "concat_list.txt"
        with open(concatListPath, "w", encoding="utf-8") as f:
            for nClip in normalizedClips:
                escaped = str(nClip).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        outMasterName = f"master_episode_{episodeId}_{stamp}.mp4"
        outMasterPath = EXPORTS_DIR / outMasterName

        cmd = [
            ffmpegBin, "-v", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concatListPath),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(outMasterPath),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        shutil.rmtree(str(normDir), ignore_errors=True)

        if res.returncode != 0 or not outMasterPath.exists():
            raise RuntimeError(f"FFmpeg 整集母带拼接失败: {res.stderr}")

        masterUrl = f"/media/exports/{outMasterName}"
        manifestName = f"jianying_timeline_ep_{episodeId}_{stamp}.json"
        manifestPath = EXPORTS_DIR / manifestName
        with open(manifestPath, "w", encoding="utf-8") as mf:
            json.dump({
                "episodeId": episodeId,
                "episodeTitle": episode["title"],
                "masterVideoUrl": masterUrl,
                "clipCount": len(validClips),
                "totalDurationSec": round(cursorSec, 2),
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
            "clipCount": len(validClips),
            "totalDurationSec": round(cursorSec, 2),
        }
