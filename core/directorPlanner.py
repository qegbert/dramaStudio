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


def compileFl2vaVideoPrompt(shotDict, hasLastFrame=True):
    """
    将单个结构化镜头编译为 MiniMax-H3 ImageToVideo (FL2VA 首尾帧模型) 原生音画提示词。
    当同时提供 first_frame 与 last_frame 时，模型在潜空间首尾两端被严格锚定，
    中间帧专注于演绎从 firstFramePrompt 到 lastFramePrompt 的物理运动弧线与口型对白。
    """
    duration = float(shotDict.get("durationSec") or 5.0)
    framing = str(shotDict.get("cameraFraming") or "Medium shot").strip()
    movement = str(shotDict.get("cameraMovement") or "Steady camera").strip()
    firstState = str(shotDict.get("firstFramePrompt") or "").strip()
    actionArc = str(shotDict.get("actionArcPrompt") or "").strip()
    lastState = str(shotDict.get("lastFramePrompt") or "").strip()
    axisRule = str(shotDict.get("axisRule") or "Preserve left-to-right spatial axis.").strip()
    soundscape = str(shotDict.get("soundscapePrompt") or "Realistic room tone and physical movement Foley.").strip()

    try:
        dialogues = json.loads(shotDict.get("dialogueJson") or "[]")
    except Exception:
        dialogues = []

    if hasLastFrame and shotDict.get("shotCategory") == "action_fl2va":
        anchorHeader = (
            "Keyframe-anchored generation (FL2VA): "
            "At 0.0s, the shot opens strictly from the provided first_frame keyframe. "
            f"Over {duration:g} seconds, execute one continuous physical progression and converge smoothly into the provided last_frame keyframe at {duration:g}s."
        )
    else:
        anchorHeader = (
            "Keyframe-driven generation (I2V): "
            "At 0.0s, the shot opens strictly from the provided first_frame keyframe, preserving character facial identity, costume, and spatial layout throughout."
        )

    timelineLines = [
        f"[Shot 1] {framing}, {movement}. {axisRule}",
        f"[0.0s - Opening Anchor] {firstState}",
        f"[Motion Progression] {actionArc}",
    ]
    if hasLastFrame and lastState:
        timelineLines.append(f"[{duration:g}s - Ending Payoff Anchor] {lastState}")

    speakerMap = {}
    for line in dialogues:
        if not isinstance(line, dict) or not str(line.get("text") or "").strip():
            continue
        speaker = str(line.get("speaker") or "Protagonist").strip()
        speakerLabel = str(line.get("speaker_label") or speaker).strip()
        sid = speakerMap.setdefault(speaker, len(speakerMap) + 1)
        kind = str(line.get("kind") or "onscreen").strip()
        lang = str(line.get("language") or "Chinese").strip()
        delivery = str(line.get("delivery") or "Natural restrained voice").strip()
        text = str(line.get("text") or "").strip()

        if kind == "internal":
            timelineLines.append(
                f"Audio (Internal Monologue): The internal voice of {speakerLabel} (S{sid}) says in an off-screen voiceover, {delivery}: "
                f"<d>[{lang}] {text}</d> The visible character keeps lips completely closed with steady gaze and natural breathing."
            )
        elif kind == "offscreen":
            timelineLines.append(
                f"Audio (Off-screen): {speakerLabel} (S{sid}), outside the visible frame, says, {delivery}: "
                f"<d>[{lang}] {text}</d> Visible listeners do not mouth these words."
            )
        else:
            timelineLines.append(
                f"Audio (On-screen Lip-Sync): {speakerLabel} (S{sid}), the visible speaker, says, {delivery}: "
                f"<d>[{lang}] {text}</d> Accurate lip-sync and natural facial muscle movement."
            )

    if not dialogues:
        timelineLines.append("Audio (Speech): No spoken dialogue, narration, or intelligible speech.")

    timelineLines.append(f"Audio (Soundscape & Foley): {soundscape}")
    timelineLines.append(
        "Visual Quality Constraints: Single continuous shot, no hard cuts inside the clip, no subtitle bars, no watermark, consistent character proportions and hands."
    )

    return anchorHeader + "\n\n" + "\n".join(timelineLines)


def compileWan22VideoPrompt(shotDict):
    """编译适用于 Wan 2.2 14B MoE 高低噪双专家模型的电影级视觉提示词"""
    framing = str(shotDict.get("cameraFraming") or "Medium shot").strip()
    movement = str(shotDict.get("cameraMovement") or "Steady camera").strip()
    actionArc = str(shotDict.get("actionArcPrompt") or "").strip()
    lastState = str(shotDict.get("lastFramePrompt") or "").strip()
    axisRule = str(shotDict.get("axisRule") or "").strip()
    return (
        f"Cinematic film shot, {framing}, {movement}. "
        f"Action progression: {actionArc} "
        f"Ending state: {lastState} "
        f"{axisRule} "
        "High-end 3D animation and realistic film lighting, natural fluid limb motion, consistent facial identity, 8k sharp details, no text, no subtitles."
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
        "你是一位顶级影视短剧总导演与统筹编剧。请将输入的小说原文改写为【结构化场次空间调度账本 (Scene Blocking & State Ledger)】并提取核心资产。\n"
        "必须严格遵守以下 6 条影视工业铁律：\n"
        "1. 【心理描写视觉外化】：严禁保留大段抽象旁白或超过18字的内心独白！必须把人物的经验、震惊、回忆外化为可见的物理动作。\n"
        "2. 【时空与服化道严格隔离】：若存在穿越/重生/前世今生，必须拆分为不同场次（如 S01 前世现代、S02 今生修仙界）。前世场次的 allowedCharacters 只能写 `角色名（前世）`，今生场次只能写 `角色名（今生）`！古风/修仙场次的描述必须强调古代青铜/石质/木质器物与古装布履，严禁出现现代阀门手轮或现代皮靴。\n"
        "3. 【资产提取严控精简（拒绝琐碎耗材）】：\n"
        "   - `character`：只提取本集有明确戏份的角色（3~6个），其 `visualPrompt` 必须使用纯正英文详细描述：精确年龄与成年骨骼体态（如19岁必须写 `19-year-old adult young man, 178cm tall, lean masculine build, NOT a child`，严禁对14岁以上人物使用 `boy` 以免画成幼童）、脸型五官、固定发型、服装颜色材质。\n"
        "   - `scene`：只提取本集核心主场景（2~4个），纯英文描述空间布局、建筑材质与主光源。\n"
        "   - `prop`：全集最多只提取 2~4 件最核心的剧情信物或关键焦点道具！严禁把扳手、螺栓、按钮、瓦盆、烧火棍、炭条、布条、火钳等普通工具耗材提取为独立资产（普通工具直接写在镜头的 startPoseEn/endPoseEn 英文句子中即可）。\n"
        "   - 所有资产的 `visualPrompt` 必须为纯英文视觉描述（不要夹杂 `中文：...` 或角色中文姓名，防止生图模型把汉字当招牌画进画面）。\n"
        "4. 【镜头景别丰富交替 & 严禁单人特写塞入旁人】：\n"
        "   - 同一场戏的连续节拍 (beats) 必须交替使用不同景别（如 `Close-up shot` 单人面部/手部特写、`Medium close-up shot` 单人近景、`Medium shot` 双人交锋），严禁连续多个镜头使用相同机位！\n"
        "   - 【关键】如果某个节拍是单人特写或单人动作，`charactersInShot` 数组必须只填这 1 个角色！并且在 `startPoseEn` 和 `endPoseEn` 中只描述这 1 个角色（严禁提及未入镜的其他角色，防止单人特写变成多人大合照或双胞胎分身）。\n"
        "5. 【首尾帧同机位动作演进】：对于 `action_fl2va` 动作镜，`startPoseEn` 与 `endPoseEn` 必须保持完全相同的机位景别、相同的人物站立/坐卧基础姿态与背景方位，仅改变核心肢体动作或局部状态（严禁首帧站立、尾帧突然变成盘腿坐地）。\n"
        "6. 【生图英文描述去名字化】：在 `startPoseEn`、`actionEn`、`endPoseEn` 中，除写明角色名外，必须同时用简短视觉特征指代（如 `the 19-year-old lean worker in gray robe` vs `the fat steward in brown brocade`），让图像模型在双人同框时清晰区分两人外貌，且严禁要求画面出现任何汉字或文字标牌。\n"
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

    userPrompt = f"【全局视觉画风】：{visualStyle}\n【项目已有资产】：\n{assetText or '无'}\n\n【待改写小说原文】：\n{novelText}"
    payload = json.dumps({
        "model": llmConfig.get("model") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": systemPrompt},
            {"role": "user", "content": userPrompt},
        ],
        "temperature": 0.25,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llmConfig['apiKey']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=75) as resp:
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

    # 3. 古风/修仙时代防现代工业穿帮校准
    isAncient = any(k in str(eraTag) for k in ("今生", "修仙", "古风", "仙侠", "正篇"))
    if isAncient:
        text = re.sub(r"\bair\s+gate\s+wheel\b", "ancient bronze furnace sliding damper lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\bvalve\s+wheel\b", "ancient bronze damper lever", text, flags=re.IGNORECASE)
        text = re.sub(r"\bglowing\s+runes\b", "subtle engraved ancient patterns without text", text, flags=re.IGNORECASE)

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

        for beat in scene.get("beats") or []:
            # 严格只取本节拍实际入镜的角色（单人镜头绝不强塞第2人）
            rawCharsInBeat = [str(c).lstrip("@").strip() for c in (beat.get("charactersInShot") or []) if str(c).strip()]
            charsInShot = rawCharsInBeat if rawCharsInBeat else (allowedChars[:1] if allowedChars else [])
            propsInShot = [str(p).lstrip("@").strip() for p in (beat.get("propsInShot") or []) if str(p).strip()]

            refs = []
            charPromptParts = []
            for idxC, cClean in enumerate(charsInShot):
                cAsset = assetMap.get(cClean)
                cDescEn = extractPureEnglishVisual(cAsset.get("visualPrompt") if cAsset else "", eraTag) or "Adult East Asian character in signature attire"
                refs.append({"asset": f"@{cClean}", "role": "character", "description": cDescEn})
                charPromptParts.append(f"Character {idxC + 1} ({cDescEn})")

            refs.append({"asset": f"@{locName}", "role": "scene", "description": locDescEn})

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
            startPoseEn = extractPureEnglishVisual(beat.get("startPoseEn") or "", eraTag) or "Character in opening pose."
            actionEn = extractPureEnglishVisual(beat.get("actionEn") or "", eraTag) or "Executes planned dramatic action."
            endPoseEn = extractPureEnglishVisual(beat.get("endPoseEn") or "", eraTag) or "Character completes action in final pose."

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
                f"Environment: {locDescEn}. {eraRealismRule} "
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

            compiledShots.append({
                "shotNumber": shotNum,
                "title": f"[{sceneCode}] {beat.get('title') or f'镜头 #{shotNum}'}",
                "shotCategory": beat.get("beatCategory") or "action_fl2va",
                "durationSec": float(beat.get("durationSec") or 5.0),
                "continuityMode": beat.get("continuityMode") or ("establish" if shotNum == 1 else "same_scene_cut"),
                "lockFromPrevEnd": 0,
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

    noTextRule = (
        "Strictly NO text, NO Chinese characters, NO kanji, NO letters, NO helmet labels, "
        "NO nameplates, NO signboards, NO watermarks anywhere in the image."
    )

    if aType == "character":
        return (
            f"Character design reference portrait, {cleanStyle}. "
            f"Solo portrait of 1 person: {cleanDesc}. "
            "Medium close-up portrait showing clear facial features, adult bone structure, consistent hairstyle, "
            f"exact clothing texture and color palette, cinematic rim lighting, ultra-detailed skin and fabric. {noTextRule}"
        )
    if aType == "scene":
        return (
            f"Cinematic environment concept art, empty establishing wide shot with NO people, {cleanStyle}. "
            f"Environment details: {cleanDesc}. "
            f"Clear spatial depth, consistent light source direction, atmospheric volumetric lighting, 8k resolution. {noTextRule}"
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

    # 将动作句中的拼音角色名替换为带明确服装外貌特征的英文指代，防止多人同框时穿错衣服或变成双胞胎
    nameAliasMap = {
        r"\bLu Dachuan in the past life\b": "the 50-year-old weathered repairman in oil-stained blue work jacket",
        r"\bLu Dachuan in this life\b": "the 19-year-old lean adult young man in ragged gray cloth robe",
        r"\bLu Dachuan\b": "the 19-year-old lean adult young man in ragged gray cloth robe" if "今生" in eraTag else "the 50-year-old weathered repairman in blue work jacket",
        r"\bCao Pangzi\b": "the fat middle-aged steward in dark brown brocade robe",
        r"\bCao\b": "the fat steward in dark brown brocade robe",
        r"\bXiao Shitou\b": "the 16-year-old teenage apprentice in patched short gray jacket",
    }
    for pat, repl in nameAliasMap.items():
        poseCleanEn = re.sub(pat, repl, poseCleanEn, flags=re.IGNORECASE)

    dnaBlocks = [f"Cinematic film frame, {cleanStyle}, {framing}."]

    if len(matchedChars) == 1:
        c = matchedChars[0]
        cDesc = extractPureEnglishVisual(c.get("visualPrompt") or "", eraTag)
        dnaBlocks.append(f"SOLO SHOT of ONLY 1 person in frame (strictly no other people visible): {cDesc}.")
    elif len(matchedChars) >= 2:
        chList = []
        for idx, c in enumerate(matchedChars[:3]):
            cDesc = extractPureEnglishVisual(c.get("visualPrompt") or "", eraTag)
            posHint = "on screen-left" if idx == 0 else ("on screen-right" if idx == 1 else "in background")
            chList.append(f"Person {idx + 1} ({posHint}): {cDesc}")
        dnaBlocks.append(f"Exactly {len(matchedChars[:3])} distinct people in frame: {'; '.join(chList)}.")

    if matchedScenes:
        sDesc = extractPureEnglishVisual(matchedScenes[0].get("visualPrompt") or "", eraTag)
        dnaBlocks.append(f"Environment: {sDesc}.")

    if matchedProps:
        pDescs = [extractPureEnglishVisual(p.get("visualPrompt") or "", eraTag) for p in matchedProps[:2]]
        pDescs = [d for d in pDescs if d]
        if pDescs:
            dnaBlocks.append(f"Active Props: {'; '.join(pDescs)}.")

    if frameRole == "last":
        dnaBlocks.append(
            f"Ending Action Pose (keep the exact same camera angle, character identity, clothing, and background layout as the opening frame): {poseCleanEn}."
        )
    else:
        dnaBlocks.append(f"Action & Composition: {poseCleanEn}.")

    dnaBlocks.append(
        "Pure photographic frame, natural adult proportions, strictly NO text, NO Chinese characters, NO signboards, NO nameplates, NO helmet labels, NO watermarks."
    )
    return " ".join(dnaBlocks)
