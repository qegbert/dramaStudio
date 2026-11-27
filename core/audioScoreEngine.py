#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 商业级动态配乐与拟音音效踩点引擎 (audioScoreEngine.py)
核心能力：
1. 【全自动动作与转场拟音踩点 (buildEpisodeSfxStem)】：
   - 零外部依赖生成高品质 44.1kHz 立体声 WAV 影视拟音素材（电影重低音冲击 Boom、情绪反转上升音 Riser、拍桌木响 Slam、纸张翻动 Rustle、木匣推拉声）；
   - 按分镜时间轴与动作/台词语义自动在毫秒级切点混入对应 SFX，彻底解决短剧“发飘、没重量”的问题。
2. 【无影云端 MiniMax-Music 3 / ACE-Step 1.5 动态配乐桥接 (generateEpisodeBgmWithCloud)】：
   - 依据剧集风格、剧情情绪自动生成专属 BGM 提示词并提交无影 ComfyUI 渲染，缓存入库；云端未开机时平滑回退默认氛围底乐。
"""

import os
import json
import math
import time
import wave
import random
import struct
import subprocess
from pathlib import Path
from contextlib import closing

from .studioStore import AUDIO_DIR, getDbConnection


def _writeWavMonoToStereo(samples, outPath, sampleRate=44100):
    """将归一化 [-1.0, 1.0] 浮点采样数组写入 16-bit 立体声 WAV 文件"""
    outPath = Path(outPath)
    outPath.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(outPath), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sampleRate)
        buf = bytearray()
        for val in samples:
            clamped = max(-0.98, min(0.98, float(val)))
            pcm = int(clamped * 32767.0)
            buf.extend(struct.pack("<hh", pcm, pcm))
        wf.writeframes(bytes(buf))


def ensureSfxLibrary(sfxDir=None):
    """生成并缓存一套影视短剧标准基础拟音库（Boom / Riser / Slam / Paper / WoodBox）"""
    if sfxDir is None:
        sfxDir = AUDIO_DIR / "sfx_bank"
    sfxDir = Path(sfxDir)
    sfxDir.mkdir(parents=True, exist_ok=True)
    sr = 44100

    # 1. 电影重低音冲击 (Cinematic Sub Boom, 1.4s)：48Hz->28Hz 指数衰减 + 轻微瞬态击打
    boomPath = sfxDir / "cinematic_boom.wav"
    if not boomPath.exists():
        n = int(sr * 1.4)
        samples = []
        for i in range(n):
            t = i / sr
            freq = 56.0 * math.exp(-t * 2.2) + 28.0
            env = math.exp(-t * 3.1) * (1.0 - math.exp(-t * 140.0))
            hit = math.sin(2.0 * math.pi * freq * t) * 0.78 + math.sin(2.0 * math.pi * 110.0 * t) * math.exp(-t * 18.0) * 0.25
            samples.append(hit * env * 0.85)
        _writeWavMonoToStereo(samples, boomPath, sr)

    # 2. 情绪反转上升气流 (Tension Riser, 1.2s)：带通滤波噪声渐强 + 低频铺垫
    riserPath = sfxDir / "tension_riser.wav"
    if not riserPath.exists():
        n = int(sr * 1.2)
        samples = []
        rng = random.Random(42)
        lp = 0.0
        for i in range(n):
            t = i / sr
            prog = t / 1.2
            env = (prog ** 2.1) * (1.0 if t < 1.12 else max(0.0, (1.2 - t) / 0.08))
            white = rng.uniform(-1.0, 1.0)
            alpha = 0.03 + 0.22 * prog
            lp = lp + alpha * (white - lp)
            tone = math.sin(2.0 * math.pi * (85.0 + 190.0 * (prog ** 1.6)) * t) * 0.25
            samples.append((lp * 0.55 + tone) * env * 0.65)
        _writeWavMonoToStereo(samples, riserPath, sr)

    # 3. 拍桌/重物落案木响 (Table Slam, 0.55s)：短促低频共振 + 木质瞬态噪声
    slamPath = sfxDir / "table_slam.wav"
    if not slamPath.exists():
        n = int(sr * 0.55)
        samples = []
        rng = random.Random(101)
        for i in range(n):
            t = i / sr
            wood = math.sin(2.0 * math.pi * 115.0 * t) * math.exp(-t * 24.0) * 0.65
            thump = math.sin(2.0 * math.pi * 62.0 * t) * math.exp(-t * 14.0) * 0.55
            crack = rng.uniform(-1.0, 1.0) * math.exp(-t * 85.0) * 0.45
            samples.append((wood + thump + crack) * 0.85)
        _writeWavMonoToStereo(samples, slamPath, sr)

    # 4. 纸张翻动/展开账页 (Paper Rustle, 0.65s)：高频带通调制摩擦声
    paperPath = sfxDir / "paper_rustle.wav"
    if not paperPath.exists():
        n = int(sr * 0.65)
        samples = []
        rng = random.Random(202)
        prev = 0.0
        for i in range(n):
            t = i / sr
            env = math.sin(math.pi * (t / 0.65)) ** 1.4 * (0.65 + 0.35 * math.sin(2.0 * math.pi * 9.0 * t))
            w = rng.uniform(-1.0, 1.0)
            hp = w - prev * 0.78
            prev = w
            samples.append(hp * env * 0.42)
        _writeWavMonoToStereo(samples, paperPath, sr)

    # 5. 木匣推拉轻叩声 (Wood Box Slide, 0.70s)：低阻尼木面摩擦
    boxPath = sfxDir / "wood_box_slide.wav"
    if not boxPath.exists():
        n = int(sr * 0.70)
        samples = []
        rng = random.Random(303)
        lp = 0.0
        for i in range(n):
            t = i / sr
            env = math.sin(math.pi * (t / 0.70)) ** 1.2
            w = rng.uniform(-1.0, 1.0)
            lp = lp + 0.08 * (w - lp)
            knock = math.sin(2.0 * math.pi * 165.0 * t) * math.exp(-max(0.0, t - 0.42) * 32.0) * (1.0 if t >= 0.42 else 0.0) * 0.45
            samples.append((lp * 0.48 + knock) * env * 0.60)
        _writeWavMonoToStereo(samples, boxPath, sr)

    return {
        "boom": boomPath,
        "riser": riserPath,
        "slam": slamPath,
        "paper": paperPath,
        "box": boxPath,
    }


def _detectShotSfxTriggers(shotDict, masterStartSec, trimmedDur):
    """根据镜头的动作、音效描述和剧情角色自动匹配精准毫秒级的拟音触发点"""
    text = (
        f"{shotDict.get('title', '')} {shotDict.get('actionArcPrompt', '')} "
        f"{shotDict.get('soundscapePrompt', '')} {shotDict.get('intentText', '')}"
    ).lower()
    triggers = []

    if any(k in text for k in ("slam", "kick", "bang", "thud", "拍", "踹", "砸", "摔", "重重")):
        triggers.append(("slam", max(0.05, masterStartSec + 0.25), 0.72))

    if any(k in text for k in ("ledger", "paper", "page", "letter", "账", "纸", "折", "信")):
        triggers.append(("paper", max(0.1, masterStartSec + 0.35), 0.55))

    if any(k in text for k in ("box", "lid", "sandalwood", "匣", "盒", "盖")):
        triggers.append(("box", max(0.1, masterStartSec + 0.28), 0.58))

    # 在无台词悬念特写 / 反应震惊镜 / 开场第一镜叠加电影重低音冲击或反转音
    hasDlg = False
    try:
        dlgs = json.loads(shotDict.get("dialogueJson") or "[]")
        hasDlg = bool(dlgs and isinstance(dlgs[0], dict) and str(dlgs[0].get("text") or "").strip())
    except Exception:
        pass
    isShockOrClose = any(k in text for k in ("shock", "seal", "cinnabar", "暗印", "震惊", "瞳孔", "定格", "特写"))
    if int(shotDict.get("shotNumber") or 0) == 1:
        triggers.append(("boom", 0.08, 0.52))
    elif (not hasDlg and isShockOrClose) or shotDict.get("lcutLeadSec", 0) > 0:
        # L-Cut 或震惊特写切入瞬间踩点
        hitTime = max(0.1, masterStartSec + (trimmedDur - 0.9 if not isShockOrClose else 0.05))
        triggers.append(("boom" if isShockOrClose else "riser", hitTime, 0.45))

    return triggers


def buildEpisodeSfxStem(timelineManifest, validShots, totalDurationSec, outSfxWav, ffmpegBin="ffmpeg"):
    """
    依据时间轴清单与镜头动作语义，合成贯穿全集的精确毫秒踩点 SFX 音轨 WAV。
    返回实际叠加的音效触发次数。
    """
    sfxBank = ensureSfxLibrary()
    shotMap = {int(s["shotNumber"]): s for s, _ in validShots}

    events = []
    for item in timelineManifest:
        sNum = int(item.get("shotNumber") or 0)
        sDict = dict(shotMap.get(sNum) or {})
        sDict["lcutLeadSec"] = item.get("lcutLeadSec", 0)
        trigs = _detectShotSfxTriggers(sDict, float(item["masterStartSec"]), float(item["trimmedDurationSec"]))
        for kind, tsSec, vol in trigs:
            if tsSec < totalDurationSec - 0.15 and kind in sfxBank:
                events.append((sfxBank[kind], int(round(tsSec * 1000)), vol))

    if not events:
        return 0

    # 限制最多 16 个高频核心音效防止过度嘈杂
    events = events[:16]
    inputs = ["-f", "lavfi", "-t", f"{totalDurationSec + 0.5:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    filters = []
    mixTags = ["[0:a]"]
    for idx, (wavPath, delayMs, vol) in enumerate(events, start=1):
        inputs.extend(["-i", str(wavPath)])
        filters.append(f"[{idx}:a]volume={vol:.2f},adelay={delayMs}|{delayMs}[s{idx}]")
        mixTags.append(f"[s{idx}]")

    filterComplex = ";".join(filters) + ";" + "".join(mixTags) + f"amix=inputs={len(mixTags)}:duration=first:normalize=0[sfxout]"
    cmd = [
        ffmpegBin, "-v", "error", "-y",
        *inputs,
        "-filter_complex", filterComplex,
        "-map", "[sfxout]", "-ar", "44100", "-ac", "2", str(outSfxWav),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0 or not Path(outSfxWav).exists():
        return 0
    return len(events)


def generateEpisodeBgmWithCloud(episodeId, customPrompt=""):
    """
    调用无影云端 ComfyUI 的 MiniMax-Music 3 大模型为指定剧集生成专属情绪配乐并入库。
    """
    from .comfyBridge import (
        checkComfyStatus,
        buildMiniMaxMusic3Workflow,
        _submitAndPollComfyWorkflow,
        _DIRECT_HTTP_OPENER,
    )

    st = checkComfyStatus()
    if not st.get("online"):
        raise RuntimeError(f"无影云端 ComfyUI 未在线 ({st.get('endpoint')})，请先开启云电脑后再生成云端定制配乐；合轴仍会自动使用内置音效踩点与氛围底乐。")

    endpoint = st["endpoint"].rstrip("/")
    try:
        with _DIRECT_HTTP_OPENER.open(f"{endpoint}/object_info/MiniMaxMusic3TextEncode", timeout=10) as r:
            nodeCheck = json.loads(r.read().decode("utf-8"))
        if "MiniMaxMusic3TextEncode" not in nodeCheck:
            raise RuntimeError("当前无影 ComfyUI 未加载 MiniMaxMusic3TextEncode 节点。")
    except Exception as err:
        raise RuntimeError(f"检查云端 MiniMax-Music 3 节点失败: {err}")

    with closing(getDbConnection()) as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episodeId,)).fetchone()
        if not ep:
            raise ValueError(f"未找到剧集 #{episodeId}")
        proj = conn.execute("SELECT * FROM projects WHERE id=?", (ep["projectId"],)).fetchone()
        shots = conn.execute("SELECT durationSec FROM shots WHERE episodeId=?", (episodeId,)).fetchall()

    totalDur = sum(float(s["durationSec"] or 4.0) for s in shots) if shots else 30.0
    targetDur = max(25.0, min(90.0, round(totalDur + 4.0, 1)))
    genre = str(proj["genre"] if proj else "").lower()

    if customPrompt and str(customPrompt).strip():
        caption = str(customPrompt).strip()
    elif any(k in genre for k in ("现代", "都市", "悬疑", "现代")):
        caption = "Dark cinematic modern suspense drama score, moody analog synth bass, tense muted strings, minimal sparse percussion, atmospheric minor key, instrumental underscore"
    else:
        caption = (
            "Dark cinematic Chinese historical palace intrigue score, tense low bowed erhu ostinato, "
            "sparse deep guzheng plucks, subtle muffled war drum pulse, nocturnal suspense atmosphere, "
            "minor key, pure instrumental film underscore, no vocals"
        )

    wf = buildMiniMaxMusic3Workflow(caption=caption, durationSec=targetDur, lyrics="[Instrumental]", steps=30)
    promptId, audioUrl, audioPath = _submitAndPollComfyWorkflow(wf, endpoint, expectedType="audio", timeoutSec=600)

    # 转为标准 44.1kHz 立体声 WAV 以保证混音最稳
    wavName = f"ep_{episodeId}_music3_{int(time.time())}.wav"
    wavPath = AUDIO_DIR / wavName
    conv = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(audioPath), "-ar", "44100", "-ac", "2", str(wavPath)],
        capture_output=True, text=True, timeout=30,
    )
    if conv.returncode == 0 and wavPath.exists() and wavPath.stat().st_size > 1024:
        finalUrl = f"/media/audio/{wavName}"
        try:
            Path(audioPath).unlink()
        except Exception:
            pass
    else:
        finalUrl = audioUrl

    with closing(getDbConnection()) as conn:
        conn.execute(
            "UPDATE episodes SET bgmAudioUrl=?, bgmPrompt=?, updatedAt=? WHERE id=?",
            (finalUrl, caption, time.time(), episodeId),
        )
        conn.commit()

    return {
        "ok": True,
        "episodeId": episodeId,
        "engine": "MiniMax-Music 3",
        "promptId": promptId,
        "bgmAudioUrl": finalUrl,
        "durationSec": targetDur,
        "caption": caption,
    }
