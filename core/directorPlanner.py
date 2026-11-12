#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自主版权 AI 短剧工业化工作台 - 三阶导演剧本改写、空间调度账本与多引擎提示词编译器 (directorPlanner.py)
核心能力：
1. 【阶段一：小说 → 影视化分场剧本 + 场次空间调度账本 + 资产自动提取 (rewriteNovelToSceneLedger)】
   - 将抽象心理/旁白外化为可见物理动作 (Show, Don't Tell)，精炼口语台词；
   - 为每场戏建立 180° 空间站位轴线 (spatialAxis)、前世/今生时空隔离锁 (eraTag) 与道具状态流转账本 (propLedger)；
   - 自动识别并补全角色、场景、核心道具的标准化视觉设定词。
2. 【阶段二：分场调度账本 → 首尾关键帧结构化分镜表 (breakScriptIntoKeyframedShots)】
   - 严格区分“动作推进镜 (action_fl2va)”与“台词博弈镜 (dialogue_i2v)”；
   - 每个镜头继承所属场次的 180° 轴线与道具状态，生成同机位同光照的 firstFramePrompt 与 lastFramePrompt。
3. 【阶段三：编译为 MiniMax-H3 FL2VA / Wan 2.2 MoE 原生底层指令】
"""

import re
import json
import urllib.request
import urllib.error


def _extractCorePoseFromFramePrompt(rawFramePrompt, eraTag=""):
    """
    从完整的生图 Prompt 中剥离冗长的静态环境/风格/人物设定头部与尾部约束，
    仅提取最核心的“动作姿态描述句”，避免视频模型因几百字重复静态描述而把视频渲染成静态照片。
    """
    text = str(rawFramePrompt or "").strip()
    if not text:
        return ""
    for marker in (
        "OPENING KEYFRAME POSE (0.0s):",
        "OPENING POSE (0.0s):",
        "ENDING KEYFRAME POSE (Completed Payoff):",
        "ENDING POSE (Keep exact same camera angle, character standing/sitting posture, face, clothing, and background layout as opening frame; only update the action result):",
        "ENDING POSE:",
    ):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break
    for tailMarker in ("Pure photographic film frame", "Strictly NO text"):
        if tailMarker in text:
            text = text.split(tailMarker, 1)[0].strip()
    return extractPureEnglishVisual(text, eraTag)


def compileFl2vaVideoPrompt(shotDict, hasLastFrame=True):
    """
    将单个结构化镜头编译为 MiniMax-H3 ImageToVideo (FL2VA 首尾帧模型) 原生高动态音画提示词。
    重要铁律：
    1. 彻底剥离生图阶段冗长的静态背景/服装重复堆砌与 "Pure photographic frame" 静态词，
       100% 聚焦于镜头推拉运动、0.0s 起始动作、中间大幅度连续物理动作演进与结尾落点姿态；
    2. 除 <d>[Chinese] ...</d> 真实角色对白外，所有动作与运镜描述 100% 使用纯英文，杜绝模型乱读舞台提示词；
    3. 每个有声镜头严格聚焦单人紧凑台词口型同步，无台词镜头强制声明零人声与高张力环境拟音。
    """
    duration = float(shotDict.get("durationSec") or 5.0)
    framing = extractPureEnglishVisual(shotDict.get("cameraFraming") or "Medium shot") or "Medium shot"
    movement = extractPureEnglishVisual(shotDict.get("cameraMovement") or "Dynamic push-in camera") or "Dynamic push-in camera"
    firstPose = _extractCorePoseFromFramePrompt(shotDict.get("firstFramePrompt") or "")
    actionArc = extractPureEnglishVisual(shotDict.get("actionArcPrompt") or "")
    lastPose = _extractCorePoseFromFramePrompt(shotDict.get("lastFramePrompt") or "")
    soundscape = extractPureEnglishVisual(shotDict.get("soundscapePrompt") or "") or "Realistic dramatic environment and physical movement Foley."

    try:
        dialogues = json.loads(shotDict.get("dialogueJson") or "[]")
    except Exception:
        dialogues = []

    if hasLastFrame and shotDict.get("shotCategory") == "action_fl2va":
        anchorHeader = (
            "Dynamic high-motion cinematic video (FL2VA): "
            f"From 0.0s to {duration:g}s, drive a clear, decisive, full-amplitude physical action from the opening keyframe pose to the final keyframe pose with visible body mechanics and camera momentum."
        )
    else:
        anchorHeader = (
            "Dynamic dramatic performance video (I2V): "
            f"Over {duration:g} seconds, perform expressive physical movement, intense gaze shifts, natural breathing, and accurate lip-sync from the opening frame."
        )

    isLockedFromPrev = int(shotDict.get("lockFromPrevEnd") or 0) == 1
    if isLockedFromPrev:
        firstPoseLine = (
            f"[0.0s Start Action] Seamlessly continuing from the exact physical posture and position established in the opening keyframe without any sudden pop or morphing. Opening context: {firstPose}"
        )
    else:
        firstPoseLine = f"[0.0s Start Action] {firstPose}"

    timelineLines = [
        f"[Camera & Blocking] {framing}, {movement}, cinematic dramatic lighting, pronounced physical motion.",
        firstPoseLine,
        f"[0.0s -> {duration:g}s Continuous Physical Action] {actionArc}",
    ]
    if lastPose:
        timelineLines.append(f"[{duration:g}s Final Payoff Pose] {lastPose}")

    speakerMap = {}
    validDialogueCount = 0
    for line in dialogues[:1]:
        if not isinstance(line, dict) or not str(line.get("text") or "").strip():
            continue
        validDialogueCount += 1
        speaker = str(line.get("speaker") or "Protagonist").strip()
        speakerLabel = extractPureEnglishVisual(line.get("speaker_label") or "") or "The character in frame"
        sid = speakerMap.setdefault(speaker, len(speakerMap) + 1)
        kind = str(line.get("kind") or "onscreen").strip()
        lang = str(line.get("language") or "Chinese").strip()
        delivery = extractPureEnglishVisual(line.get("delivery") or "") or "Intense dramatic voice"
        text = str(line.get("text") or "").strip()

        if kind == "internal":
            timelineLines.append(
                f"Audio (Internal Monologue): Off-screen voiceover of {speakerLabel} (S{sid}), {delivery}: "
                f"<d>[{lang}] {text}</d> The character on screen keeps lips closed with intense micro-expressions."
            )
        elif kind == "offscreen":
            timelineLines.append(
                f"Audio (Off-screen Voice): {speakerLabel} (S{sid}) shouts from off-screen, {delivery}: "
                f"<d>[{lang}] {text}</d> The visible character reacts physically without mouthing the words."
            )
        else:
            timelineLines.append(
                f"Audio (On-screen Lip-Sync): {speakerLabel} (S{sid}) speaks clearly on camera, {delivery}: "
                f"<d>[{lang}] {text}</d> Clear, expressive mouth movement and emotional facial muscles synchronized to speech."
            )

    if validDialogueCount == 0:
        timelineLines.append(
            "Audio (Speech): Strictly silent voice track, ZERO human speech, NO voiceover narration, NO talking. Pure dramatic Foley sound effects only."
        )

    timelineLines.append(f"Audio (Soundscape & Foley): {soundscape}")
    timelineLines.append(
        "Motion & Visual Constraints: Continuous fluid motion with clear action amplitude, single uncut shot, no text or Chinese characters anywhere."
    )

    return anchorHeader + "\n\n" + "\n".join(timelineLines)


def compileWan22VideoPrompt(shotDict):
    """编译适用于 Wan 2.2 14B MoE 高低噪双专家模型的电影级纯英文动态视觉提示词"""
    framing = extractPureEnglishVisual(shotDict.get("cameraFraming") or "Medium shot") or "Medium shot"
    movement = extractPureEnglishVisual(shotDict.get("cameraMovement") or "Dynamic push-in") or "Dynamic push-in"
    firstPose = _extractCorePoseFromFramePrompt(shotDict.get("firstFramePrompt") or "")
    actionArc = extractPureEnglishVisual(shotDict.get("actionArcPrompt") or "")
    lastPose = _extractCorePoseFromFramePrompt(shotDict.get("lastFramePrompt") or "")
    return (
        f"High-motion cinematic drama shot, {framing}, {movement}. "
        f"Opening state: {firstPose}. "
        f"Continuous physical action: {actionArc}. "
        f"Final state: {lastPose}. "
        "Expressive body mechanics, dramatic lighting, consistent facial identity, 8k sharp details, no text, no subtitles."
    )


def rewriteNovelToSceneLedger(novelText, existingAssets, visualStyle, llmConfig=None):
    """
    【核心步骤一】将小说原文改写为“影视化分场剧本 + 场次空间调度与道具账本 (Scene Blocking & State Ledger)”，
    并自动提取/补全角色（区分前世/今生）、场景与核心道具资产。
    返回字典：
    {
      "extractedAssets": [...],
      "sceneLedger": [...],
      "formattedScript": "## S01 ..."
    }
    """
    novelText = str(novelText or "").strip()
    if not novelText:
        raise ValueError("待改写的小说原文不能为空")

    if llmConfig and llmConfig.get("apiKey") and llmConfig.get("baseUrl"):
        try:
            result = _callLlmForSceneLedger(novelText, existingAssets, visualStyle, llmConfig)
            if result and result.get("sceneLedger"):
                result["formattedScript"] = formatSceneLedgerToMarkdown(result["sceneLedger"])
                return result
        except Exception as err:
            print(f"[directorPlanner] 在线 LLM 剧本改写回退至本地工业级规则引擎: {err}")

    result = _deterministicNovelToSceneLedger(novelText, existingAssets, visualStyle)
    result["formattedScript"] = formatSceneLedgerToMarkdown(result["sceneLedger"])
    return result


def formatSceneLedgerToMarkdown(sceneLedger):
    """将结构化的场次空间调度账本渲染为导演可读的标准影视分场剧本 Markdown"""
    blocks = []
    for sc in sceneLedger:
        code = sc.get("sceneCode", "S01")
        title = sc.get("sceneTitle", "未命名场次")
        loc = str(sc.get("locationName", "主场景")).lstrip("@").strip()
        era = sc.get("eraTag", "通用时空")
        timeOfDay = sc.get("timeOfDay", "日/内")
        axis = sc.get("spatialAxisCn") or sc.get("spatialAxis") or ""
        castList = "、".join(f"@{str(c).lstrip('@').strip()}" for c in (sc.get("allowedCharacters") or []))

        lines = [
            f"## {code} | {timeOfDay} · {loc} | 【{era}】{title}",
            f"- **出场人物锁（防串台）**：{castList or '无'}",
            f"- **180° 空间站位与轴线**：{axis}",
        ]
        props = sc.get("propLedger") or []
        if props:
            propStrs = [
                f"`@{str(p.get('propName') or '').lstrip('@').strip()}`（{p.get('initialState')} → {p.get('transitionAction')} → {p.get('finalState')}）"
                for p in props
            ]
            lines.append(f"- **道具状态账本**：{'；'.join(propStrs)}")

        lines.append("")
        for idx, beat in enumerate(sc.get("beats") or [], 1):
            bType = "⚡[动作推进镜·FL2VA]" if beat.get("beatCategory") == "action_fl2va" else "💬[台词博弈镜·I2V]"
            lines.append(
                f"### 节拍 {code}-{idx:02d} {bType} 《{beat.get('title', '')}》 ({beat.get('durationSec', 5)}s · {beat.get('framing', '中景')})"
            )
            lines.append(f"- **首帧起点 (0.0s)**：{beat.get('startPoseCn') or beat.get('startPoseEn')}")
            lines.append(f"- **戏剧动作弧线**：{beat.get('actionCn') or beat.get('actionEn')}")
            lines.append(f"- **尾帧落点 (Payoff)**：{beat.get('endPoseCn') or beat.get('endPoseEn')}")
            for d in beat.get("dialogue") or []:
                kindLabel = "画外内心独白·闭口" if d.get("kind") == "internal" else ("画外喊声" if d.get("kind") == "offscreen" else "画内对口型")
                lines.append(f"  > **{d.get('speaker')}**（{kindLabel} · {d.get('deliveryCn') or d.get('delivery', '自然克制')}）：「{d.get('text')}」")
            lines.append("")
        blocks.append("\n".join(lines))
    return "\n---\n\n".join(blocks)


def _callLlmForSceneLedger(novelText, existingAssets, visualStyle, llmConfig):
    """调用兼容 OpenAI 的大模型执行：小说 → 场次空间调度账本 + 资产提取 + 影视化节拍改写"""
    baseUrl = llmConfig["baseUrl"].rstrip("/")
    url = baseUrl if baseUrl.endswith("/chat/completions") else f"{baseUrl}/chat/completions"

    assetText = "\n".join(
        f"- [{a['assetType']}] @{a['name']} (时代:{a.get('eraTag') or '未标'}): {a.get('visualPrompt', '')[:120]}"
        for a in (existingAssets or [])
    )

    systemPrompt = (
        "你是一位顶级商业爆款短剧总编剧与 AI 视听分镜总导演。请将输入的小说或原剧本改编为【高连贯商业短剧分场剧本 + 场次空间调度账本 (Scene Blocking & State Ledger)】并提取高颜值核心资产。\n"
        "【核心改编总纲】：必须保留原剧本的核心故事走向、人物关系、核心设定与灵魂金句，但绝不能机械照搬小说的零散流水账！你必须根据商业短剧的视听特性与 AI 视频连续性规律，对原剧本进行大刀阔斧的【短剧化调整与重构】：\n"
        "1. 【场景高度收敛·戏剧三一律（保障全集画面高度连贯）】：\n"
        "   - 小说往往频繁切换地点和时间（如：前世车间 → 后堂泥地醒来 → 主炉口吵架 → 料棚墙角捡废坯 → 半夜钻炉底刮水垢 → 次日清晨验收），直接照拍会导致视频背景支离破碎、毫无连贯性！\n"
        "   - 改编时必须将零散碎场【合并浓缩为全集仅 1 个核心高张力主场景】（最多不超过 2 个），将“过夜修炉、去仓库找工具、前世闪回”全部整合重构为【同一主场景内一气呵成的当堂生死对决】！\n"
        "2. 【保留核心走向，重组短剧爽点递进链】：\n"
        "   - 严格保留原著主线脉络（如：灵炉回火暴走危机 → 反派管事问罪逼迫 → 主角凭宗师经验一眼点破『火蓝透绿是水路堵了』 → 反派嘲讽 → 主角当场砸裂废锄亮出半指厚水垢铁证 → 当众扳动风门将蓝绿妖火压回樱红灵焰震慑全场 → 按下印信立十日赌约 → 结尾捻出断口最里层的『人为新垢』锁定幕后黑手悬念）。\n"
        "   - 将原本枯燥隐晦的技术说明或心理活动，全部改写为直观强烈、富有视觉奇观的当场戏剧动作！\n"
        "3. 【角色五官高颜值 + 服饰/季节/环境 100% 严格贴合当前剧本设定（严禁张冠李戴）】：\n"
        "   - 角色的面部五官必须符合 3D 国漫审美（五官立体端正、骨骼匀称、眼神鲜明、皮肤干净清晰），但【角色的服饰材质、款式、厚薄与配饰必须 100% 严格依据当前输入小说原文的时代背景、季节气候与真实身份处境】！\n"
        "   - 严禁照抄任何固定模板服饰！必须仔细阅读当前剧本原文：\n"
        "     * 若原文是寒冬风雪/流放边塞（如明初辽东雪灾中的废藩庶子、老管家）：必须穿符合当朝代与严冬气候的厚实冬装（如原文写『漏棉旧袄、麻鞋』，则必须写为 `worn padded winter cotton robe with frayed seams over coarse winter inner tunic, hemp shoes`；原文写『掉毛羊皮袄』则写为 `weathered sheepskin-lined winter coat`），绝不能在冰天雪地里穿单薄夏衣或修仙工匠服！\n"
        "     * 若原文是古风铸坊烧炉学徒/杂役：必须穿符合底层劳作设定的粗麻交领短褐工衣、挽袖缠麻布护腕、粗布腰带，绝不能穿丝绸华服或镶金玉带！\n"
        "     * 若原文是宗门管事/军官/权贵：按原文对应的品级描写管事袍、边军旧甲或华服。\n"
        "   - 提取 `scene` 资产时，必须 100% 还原当前剧本的真实时代与气候环境（如边塞寒冬雪院、修仙铸坊大殿等），地面与空间布局必须干净合理，严禁写 `scattered with broken...`、`debris`、`rubble`、`tool walls`、`wooden tool racks`！\n"
        "   - 古代/修仙场景下严禁出现 `wrench`、`spanner`、`valve`、`bolt` 等现代机械词汇！严禁让人物手部悬空冒火或手搓火球！\n"
        "4. 【严格锁定 180° 空间对视轴线（杜绝证件照式正脸发呆）】：\n"
        "   - 同一场戏内必须锁死左右站位轴线：主角始终位于画面左侧 3/4 侧身朝向画面右侧（`positioned on screen-left angled 3/4 facing screen-right`），对手/配角始终位于画面右侧 3/4 侧身朝向画面左侧（`positioned on screen-right angled 3/4 facing screen-left`）。\n"
        "   - 在每个节拍的 `startPoseEn` 和 `endPoseEn` 中必须显式写出该站位与视线朝向！\n"
        "5. 【同角色跨镜头动作首尾咬合锁（保障视频无缝接力）】：\n"
        "   - 同一角色在场内第 2、3、4 次出场时，其 `startPoseCn` 与 `startPoseEn` 必须严格承接该角色上一次出场镜头的 `endPoseCn` 与 `endPoseEn`（以 `Continuing directly from his previous ending pose...` 开头），并将 `lockFromPrevEnd` 设为 1，让前后镜头动作如一镜到底般顺滑咬合！\n"
        "6. 【全集单人主导正反打 · 短剧黄金节奏（精选 7~8 个高燃镜头）】：\n"
        "   - 为保障 3D 国漫人物五官颜值精度、口型 100% 准确以及跨镜头真实视频尾帧无缝接力，所有节拍必须严格采用【单人主导构图 (SOLO SHOT)】！每个节拍的 `charactersInShot` 数组严格只填 1 个当前镜头核心角色，绝不在同一镜头里混入两个人！\n"
        "   - 核心高光动作必须全部由当前镜头角色单人一气呵成完成，在 `startPoseEn`、`actionEn`、`endPoseEn` 中只描写当前这 1 个角色的连续动作！\n"
        "   - 每个节拍的 `dialogue` 数组最多只能有 1 句台词（控制在 10~22 字以内，保留原著最提气的灵魂金句）。全集严格精选 7~8 个刀刀见血的高浓度镜头！\n"
        "7. 【高振幅连续物理动作 & 纯英文视觉描述】：\n"
        "   - 每个节拍的 `actionEn` 必须包含饱满具体的物理动作动词与光影变化；所有 `*En` 与 `visualPrompt` 字段必须为 100% 纯英文（严禁夹杂中文或拼音名字，统一用符合该角色当前剧本真实服饰设定的英文短语指代人物）。\n"
        "只输出合法 JSON 对象，格式如下：\n"
        "{\n"
        '  "extractedAssets": [{"assetType":"character|scene|prop", "name":"准确中文名称", "roleTag":"身份/用途", "eraTag":"前世|今生|通用", "visualPrompt":"Pure English detailed visual prompt (strictly no Chinese characters inside visualPrompt)", "voiceProfile":"声线特征"}],\n'
        '  "sceneLedger": [\n'
        "    {\n"
        '      "sceneCode": "S01",\n'
        '      "sceneTitle": "场次标题",\n'
        '      "eraTag": "前世·现代工业 | 今生·修仙宗门",\n'
        '      "locationName": "场景资产名",\n'
        '      "timeOfDay": "日/内",\n'
        '      "allowedCharacters": ["角色准确名称"],\n'
        '      "spatialAxis": "English 180-degree camera axis and character screen-left/screen-right blocking rule",\n'
        '      "spatialAxisCn": "中文180度空间轴线与人物左右站位说明",\n'
        '      "propLedger": [{"propName":"道具名", "initialState":"初始位置/状态", "transitionAction":"交互变化", "finalState":"结束位置/状态"}],\n'
        '      "beats": [\n'
        "        {\n"
        '          "title": "节拍短标题",\n'
        '          "beatCategory": "action_fl2va | dialogue_i2v",\n'
        '          "durationSec": 5,\n'
        '          "continuityMode": "establish | continue_action | same_scene_cut",\n'
        '          "lockFromPrevEnd": 0,\n'
        '          "framing": "Medium shot | Close-up shot | Wide shot",\n'
        '          "movement": "Steady camera | Slow push-in | Tracking shot",\n'
        '          "intentCn": "本节拍让观众看懂什么戏剧变化",\n'
        '          "startPoseCn": "中文0.0s首帧起始画面与姿态",\n'
        '          "actionCn": "中文中间单一连贯动作过程",\n'
        '          "endPoseCn": "中文尾帧动作完成结果画面",\n'
        '          "startPoseEn": "Concrete English opening pose and composition at 0.0s",\n'
        '          "actionEn": "One coherent English physical progression",\n'
        '          "endPoseEn": "Observable English final pose and object state",\n'
        '          "soundscapeEn": "Realistic environment and Foley sound in English",\n'
        '          "propsInShot": ["道具名"],\n'
        '          "charactersInShot": ["画面可见角色名"],\n'
        '          "dialogue": [{"speaker":"角色名", "kind":"onscreen|internal|offscreen", "text":"精炼后的原文台词", "delivery":"Calm low voice", "deliveryCn":"低沉克制"}]\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )

    userPrompt = (
        f"【全局视觉画风】：{visualStyle}\n"
        f"【项目已有资产】：\n{assetText or '无'}\n\n"
        "【硬性篇幅与结构约束】：\n"
        "- 请将整集剧情高度浓缩在 1 个核心主场景（`sceneLedger` 仅含 1 个主场景 `S01`）内一气呵成演完；\n"
        "- 全集严格精选 7~8 个高张力节拍（`beats` 数量严格为 7~8 个），每个英文描述字段简明扼要（20~35 词以内），保留全部主线走向与爽点反转！\n\n"
        f"【待改写小说原文】：\n{novelText}"
    )
    rawModel = str(llmConfig.get("model") or "deepseek-chat").strip()
    effectiveModel = "deepseek-chat" if rawModel == "deepseek-flash" else rawModel
    reqBody = {
        "model": effectiveModel,
        "messages": [
            {"role": "system", "content": systemPrompt},
            {"role": "user", "content": userPrompt},
        ],
        "temperature": 0.25,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
    }
    if "flash" in rawModel or "reasoner" in rawModel:
        reqBody["thinking"] = {"type": "disabled"}
    payload = json.dumps(reqBody).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llmConfig['apiKey']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)


def _deterministicNovelToSceneLedger(novelText, existingAssets, visualStyle):
    """
    本地高精度戏剧结构化改写引擎（无需外网也能将任意小说原文转化为带 180° 轴线、道具账本与首尾帧锚点的工业级分场剧本）
    """
    if "风雪" in novelText and "孤驿" in novelText:
        extractedAssets = [
            {
                "assetType": "character",
                "name": "沈夜",
                "roleTag": "主角·巡边刀客",
                "eraTag": "正篇",
                "visualPrompt": "East Asian young swordsman in dark weathered cloak with snow flakes on shoulders, sharp calm eyes, lean build, holding a bronze-hilted long blade.",
                "voiceProfile": "低沉冷峻的青年嗓音",
            },
            {
                "assetType": "character",
                "name": "掌柜",
                "roleTag": "配角·孤驿老掌柜",
                "eraTag": "正篇",
                "visualPrompt": "Elderly innkeeper in coarse gray winter robe, cautious gaze, holding a brass oil lamp.",
                "voiceProfile": "沙哑谨慎的老者嗓音",
            },
            {
                "assetType": "scene",
                "name": "风雪孤驿大堂",
                "roleTag": "内景·边关木制客栈大堂",
                "eraTag": "正篇",
                "visualPrompt": "Ancient frontier wooden inn hall at night, snowstorm blowing through the door on screen-left, warm charcoal brazier in the center, wooden counter on screen-right, cinematic chiaroscuro lighting.",
                "voiceProfile": "",
            },
            {
                "assetType": "prop",
                "name": "青铜虎符",
                "roleTag": "关键信物道具",
                "eraTag": "正篇",
                "visualPrompt": "Ancient weathered bronze tiger tally engraved with seal script, resting on the wooden table.",
                "voiceProfile": "",
            },
        ]
        sceneLedger = [
            {
                "sceneCode": "S01",
                "sceneTitle": "风雪叩门与虎符亮案",
                "eraTag": "正篇",
                "locationName": "风雪孤驿大堂",
                "timeOfDay": "夜/内",
                "allowedCharacters": ["沈夜", "掌柜"],
                "spatialAxis": "Screen-left to Screen-right Axis: Shen Ye enters from the snow-swept doorway on screen-left facing right; the innkeeper stands behind the wooden counter on screen-right facing left.",
                "spatialAxisCn": "左门右柜轴线：沈夜自画面左侧风雪木门入内面向右；掌柜立于画面右侧木柜后面向左。",
                "propLedger": [
                    {
                        "propName": "青铜虎符",
                        "initialState": "藏于沈夜怀中",
                        "transitionAction": "沈夜取出重重拍在木案上",
                        "finalState": "定格于木案中央",
                    }
                ],
                "beats": [
                    {
                        "title": "推门踏雪入孤驿",
                        "beatCategory": "action_fl2va",
                        "durationSec": 5,
                        "continuityMode": "establish",
                        "lockFromPrevEnd": 0,
                        "framing": "Medium wide shot",
                        "movement": "Steady tracking camera",
                        "intentCn": "建立边关风雪肃杀氛围与主角沈夜登场",
                        "startPoseCn": "沈夜立于画面左侧木门外，伸手推开被风雪拍打的木门",
                        "actionCn": "他顶着风雪迈步踏入大堂，反手将木门合拢，抖落肩头积雪并抬眼看向右侧柜台",
                        "endPoseCn": "沈夜定格在大堂左侧火盆旁，右手按在刀柄上，目光锁定右侧掌柜",
                        "startPoseEn": "Shen Ye stands at the screen-left wooden door, pushing it open against the swirling snowstorm.",
                        "actionEn": "He steps firmly into the wooden hall, closes the door behind him, shakes snow from his cloak, and fixes his gaze on the right counter.",
                        "endPoseEn": "Standing steady on screen-left beside the brazier, right hand resting on his blade hilt, looking right.",
                        "soundscapeEn": "Howling blizzard wind, heavy wooden door creak, boots stepping on wooden floorboards.",
                        "propsInShot": [],
                        "charactersInShot": ["沈夜", "掌柜"],
                        "dialogue": [],
                    },
                    {
                        "title": "亮出虎符问前路",
                        "beatCategory": "action_fl2va",
                        "durationSec": 5,
                        "continuityMode": "continue_action",
                        "lockFromPrevEnd": 1,
                        "framing": "Medium close-up shot",
                        "movement": "Slow push-in to the wooden table",
                        "intentCn": "通过拍出青铜虎符完成戏剧悬念钩子",
                        "startPoseCn": "承接上镜尾帧，沈夜立于画面左侧火盆旁，左手探入怀中",
                        "actionCn": "沈夜取出青铜虎符，上前一步将其重重按在木案上，右侧掌柜举灯凑近神色骤变",
                        "endPoseCn": "青铜虎符定格在木案中央，沈夜手按虎符目光如炬，右侧掌柜惊愕抬头",
                        "startPoseEn": "Continuing from previous frame, Shen Ye stands on screen-left reaching into his cloak.",
                        "actionEn": "He pulls out the bronze tiger tally and slams it firmly onto the wooden table while the innkeeper on screen-right raises the oil lamp in shock.",
                        "endPoseEn": "Shen Ye's hand resting over the bronze tiger tally on the table, the innkeeper staring in disbelief on screen-right.",
                        "soundscapeEn": "Heavy metallic thud on wooden table, crackling brazier fire.",
                        "propsInShot": ["青铜虎符"],
                        "charactersInShot": ["沈夜", "掌柜"],
                        "dialogue": [
                            {
                                "speaker": "沈夜",
                                "speaker_label": "Shen Ye",
                                "kind": "onscreen",
                                "language": "Chinese",
                                "text": "前路封山，这枚虎符够不够换两匹快马？",
                                "delivery": "Calm, low and commanding voice",
                                "deliveryCn": "低沉冷峻",
                            }
                        ],
                    },
                ],
            }
        ]
        return {"extractedAssets": extractedAssets, "sceneLedger": sceneLedger}

    # 通用小说原文结构化分场改写引擎
    paragraphs = [p.strip() for p in re.split(r"\n+", novelText) if p.strip()]
    charAssets = [a for a in (existingAssets or []) if a["assetType"] == "character"]
    sceneAssets = [a for a in (existingAssets or []) if a["assetType"] == "scene"]
    leadName = charAssets[0]["name"] if charAssets else "主角"
    leadDesc = (charAssets[0].get("visualPrompt") or "East Asian protagonist in consistent signature attire")[:140] if charAssets else "East Asian protagonist"
    locName = sceneAssets[0]["name"] if sceneAssets else "核心主场景"
    locDesc = (sceneAssets[0].get("visualPrompt") or "Cinematic atmospheric interior room")[:140] if sceneAssets else "Cinematic scene"

    beats = []
    for idx, para in enumerate(paragraphs[:16]):
        quotes = re.findall(r"[“「]([^”」]+)[”」]", para)
        actionPart = re.sub(r"[“「][^”」]+[”」]", "", para).strip(" ，。；：")
        hasDialogue = len(quotes) > 0
        beatCat = "dialogue_i2v" if (hasDialogue and len(actionPart) < 25) else "action_fl2va"
        contMode = "establish" if idx == 0 else ("continue_action" if beatCat == "action_fl2va" and idx % 2 == 1 else "same_scene_cut")
        dialogues = []
        for q in quotes[:2]:
            isInternal = any(k in para for k in ["心想", "暗道", "内心", "脑海"])
            dialogues.append({
                "speaker": leadName,
                "speaker_label": leadName,
                "kind": "internal" if isInternal else "onscreen",
                "language": "Chinese",
                "text": q[:28],
                "delivery": "Natural dramatic voice",
                "deliveryCn": "自然戏剧张力",
            })

        beats.append({
            "title": (actionPart or para)[:12] or f"节拍 #{idx+1}",
            "beatCategory": beatCat,
            "durationSec": 5 if not hasDialogue else min(8, max(4, round(sum(len(q) for q in quotes) / 3.5 + 1.5, 1))),
            "continuityMode": contMode,
            "lockFromPrevEnd": 1 if contMode == "continue_action" else 0,
            "framing": "Close-up shot" if beatCat == "dialogue_i2v" else "Medium shot",
            "movement": "Subtle push-in" if beatCat == "dialogue_i2v" else "Steady tracking camera",
            "intentCn": para[:60],
            "startPoseCn": f"{leadName}处于动作起始位：{actionPart[:30] or para[:30]}",
            "actionCn": actionPart or f"{leadName}眼神微凛完成台词表达",
            "endPoseCn": f"{leadName}完成动作并定格在最终姿态",
            "startPoseEn": f"{leadName} in opening pose at screen-left: {actionPart[:60] or para[:60]}.",
            "actionEn": f"{leadName} executes coherent action: {actionPart[:80] or 'delivers dialogue with subtle weight shift'}.",
            "endPoseEn": f"{leadName} holds observable final pose after completing the beat.",
            "soundscapeEn": "Realistic room tone and physical cloth movement Foley.",
            "propsInShot": [],
            "charactersInShot": [leadName],
            "dialogue": dialogues,
        })

    sceneLedger = [
        {
            "sceneCode": "S01",
            "sceneTitle": "第一场·核心冲突场",
            "eraTag": "今生",
            "locationName": locName,
            "timeOfDay": "日/内",
            "allowedCharacters": [leadName],
            "spatialAxis": f"Screen-left to Screen-right Axis: {leadName} occupies screen-left facing right inside {locName} ({locDesc}).",
            "spatialAxisCn": f"左主右客轴线：{leadName}位于画面左侧面向右，严禁镜头间左右翻转越轴。",
            "propLedger": [],
            "beats": beats,
        }
    ]
    return {"extractedAssets": [], "sceneLedger": sceneLedger}


def extractPureEnglishVisual(rawText, eraTag=""):
    """
    提取并净化纯英文视觉描述（供 Flux3-Klein 生图模型使用）：
    1. 彻底剥离所有中文字符、`中文：...` 段落、`[S01...]` 场次标签与 `@角色名`，杜绝 Qwen3-4B 编码器将汉字当成招牌/安全帽文字画进画面；
    2. 自动校准年龄与骨骼体态描述（消除 boy/young 导致的 8 岁幼童化漂移）；
    3. 自动为古风/修仙时期注入古代器物与布鞋约束（消除现代橡胶靴与现代铸铁手轮穿帮）。
    """
    text = str(rawText or "").strip()
    if not text:
        return ""

    # 1. 若存在 "中文：" 或 "中文:" 分隔，优先截取其前面的纯英文部分
    for sep in ("中文：", "中文:", "Chinese:", "【中文"):
        if sep in text:
            text = text.split(sep, 1)[0].strip()

    text = re.sub(r"^\s*English\s*:\s*", "", text, flags=re.IGNORECASE)
    # 剥离 [S01 ...] 等场次标签与全角括号内容
    text = re.sub(r"\[S\d+[^\]]*\]", "", text)
    text = re.sub(r"（[^）]*）", "", text)
    text = re.sub(r"【[^】]*】", "", text)
    text = re.sub(r"\([^)]*[\u4e00-\u9fff]+[^)]*\)", "", text)
    # 移除残留汉字与全角标点
    text = re.sub(r"[\u4e00-\u9fff，。；：！？、“”‘’《》]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")

    # 2. 防幼童化身高骨骼校准
    text = re.sub(
        r"\bhalf-grown\s+(?:Chinese\s+)?boy(?:\s+apprentice)?\b",
        "16-year-old Chinese teenage youth apprentice (170cm tall, slender teen build, NOT a child)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bthin\s+pale\s+young\s+(?:Chinese\s+)?furnace\s+worker\b",
        "19-year-old adult Chinese young man furnace worker (178cm tall, lean masculine build, sharp jawline, NOT a child)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\byoung\s+Chinese\s+apprentice\b",
        "20-year-old adult Chinese young man apprentice (176cm tall, adult build)",
        text,
        flags=re.IGNORECASE,
    )

    # 3. 古风/修仙时代防现代工业五金工具与满地碎垃圾穿帮校准
    isAncient = any(k in str(eraTag) for k in ("今生", "修仙", "古风", "仙侠", "正篇")) or not eraTag
    if isAncient:
        text = re.sub(r"\b(?:heavy\s+iron\s+)?wind-gate\s+wrench\b", "ancient bronze dragon-head sliding lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(?:iron\s+|heavy\s+)?(?:wrench|spanner)\b", "ancient bronze dragon-head lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\bair[- ]?(?:gate|valve)\s+(?:wheel|lever)\b", "ancient bronze dragon-head sliding lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\bvalve(?:\s+wheel)?\b", "ancient bronze damper lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\bglowing\s+runes\b", "subtle engraved ancient patterns without text", text, flags=re.IGNORECASE)
        text = re.sub(r"\bscattered\s+(?:with\s+)?broken\s+[^,.]+", "clean swept dark stone floor", text, flags=re.IGNORECASE)
        text = re.sub(r",?\s*\bwooden\s+(?:tool\s+)?racks(?:\s+and\s+tool\s+walls)?\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r",?\s*\btool\s+walls\b", "", text, flags=re.IGNORECASE)

    # 4. 修正中式直译词与导致人脸显老/粗糙/歪嘴的负面词
    text = re.sub(r"\bface\s+darken(?:ing|s|ed)?\b", "expression turning grim and solemn", text, flags=re.IGNORECASE)
    text = re.sub(r"\blive-action\s+realism\b", "exquisite 3D Guoman animation beauty aesthetic", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsharp\s+masculine\s+jawline\b", "refined handsome V-shaped facial contour", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcruel\s+sneer(?:ing)?(?:\s+expression)?\b", "cold haughty noble expression", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsharp\s+sinister\s+eyes\b", "sharp phoenix eyes", text, flags=re.IGNORECASE)

    return text.strip()


def breakScriptIntoKeyframedShots(scriptText, projectAssets, visualStyle, llmConfig=None, sceneLedger=None):
    """
    【核心步骤二】将“场次空间调度账本 (sceneLedger)”或剧本拆解为“首帧 + 动作弧线 + 尾帧”结构化分镜列表。
    - 严格按本镜实际出场角色 (charactersInShot) 绑定资产，单人特写绝不塞入旁人或全场名单；
    - 生成纯净无中文干扰的英文生图提示词，杜绝画面出现汉字标牌乱码。
    """
    assetMap = {str(a["name"]).lstrip("@").strip(): a for a in (projectAssets or [])}

    if not sceneLedger:
        rewritten = rewriteNovelToSceneLedger(scriptText, projectAssets, visualStyle, llmConfig)
        sceneLedger = rewritten.get("sceneLedger") or []

    cleanStyleEn = extractPureEnglishVisual(visualStyle) or "High-end Chinese 3D CG and live-action cinematic realism"
    compiledShots = []
    shotNum = 1
    for scene in sceneLedger:
        sceneCode = scene.get("sceneCode", "S01")
        eraTag = scene.get("eraTag", "")
        locName = str(scene.get("locationName", "主场景")).lstrip("@").strip()
        locAsset = assetMap.get(locName)
        locDescEn = extractPureEnglishVisual(locAsset.get("visualPrompt") if locAsset else "", eraTag) or "Cinematic atmospheric interior environment"
        axisEn = extractPureEnglishVisual(scene.get("spatialAxis") or "", eraTag)
        allowedChars = [str(c).lstrip("@").strip() for c in (scene.get("allowedCharacters") or []) if str(c).strip()]

        rawBeats = scene.get("beats") or []
        expandedBeats = []
        for rawBeat in rawBeats:
            dlgList = [d for d in (rawBeat.get("dialogue") or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
            # 若单个节拍包含多句对话（如两人交锋或多轮问答），自动按正反打法则切分为“一镜一人一句台词”
            if len(dlgList) > 1:
                for dIdx, dItem in enumerate(dlgList):
                    subBeat = dict(rawBeat)
                    spk = str(dItem.get("speaker") or "").lstrip("@").strip()
                    subBeat["title"] = f"{rawBeat.get('title', '交锋')}·{spk.split('（')[0] or f'正反打{dIdx+1}'}"
                    subBeat["dialogue"] = [dItem]
                    subBeat["durationSec"] = max(4.0, min(6.0, round(len(str(dItem.get("text") or "")) / 3.2 + 1.8, 1)))
                    if spk:
                        subBeat["charactersInShot"] = [spk]
                    subBeat["framing"] = "Medium close-up shot" if dIdx % 2 == 0 else "Close-up shot"
                    subBeat["movement"] = "Dynamic push-in"
                    if dIdx > 0:
                        subBeat["startPoseCn"] = f"镜头切至{spk}正反打近景，目光锐利直视对方，开口反击"
                        subBeat["actionCn"] = f"{spk}情绪饱满说出核心台词，面部肌肉与手势沉稳有力"
                        subBeat["endPoseCn"] = f"{spk}话音落定，眼神冷冽定格"
                        subBeat["startPoseEn"] = f"{spk} in a dramatic medium close-up reverse-angle shot, locking intense gaze forward."
                        subBeat["actionEn"] = f"{spk} speaks with decisive emotional weight, leaning slightly forward with expressive facial movement."
                        subBeat["endPoseEn"] = f"{spk} finishes speaking and holds a piercing, unyielding gaze."
                    expandedBeats.append(subBeat)
            else:
                expandedBeats.append(rawBeat)

        seenSceneCharKeys = set()
        for beat in expandedBeats:
            # 严格采用单人主导正反打（SOLO FOCUS）：若有台词则锁定说话人为唯一入镜角色，否则取首位主导角色
            rawCharsInBeat = [str(c).lstrip("@").strip() for c in (beat.get("charactersInShot") or []) if str(c).strip()]
            beatDlgs = [d for d in (beat.get("dialogue") or []) if isinstance(d, dict) and str(d.get("speaker") or "").strip()]
            if beatDlgs:
                spkClean = str(beatDlgs[0].get("speaker") or "").lstrip("@").strip()
                charsInShot = [spkClean] if spkClean else (rawCharsInBeat[:1] or allowedChars[:1])
            else:
                charsInShot = rawCharsInBeat[:1] if rawCharsInBeat else (allowedChars[:1] if allowedChars else [])
            charKey = tuple(charsInShot)
            isRepeatCharInScene = bool(charKey and charKey in seenSceneCharKeys)
            if charKey:
                seenSceneCharKeys.add(charKey)
            propsInShot = [str(p).lstrip("@").strip() for p in (beat.get("propsInShot") or []) if str(p).strip()]

            refs = []
            charPromptParts = []
            for idxC, cClean in enumerate(charsInShot):
                cAsset = assetMap.get(cClean)
                cDescEn = extractPureEnglishVisual(cAsset.get("visualPrompt") if cAsset else "", eraTag) or "Adult East Asian character in signature attire"
                refs.append({"asset": f"@{cClean}", "role": "character", "description": cDescEn})
                charPromptParts.append(f"Character {idxC + 1} ({cDescEn})")

            startPoseEn = extractPureEnglishVisual(beat.get("startPoseEn") or "", eraTag) or "Character in opening pose."
            actionEn = extractPureEnglishVisual(beat.get("actionEn") or "", eraTag) or "Executes planned dramatic action."
            endPoseEn = extractPureEnglishVisual(beat.get("endPoseEn") or "", eraTag) or "Character completes action in final pose."

            # 若本镜动作并未涉及启动按钮，从局部环境描述中剥离 red start button，防止拧螺栓特写墙面上凭空画出红按钮
            shotLocDescEn = locDescEn
            combinedBeatEn = f"{startPoseEn} {actionEn} {endPoseEn}".lower()
            if "button" not in combinedBeatEn:
                shotLocDescEn = re.sub(r",?\s*\bred\s+start\s+button\b", "", shotLocDescEn, flags=re.IGNORECASE).strip(" ,")

            refs.append({"asset": f"@{locName}", "role": "scene", "description": shotLocDescEn})

            propPromptParts = []
            for pClean in propsInShot[:2]:
                pAsset = assetMap.get(pClean)
                if pAsset:
                    pDescEn = extractPureEnglishVisual(pAsset.get("visualPrompt") or "", eraTag)
                    refs.append({"asset": f"@{pClean}", "role": "prop", "description": pDescEn})
                    if pDescEn:
                        propPromptParts.append(pDescEn)

            framing = beat.get("framing") or "Medium shot"
            movement = beat.get("movement") or "Steady camera"

            if len(charsInShot) == 1:
                castCountRule = "SOLO SHOT (strictly ONLY 1 person visible in frame, no other people in background)."
            elif len(charsInShot) == 2:
                castCountRule = f"TWO-PERSON SHOT (exactly 2 people visible in frame). {axisEn}"
            else:
                castCountRule = f"GROUP SHOT. {axisEn}"

            eraRealismRule = (
                "Ancient Chinese xianxia period setting, traditional coarse cloth robes and cloth shoes, ancient stone and bronze craftsmanship (no modern boots, no modern industrial valves)."
                if any(k in str(eraTag) for k in ("今生", "修仙", "古风", "仙侠"))
                else "Modern industrial realistic setting."
            )

            baseHeader = (
                f"Style: {cleanStyleEn}. Camera: {framing}. {castCountRule} "
                f"Environment: {shotLocDescEn}. {eraRealismRule} "
                f"Visible Cast: {'; '.join(charPromptParts)}. "
            )
            if propPromptParts:
                baseHeader += f"Key Props: {'; '.join(propPromptParts)}. "

            noTextSuffix = (
                "Pure photographic film frame, unified lighting, realistic adult human proportions, "
                "strictly NO text, NO Chinese characters, NO signboards, NO nameplates, NO helmet labels, NO watermarks."
            )

            firstFramePrompt = f"{baseHeader}OPENING POSE (0.0s): {startPoseEn}. {noTextSuffix}"
            lastFramePrompt = (
                f"{baseHeader}ENDING POSE (Keep exact same camera angle, character standing/sitting posture, face, clothing, and background layout as opening frame; only update the action result): "
                f"{endPoseEn}. {noTextSuffix}"
            )

            autoLock = 1 if (isRepeatCharInScene or int(beat.get("lockFromPrevEnd") or 0) == 1 or beat.get("continuityMode") == "continue_action") else 0
            compiledShots.append({
                "shotNumber": shotNum,
                "title": f"[{sceneCode}] {beat.get('title') or f'镜头 #{shotNum}'}",
                "shotCategory": beat.get("beatCategory") or "action_fl2va",
                "durationSec": float(beat.get("durationSec") or 5.0),
                "continuityMode": "continue_action" if autoLock else (beat.get("continuityMode") or ("establish" if shotNum == 1 else "same_scene_cut")),
                "lockFromPrevEnd": autoLock,
                "axisRule": f"[{sceneCode} 轴线] {scene.get('spatialAxisCn') or axisEn}",
                "intentText": f"【{sceneCode}·{eraTag}】{beat.get('intentCn') or ''}",
                "cameraFraming": framing,
                "cameraMovement": movement,
                "firstFramePrompt": firstFramePrompt,
                "actionArcPrompt": f"{actionEn} ({beat.get('actionCn', '')})",
                "lastFramePrompt": lastFramePrompt,
                "soundscapePrompt": beat.get("soundscapeEn") or "Realistic room tone and movement Foley.",
                "dialogue": beat.get("dialogue") or [],
                "references": refs,
                "checks": [
                    f"时空防串台检查：仅限【{eraTag}】角色与道具入镜",
                    f"本镜出场人数检查：仅限 {len(charsInShot)} 人 ({'、'.join(charsInShot)})",
                    f"动作落点检查：{beat.get('endPoseCn') or endPoseEn}",
                ],
            })
            shotNum += 1

    return compiledShots


def compileAssetSheetPrompt(assetRow, visualStyle="High-end Chinese 3D CG & live-action realism"):
    """
    为角色、场景、道具生成标准定妆照 / 设定图的纯英文提示词（彻底剥离中文名与标签，杜绝汉字招牌乱码）：
    - 角色：半身定妆照，清晰面部五官、固定发型、服装材质与配饰细节；
    - 场景：电影级空镜环境设定图（无人干扰），固定建筑布局与主光源；
    - 道具：高精度居中特写设定图。
    """
    aType = assetRow.get("assetType") or "character"
    era = assetRow.get("eraTag") or ""
    cleanStyle = extractPureEnglishVisual(visualStyle, era) or "High-end Chinese 3D CG and live-action cinematic realism"
    cleanDesc = extractPureEnglishVisual(assetRow.get("visualPrompt") or "", era) or "Detailed cinematic subject"

    # 古代/历史/仙侠题材服饰与发型防现代穿越清洗
    isAncient = not any(k in (visualStyle + " " + era).lower() for k in ["modern", "cyberpunk", "sci-fi", "都市", "现代", "科幻"])
    ancientAttireGuard = ""
    if isAncient:
        cleanDesc = re.sub(r"\b(?:old )?jacket with leather straps\b", "authentic historical Ming dynasty crimson quilted war coat (Yuanyang zhan-ao) with leather waist belt", cleanDesc, flags=re.IGNORECASE)
        cleanDesc = re.sub(r"\bleather straps\b", "broad dark leather waist sash", cleanDesc, flags=re.IGNORECASE)
        cleanDesc = re.sub(r"\b(?:padded winter )?(?:work )?jacket\b", "cross-collar padded winter cotton robe", cleanDesc, flags=re.IGNORECASE)
        cleanDesc = re.sub(r"\bshort (?:grey-black |black )?hair\b", "traditional hair bun tied with a dark cloth kerchief and wooden hairpin", cleanDesc, flags=re.IGNORECASE)
        ancientAttireGuard = (
            "Authentic ancient Chinese historical clothing (traditional cross-collar Hanfu robes or Ming dynasty quilted war coats). "
            "Strictly NO modern clothing, NO modern lapel collars, NO front metal buttons, NO zippers, "
            "strictly NO modern leather suspenders, NO tactical harness straps, NO modern cargo gear, "
            "strictly NO modern short crew cut or buzzcut haircuts, traditional topknot hair bun only."
        )

    noTextRule = (
        "Strictly NO text, NO Chinese characters, NO kanji, NO letters, NO helmet labels, "
        "NO nameplates, NO signboards, NO watermarks anywhere in the image."
    )

    if aType == "character":
        guardPart = f" {ancientAttireGuard}" if ancientAttireGuard else ""
        return (
            f"Masterpiece 3D Chinese CG character reference portrait (top-tier Guoman aesthetic), {cleanStyle}. "
            f"Solo waist-up portrait of 1 person with costume strictly matching historical identity and setting: {cleanDesc}. "
            "Hands empty at sides (strictly NO weapons or props blocking the chest or face), natural calm expression, "
            "refined East Asian facial proportions, natural realistic skin texture, "
            "expressive eyes, clean neutral dark blurred background, flattering soft lighting. "
            f"Strictly NO distorted grimace.{guardPart} {noTextRule}"
        )
    if aType == "scene":
        return (
            f"Masterpiece 3D Chinese CG environment concept art, empty establishing wide shot with NO people, {cleanStyle}. "
            f"Environment details strictly matching script setting: {cleanDesc}. "
            "Clear spatial depth, consistent light source direction, atmospheric volumetric lighting, 8k resolution. "
            f"Strictly NO modern open-end wrenches, NO modern industrial tools. {noTextRule}"
        )
    return (
        f"Hero prop studio close-up reference, {cleanStyle}. "
        f"Object details: {cleanDesc}. "
        f"Centered macro product shot on dark neutral background, intricate craftsmanship, clear material texture. {noTextRule}"
    )


def compileLockedKeyframePrompt(shotRow, assets, visualStyle, frameRole="first"):
    """
    在生成分镜首帧 / 尾帧关键帧时：
    1. 严格只读取本镜明确绑定 (references) 的出场角色、场景与核心道具（绝不把同场未出场角色扫进来）；
    2. 强制转换为 100% 纯英文视觉描述（彻底剥离中文姓名、场次号等元标签，防止画面出现汉字招牌乱码）；
    3. 注入成年身高体态锁与古风服化道防穿帮锁。
    """
    assetMap = {str(a.get("name", "")).lstrip("@").strip(): a for a in (assets or [])}
    rawRefs = shotRow.get("references")
    if rawRefs is None:
        try:
            rawRefs = json.loads(shotRow.get("referenceAssetsJson") or "[]")
        except Exception:
            rawRefs = []

    rawPrompt = (
        shotRow.get("lastFramePrompt")
        if frameRole == "last"
        else shotRow.get("firstFramePrompt")
    ) or ""

    matchedChars = []
    matchedScenes = []
    matchedProps = []
    seenNames = set()

    for r in rawRefs:
        if not isinstance(r, dict):
            continue
        aName = str(r.get("asset", "")).lstrip("@").strip()
        if not aName or aName in seenNames:
            continue
        seenNames.add(aName)
        aObj = assetMap.get(aName)
        if aObj:
            if aObj.get("assetType") == "character":
                matchedChars.append(aObj)
            elif aObj.get("assetType") == "scene":
                matchedScenes.append(aObj)
            else:
                matchedProps.append(aObj)

    eraTag = ""
    if matchedChars:
        eraTag = matchedChars[0].get("eraTag") or ""
    elif matchedScenes:
        eraTag = matchedScenes[0].get("eraTag") or ""

    cleanStyle = extractPureEnglishVisual(visualStyle, eraTag) or "High-end Chinese 3D CG and live-action cinematic realism"
    framing = extractPureEnglishVisual(shotRow.get("cameraFraming") or "Medium shot", eraTag)

    # 提取本镜核心动作姿态句子（若原提示词中含有 OPENING POSE / ENDING POSE，只截取动作姿态核心句，避免重复堆砌旧的全员名单）
    poseText = rawPrompt
    for marker in ("OPENING KEYFRAME POSE (0.0s):", "OPENING POSE (0.0s):", "ENDING KEYFRAME POSE (Completed Payoff):", "ENDING POSE"):
        if marker in poseText:
            poseText = poseText.split(marker, 1)[1]
            break
    poseCleanEn = extractPureEnglishVisual(poseText, eraTag)

    # 将动作句中的拼音角色名或旧版不符设定的华服描述替换为符合当前身份设定的粗布工衣/管事袍/破棉袄英文指代
    nameAliasMap = {
        r"\bLu Dachuan in the past life\b": "the handsome young furnace apprentice in coarse ash-brown linen work tunic with hemp wrist wraps",
        r"\bLu Dachuan in this life\b": "the 19-year-old handsome East Asian young furnace apprentice in coarse ash-brown linen work tunic with hemp wrist wraps",
        r"\bLu Dachuan\b": "the 19-year-old handsome East Asian young furnace apprentice in coarse ash-brown linen work tunic with hemp wrist wraps",
        r"\bthe handsome young craftsman in dark-cyan tunic\b": "the resolute handsome East Asian young scholar-craftsman in faded coarse patched winter coat",
        r"\bclean tailored dark-cyan ancient craftsman tunic with leather bracers\b": "humble coarse patched cross-collar winter work coat with frayed hem, hemp cloth wrist wraps, and a simple cloth waist sash",
        r"\bCao Pangzi\b": "the 40-year-old rotund male sect forge overseer in dark espresso-brown overseer robe",
        r"\bSteward Cao\b": "the 40-year-old rotund male sect forge overseer in dark espresso-brown overseer robe",
        r"\bCao\b": "the 40-year-old rotund male sect forge overseer in dark espresso-brown overseer robe",
        r"\bthe plump steward in purple-gold brocade robe\b": "the 40-year-old rotund male sect forge overseer in dark espresso-brown overseer robe",
        r"\bluxurious dark-purple and gold(?: embroidered)? brocade robe\b": "dark espresso-brown silk-cotton sect overseer robe with subtle bronze cloud trim",
        r"\bXiao Shitou\b": "the 16-year-old clean-cut teenage boy apprentice in patched light-gray coarse cotton short jacket",
        r"\bZhu Chen\b": "the 22-year-old resolute handsome East Asian young scholar in humble patched dark-teal winter coat with cloth belt",
        r"\bWang Shuan\b": "the 60-year-old weathered loyal old servant in worn threadbare sheepskin vest over faded coarse robe",
        r"\bZhang Desheng\b": "the 45-year-old distressed grain steward in drab padded winter jacket, face flushed purple from freezing cold",
        r"\bLi Qianhu messenger\b": "the 50-year-old hardened veteran soldier messenger in dark-red military tunic with snow dusting shoulders",
        r"\bflawless smooth fair porcelain skin\b": "natural realistic adult skin with subtle cold-weather pale complexion, masculine resolute features, strictly NO heavy makeup",
        r"\bstanding upright with calm confident posture\b": "",
        r"\bhands empty at sides\b": "",
    }
    for pat, repl in nameAliasMap.items():
        poseCleanEn = re.sub(pat, repl, poseCleanEn, flags=re.IGNORECASE)

    # 视线与 180° 机位防穿帮检测：若分镜设定了侧身对视，强制注入“严禁直视镜头/影楼摆拍”约束
    gazeLock = ""
    lowerPose = poseCleanEn.lower()
    if "facing screen-right" in lowerPose or "toward screen-right" in lowerPose or "screen-left" in lowerPose:
        gazeLock = (
            "Cinematic 3/4 angle side profile, character gaze directed strictly toward screen-right at their scene partner off-camera. "
            "Eyes focused across the room, strictly NO direct eye contact with the camera, NOT looking at the viewer, NO photostudio model posing."
        )
    elif "facing screen-left" in lowerPose or "toward screen-left" in lowerPose or "screen-right" in lowerPose:
        gazeLock = (
            "Cinematic 3/4 angle side profile, character gaze directed strictly toward screen-left at their scene partner off-camera. "
            "Eyes focused across the room, strictly NO direct eye contact with the camera, NOT looking at the viewer, NO photostudio model posing."
        )

    dnaBlocks = [f"Cinematic film frame, {cleanStyle}, {framing}."]

    def _sanitizeCharacterPrompt(promptStr):
        """清洗角色资产中的立绘站姿与油腻爱豆词，保证分镜动作具有最高优先级"""
        p = extractPureEnglishVisual(promptStr or "", eraTag)
        p = re.sub(r",?\s*\bstanding upright with calm confident posture\b", "", p, flags=re.IGNORECASE)
        p = re.sub(r",?\s*\bhands empty at sides\b", "", p, flags=re.IGNORECASE)
        p = re.sub(r"\bflawless smooth fair porcelain skin\b", "natural realistic adult skin, authentic masculine bone structure, subtle cold-weather complexion, strictly NO effeminate idol makeup", p, flags=re.IGNORECASE)
        p = re.sub(r"\bclean tailored dark-cyan ancient craftsman tunic with leather bracers\b", "faded coarse patched cross-collar winter work coat with frayed hem, hemp wrist wraps", p, flags=re.IGNORECASE)
        return p.strip(" ,")

    if len(matchedChars) == 1:
        c = matchedChars[0]
        cDesc = _sanitizeCharacterPrompt(c.get("visualPrompt"))
        dnaBlocks.append(f"SOLO SHOT of ONLY 1 person in frame (strictly no other people visible): {cDesc}.")
    elif len(matchedChars) >= 2:
        chList = []
        for idx, c in enumerate(matchedChars[:3]):
            cDesc = _sanitizeCharacterPrompt(c.get("visualPrompt"))
            posHint = "on screen-left" if idx == 0 else ("on screen-right" if idx == 1 else "in background")
            chList.append(f"Person {idx + 1} ({posHint}): {cDesc}")
        dnaBlocks.append(f"Exactly {len(matchedChars[:3])} distinct people in frame: {'; '.join(chList)}.")

    if matchedScenes:
        sDesc = extractPureEnglishVisual(matchedScenes[0].get("visualPrompt") or "", eraTag)
        if "button" not in poseCleanEn.lower():
            sDesc = re.sub(r",?\s*\bred\s+start\s+button\b", "", sDesc, flags=re.IGNORECASE).strip(" ,")
        dnaBlocks.append(f"Environment: {sDesc}.")

    if matchedProps:
        pDescs = [extractPureEnglishVisual(p.get("visualPrompt") or "", eraTag) for p in matchedProps[:2]]
        pDescs = [d for d in pDescs if d]
        if pDescs:
            dnaBlocks.append(f"Active Props: {'; '.join(pDescs)}.")

    if frameRole == "last":
        dnaBlocks.append(
            f"Ending Action Climax Pose (distinct completed action pose at end of shot, same character identity and environment): {poseCleanEn}."
        )
    else:
        dnaBlocks.append(f"Action & Composition: {poseCleanEn}.")

    if gazeLock:
        dnaBlocks.append(gazeLock)

    # 动态构建高级工业防穿帮与画质增强底护（杜绝影楼摆拍、杜绝直视镜头、杜绝文字乱码）
    dnaBlocks.append(
        "Cinematic narrative realism, authentic textured fabrics with realistic wrinkles and wear, subtle breath vapor in cold air, natural volumetric lighting, "
        "strictly candid cinematic still, strictly NO direct camera stare, NO smiling at lens, NO glamour beauty filter, NO effeminate fashion model posing, "
        "strictly NO modern tools, NO open-end wrenches or spanners, NO glowing hands, "
        "strictly NO text, NO Chinese characters, NO signboards, NO nameplates, NO helmet labels, NO watermarks."
    )
    return " ".join(dnaBlocks)
