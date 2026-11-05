#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 无影云端 ComfyUI 双关键帧与多引擎视频桥接器 (comfyBridge.py)
核心能力：
1. Flux3-Klein 高清分镜首帧 (firstFrame) 与尾帧 (lastFrame) 生成；
2. MiniMax-H3 FL2VA (MiniMaxH3ImageToVideo) 首尾帧双锚点音画同步生成（支持 3D 潜空间超分）；
3. Wan 2.2 14B MoE 高低噪双专家电影级图生视频；
4. 云端在线状态探测、图片上传、异步队列轮询与结果自动回写。
"""

import os
import time
import json
import uuid
import random
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from contextlib import closing

from .studioStore import getDbConnection, KEYFRAMES_DIR, VIDEOS_DIR, ASSETS_DIR
from .directorPlanner import compileFl2vaVideoPrompt, compileWan22VideoPrompt
from .continuityManager import (
    resolveMediaUrlToPath,
    syncEpisodeFrameLocks,
    inspectVideoQuality,
    findFfmpegBinary,
)

# 强制直连无影云 Tailscale / 局域网地址，绕过系统代理
os.environ["no_proxy"] = "*"


def getSettingValue(key, defaultVal=""):
    """从数据库读取配置项"""
    with closing(getDbConnection()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else defaultVal


def checkComfyStatus():
    """探测无影云端 ComfyUI 服务是否在线并返回系统状态"""
    endpoint = getSettingValue("comfyEndpoint", "http://127.0.0.1:8188").rstrip("/")
    try:
        req = urllib.request.Request(f"{endpoint}/system_stats", method="GET")
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            devices = data.get("devices") or []
            gpuName = devices[0].get("name", "GPU") if devices else "Cloud GPU"
            return {"online": True, "endpoint": endpoint, "gpu": gpuName}
    except Exception as err:
        return {"online": False, "endpoint": endpoint, "error": str(err)}


def getAspectRatioDimensions(aspectRatio="16:9", isVideo=True):
    """计算符合 H3 / Flux3 32像素网格约束的标准分辨率"""
    ratio = str(aspectRatio or "16:9").strip()
    if isVideo:
        if ratio == "9:16":
            return 768, 1344
        if ratio == "1:1":
            return 1024, 1024
        return 1344, 768
    else:
        if ratio == "9:16":
            return 832, 1472
        if ratio == "1:1":
            return 1024, 1024
        return 1472, 832


def calculateH3FrameGrid(durationSec=5.0, fps=24):
    """
    计算符合 MiniMax-H3 17k+5 帧数网格约束的总帧数：
    例如 5秒 -> 124帧，6秒 -> 141帧，8秒 -> 192帧
    """
    rawFrames = max(5, round(float(durationSec or 5.0) * fps))
    remainder = (5 - (rawFrames % 17)) % 17
    return max(73, rawFrames + remainder)


def uploadImageToComfy(imagePath, comfyEndpoint):
    """将本地关键帧或资产图片上传至无影云端 ComfyUI input 目录"""
    pathObj = Path(imagePath)
    if not pathObj.exists():
        raise FileNotFoundError(f"待上传图片不存在: {imagePath}")

    boundary = f"----DramaStudioBoundary{uuid.uuid4().hex}"
    filename = f"ds_{int(time.time()*1000)}_{pathObj.name}"
    fileBytes = pathObj.read_bytes()

    bodyParts = [
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode("utf-8"),
        b"Content-Type: image/png\r\n\r\n",
        fileBytes,
        f"\r\n--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(bodyParts)

    req = urllib.request.Request(
        f"{comfyEndpoint.rstrip('/')}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resData = json.loads(resp.read().decode("utf-8"))
        return resData.get("name", filename)


def buildFlux3KeyframeWorkflow(promptText, width=1472, height=832, steps=16, seed=None):
    """构建无影云端 Flux3-Klein 高清关键帧生成工作流"""
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux3_image_klein_sd_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_bf16.safetensors", "type": "flux3"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": promptText, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"DS_KEYFRAME_{int(time.time())}", "images": ["8", 0]}},
    }


def buildH3Fl2vaWorkflow(
    promptText,
    firstFrameUploadedName,
    lastFrameUploadedName=None,
    width=1344,
    height=768,
    length=124,
    steps=20,
    enableUpscale3d=True,
    seed=None,
):
    """
    构建无影云端 MiniMax-H3 FL2VA (MiniMaxH3ImageToVideo) 首尾帧双锚点工作流：
    - 核心优势：直接将 first_frame 和 last_frame 注入潜空间首尾边界，彻底解决只给参考图导致的
      “动作演一半停住”、“情节表达不出”与“空间透视散架”问题！
    - 支持挂载 MinimaxH3LatentUpscalerNode3D 提升画面锐度与面部细节。
    """
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)

    width = max(384, (int(width) // 32) * 32)
    height = max(384, (int(height) // 32) * 32)
    length = max(73, (int(length) // 17) * 17 + 5)

    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
        "6": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["5", 0], "shift_video": 12.0, "shift_audio": 3.0}},
        "7": {"class_type": "LoadImage", "inputs": {"image": firstFrameUploadedName}},
    }

    i2vInputs = {
        "clip": ["2", 0],
        "vae": ["3", 0],
        "first_frame": ["7", 0],
        "prompt": promptText,
        "width": width,
        "height": height,
        "length": length,
    }

    if lastFrameUploadedName:
        wf["8"] = {"class_type": "LoadImage", "inputs": {"image": lastFrameUploadedName}}
        i2vInputs["last_frame"] = ["8", 0]

    wf["9"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": i2vInputs}
    wf["10"] = {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["9", 0]}}
    wf["11"] = {"class_type": "BasicScheduler", "inputs": {"model": ["6", 0], "scheduler": "simple", "steps": int(steps), "denoise": 1.0}}
    wf["12"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
    wf["13"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["14"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0],
            "guider": ["10", 0],
            "sampler": ["12", 0],
            "sigmas": ["11", 0],
            "latent_image": ["9", 1],
        },
    }

    videoLatentOut = ["14", 0]
    if enableUpscale3d:
        wf["15"] = {
            "class_type": "MinimaxH3LatentUpscalerNode3D",
            "inputs": {
                "latent": videoLatentOut,
                "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                "scale": 1.5,
                "device": "cuda",
                "precision": "fp16",
            },
        }
        videoLatentOut = ["15", 0]

    wf["16"] = {"class_type": "VAEDecode", "inputs": {"samples": videoLatentOut, "vae": ["3", 0]}}
    wf["17"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["4", 0]}}
    wf["18"] = {
        "class_type": "VHS_VideoCombine",
        "inputs": {
            "images": ["16", 0],
            "audio": ["17", 0],
            "frame_rate": 24.0,
            "loop_count": 0,
            "pingpong": False,
            "format": "video/h264-mp4",
            "save_output": True,
            "filename_prefix": f"DS_H3_FL2VA_{int(time.time())}",
        },
    }
    return wf


def buildWan22MoeWorkflow(promptText, firstFrameUploadedName, width=1280, height=720, numFrames=81, steps=20, seed=None):
    """构建无影云端 Wan 2.2 14B MoE 高低噪双专家高清图生视频工作流"""
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)
    splitStep = max(4, int(steps * 0.45))
    negPrompt = "色调艳丽，过曝，静态，细节模糊不清，字幕，水印，画作，静止，整体发灰，最差质量，低质量，残缺，多余的手指，变形，畸形，扭曲，disfigured, ugly, blurred, lowres, watermark, text"

    return {
        "1": {"class_type": "WanVideoModelLoader", "inputs": {"model": "WanVideo/2_2/Wan2_2-I2V-A14B-HIGH_fp8_e4m3fn_scaled_KJ.safetensors", "base_precision": "bf16", "quantization": "fp8_e4m3fn_scaled", "load_device": "main_device"}},
        "2": {"class_type": "WanVideoModelLoader", "inputs": {"model": "WanVideo/2_2/Wan2_2-I2V-A14B-LOW_fp8_e4m3fn_scaled_KJ.safetensors", "base_precision": "bf16", "quantization": "fp8_e4m3fn_scaled", "load_device": "main_device"}},
        "3": {"class_type": "WanVideoVAELoader", "inputs": {"model_name": "Wan2_1_VAE_bf16.safetensors", "precision": "bf16"}},
        "4": {"class_type": "LoadWanVideoT5TextEncoder", "inputs": {"model_name": "umt5-xxl-enc-bf16.safetensors", "precision": "bf16"}},
        "5": {"class_type": "WanVideoTextEncode", "inputs": {"positive_prompt": promptText, "negative_prompt": negPrompt, "t5": ["4", 0]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": firstFrameUploadedName}},
        "7": {"class_type": "WanVideoImageToVideoEncode", "inputs": {"start_image": ["6", 0], "vae": ["3", 0], "width": width, "height": height, "num_frames": numFrames, "noise_aug_strength": 0.03, "start_latent_strength": 1.0, "end_latent_strength": 1.0, "force_offload": False}},
        "8": {"class_type": "WanVideoSampler", "inputs": {"model": ["1", 0], "text_embeds": ["5", 0], "image_embeds": ["7", 0], "steps": steps, "cfg": 5.0, "shift": 5.0, "seed": seed, "scheduler": "unipc", "force_offload": False, "riflex_freq_index": 0, "start_step": 0, "end_step": splitStep}},
        "9": {"class_type": "WanVideoSampler", "inputs": {"model": ["2", 0], "text_embeds": ["5", 0], "image_embeds": ["7", 0], "samples": ["8", 0], "steps": steps, "cfg": 5.0, "shift": 5.0, "seed": seed, "scheduler": "unipc", "force_offload": False, "riflex_freq_index": 0, "start_step": splitStep, "end_step": -1}},
        "10": {"class_type": "WanVideoDecode", "inputs": {"vae": ["3", 0], "samples": ["9", 0], "enable_vae_tiling": False, "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128}},
        "11": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["10", 0], "frame_rate": 16.0, "loop_count": 0, "pingpong": False, "format": "video/h264-mp4", "save_output": True, "filename_prefix": f"DS_WAN22_{int(time.time())}"}},
    }


def _submitAndPollComfyWorkflow(workflowDict, comfyEndpoint, expectedType="image", timeoutSec=1800):
    """提交工作流到 ComfyUI 并轮询直至下载产物"""
    endpoint = comfyEndpoint.rstrip("/")
    payload = json.dumps({"prompt": workflowDict}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resJson = json.loads(resp.read().decode("utf-8"))
        promptId = resJson.get("prompt_id")
        if not promptId:
            raise RuntimeError(f"ComfyUI 未返回有效 prompt_id: {resJson}")

    started = time.time()
    while time.time() - started < timeoutSec:
        time.sleep(2.5)
        try:
            histReq = urllib.request.Request(f"{endpoint}/history/{promptId}", method="GET")
            with urllib.request.urlopen(histReq, timeout=10) as histResp:
                histData = json.loads(histResp.read().decode("utf-8"))
        except Exception:
            continue

        if promptId not in histData:
            continue

        entry = histData[promptId]
        statusObj = entry.get("status") or {}
        if statusObj.get("status_str") == "error":
            raise RuntimeError(f"云端 ComfyUI 渲染报错: {json.dumps(statusObj.get('messages', []), ensure_ascii=False)}")

        outputs = entry.get("outputs") or {}
        for _, nodeOut in outputs.items():
            mediaItems = nodeOut.get("gifs") or nodeOut.get("videos") or nodeOut.get("images") or []
            for item in mediaItems:
                fname = item.get("filename")
                subfolder = item.get("subfolder", "")
                ftype = item.get("type", "output")
                if not fname:
                    continue
                isVideoFile = fname.lower().endswith((".mp4", ".webm", ".mov"))
                if expectedType == "video" and not isVideoFile:
                    continue
                if expectedType == "image" and isVideoFile:
                    continue

                query = urllib.parse.urlencode({"filename": fname, "subfolder": subfolder, "type": ftype})
                viewUrl = f"{endpoint}/view?{query}"
                with urllib.request.urlopen(viewUrl, timeout=60) as dlResp:
                    fileBytes = dlResp.read()

                if expectedType == "video":
                    localName = f"shot_vid_{promptId[:8]}_{int(time.time())}.mp4"
                    localPath = VIDEOS_DIR / localName
                    localPath.write_bytes(fileBytes)
                    return promptId, f"/media/videos/{localName}", localPath
                else:
                    localName = f"shot_kf_{promptId[:8]}_{int(time.time())}.png"
                    localPath = KEYFRAMES_DIR / localName
                    localPath.write_bytes(fileBytes)
                    return promptId, f"/media/keyframes/{localName}", localPath

    raise TimeoutError(f"等待无影云端渲染超时 ({timeoutSec}s)")


def _generateLocalFallbackKeyframe(shotRow, frameRole="first", width=1280, height=720):
    """
    当无影云端主机未开启时，支持基于已有角色/场景参考图与分镜构图指令生成高清预演关键帧（StoryReel Previz），
    确保导演在离线环境下也能完整验证“首尾帧锚定 -> 帧锁传递 -> 动态预演成片”全链路。
    """
    from PIL import Image, ImageDraw

    # 优先查找镜头关联的角色或场景资产图作为基底
    baseImg = None
    with closing(getDbConnection()) as conn:
        assets = conn.execute("SELECT * FROM assets WHERE projectId=?", (shotRow["projectId"],)).fetchall()
        refs = json.loads(shotRow["referenceAssetsJson"] or "[]")
        refNames = [str(r.get("asset", "")).lstrip("@") for r in refs if isinstance(r, dict)]

        sceneAsset = next((a for a in assets if a["assetType"] == "scene" and a["name"] in refNames and a["imageUrl"]), None)
        charAsset = next((a for a in assets if a["assetType"] == "character" and a["name"] in refNames and a["imageUrl"]), None)
        if not sceneAsset:
            sceneAsset = next((a for a in assets if a["assetType"] == "scene" and a["imageUrl"]), None)
        if not charAsset:
            charAsset = next((a for a in assets if a["assetType"] == "character" and a["imageUrl"]), None)

        chosenUrl = (charAsset["imageUrl"] if frameRole == "last" and charAsset else None) or (sceneAsset["imageUrl"] if sceneAsset else None) or (charAsset["imageUrl"] if charAsset else None)
        if chosenUrl:
            p = resolveMediaUrlToPath(chosenUrl)
            if p and p.exists():
                try:
                    baseImg = Image.open(str(p)).convert("RGB")
                except Exception:
                    baseImg = None

    canvas = Image.new("RGB", (width, height), (16, 20, 28))
    if baseImg:
        # 针对首帧和尾帧施加微小的推拉景别差异，体现起始状态到动作落点的推进
        srcW, srcH = baseImg.size
        if frameRole == "last":
            cropBox = (int(srcW * 0.06), int(srcH * 0.06), int(srcW * 0.94), int(srcH * 0.94))
            cropped = baseImg.crop(cropBox).resize((width, height), Image.Resampling.LANCZOS)
        else:
            cropped = baseImg.resize((width, height), Image.Resampling.LANCZOS)
        canvas.paste(cropped, (0, 0))
    else:
        draw = ImageDraw.Draw(canvas)
        for y in range(height):
            r = int(18 + (y / height) * 24)
            g = int(22 + (y / height) * 30)
            b = int(34 + (y / height) * 45)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

    localName = f"kf_shot_{shotRow['id']}_{frameRole}_{int(time.time()*1000)}.png"
    localPath = KEYFRAMES_DIR / localName
    canvas.save(str(localPath), format="PNG")
    return f"/media/keyframes/{localName}"


def _generateLocalFallbackVideo(shotRow, firstPath, lastPath, width=1280, height=720):
    """
    当无影云端离线时，使用首帧与尾帧通过 FFmpeg 合成平滑过渡的导演动态分镜预演视频（Animatic Previz），
    用于离线检验整集分镜节奏与剪辑连续性。
    """
    ffmpegBin = findFfmpegBinary("ffmpeg")
    dur = max(2.0, float(shotRow["durationSec"] or 5.0))
    localName = f"animatic_shot_{shotRow['id']}_{int(time.time())}.mp4"
    localPath = VIDEOS_DIR / localName

    if lastPath and Path(lastPath).exists() and str(lastPath) != str(firstPath):
        halfDur = round(dur / 2.0, 2)
        fadeStart = max(0.5, round(halfDur - 0.6, 2))
        cmd = [
            ffmpegBin, "-v", "error", "-y",
            "-loop", "1", "-t", str(halfDur + 0.6), "-i", str(firstPath),
            "-loop", "1", "-t", str(halfDur + 0.6), "-i", str(lastPath),
            "-f", "lavfi", "-t", str(dur), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=24[v0];"
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=24[v1];"
            f"[v0][v1]xfade=transition=SmoothLeft:duration=0.6:offset={fadeStart}[vout]",
            "-map", "[vout]", "-map", "2:a",
            "-t", str(dur),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(localPath),
        ]
    else:
        cmd = [
            ffmpegBin, "-v", "error", "-y",
            "-loop", "1", "-t", str(dur), "-i", str(firstPath),
            "-f", "lavfi", "-t", str(dur), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(localPath),
        ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0 or not localPath.exists():
        # 最简兜底合成
        simpleCmd = [
            ffmpegBin, "-v", "error", "-y",
            "-loop", "1", "-t", str(dur), "-i", str(firstPath),
            "-f", "lavfi", "-t", str(dur), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", f"scale={width}:{height},setsar=1,fps=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(localPath),
        ]
        subprocess.run(simpleCmd, capture_output=True, text=True, timeout=60)

    return f"/media/videos/{localName}", localPath


def enqueueKeyframeTask(shotId, frameRole="both"):
    """
    提交分镜首帧 / 尾帧 / 双关键帧生成任务：
    - frameRole: 'first' | 'last' | 'both'
    - 如果该镜头开启了 lockFromPrevEnd=1 且上一镜头已有尾帧，则首帧自动无损锁定上一镜尾帧！
    """
    now = time.time()
    taskId = f"kf_{uuid.uuid4().hex[:10]}"
    with closing(getDbConnection()) as conn:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shot:
            raise ValueError("未找到指定镜头")
        project = conn.execute("SELECT * FROM projects WHERE id=?", (shot["projectId"],)).fetchone()
        conn.execute(
            """
            INSERT INTO renderTasks (id, projectId, episodeId, shotId, taskType, engineName, status, phaseText, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, ?, 'flux3_klein', 'running', '正在生成关键帧...', ?, ?)
            """,
            (taskId, shot["projectId"], shot["episodeId"], shotId, f"{frameRole}_keyframe", now, now),
        )
        conn.commit()

    def worker():
        try:
            with closing(getDbConnection()) as conn:
                shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                projRow = dict(conn.execute("SELECT * FROM projects WHERE id=?", (shotRow["projectId"],)).fetchone())

            # 先执行帧锁检查：如果锁定了上一镜尾帧，首帧直接复用上一镜尾帧
            syncEpisodeFrameLocks(shotRow["episodeId"])
            with closing(getDbConnection()) as conn:
                shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())

            width, height = getAspectRatioDimensions(projRow.get("aspectRatio", "16:9"), isVideo=False)
            comfyStatus = checkComfyStatus()
            sharedSeed = random.randint(1, 2147483647)

            firstUrl = shotRow.get("firstFrameUrl") or ""
            lastUrl = shotRow.get("lastFrameUrl") or ""

            needFirst = frameRole in ("first", "both") and not (int(shotRow.get("lockFromPrevEnd") or 0) == 1 and firstUrl)
            needLast = frameRole in ("last", "both") and (shotRow.get("shotCategory") == "action_fl2va" or frameRole == "last")

            if comfyStatus["online"]:
                if needFirst:
                    wf1 = buildFlux3KeyframeWorkflow(shotRow["firstFramePrompt"], width=width, height=height, seed=sharedSeed)
                    _, firstUrl, _ = _submitAndPollComfyWorkflow(wf1, comfyStatus["endpoint"], expectedType="image")
                if needLast:
                    wf2 = buildFlux3KeyframeWorkflow(shotRow["lastFramePrompt"], width=width, height=height, seed=sharedSeed)
                    _, lastUrl, _ = _submitAndPollComfyWorkflow(wf2, comfyStatus["endpoint"], expectedType="image")
                modeNote = "无影云端 Flux3-Klein 渲染完成"
            else:
                if needFirst:
                    firstUrl = _generateLocalFallbackKeyframe(shotRow, "first", width=width, height=height)
                if needLast:
                    lastUrl = _generateLocalFallbackKeyframe(shotRow, "last", width=width, height=height)
                modeNote = "云端未开机，已生成本地高清预演关键帧 (Previz)"

            with closing(getDbConnection()) as conn:
                newStatus = "keyframes_ready" if (firstUrl or lastUrl) else shotRow["status"]
                conn.execute(
                    "UPDATE shots SET firstFrameUrl=?, lastFrameUrl=?, status=?, updatedAt=? WHERE id=?",
                    (firstUrl, lastUrl, newStatus, time.time(), shotId),
                )
                conn.execute(
                    "UPDATE renderTasks SET status='succeeded', phaseText=?, outputUrl=?, updatedAt=? WHERE id=?",
                    (modeNote, lastUrl or firstUrl, time.time(), taskId),
                )
                conn.commit()

            # 如果本镜生成了新的尾帧，自动向后传递帧锁给下一镜首帧
            syncEpisodeFrameLocks(shotRow["episodeId"])
        except Exception as err:
            with closing(getDbConnection()) as conn:
                conn.execute(
                    "UPDATE renderTasks SET status='failed', phaseText='关键帧生成失败', errorMessage=?, updatedAt=? WHERE id=?",
                    (str(err), time.time(), taskId),
                )
                conn.commit()

    threading.Thread(target=worker, daemon=True).start()
    return taskId


def enqueueShotVideoTask(shotId, engineOverride=None):
    """
    提交单个镜头的视频生成任务：
    1. 若镜头尚无首帧关键帧（或动作镜尚无尾帧关键帧），自动补齐关键帧；
    2. 根据所选引擎（默认 h3_fl2va 首尾帧双锚点引擎，或 wan22_moe 双专家引擎）编译提示词；
    3. 提交无影云端 ComfyUI 渲染，完成后自动执行 Auto-QC 质检并触发下一镜连续性帧锁。
    """
    now = time.time()
    taskId = f"vid_{uuid.uuid4().hex[:10]}"
    with closing(getDbConnection()) as conn:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shot:
            raise ValueError("未找到指定镜头")
        engine = engineOverride or shot["videoEngine"] or "h3_fl2va"
        conn.execute(
            "UPDATE shots SET status='rendering', videoEngine=?, updatedAt=? WHERE id=?",
            (engine, now, shotId),
        )
        conn.execute(
            """
            INSERT INTO renderTasks (id, projectId, episodeId, shotId, taskType, engineName, status, phaseText, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, 'shot_video', ?, 'running', '正在校验首尾关键帧与连续性锚点...', ?, ?)
            """,
            (taskId, shot["projectId"], shot["episodeId"], shotId, engine, now, now),
        )
        conn.commit()

    def worker():
        try:
            with closing(getDbConnection()) as conn:
                shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                projRow = dict(conn.execute("SELECT * FROM projects WHERE id=?", (shotRow["projectId"],)).fetchone())

            # 1. 同步上一镜尾帧帧锁
            syncEpisodeFrameLocks(shotRow["episodeId"])
            with closing(getDbConnection()) as conn:
                shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())

            vWidth, vHeight = getAspectRatioDimensions(projRow.get("aspectRatio", "16:9"), isVideo=True)
            comfyStatus = checkComfyStatus()

            # 2. 确保首帧（以及动作推进镜的尾帧）就绪
            firstUrl = shotRow.get("firstFrameUrl") or ""
            lastUrl = shotRow.get("lastFrameUrl") or ""
            if not firstUrl:
                if comfyStatus["online"]:
                    wfFirst = buildFlux3KeyframeWorkflow(shotRow["firstFramePrompt"], width=vWidth, height=vHeight)
                    _, firstUrl, _ = _submitAndPollComfyWorkflow(wfFirst, comfyStatus["endpoint"], expectedType="image")
                else:
                    firstUrl = _generateLocalFallbackKeyframe(shotRow, "first", width=vWidth, height=vHeight)

            if shotRow.get("shotCategory") == "action_fl2va" and not lastUrl:
                if comfyStatus["online"]:
                    wfLast = buildFlux3KeyframeWorkflow(shotRow["lastFramePrompt"], width=vWidth, height=vHeight)
                    _, lastUrl, _ = _submitAndPollComfyWorkflow(wfLast, comfyStatus["endpoint"], expectedType="image")
                else:
                    lastUrl = _generateLocalFallbackKeyframe(shotRow, "last", width=vWidth, height=vHeight)

            with closing(getDbConnection()) as conn:
                conn.execute(
                    "UPDATE shots SET firstFrameUrl=?, lastFrameUrl=?, updatedAt=? WHERE id=?",
                    (firstUrl, lastUrl, time.time(), shotId),
                )
                conn.execute(
                    "UPDATE renderTasks SET phaseText='关键帧已锁定，正在提交视频扩散模型渲染...', updatedAt=? WHERE id=?",
                    (time.time(), taskId),
                )
                conn.commit()

            firstPath = resolveMediaUrlToPath(firstUrl)
            lastPath = resolveMediaUrlToPath(lastUrl) if lastUrl else None

            engine = engineOverride or shotRow.get("videoEngine") or "h3_fl2va"
            enableUpscale = str(projRow.get("enableUpscale3d", 1)) == "1"
            steps = int(getSettingValue("h3SamplingSteps", "20") or 20)
            frameLength = calculateH3FrameGrid(shotRow.get("durationSec", 5.0))

            if comfyStatus["online"]:
                upFirst = uploadImageToComfy(firstPath, comfyStatus["endpoint"])
                upLast = uploadImageToComfy(lastPath, comfyStatus["endpoint"]) if (lastPath and lastPath.exists() and shotRow.get("shotCategory") == "action_fl2va") else None

                if engine == "wan22_moe":
                    compiledPrompt = compileWan22VideoPrompt(shotRow)
                    wfVideo = buildWan22MoeWorkflow(
                        compiledPrompt,
                        upFirst,
                        width=1280 if vWidth >= vHeight else 720,
                        height=720 if vWidth >= vHeight else 1280,
                        numFrames=81,
                        steps=20,
                    )
                else:
                    compiledPrompt = compileFl2vaVideoPrompt(shotRow, hasLastFrame=bool(upLast))
                    wfVideo = buildH3Fl2vaWorkflow(
                        compiledPrompt,
                        upFirst,
                        lastFrameUploadedName=upLast,
                        width=vWidth,
                        height=vHeight,
                        length=frameLength,
                        steps=steps,
                        enableUpscale3d=enableUpscale,
                    )

                promptId, videoUrl, videoLocalPath = _submitAndPollComfyWorkflow(
                    wfVideo, comfyStatus["endpoint"], expectedType="video"
                )
                phaseSummary = f"无影云端 {engine} 渲染完成"
            else:
                compiledPrompt = compileFl2vaVideoPrompt(shotRow, hasLastFrame=bool(lastPath))
                videoUrl, videoLocalPath = _generateLocalFallbackVideo(
                    shotRow, firstPath, lastPath, width=vWidth, height=vHeight
                )
                promptId = "local_previz"
                phaseSummary = "云端未开机，已生成首尾帧动态分镜预演视频 (Animatic Previz)"

            # 3. 执行 Auto-QC 视频质检
            qcReport = inspectVideoQuality(videoLocalPath)
            qcReport["compiledPrompt"] = compiledPrompt
            qcReport["engine"] = engine
            qcReport["usedFirstFrame"] = firstUrl
            qcReport["usedLastFrame"] = lastUrl if shotRow.get("shotCategory") == "action_fl2va" else ""

            with closing(getDbConnection()) as conn:
                conn.execute(
                    "UPDATE shots SET videoUrl=?, qcReportJson=?, status='completed', updatedAt=? WHERE id=?",
                    (videoUrl, json.dumps(qcReport, ensure_ascii=False), time.time(), shotId),
                )
                conn.execute(
                    """
                    UPDATE renderTasks
                    SET status='succeeded', phaseText=?, comfyPromptId=?, outputUrl=?, auditJson=?, updatedAt=?
                    WHERE id=?
                    """,
                    (phaseSummary, promptId, videoUrl, json.dumps(qcReport, ensure_ascii=False), time.time(), taskId),
                )
                conn.commit()

            # 4. 触发后续镜头的连续性帧锁更新
            syncEpisodeFrameLocks(shotRow["episodeId"])
        except Exception as err:
            with closing(getDbConnection()) as conn:
                conn.execute("UPDATE shots SET status='failed', updatedAt=? WHERE id=?", (time.time(), shotId))
                conn.execute(
                    "UPDATE renderTasks SET status='failed', phaseText='视频生成失败', errorMessage=?, updatedAt=? WHERE id=?",
                    (str(err), time.time(), taskId),
                )
                conn.commit()

    threading.Thread(target=worker, daemon=True).start()
    return taskId
