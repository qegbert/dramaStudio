#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 无影云端 IndexTTS-2 配音先行桥接层 (ttsBridge.py)
核心职责（Audio-First 商业短剧管线）：
1. 对接无影云电脑上部署的 IndexTTS-2 零样本声纹克隆配音微服务（默认 8895 端口，按需加载、配完即释放显存）；
2. 自动匹配发言角色的固定声纹母本 (assets.voiceRefUrl)，生成高情感演技的中文影视配音 WAV；
3. 用 ffprobe 精确测算每句台词真实秒数，反推并锁死该镜头时长与 H3 帧数网格，彻底消除台词腰斩与跨镜音色飘移。
"""

import os
import json
import time
import uuid
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from contextlib import closing

from .studioStore import AUDIO_DIR, getDbConnection
from .continuityManager import findFfmpegBinary, resolveMediaUrlToPath, backfillEpisodeVoiceBanks

_DIRECT_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def getTtsEndpoint():
    """读取配置的 IndexTTS-2 服务端点（默认推导为与 ComfyUI 同主机 8895 端口）"""
    with closing(getDbConnection()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='ttsEndpoint'").fetchone()
        if row and str(row["value"] or "").strip():
            return str(row["value"]).strip().rstrip("/")
        comfyRow = conn.execute("SELECT value FROM settings WHERE key='comfyEndpoint'").fetchone()
        if comfyRow and str(comfyRow["value"] or "").strip():
            ep = str(comfyRow["value"]).strip()
            if "://" in ep:
                hostPart = ep.split("://", 1)[1].split("/", 1)[0].split(":")[0]
                return f"http://{hostPart}:8895"
    return os.environ.get("WUYING_TTS_ENDPOINT", "http://100.70.124.92:8895").rstrip("/")


def checkTtsStatus():
    """探测无影云端 IndexTTS-2 配音微服务在线状态"""
    endpoint = getTtsEndpoint()
    try:
        req = urllib.request.Request(f"{endpoint}/health", method="GET")
        with _DIRECT_HTTP_OPENER.open(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "online": True,
                "endpoint": endpoint,
                "engine": data.get("engine", "IndexTTS-2"),
                "device": data.get("device", "cuda"),
                "loadedInVram": bool(data.get("loadedInVram", False)),
            }
    except Exception as err:
        return {
            "online": False,
            "endpoint": endpoint,
            "error": str(err),
        }


def _probeAudioDurationSec(audioPath):
    """使用 ffprobe 精确读取音频文件时长（秒）"""
    ffprobeBin = findFfmpegBinary("ffprobe")
    try:
        res = subprocess.run(
            [ffprobeBin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audioPath)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(res.stdout or "{}")
        return round(float((data.get("format") or {}).get("duration") or 0.0), 3)
    except Exception:
        return 0.0


def _encodeMultipartForm(fields, fileFieldName, filePath):
    """手写零依赖 multipart/form-data 编码，用于向 IndexTTS-2 微服务上传参考声纹 WAV"""
    boundary = f"----DramaForgeTTS{uuid.uuid4().hex}"
    chunks = []
    for k, v in fields.items():
        if v is None:
            continue
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(v).encode("utf-8"))
        chunks.append(b"\r\n")

    if filePath and Path(filePath).exists():
        rawBytes = Path(filePath).read_bytes()
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{fileFieldName}"; filename="{Path(filePath).name}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n".encode("utf-8")
        )
        chunks.append(rawBytes)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _findSpeakerVoiceRef(projectId, speakerName):
    """按发言人名称在项目资产库中查找已锁定的固定声纹参考音频"""
    cleanName = str(speakerName or "").lstrip("@").strip()
    if not cleanName:
        return None, ""
    with closing(getDbConnection()) as conn:
        rows = conn.execute(
            "SELECT * FROM assets WHERE projectId=? AND assetType='character'",
            (projectId,),
        ).fetchall()
    for r in rows:
        aName = str(r["name"] or "").lstrip("@").strip()
        if aName == cleanName or cleanName in aName or aName in cleanName:
            refUrl = str(r["voiceRefUrl"] or "").strip()
            refPath = resolveMediaUrlToPath(refUrl) if refUrl else None
            if refPath and refPath.exists():
                return refPath, str(r["voiceProfile"] or "")
            return None, str(r["voiceProfile"] or "")
    return None, ""


def synthesizeShotDialogueAudio(shotId, forceRegenerate=False):
    """
    为单个镜头预先生成 IndexTTS-2 固定声纹配音 WAV：
    - 若镜头无台词，返回 skipped；
    - 若已有配音且 forceRegenerate=False，直接复用；
    - 生成后自动用 ffprobe 测算真实音频秒数，并同步更新 shots.ttsAudioUrl、shots.ttsDurationSec 与 shots.durationSec。
    """
    with closing(getDbConnection()) as conn:
        shotRow = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shotRow:
            raise ValueError(f"未找到镜头 #{shotId}")
        shot = dict(shotRow)

    try:
        dialogues = json.loads(shot.get("dialogueJson") or "[]")
    except Exception:
        dialogues = []

    validLines = [d for d in dialogues if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if not validLines:
        return {"status": "skipped", "reason": "本镜无台词", "shotId": shotId}

    existingUrl = str(shot.get("ttsAudioUrl") or "").strip()
    existingPath = resolveMediaUrlToPath(existingUrl) if existingUrl else None
    if (not forceRegenerate) and existingPath and existingPath.exists() and existingPath.stat().st_size > 2048:
        durSec = _probeAudioDurationSec(existingPath)
        return {
            "status": "reused",
            "shotId": shotId,
            "ttsAudioUrl": existingUrl,
            "ttsDurationSec": durSec,
        }

    # 先尝试从同项目已生成视频回填缺失的角色声纹库
    try:
        backfillEpisodeVoiceBanks(shot["episodeId"])
    except Exception:
        pass

    line = validLines[0]
    text = str(line.get("text") or "").strip()
    speaker = str(line.get("speaker") or "").lstrip("@").strip()
    delivery = str(line.get("deliveryCn") or line.get("delivery") or "").strip()

    voiceRefPath, voiceProfile = _findSpeakerVoiceRef(shot["projectId"], speaker)
    endpoint = getTtsEndpoint()

    bodyBytes, contentType = _encodeMultipartForm(
        {
            "text": text,
            "speaker": speaker,
            "emo_text": delivery or voiceProfile or "自然克制，戏剧张力",
            "unload_after": "1",
        },
        "ref_audio",
        voiceRefPath,
    )
    req = urllib.request.Request(
        f"{endpoint}/synthesize",
        data=bodyBytes,
        headers={"Content-Type": contentType},
        method="POST",
    )
    try:
        with _DIRECT_HTTP_OPENER.open(req, timeout=180) as resp:
            wavBytes = resp.read()
    except urllib.error.HTTPError as httpErr:
        detail = httpErr.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"无影 IndexTTS-2 配音接口返回错误 ({httpErr.code}): {detail[:240]}")
    except Exception as err:
        raise RuntimeError(f"连接无影 IndexTTS-2 配音服务失败 ({endpoint}): {err}")

    if not wavBytes or len(wavBytes) < 1024:
        raise RuntimeError("无影 IndexTTS-2 返回的音频内容为空或过短")

    fileName = f"shot_{shotId}_tts_{int(time.time())}.wav"
    outPath = AUDIO_DIR / fileName
    rawPath = AUDIO_DIR / f"raw_{fileName}"
    rawPath.write_bytes(wavBytes)

    # 商业短剧配音节奏校准：将偏慢的默认语速稳妥提速至 1.22x，并在开头保留 0.35s 张嘴起手缓冲静音，防止第 0.00s 闭嘴出声
    ffmpegBin = findFfmpegBinary("ffmpeg")
    speedOk = False
    try:
        spRes = subprocess.run(
            [
                ffmpegBin, "-v", "error", "-y", "-i", str(rawPath),
                "-af", "silenceremove=start_periods=1:start_threshold=-42dB:stop_periods=-1:stop_duration=0.15:stop_threshold=-42dB,atempo=1.22,adelay=350|350,highpass=f=75",
                "-ar", "22050", "-ac", "1", str(outPath),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if spRes.returncode == 0 and outPath.exists() and outPath.stat().st_size > 1024:
            speedOk = True
    except Exception:
        pass
    if not speedOk:
        outPath.write_bytes(wavBytes)
    if rawPath.exists():
        try:
            rawPath.unlink()
        except Exception:
            pass

    relUrl = f"/media/audio/{fileName}"

    audioDur = _probeAudioDurationSec(outPath)
    # 根据真实配音时长反推紧凑镜头时长（保留 0.35s 呼吸气口，上限 7.0s，下限 2.6s）
    recommendedShotDur = round(min(7.0, max(2.6, audioDur + 0.35)), 2) if audioDur > 0.2 else float(shot.get("durationSec") or 4.5)

    with closing(getDbConnection()) as conn:
        conn.execute(
            "UPDATE shots SET ttsAudioUrl=?, ttsDurationSec=?, durationSec=?, updatedAt=? WHERE id=?",
            (relUrl, audioDur, recommendedShotDur, time.time(), shotId),
        )
        conn.commit()

    return {
        "status": "generated",
        "shotId": shotId,
        "speaker": speaker,
        "text": text,
        "usedVoiceRef": str(voiceRefPath.name) if voiceRefPath else "default",
        "ttsAudioUrl": relUrl,
        "ttsDurationSec": audioDur,
        "recommendedShotDur": recommendedShotDur,
    }


def synthesizeEpisodeDialogues(episodeId, forceRegenerate=False):
    """一键为整集所有含台词的镜头预先生成 IndexTTS-2 固定声纹配音音轨"""
    with closing(getDbConnection()) as conn:
        shots = conn.execute(
            "SELECT id, shotNumber FROM shots WHERE episodeId=? ORDER BY shotNumber ASC",
            (episodeId,),
        ).fetchall()

    results = []
    for s in shots:
        res = synthesizeShotDialogueAudio(s["id"], forceRegenerate=forceRegenerate)
        res["shotNumber"] = s["shotNumber"]
        results.append(res)
    return {
        "episodeId": episodeId,
        "totalShots": len(shots),
        "generatedCount": sum(1 for r in results if r.get("status") == "generated"),
        "reusedCount": sum(1 for r in results if r.get("status") == "reused"),
        "results": results,
    }
