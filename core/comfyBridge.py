#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 无影云端 ComfyUI 双关键帧与多引擎视频桥接器 (comfyBridge.py)
核心能力：
1. Flux3-Klein 高清分镜首帧 (firstFrame) 与尾帧 (lastFrame) 生成；
2. MiniMax-H3 FL2VA (MiniMaxH3ImageToVideo) 首尾帧双锚点音画同步生成（支持 PDD 8 步加速与 3D 潜空间超分）；
3. MiniMax-H3 Ref2VA (MiniMaxH3ReferenceToVideo) 多图参考主体一致性生成（支持 PDD 8 步加速与 3D 潜空间超分）；
4. FastH3 8 步极速图生视频；
5. Wan 2.2 14B MoE 高低噪双专家电影级图生视频；
6. 云端在线状态探测、图片上传、异步队列轮询与结果自动回写。
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
from .directorPlanner import (
    compileFl2vaVideoPrompt,
    compileWan22VideoPrompt,
    compileAssetSheetPrompt,
    compileLockedKeyframePrompt,
)
from .continuityManager import (
    resolveMediaUrlToPath,
    syncEpisodeFrameLocks,
    extractHighResLastFrameFromVideo,
    extractAndLockCharacterVoiceBank,
    backfillEpisodeVoiceBanks,
    inspectVideoQuality,
    findFfmpegBinary,
)

# 为无影云端 Tailscale / 局域网地址定制直连 Opener（不污染全局 os.environ["no_proxy"]，以免影响外网 LLM API 访问）
_DIRECT_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 云端图片上传去重缓存：{(endpoint, absPath, mtime): uploadedFilename}
_UPLOADED_IMAGE_CACHE = {}
_UPLOAD_LOCK = threading.Lock()
# 云端任务排队锁：防止用户连点多个镜头时并发上传挤爆 Tailscale 中继带宽
_COMFY_JOB_SEMAPHORE = threading.BoundedSemaphore(1)


def getSettingValue(key, defaultVal=""):
    """从数据库读取配置项"""
    with closing(getDbConnection()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else defaultVal


def checkComfyStatus():
    """探测无影云端 ComfyUI 服务是否在线并返回系统状态与显存规格"""
    endpoint = getSettingValue("comfyEndpoint", "http://127.0.0.1:8188").rstrip("/")
    try:
        req = urllib.request.Request(f"{endpoint}/system_stats", method="GET")
        with _DIRECT_HTTP_OPENER.open(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            devices = data.get("devices") or []
            gpuName = devices[0].get("name", "GPU") if devices else "Cloud GPU"
            vramTotal = int(devices[0].get("vram_total", 0)) if devices else 0
            vramGb = round(vramTotal / 1e9, 1) if vramTotal else 0.0
            is24GbCard = bool(0 < vramTotal <= 27 * 1e9)
            return {"online": True, "endpoint": endpoint, "gpu": gpuName, "vramGb": vramGb, "is24GbCard": is24GbCard}
    except Exception as err:
        return {"online": False, "endpoint": endpoint, "error": str(err), "vramGb": 0.0, "is24GbCard": False}


def getAspectRatioDimensions(aspectRatio="16:9", isVideo=True, is24GbCard=False):
    """计算符合 H3 / Flux3 32像素网格约束的标准分辨率（支持 24GB 显存自适应防爆档）"""
    ratio = str(aspectRatio or "16:9").strip()
    if is24GbCard:
        if ratio == "9:16":
            return (512, 896) if isVideo else (576, 1024)
        if ratio == "1:1":
            return 768, 768
        return (896, 512) if isVideo else (1024, 576)
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
    """
    将本地关键帧或资产图片上传至无影云端 ComfyUI input 目录：
    1. 内置 (endpoint, path, mtime) 去重缓存，帧锁复用的同一张图片 0 秒直接返回；
    2. 对超过 350KB 的大图自动在内存中转为 4:4:4 Q95 高保真 JPEG 流上传，体积缩小约 12~15 倍，
       解决 Tailscale 异地/中继隧道 (约 50KB/s) 下上传 2.4MB PNG 超过 30s 导致 <urlopen error timed out> 的问题；
    3. 加全局上传锁 + 180s 超时 + 3 次自动重试，杜绝多镜头并发点击时抢占带宽超时。
    """
    import io
    from PIL import Image

    pathObj = Path(imagePath).resolve()
    if not pathObj.exists():
        raise FileNotFoundError(f"待上传图片不存在: {imagePath}")

    epClean = comfyEndpoint.rstrip("/")
    statObj = pathObj.stat()
    cacheKey = (epClean, str(pathObj), statObj.st_mtime_ns)

    with _UPLOAD_LOCK:
        if cacheKey in _UPLOADED_IMAGE_CACHE:
            return _UPLOADED_IMAGE_CACHE[cacheKey]

        fileBytes = pathObj.read_bytes()
        mimeType = "image/png"
        extName = pathObj.suffix or ".png"

        # 当图片体积较大时，转为 4:4:4 无色度抽样 Q95 JPEG，兼顾零肉眼/潜空间损耗与极速跨网传输
        if len(fileBytes) > 350 * 1024:
            try:
                with Image.open(io.BytesIO(fileBytes)) as im:
                    rgbIm = im.convert("RGB")
                    buf = io.BytesIO()
                    rgbIm.save(buf, format="JPEG", quality=95, subsampling=0, optimize=True)
                    fileBytes = buf.getvalue()
                    mimeType = "image/jpeg"
                    extName = ".jpg"
            except Exception:
                pass

        boundary = f"----DramaStudioBoundary{uuid.uuid4().hex}"
        filename = f"ds_{int(time.time()*1000)}_{pathObj.stem}{extName}"

        bodyParts = [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {mimeType}\r\n\r\n".encode("utf-8"),
            fileBytes,
            f"\r\n--{boundary}\r\n".encode("utf-8"),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(bodyParts)

        lastErr = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"{epClean}/upload/image",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with _DIRECT_HTTP_OPENER.open(req, timeout=180) as resp:
                    resData = json.loads(resp.read().decode("utf-8"))
                    uploadedName = resData.get("name", filename)
                    _UPLOADED_IMAGE_CACHE[cacheKey] = uploadedName
                    return uploadedName
            except Exception as err:
                lastErr = err
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"上传关键帧至云端超时或失败: {lastErr}")


def uploadAudioToComfy(audioPath, comfyEndpoint):
    """将本地角色参考音色音频上传至无影云端 ComfyUI input 目录（带去重缓存）"""
    endpoint = comfyEndpoint.rstrip("/")
    pathObj = Path(audioPath)
    if not pathObj.exists():
        raise FileNotFoundError(f"参考音频文件不存在: {audioPath}")
    stat = pathObj.stat()
    cacheKey = (endpoint, str(pathObj.resolve()), stat.st_mtime_ns, stat.st_size)
    with _UPLOAD_LOCK:
        cached = _UPLOADED_IMAGE_CACHE.get(cacheKey)
        if cached:
            return cached

    boundary = f"----DramaForgeAudio{uuid.uuid4().hex}"
    filename = pathObj.name
    fileBytes = pathObj.read_bytes()
    ext = pathObj.suffix.lower()
    mimeType = "audio/wav" if ext == ".wav" else ("audio/mpeg" if ext == ".mp3" else "application/octet-stream")
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mimeType}\r\n\r\n"
    ).encode("utf-8") + fileBytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        f"{endpoint}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with _DIRECT_HTTP_OPENER.open(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        uploadedName = data.get("name") or filename
        with _UPLOAD_LOCK:
            _UPLOADED_IMAGE_CACHE[cacheKey] = uploadedName
        return uploadedName


def buildH3StillKeyframeWorkflow(
    promptText,
    width=1344,
    height=768,
    seed=None,
    sourceImageUploadedName=None,
    referenceUploadedNames=None,
    sourceFidelity=0.72,
):
    """
    构建无影云端 MiniMax-H3 (33B + Qwen3-VL 32B + PDD 8 步加速) 电影级关键帧生图与参考编辑工作流：
    - 有源图/参考图时：使用 Ref2VA + H3ReferenceEditPrepare（最多支持 9 张参考图，用于角色定妆照锁定与首帧->尾帧精准演进编辑）；
    - 无参考图时：使用 FL2VA + H3TextToImagePrepare（用于高指令遵循度的中文古装/写实首帧与定妆图生成）。
    """
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)

    width = max(384, (int(width) // 32) * 32)
    height = max(384, (int(height) // 32) * 32)

    maxRefs = 2 if width <= 1024 else 9
    allRefs = []
    if sourceImageUploadedName:
        allRefs.append(sourceImageUploadedName)
    for r in (referenceUploadedNames or []):
        if r and r not in allRefs and len(allRefs) < maxRefs:
            allRefs.append(r)

    useRefEdit = len(allRefs) > 0
    unetName = "minimax_h3_ref2va_pruned_int8_convrot.safetensors" if useRefEdit else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    pddName = "minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors" if useRefEdit else "minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors"

    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unetName, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "5": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
        "6": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["5", 0], "shift_video": 12.0, "shift_audio": 3.0}},
        "6b": {
            "class_type": "MiniMaxH3PDDAccApply",
            "inputs": {
                "model": ["6", 0],
                "pdd_file": pddName,
                "nfe": "8",
                "lora_strength": 1.0,
                "head_strength": 1.0,
                "on_off_grid": "error",
                "partition_check": "error",
            },
        },
    }

    if useRefEdit:
        wf["7_1"] = {"class_type": "LoadImage", "inputs": {"image": allRefs[0]}}
        editInputs = {
            "clip": ["2", 0],
            "vae": ["3", 0],
            "source_image": ["7_1", 0],
            "edit_instruction": promptText,
            "width": width,
            "height": height,
            "quality_profile": "recommended | 5 frames",
            "source_fidelity": float(sourceFidelity),
            "source_fit": "crop_center",
            "reference_detail": "match_generation_area",
            "optimize_for_still": True,
        }
        for idx, extraRef in enumerate(allRefs[1:9], start=2):
            nodeId = f"7_{idx}"
            wf[nodeId] = {"class_type": "LoadImage", "inputs": {"image": extraRef}}
            editInputs[f"reference_image_{idx}"] = [nodeId, 0]
        wf["9"] = {"class_type": "H3ReferenceEditPrepare", "inputs": editInputs}
    else:
        wf["9"] = {
            "class_type": "H3TextToImagePrepare",
            "inputs": {
                "clip": ["2", 0],
                "prompt": promptText,
                "width": width,
                "height": height,
                "quality_profile": "recommended | 5 frames",
                "optimize_for_still": True,
            },
        }

    wf["10"] = {"class_type": "BasicGuider", "inputs": {"model": ["6b", 0], "conditioning": ["9", 0]}}
    wf["12"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    wf["13"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["14"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0],
            "guider": ["10", 0],
            "sampler": ["12", 0],
            "sigmas": ["6b", 1],
            "latent_image": ["9", 1],
        },
    }
    wf["15"] = {
        "class_type": "MiniMaxH3StillDecodeT8",
        "inputs": {"av_latent": ["14", 0], "video_vae": ["3", 0], "frame_selection": "middle", "frame_index": 0},
    }
    selectorInputs = {
        "frames": ["15", 1],
        "strategy": "balanced_edit" if useRefEdit else "best_quality",
        "manual_index": 0,
        "skip_first_frames": 0,
        "candidate_start": 0.0,
        "candidate_end": 1.0,
        "similarity_weight": 0.65,
        "top_k": 4,
    }
    if useRefEdit:
        selectorInputs["source_image"] = ["7_1", 0]
    wf["15b"] = {"class_type": "H3ImageFrameSelector", "inputs": selectorInputs}
    wf["16"] = {"class_type": "SaveImage", "inputs": {"images": ["15b", 0], "filename_prefix": f"DS_H3_KF_{int(time.time())}"}}
    return wf


def buildKeyframeWorkflow(
    promptText,
    width=1344,
    height=768,
    seed=None,
    sourceImageUploadedName=None,
    referenceUploadedNames=None,
    sourceFidelity=0.72,
):
    """根据全局 keyframeEngine 设置自动分发至 H3 33B 电影级生图（默认）或 Flux3 备用生图"""
    kfEngine = str(getSettingValue("keyframeEngine", "h3_still") or "h3_still").strip().lower()
    if kfEngine == "flux3":
        return buildFlux3KeyframeWorkflow(
            promptText,
            width=width,
            height=height,
            steps=26,
            guidance=3.5,
            seed=seed,
            referenceUploadedNames=referenceUploadedNames,
            initImageUploadedName=sourceImageUploadedName,
            denoise=0.75 if sourceImageUploadedName else 1.0,
        )
    return buildH3StillKeyframeWorkflow(
        promptText,
        width=width,
        height=height,
        seed=seed,
        sourceImageUploadedName=sourceImageUploadedName,
        referenceUploadedNames=referenceUploadedNames,
        sourceFidelity=sourceFidelity,
    )


def buildFlux3KeyframeWorkflow(
    promptText,
    width=1472,
    height=832,
    steps=26,
    guidance=3.5,
    seed=None,
    referenceUploadedNames=None,
    initImageUploadedName=None,
    denoise=1.0,
):
    """
    构建无影云端 Flux3-Klein 高清工业级关键帧与定妆图生成工作流：
    1. 注入 FluxGuidance (默认 3.5) 核心引导节点，彻底激活高阶微观皮肤毛孔、骨相立体度与服饰纹理；
    2. 采样步数由 16 步全面提升至 26 步，配合 beta 调度器实现高频细节完美收敛；
    3. 在负向条件中强力封杀塑料假脸、娃娃脸、畸变五官、中文乱码与现代穿帮元素；
    4. 支持挂载 1~2 张 ReferenceLatent 潜空间参考图（锁定角色外貌与服装）。
    """
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)
    negText = (
        "plastic skin, doll face, mannequin, smooth fake porcelain face, heavy makeup, 3d render plastic sheen, "
        "deformed eyes, misaligned pupils, cross-eyed, deformed fingers, extra fingers, mutated hands, bad anatomy, "
        "amateur drawing, blurry, low resolution, oversaturated, text, Chinese characters, kanji, hanzi, signboard, "
        "nameplate, helmet label, plaque text, embroidered letters, chest badge, metal zipper, modern zipper, "
        "modern jacket, modern suspenders, modern tactical harness, modern buttons, watermark, subtitle, logo, "
        "child, kid, toddler, modern rubber boots, modern steel valve wheel"
    )
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux3_image_klein_sd_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_bf16.safetensors", "type": "flux3"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": promptText, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negText, "clip": ["2", 0]}},
        "40": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["4", 0], "guidance": float(guidance)}},
    }

    if initImageUploadedName:
        wf["30"] = {"class_type": "LoadImage", "inputs": {"image": initImageUploadedName}}
        wf["6"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["30", 0], "vae": ["3", 0]}}
    else:
        wf["6"] = {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}}

    posCondOut = ["40", 0]
    refList = [r for r in (referenceUploadedNames or []) if r]
    for idx, upName in enumerate(refList[:2]):
        loadNodeId = str(20 + idx * 3)
        encodeNodeId = str(21 + idx * 3)
        refNodeId = str(22 + idx * 3)
        wf[loadNodeId] = {"class_type": "LoadImage", "inputs": {"image": upName}}
        wf[encodeNodeId] = {"class_type": "VAEEncode", "inputs": {"pixels": [loadNodeId, 0], "vae": ["3", 0]}}
        wf[refNodeId] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": posCondOut, "latent": [encodeNodeId, 0]}}
        posCondOut = [refNodeId, 0]

    wf["7"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": seed,
            "steps": int(steps),
            "cfg": 2.5,
            "sampler_name": "euler",
            "scheduler": "beta",
            "denoise": float(denoise),
            "model": ["1", 0],
            "positive": posCondOut,
            "negative": ["5", 0],
            "latent_image": ["6", 0],
        },
    }
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    wf["9"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": f"DS_KEYFRAME_{int(time.time())}", "images": ["8", 0]}}
    return wf


def _appendH3SamplerAndDecode(
    wf,
    modelRef,
    conditioningRef,
    latentRef,
    steps=8,
    seed=None,
    enableUpscale3d=False,
    filenamePrefix="DS_H3",
    pddFile=None,
    overrideAudioRef=None,
):
    """为 MiniMax-H3 系列工作流统一挂载 PDD 8 步加速/标准采样器与 3D 潜空间超分解码链路"""
    if not seed or seed < 0:
        seed = random.randint(1, 2147483647)

    stepInt = int(steps or 8)
    usePdd = bool(pddFile) and stepInt <= 8

    if usePdd:
        nfeStr = "4" if stepInt <= 4 else ("6" if stepInt <= 6 else "8")
        wf["6b"] = {
            "class_type": "MiniMaxH3PDDAccApply",
            "inputs": {
                "model": modelRef,
                "pdd_file": pddFile,
                "nfe": nfeStr,
                "lora_strength": 1.0,
                "head_strength": 1.0,
                "on_off_grid": "error",
                "partition_check": "error",
            },
        }
        activeModelRef = ["6b", 0]
        sigmasRef = ["6b", 1]
        samplerName = "euler"
    else:
        activeModelRef = modelRef
        wf["11"] = {
            "class_type": "BasicScheduler",
            "inputs": {"model": activeModelRef, "scheduler": "simple", "steps": stepInt, "denoise": 1.0},
        }
        sigmasRef = ["11", 0]
        samplerName = "res_multistep"

    wf["10"] = {"class_type": "BasicGuider", "inputs": {"model": activeModelRef, "conditioning": conditioningRef}}
    wf["12"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": samplerName}}
    wf["13"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    wf["14"] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": ["13", 0],
            "guider": ["10", 0],
            "sampler": ["12", 0],
            "sigmas": sigmasRef,
            "latent_image": latentRef,
        },
    }

    if enableUpscale3d:
        # 使用 MiniMaxH3AVDecodeT8 安全拆包音画联合 NestedTensor，避免 MinimaxH3LatentUpscalerNode3D 直接克隆 NestedTensor 崩溃
        wf["15"] = {
            "class_type": "MiniMaxH3AVDecodeT8",
            "inputs": {"av_latent": ["14", 0], "video_vae": ["3", 0], "audio_vae": ["4", 0]},
        }
        wf["16"] = {
            "class_type": "MinimaxH3LatentUpscalerNode3D",
            "inputs": {
                "latent": ["15", 2],
                "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                "scale": 1.5,
                "device": "cuda",
                "precision": "fp16",
            },
        }
        wf["17"] = {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["3", 0]}}
        imagesOut = ["17", 0]
        audioOut = ["15", 1]
    else:
        wf["16"] = {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}}
        wf["17"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["4", 0]}}
        imagesOut = ["16", 0]
        audioOut = ["17", 0]

    wf["18"] = {
        "class_type": "VHS_VideoCombine",
        "inputs": {
            "images": imagesOut,
            "audio": overrideAudioRef if overrideAudioRef is not None else audioOut,
            "frame_rate": 24.0,
            "loop_count": 0,
            "pingpong": False,
            "format": "video/h264-mp4",
            "save_output": True,
            "filename_prefix": f"{filenamePrefix}_{int(time.time())}",
        },
    }
    return wf


def buildH3AudioDrivenWorkflow(
    promptText,
    firstFrameUploadedName,
    driveAudioUploadedName,
    lastFrameUploadedName=None,
    referenceUploadedNames=None,
    width=1344,
    height=768,
    length=124,
    steps=16,
    enableUpscale3d=False,
    seed=None,
    useRef2vaBase=False,
):
    """
    构建商业短剧生产级 Audio-First 锁声驱动口型工作流（基于 MiniMaxH3AudioConditioningT8）：
    - 将无影 IndexTTS-2 预先配好的角色台词 WAV 挂入 drive_audio，并将 audio_mode 设为 lock_source；
    - H3 按外部真实原声精准驱动人物口型、微表情与呼吸节奏，同时将无损 mux_audio 直通最终视频合成；
    - 同时兼容 FL2VA（24GB/48GB 通用）与 Ref2VA（多图参考高显存模式）。
    """
    width = max(384, (int(width) // 32) * 32)
    height = max(384, (int(height) // 32) * 32)
    length = max(73, (int(length) // 17) * 17 + 5)

    unetFile = "minimax_h3_ref2va_pruned_int8_convrot.safetensors" if useRef2vaBase else "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    pddFile = "minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors" if useRef2vaBase else "minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors"

    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unetFile, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
        "6": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["5", 0], "shift_video": 12.0, "shift_audio": 3.0}},
        "7": {"class_type": "LoadImage", "inputs": {"image": firstFrameUploadedName}},
        "7_drv": {"class_type": "LoadAudio", "inputs": {"audio": driveAudioUploadedName}},
    }

    # 音频锁声驱动模式下，显式声明 <Audio 1> 作为原声驱动源，同时去除可能冲突的即兴台词要求
    drivenPrompt = (
        f"Synchronize the speaker's lip movements, jaw motion, breathing and subtle facial micro-expressions strictly to <Audio 1>. "
        f"{promptText}"
    )

    condInputs = {
        "clip": ["2", 0],
        "video_vae": ["3", 0],
        "audio_vae": ["4", 0],
        "prompt": drivenPrompt,
        "width": width,
        "height": height,
        "length": length,
        "task_type": "Ref2VA" if useRef2vaBase else "FL2VA",
        "audio_mode": "lock_source",
        "audio_denoise_strength": 0.0,
        "add_source_as_reference": True,
        "prompt_primary_audio_ordinal": 1,
        "strict_prompt_tags": False,
        "ref_image_size": "match",
        "reference_video_policy": "official_2_to_15s",
        "drive_audio": ["7_drv", 0],
        "final_audio": ["7_drv", 0],
        "first_frame": ["7", 0],
    }

    if lastFrameUploadedName and not useRef2vaBase:
        wf["8"] = {"class_type": "LoadImage", "inputs": {"image": lastFrameUploadedName}}
        condInputs["last_frame"] = ["8", 0]

    if useRef2vaBase and referenceUploadedNames:
        for idx, refName in enumerate([r for r in referenceUploadedNames if r][:4], start=1):
            nodeId = f"7_r{idx}"
            wf[nodeId] = {"class_type": "LoadImage", "inputs": {"image": refName}}
            condInputs[f"ref_image_{idx}"] = [nodeId, 0]

    wf["9"] = {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": condInputs}
    return _appendH3SamplerAndDecode(
        wf,
        modelRef=["6", 0],
        conditioningRef=["9", 0],
        latentRef=["9", 1],
        steps=steps,
        seed=seed,
        enableUpscale3d=enableUpscale3d,
        filenamePrefix="DS_H3_AUDIO_LOCKED",
        pddFile=pddFile,
        overrideAudioRef=["9", 2],
    )


def buildH3Fl2vaWorkflow(
    promptText,
    firstFrameUploadedName,
    lastFrameUploadedName=None,
    width=1344,
    height=768,
    length=124,
    steps=8,
    enableUpscale3d=False,
    seed=None,
):
    """
    构建无影云端 MiniMax-H3 FL2VA (MiniMaxH3ImageToVideo) 首尾帧双锚点工作流：
    - 默认挂载 PDD 8 步极速加速 (minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors)；
    - 当 steps > 8 时自动回退完整原生多步采样；
    - 支持通过 MiniMaxH3AVDecodeT8 + MinimaxH3LatentUpscalerNode3D 安全执行 3D 潜空间超分。
    """
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
    return _appendH3SamplerAndDecode(
        wf,
        modelRef=["6", 0],
        conditioningRef=["9", 0],
        latentRef=["9", 1],
        steps=steps,
        seed=seed,
        enableUpscale3d=enableUpscale3d,
        filenamePrefix="DS_H3_FL2VA",
        pddFile="minimax_h3_fl2va_pdd_acc_8step_comfyui.safetensors",
    )


def buildH3Ref2vaWorkflow(
    promptText,
    firstFrameUploadedName,
    referenceUploadedNames=None,
    refAudioUploadedName=None,
    width=1344,
    height=768,
    length=124,
    steps=8,
    enableUpscale3d=False,
    seed=None,
):
    """
    构建无影云端 MiniMax-H3 Ref2VA (MiniMaxH3ReferenceToVideo) 多图参考主体一致性工作流：
    - 使用 minimax_h3_ref2va_pruned_int8_convrot.safetensors + PDD 8 步加速；
    - 支持同时注入首帧、角色/场景参考图与角色参考音色音频 (ref_audio_1)，保持跨镜头人物外貌与声音高度一致。
    """
    width = max(384, (int(width) // 32) * 32)
    height = max(384, (int(height) // 32) * 32)
    length = max(73, (int(length) // 17) * 17 + 5)

    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {"model": ["1", 0]}},
        "6": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["5", 0], "shift_video": 12.0, "shift_audio": 3.0}},
    }

    refList = [firstFrameUploadedName] if firstFrameUploadedName else []
    for refName in (referenceUploadedNames or []):
        if refName and refName not in refList and len(refList) < 9:
            refList.append(refName)

    refInputs = {
        "clip": ["2", 0],
        "vae": ["3", 0],
        "audio_vae": ["4", 0],
        "prompt": promptText,
        "width": width,
        "height": height,
        "length": length,
        "ref_image_size": "match",
    }
    for idx, refName in enumerate(refList, start=1):
        nodeId = f"7_{idx}"
        wf[nodeId] = {"class_type": "LoadImage", "inputs": {"image": refName}}
        refInputs[f"ref_image_{idx}"] = [nodeId, 0]

    if refAudioUploadedName:
        wf["7_a1"] = {"class_type": "LoadAudio", "inputs": {"audio": refAudioUploadedName}}
        refInputs["ref_audio_1"] = ["7_a1", 0]

    wf["9"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": refInputs}
    return _appendH3SamplerAndDecode(
        wf,
        modelRef=["6", 0],
        conditioningRef=["9", 0],
        latentRef=["9", 1],
        steps=steps,
        seed=seed,
        enableUpscale3d=enableUpscale3d,
        filenamePrefix="DS_H3_REF2VA",
        pddFile="minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors",
    )


def buildFastH3Workflow(
    promptText,
    firstFrameUploadedName,
    width=1344,
    height=768,
    length=124,
    steps=8,
    enableUpscale3d=False,
    seed=None,
):
    """构建无影云端 FastH3 (fastvideo_fasth3_8step_v2_pruned_int8_convrot) 8 步极速单首帧工作流"""
    width = max(384, (int(width) // 32) * 32)
    height = max(384, (int(height) // 32) * 32)
    length = max(73, (int(length) // 17) * 17 + 5)

    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "7": {"class_type": "LoadImage", "inputs": {"image": firstFrameUploadedName}},
        "9": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "first_frame": ["7", 0],
                "prompt": promptText,
                "width": width,
                "height": height,
                "length": length,
            },
        },
    }
    return _appendH3SamplerAndDecode(
        wf,
        modelRef=["1", 0],
        conditioningRef=["9", 0],
        latentRef=["9", 1],
        steps=min(8, max(4, int(steps or 8))),
        seed=seed,
        enableUpscale3d=enableUpscale3d,
        filenamePrefix="DS_FASTH3",
        pddFile=None,
    )


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
    """提交工作流到 ComfyUI 并轮询直至下载产物（带跨网超时放宽与自动重试）"""
    endpoint = comfyEndpoint.rstrip("/")
    payload = json.dumps({"prompt": workflowDict}).encode("utf-8")

    promptId = None
    lastSubmitErr = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{endpoint}/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _DIRECT_HTTP_OPENER.open(req, timeout=90) as resp:
                resJson = json.loads(resp.read().decode("utf-8"))
                promptId = resJson.get("prompt_id")
                if not promptId:
                    raise RuntimeError(f"ComfyUI 未返回有效 prompt_id: {resJson}")
                break
        except Exception as err:
            lastSubmitErr = err
            time.sleep(2.0 * (attempt + 1))

    if not promptId:
        raise RuntimeError(f"提交工作流至 ComfyUI 超时或失败: {lastSubmitErr}")

    started = time.time()
    while time.time() - started < timeoutSec:
        time.sleep(2.5)
        try:
            histReq = urllib.request.Request(f"{endpoint}/history/{promptId}", method="GET")
            with _DIRECT_HTTP_OPENER.open(histReq, timeout=25) as histResp:
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
                fileBytes = None
                lastDlErr = None
                for dlAttempt in range(3):
                    try:
                        with _DIRECT_HTTP_OPENER.open(viewUrl, timeout=300) as dlResp:
                            fileBytes = dlResp.read()
                            break
                    except Exception as dlErr:
                        lastDlErr = dlErr
                        time.sleep(2.0 * (dlAttempt + 1))
                if fileBytes is None:
                    raise RuntimeError(f"从云端下载渲染产物超时或失败: {lastDlErr}")

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


def _generateLocalFallbackAssetImage(assetRow, width=1024, height=1024):
    """当无影云离线时，为角色/场景/道具生成本地预演设定图"""
    from PIL import Image, ImageDraw

    aType = assetRow.get("assetType") or "character"
    palette = (38, 28, 52) if aType == "character" else (20, 42, 54) if aType == "scene" else (48, 36, 22)
    canvas = Image.new("RGB", (width, height), palette)
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        r = min(255, int(palette[0] + (y / height) * 35))
        g = min(255, int(palette[1] + (y / height) * 28))
        b = min(255, int(palette[2] + (y / height) * 42))
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    draw.rectangle([28, 28, width - 28, height - 28], outline=(245, 158, 11), width=3)

    localName = f"asset_ref_{assetRow['id']}_{int(time.time()*1000)}.png"
    localPath = ASSETS_DIR / localName
    canvas.save(str(localPath), format="PNG")
    return f"/media/assets/{localName}"


def enqueueAssetImageTask(assetId):
    """
    为单个角色、场景或道具生成标准定妆照 / 设定参考图：
    生成后的定妆照将保存在 assets.imageUrl 中，供导演审阅锁定，并在后续分镜生成时作为视觉基准。
    """
    import shutil

    now = time.time()
    taskId = f"ast_{uuid.uuid4().hex[:10]}"
    with closing(getDbConnection()) as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id=?", (assetId,)).fetchone()
        if not asset:
            raise ValueError("未找到指定设定资产")
        ep = conn.execute("SELECT id FROM episodes WHERE projectId=? ORDER BY episodeNumber ASC LIMIT 1", (asset["projectId"],)).fetchone()
        epId = ep["id"] if ep else 0
        conn.execute(
            """
            INSERT INTO renderTasks (id, projectId, episodeId, shotId, taskType, engineName, status, phaseText, auditJson, createdAt, updatedAt)
            VALUES (?, ?, ?, 0, 'asset_image', 'flux3_klein', 'running', ?, ?, ?, ?)
            """,
            (
                taskId,
                asset["projectId"],
                epId,
                f"正在排队生成 @{asset['name']} 定妆设定图...",
                json.dumps({"assetId": assetId, "assetName": asset["name"]}, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()

    def worker():
        try:
            with _COMFY_JOB_SEMAPHORE:
                with closing(getDbConnection()) as conn:
                    assetRow = dict(conn.execute("SELECT * FROM assets WHERE id=?", (assetId,)).fetchone())
                    projRow = dict(conn.execute("SELECT * FROM projects WHERE id=?", (assetRow["projectId"],)).fetchone())
                    conn.execute(
                        "UPDATE renderTasks SET phaseText=?, updatedAt=? WHERE id=?",
                        (f"正在生成 @{assetRow['name']} 定妆照/设定图...", time.time(), taskId),
                    )
                    conn.commit()

                visualStyle = projRow.get("visualStyle") or "3D国漫电影级写实质感"
                promptText = compileAssetSheetPrompt(assetRow, visualStyle=visualStyle)
                aType = assetRow.get("assetType") or "character"
                width, height = (1024, 1024) if aType != "scene" else (1472, 832)
                freshSeed = random.randint(1, 2147483647)

                comfyStatus = checkComfyStatus()
                if not comfyStatus["online"]:
                    raise RuntimeError(f"云端 ComfyUI 算力服务未在线 ({comfyStatus.get('endpoint')})，已拒绝生成本地假设定图")
                wf = buildKeyframeWorkflow(promptText, width=width, height=height, seed=freshSeed)
                _, kfUrl, kfPath = _submitAndPollComfyWorkflow(wf, comfyStatus["endpoint"], expectedType="image")
                localName = f"asset_ref_{assetId}_{int(time.time()*1000)}.png"
                assetDest = ASSETS_DIR / localName
                shutil.copy2(str(kfPath), str(assetDest))
                imgUrl = f"/media/assets/{localName}"
                modeNote = f"@{assetRow['name']} 云端定妆设定图已生成"

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE assets SET imageUrl=?, updatedAt=? WHERE id=?",
                        (imgUrl, time.time(), assetId),
                    )
                    conn.execute(
                        "UPDATE renderTasks SET status='succeeded', phaseText=?, outputUrl=?, updatedAt=? WHERE id=?",
                        (modeNote, imgUrl, time.time(), taskId),
                    )
                    conn.commit()
        except Exception as err:
            with closing(getDbConnection()) as conn:
                conn.execute(
                    "UPDATE renderTasks SET status='failed', phaseText='设定图生成失败', errorMessage=?, updatedAt=? WHERE id=?",
                    (str(err), time.time(), taskId),
                )
                conn.commit()

    threading.Thread(target=worker, daemon=True).start()
    return taskId


def _collectShotCharacterRefUploads(shotRow, projectAssets, comfyEndpoint):
    """
    【即梦 Seedance 全能多主体参考 (Omni-Reference)】
    按优先级收集本镜绑定的“角色定妆图 (character) -> 场景空间母板图 (scene) -> 核心交互道具图 (prop)”并上传至云端，
    供 H3ReferenceEditPrepare 与 MiniMaxH3ReferenceToVideo 同时锁死人物外貌、房间建筑光影与道具造型（最多 6 张）。
    """
    assetMap = {str(a.get("name", "")).lstrip("@").strip(): a for a in (projectAssets or [])}
    try:
        refs = json.loads(shotRow.get("referenceAssetsJson") or "[]")
    except Exception:
        refs = []

    charUploads, sceneUploads, propUploads = [], [], []
    for r in refs:
        if not isinstance(r, dict):
            continue
        aName = str(r.get("asset", "")).lstrip("@").strip()
        aObj = assetMap.get(aName)
        if not aObj or not aObj.get("imageUrl"):
            continue
        imgUrl = str(aObj["imageUrl"])
        if "_1793949" in imgUrl:
            continue
        localPath = resolveMediaUrlToPath(imgUrl)
        if not localPath or not localPath.exists():
            continue
        try:
            upName = uploadImageToComfy(localPath, comfyEndpoint)
        except Exception:
            continue
        if not upName:
            continue
        aType = aObj.get("assetType")
        if aType == "character" and upName not in charUploads:
            charUploads.append(upName)
        elif aType == "scene" and upName not in sceneUploads:
            sceneUploads.append(upName)
        elif aType == "prop" and upName not in propUploads:
            propUploads.append(upName)

    omniList = []
    for item in charUploads[:3] + sceneUploads[:1] + propUploads[:2]:
        if item not in omniList:
            omniList.append(item)
    return omniList[:6]


def _collectShotVoiceRefUpload(shotRow, projectAssets, comfyEndpoint):
    """收集本镜主说话角色的固定声纹参考音频 (assets.voiceRefUrl) 并上传至无影云端，供 Ref2VA 锁声"""
    assetMap = {str(a.get("name", "")).lstrip("@").strip(): a for a in (projectAssets or [])}
    candidateNames = []
    try:
        for line in json.loads(shotRow.get("dialogueJson") or "[]"):
            if isinstance(line, dict) and str(line.get("speaker") or "").strip():
                candidateNames.append(str(line["speaker"]).lstrip("@").strip())
    except Exception:
        pass
    try:
        for r in json.loads(shotRow.get("referenceAssetsJson") or "[]"):
            if isinstance(r, dict) and r.get("role") == "character" and str(r.get("asset") or "").strip():
                candidateNames.append(str(r["asset"]).lstrip("@").strip())
    except Exception:
        pass

    for name in candidateNames:
        aObj = assetMap.get(name)
        if not aObj or not aObj.get("voiceRefUrl"):
            continue
        localPath = resolveMediaUrlToPath(aObj["voiceRefUrl"])
        if localPath and localPath.exists():
            try:
                return uploadAudioToComfy(localPath, comfyEndpoint)
            except Exception:
                continue
    return None


def _findSameSceneCameraRigMasterPlate(shotRow, comfyEndpoint):
    """
    【即梦式多机位正反打 & 同角色无缝延长防跳切 (Anti-Jump-Cut & Multi-Cam Rig)】
    1. 若紧邻上一镜就是同一角色 (A -> A 连续镜头)：优先直接沿用上一镜已生成视频的真实最后一帧 (lastFrameUrl)，
       实现即梦式“视频延长”一镜到底，彻底杜绝同机位闪回跳切 (Jump Cut)！
    2. 若中间隔了对手反打镜头 (A -> B -> A 正反打切回)：才复用该角色在本场戏首次生成的同机位母板底图。
    """
    import re
    titleStr = str(shotRow.get("title") or "")
    mScene = re.search(r"\[(S\d+)\]", titleStr)
    scenePrefix = mScene.group(1) if mScene else ""

    def _charTuple(rowDict):
        try:
            refs = json.loads(rowDict.get("referenceAssetsJson") or "[]")
        except Exception:
            refs = []
        return tuple(
            str(r.get("asset") or "").lstrip("@").strip()
            for r in refs
            if isinstance(r, dict) and r.get("role") == "character" and str(r.get("asset") or "").strip()
        )

    def _framingBucket(fText):
        t = str(fText or "").lower()
        if any(k in t for k in ("extreme close", "close-up", "closeup", "特写")):
            return "closeup"
        if any(k in t for k in ("wide", "long", "establishing", "远景", "全景")):
            return "wide"
        return "medium"

    targetChars = _charTuple(shotRow)
    targetBucket = _framingBucket(shotRow.get("cameraFraming"))

    with closing(getDbConnection()) as conn:
        earlierShots = conn.execute(
            "SELECT * FROM shots WHERE episodeId=? AND shotNumber < ? ORDER BY shotNumber ASC",
            (shotRow["episodeId"], shotRow["shotNumber"]),
        ).fetchall()

    if not earlierShots:
        return None

    # 防跳切法则 1：若紧邻上一镜就是同一角色（且非空），绝不拿上一镜 0.0s 起点图重画，而是直接返回上一镜真实尾帧做无缝延长
    immedPrev = dict(earlierShots[-1])
    if targetChars and _charTuple(immedPrev) == targetChars:
        prevEndUrl = str(immedPrev.get("lastFrameUrl") or "")
        if prevEndUrl:
            localEnd = resolveMediaUrlToPath(prevEndUrl)
            if localEnd and localEnd.exists():
                try:
                    return uploadImageToComfy(localEnd, comfyEndpoint)
                except Exception:
                    pass
        return None

    # 筛选同场戏历史有效高清首帧母板
    sameSceneEarlier = []
    for earlier in earlierShots:
        eDict = dict(earlier)
        eUrl = str(eDict.get("firstFrameUrl") or "")
        if not eUrl or eUrl.startswith("/media/keyframes/extracted_last_"):
            continue
        if scenePrefix and f"[{scenePrefix}]" not in str(eDict.get("title") or ""):
            continue
        sameSceneEarlier.append((eDict, eUrl))

    # 正反打 & 阵营分组法则 2：隔镜切回同一角色或同一阵营小组且景别一致时，复用首次同机位固定母板底图
    if targetChars:
        for eDict, eUrl in sameSceneEarlier:
            if eDict["id"] == immedPrev["id"]:
                continue
            if _charTuple(eDict) == targetChars and _framingBucket(eDict.get("cameraFraming")) == targetBucket:
                localPlate = resolveMediaUrlToPath(eUrl)
                if localPlate and localPlate.exists():
                    try:
                        return uploadImageToComfy(localPlate, comfyEndpoint)
                    except Exception:
                        continue

    # 多人重定场 & 道具桌面母板法则 3：
    # - 多人/双人全景重定场镜自动回溯复用本场戏最早的全景定场母板，保证房间格局 100% 不变；
    # - 无人物纯道具特写镜优先复用同场定场母板保持桌面材质与烛光色温统一。
    if targetBucket == "wide" or (not targetChars):
        for eDict, eUrl in sameSceneEarlier:
            if _framingBucket(eDict.get("cameraFraming")) == "wide" or (not targetChars and len(_charTuple(eDict)) >= 2):
                localPlate = resolveMediaUrlToPath(eUrl)
                if localPlate and localPlate.exists():
                    try:
                        return uploadImageToComfy(localPlate, comfyEndpoint)
                    except Exception:
                        continue
    return None


def enqueueKeyframeTask(shotId, frameRole="both", forceRegenerate=True):
    """
    提交分镜首帧 / 尾帧 / 双关键帧生成或重新生成任务：
    - frameRole: 'first' | 'last' | 'both'
    - forceRegenerate: 为 True 时强制重新生成对应关键帧（即使之前已有图片或曾勾选 lockFromPrevEnd）
    """
    now = time.time()
    taskId = f"kf_{uuid.uuid4().hex[:10]}"
    with closing(getDbConnection()) as conn:
        shot = conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone()
        if not shot:
            raise ValueError("未找到指定镜头")
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
                conn.execute(
                    "UPDATE renderTasks SET phaseText='正在排队等待云端通道空闲...', updatedAt=? WHERE id=?",
                    (time.time(), taskId),
                )
                conn.commit()
            with _COMFY_JOB_SEMAPHORE:
                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE renderTasks SET phaseText='正在生成关键帧画面...', updatedAt=? WHERE id=?",
                        (time.time(), taskId),
                    )
                    conn.commit()
                    shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                    projRow = dict(conn.execute("SELECT * FROM projects WHERE id=?", (shotRow["projectId"],)).fetchone())

                syncEpisodeFrameLocks(shotRow["episodeId"], overwriteExisting=True)
                with closing(getDbConnection()) as conn:
                    shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                    projectAssets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (shotRow["projectId"],))]

                comfyStatus = checkComfyStatus()
                width, height = getAspectRatioDimensions(
                    projRow.get("aspectRatio", "16:9"),
                    isVideo=False,
                    is24GbCard=bool(comfyStatus.get("is24GbCard")),
                )
                sharedSeed = random.randint(1, 2147483647)

                firstUrl = shotRow.get("firstFrameUrl") or ""
                lastUrl = shotRow.get("lastFrameUrl") or ""

                needFirst = frameRole in ("first", "both") and not (int(shotRow.get("lockFromPrevEnd") or 0) == 1 and firstUrl)
                needLast = frameRole in ("last", "both") and (shotRow.get("shotCategory") == "action_fl2va" or frameRole == "last")

                lockedFirstPrompt = compileLockedKeyframePrompt(shotRow, projectAssets, projRow.get("visualStyle", ""), frameRole="first")
                lockedLastPrompt = compileLockedKeyframePrompt(shotRow, projectAssets, projRow.get("visualStyle", ""), frameRole="last")

                if not comfyStatus["online"]:
                    raise RuntimeError(f"云端 ComfyUI 算力服务未在线 ({comfyStatus.get('endpoint')})，已拒绝生成本地假关键帧")

                epUrl = comfyStatus["endpoint"]
                charRefUploads = _collectShotCharacterRefUploads(shotRow, projectAssets, epUrl)
                if comfyStatus.get("is24GbCard"):
                    charRefUploads = charRefUploads[:2]
                firstPathLocal = resolveMediaUrlToPath(firstUrl) if firstUrl else None

                if needFirst:
                    rigMasterPlate = _findSameSceneCameraRigMasterPlate(shotRow, epUrl)
                    firstPromptForRender = (
                        f"Keep the exact same camera angle, room architecture, background props, lighting and character identity from <Picture 1>, "
                        f"update the character's acting expression and gesture for this shot: {lockedFirstPrompt}"
                        if rigMasterPlate
                        else lockedFirstPrompt
                    )
                    wf1 = buildKeyframeWorkflow(
                        firstPromptForRender,
                        width=width,
                        height=height,
                        seed=sharedSeed,
                        sourceImageUploadedName=rigMasterPlate,
                        referenceUploadedNames=charRefUploads,
                        sourceFidelity=0.78 if rigMasterPlate else 0.65,
                    )
                    _, firstUrl, firstPathLocal = _submitAndPollComfyWorkflow(wf1, epUrl, expectedType="image")

                if needLast:
                    # 尾帧基于首帧做高保真动作演进编辑（首帧作 <Picture 1>，角色定妆图作后续参考），确保场景/光影/脸部 100% 连续
                    firstUploadedForEdit = uploadImageToComfy(firstPathLocal, epUrl) if (firstPathLocal and firstPathLocal.exists()) else None
                    lastEditPrompt = (
                        f"Keep the exact same scene, background architecture, lighting, costume and character face from <Picture 1>, "
                        f"only evolve the character's pose and facial expression to the final dramatic beat: {lockedLastPrompt}"
                        if firstUploadedForEdit
                        else lockedLastPrompt
                    )
                    wf2 = buildKeyframeWorkflow(
                        lastEditPrompt,
                        width=width,
                        height=height,
                        seed=sharedSeed + 10007,
                        sourceImageUploadedName=firstUploadedForEdit,
                        referenceUploadedNames=charRefUploads,
                        sourceFidelity=0.72,
                    )
                    _, lastUrl, _ = _submitAndPollComfyWorkflow(wf2, epUrl, expectedType="image")
                modeNote = "无影云端 H3-33B 电影级关键帧渲染完成（已启用首尾帧同源参考编辑）"

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
                conn.execute(
                    "UPDATE renderTasks SET phaseText='正在排队等待云端通道空闲...', updatedAt=? WHERE id=?",
                    (time.time(), taskId),
                )
                conn.commit()
            with _COMFY_JOB_SEMAPHORE:
                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE renderTasks SET phaseText='正在校验首尾关键帧与连续性锚点...', updatedAt=? WHERE id=?",
                        (time.time(), taskId),
                    )
                    conn.commit()
                    shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                    projRow = dict(conn.execute("SELECT * FROM projects WHERE id=?", (shotRow["projectId"],)).fetchone())

                # 1. 同步上一镜帧锁并自动回填本集已生成镜头的角色固定声纹母本
                syncEpisodeFrameLocks(shotRow["episodeId"], overwriteExisting=True)
                try:
                    backfillEpisodeVoiceBanks(shotRow["episodeId"])
                except Exception as vbErr:
                    print(f"[comfyBridge] 声纹回填提示: {vbErr}")
                with closing(getDbConnection()) as conn:
                    shotRow = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shotId,)).fetchone())
                    projectAssets = [dict(a) for a in conn.execute("SELECT * FROM assets WHERE projectId=?", (shotRow["projectId"],))]

                comfyStatus = checkComfyStatus()
                if not comfyStatus["online"]:
                    raise RuntimeError(f"云端 ComfyUI 算力服务未在线 ({comfyStatus.get('endpoint')})，已拒绝生成本地静态推拉假视频")
                is24Gb = bool(comfyStatus.get("is24GbCard"))
                vWidth, vHeight = getAspectRatioDimensions(projRow.get("aspectRatio", "16:9"), isVideo=True, is24GbCard=is24Gb)

                # 2. 确保首帧就绪：若紧邻上一镜为同角色连续镜头且上一镜已有成片，强制提取上一镜真实最末视频帧作为本镜首帧（实现即梦式 0 跳切“视频延长”）
                firstUrl = shotRow.get("firstFrameUrl") or ""
                lastUrl = shotRow.get("lastFrameUrl") or ""
                with closing(getDbConnection()) as conn:
                    prevShotRow = conn.execute(
                        "SELECT * FROM shots WHERE episodeId=? AND shotNumber < ? ORDER BY shotNumber DESC LIMIT 1",
                        (shotRow["episodeId"], shotRow["shotNumber"]),
                    ).fetchone()
                if prevShotRow:
                    prevDict = dict(prevShotRow)
                    from .continuityManager import _extractShotCharacterKey
                    curChars = _extractShotCharacterKey(shotRow)
                    prevChars = _extractShotCharacterKey(prevDict)
                    if (int(shotRow.get("lockFromPrevEnd") or 0) == 1 or (curChars and curChars == prevChars)) and prevDict.get("videoUrl"):
                        try:
                            exactPrevLastUrl = extractHighResLastFrameFromVideo(prevDict["id"], forceFromVideo=True)
                            if exactPrevLastUrl:
                                firstUrl = exactPrevLastUrl
                                with closing(getDbConnection()) as conn:
                                    conn.execute(
                                        "UPDATE shots SET firstFrameUrl=?, lockFromPrevEnd=1, continuityMode='continue_action', updatedAt=? WHERE id=?",
                                        (firstUrl, time.time(), shotId),
                                    )
                                    conn.commit()
                        except Exception as extErr:
                            print(f"[comfyBridge] 自动提取上一镜末尾帧做无缝延长提示: {extErr}")
                # 自动识别并淘汰旧版遗留的带中文标牌乱码关键帧或离线假关键帧
                if firstUrl and any(oldTag in firstUrl for oldTag in ("_1793949", "_1793950", "_179436178")):
                    firstUrl = ""
                    lastUrl = ""
                charRefUploads = _collectShotCharacterRefUploads(shotRow, projectAssets, comfyStatus["endpoint"])
                voiceRefUpload = _collectShotVoiceRefUpload(shotRow, projectAssets, comfyStatus["endpoint"])
                sharedKfSeed = random.randint(1, 2147483647)
                if not firstUrl:
                    rigMasterPlate = _findSameSceneCameraRigMasterPlate(shotRow, comfyStatus["endpoint"])
                    lockedFirst = compileLockedKeyframePrompt(shotRow, projectAssets, projRow.get("visualStyle", ""), frameRole="first")
                    firstPromptForRender = (
                        f"Keep the exact same camera angle, room architecture, background props, lighting and character identity from <Picture 1>, "
                        f"update the character's acting expression and gesture for this shot: {lockedFirst}"
                        if rigMasterPlate
                        else lockedFirst
                    )
                    wfFirst = buildKeyframeWorkflow(
                        firstPromptForRender,
                        width=vWidth,
                        height=vHeight,
                        seed=sharedKfSeed,
                        sourceImageUploadedName=rigMasterPlate,
                        referenceUploadedNames=charRefUploads,
                        sourceFidelity=0.78 if rigMasterPlate else 0.65,
                    )
                    _, firstUrl, _ = _submitAndPollComfyWorkflow(wfFirst, comfyStatus["endpoint"], expectedType="image")

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE shots SET firstFrameUrl=?, updatedAt=? WHERE id=?",
                        (firstUrl, time.time(), shotId),
                    )
                    conn.execute(
                        "UPDATE renderTasks SET phaseText='正在上传首帧关键帧至无影云端...', updatedAt=? WHERE id=?",
                        (time.time(), taskId),
                    )
                    conn.commit()

                firstPath = resolveMediaUrlToPath(firstUrl)
                lastPath = resolveMediaUrlToPath(lastUrl) if lastUrl else None

                engine = engineOverride or shotRow.get("videoEngine") or "h3_fl2va"
                # 配音先行（Audio-First）：若本镜含台词且已开启音频锁声驱动且无影 IndexTTS-2 在线，先自动配音并按真实音频时长锁帧数
                driveAudioUploaded = None
                try:
                    shotDlgs = json.loads(shotRow.get("dialogueJson") or "[]")
                except Exception:
                    shotDlgs = []
                hasSpokenLine = any(isinstance(d, dict) and str(d.get("text") or "").strip() for d in shotDlgs)
                enableAudioDrive = str(getSettingValue("enableAudioDrivenLipSync", "1")) == "1"
                if hasSpokenLine and enableAudioDrive and engine != "wan22_moe":
                    try:
                        from .ttsBridge import checkTtsStatus, synthesizeShotDialogueAudio
                        ttsUrl = str(shotRow.get("ttsAudioUrl") or "").strip()
                        ttsPath = resolveMediaUrlToPath(ttsUrl) if ttsUrl else None
                        if not (ttsPath and ttsPath.exists() and ttsPath.stat().st_size > 2048):
                            if checkTtsStatus().get("online"):
                                with closing(getDbConnection()) as conn:
                                    conn.execute(
                                        "UPDATE renderTasks SET phaseText='正在调用无影 IndexTTS-2 生成角色固定声纹配音...', updatedAt=? WHERE id=?",
                                        (time.time(), taskId),
                                    )
                                    conn.commit()
                                ttsRes = synthesizeShotDialogueAudio(shotId, forceRegenerate=False)
                                ttsUrl = ttsRes.get("ttsAudioUrl") or ""
                                ttsPath = resolveMediaUrlToPath(ttsUrl) if ttsUrl else None
                                if ttsRes.get("recommendedShotDur"):
                                    shotRow["durationSec"] = float(ttsRes["recommendedShotDur"])
                        if ttsPath and ttsPath.exists():
                            driveAudioUploaded = uploadAudioToComfy(ttsPath, comfyStatus["endpoint"])
                    except Exception as ttsErr:
                        print(f"[comfyBridge] IndexTTS-2 配音先行回退原生发声: {ttsErr}")

                enableUpscale = (str(projRow.get("enableUpscale3d", 0)) == "1") and (not is24Gb)
                steps = max(4, int(getSettingValue("h3SamplingSteps", "16") or 16))
                frameLength = calculateH3FrameGrid(shotRow.get("durationSec", 5.0))
                if is24Gb:
                    # 896x512 画幅像素面积仅占 1344x768 的 44%，24GB 卡可安全跑 107~124 帧 (4.5~5.17s)，确保长句台词从容说完不腰斩
                    frameLength = min(124, max(107, frameLength))
                    charRefUploads = charRefUploads[:2]

                upFirst = uploadImageToComfy(firstPath, comfyStatus["endpoint"])
                # 默认关闭强制首尾双锚点（单首帧自由演进动作更自然流畅；仅在显式开启 strictDualFrameAnchor=1 时锁尾帧）
                useDualAnchor = str(getSettingValue("strictDualFrameAnchor", "0")) == "1"
                upLast = (
                    uploadImageToComfy(lastPath, comfyStatus["endpoint"])
                    if (useDualAnchor and lastPath and lastPath.exists() and shotRow.get("shotCategory") == "action_fl2va")
                    else None
                )

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE renderTasks SET phaseText='关键帧已锁定，正在提交视频扩散模型渲染...', updatedAt=? WHERE id=?",
                        (time.time(), taskId),
                    )
                    conn.commit()

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
                elif driveAudioUploaded:
                    compiledPrompt = compileFl2vaVideoPrompt(shotRow, hasLastFrame=bool(upLast))
                    wfVideo = buildH3AudioDrivenWorkflow(
                        compiledPrompt,
                        upFirst,
                        driveAudioUploadedName=driveAudioUploaded,
                        lastFrameUploadedName=upLast,
                        referenceUploadedNames=charRefUploads if (not is24Gb and engine == "h3_ref2va") else None,
                        width=vWidth,
                        height=vHeight,
                        length=frameLength,
                        steps=steps,
                        enableUpscale3d=enableUpscale,
                        useRef2vaBase=(not is24Gb and engine == "h3_ref2va"),
                    )
                elif (not is24Gb) and (engine == "h3_ref2va" or (engine == "h3_fl2va" and not upLast and (voiceRefUpload or charRefUploads))):
                    compiledPrompt = compileFl2vaVideoPrompt(shotRow, hasLastFrame=False)
                    wfVideo = buildH3Ref2vaWorkflow(
                        compiledPrompt,
                        upFirst,
                        referenceUploadedNames=charRefUploads,
                        refAudioUploadedName=voiceRefUpload,
                        width=vWidth,
                        height=vHeight,
                        length=frameLength,
                        steps=steps,
                        enableUpscale3d=enableUpscale,
                    )
                elif engine == "fasth3":
                    compiledPrompt = compileFl2vaVideoPrompt(shotRow, hasLastFrame=False)
                    wfVideo = buildFastH3Workflow(
                        compiledPrompt,
                        upFirst,
                        width=vWidth,
                        height=vHeight,
                        length=frameLength,
                        steps=steps,
                        enableUpscale3d=enableUpscale,
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

                # 3. 执行 Auto-QC 视频质检
                qcReport = inspectVideoQuality(videoLocalPath)
                qcReport["compiledPrompt"] = compiledPrompt
                qcReport["engine"] = engine
                qcReport["usedFirstFrame"] = firstUrl
                qcReport["usedLastFrame"] = lastUrl if shotRow.get("shotCategory") == "action_fl2va" else ""

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE shots SET videoUrl=?, qcReportJson=?, updatedAt=? WHERE id=?",
                        (videoUrl, json.dumps(qcReport, ensure_ascii=False), time.time(), shotId),
                    )
                    conn.commit()

                # 4. 自动提取角色黄金声纹入库锁定（若该角色首次开口），并提取视频末尾帧更新帧锁
                try:
                    extractAndLockCharacterVoiceBank(shotId)
                except Exception as vbErr:
                    print(f"[comfyBridge] 提取角色声纹警告 (shotId={shotId}): {vbErr}")
                try:
                    extractHighResLastFrameFromVideo(shotId)
                except Exception as exErr:
                    print(f"[comfyBridge] 提取视频末尾帧警告 (shotId={shotId}): {exErr}")
                syncEpisodeFrameLocks(shotRow["episodeId"], overwriteExisting=True)

                with closing(getDbConnection()) as conn:
                    conn.execute(
                        "UPDATE shots SET status='completed', updatedAt=? WHERE id=?",
                        (time.time(), shotId),
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
