# VideoForge · 品牌提示词（Brand Prompt）

> **给所有调出图/出视频模型的其他 Agent 用的统一视觉 DNA。**
>
> 直接复制对应段落，替换 `{占位符}` 即可使用。所有 VideoForge 出片默认带这一层基底——保证跨项目、跨镜头、跨 agent 出来的画面看起来是**同一套作品**，不是东拼西凑。

---

## 0. 怎么用（30 秒上手）

```text
┌──────────────────────────────────────────────────────────┐
│  1. 必加：BASE_STYLE              ← 视觉 DNA，所有图必带  │
│  2. 选加：CHARACTER_SIGNATURE     ← 角色出图必带，保同一张脸 │
│  3. 选加：SCENE_CARD              ← 场景出图必带，保色调统一 │
│  4. 必加：SHOT_7ELEMENTS          ← 镜头七要素，主体描述      │
│  5. 必加：QUALITY_TAIL            ← 质量词尾段，结尾          │
│  6. 必加：NEGATIVE_PROMPT         ← 负面清单，单独通道        │
└──────────────────────────────────────────────────────────┘
```

模板：[BASE_STYLE] + [CHARACTER_SIGNATURE?] + [SCENE_CARD?] + [SHOT_7ELEMENTS] + [QUALITY_TAIL] + [NEGATIVE_PROMPT]

---

## 1. BASE_STYLE · 视觉 DNA（必加）

这是 VideoForge 出图的统一基底。所有提示词放在**最前面**，让模型在理解主体之前先锁定品牌色调和质感。

### 默认（cinematic 写实电影感）

```text
Cinematic visual style. Natural film-like color grading with rich shadow detail
and warm-cool tonal balance. Shallow depth of field with soft bokeh. Photorealistic
skin and fabric texture. Atmospheric motivated lighting. Film grain, slight.
No anime stylization. No illustration. No painterly brushstrokes.
16:9 composition, eye-line at lower third, leading lines toward subject.
```

约 70 个英文词。**不要再翻译成中文**——视频/图像模型对英文摄影术语更敏感。

### 何时换 BASE_STYLE

把上面这段换成下面任意一种（详见第 5 节）：

| 触发场景 | 替换为 |
|---|---|
| 用户明确说"动漫/二次元" | `BASE_STYLE.anime` |
| 国风/水墨/古装剧 | `BASE_STYLE.chinese_ink` |
| 赛博朋克/霓虹/未来 | `BASE_STYLE.cyberpunk` |
| 纪录片/纪实风格 | `BASE_STYLE.documentary` |
| 游戏 CG / Unreal 引擎感 | `BASE_STYLE.3d_render` |

---

## 2. CHARACTER_SIGNATURE · 角色签名（角色出图必加）

跨镜头保持同一张脸的关键。**每个角色生成时就钉死一次**，之后所有镜头原样复用。

### 槽位（缺哪个省哪个，但顺序固定）

```text
CHARACTER_SIGNATURE = "{name}, {age}-year-old {gender},
face: {face_shape} with {skin} skin, {eyes} eyes, {hair},
build: {height_build},
distinctive: {distinctive_marks},
wearing: {costume_top}, {costume_bottom}, {footwear},
accessories: {accessories},
expression_default: {default_emotion}"
```

### 示例

```text
CHARACTER_SIGNATURE = "李队长, 35-year-old male,
face: square jaw with weathered tan skin, deep-set dark brown eyes, short-cropped black hair with grey temples,
build: 185cm tall, broad-shouldered athletic,
distinctive: diagonal scar across left eyebrow,
wearing: dark grey linen tunic with high collar, weathered brown leather belt, dark wool trousers, black cloth boots,
accessories: worn iron broadsword on back, simple iron armband on right wrist,
expression_default: stoic and alert"
```

### 关键约束

- **外形特征用形容词 + 名词的稳定组合**，不要写"年轻时/曾经/看起来像"这种会随镜头漂移的描述
- **服装按"上装/下装/鞋/配饰"分槽**，逐槽复用，不要每次重写整段
- **distinctive 必须有 1 个以上**识别锚点（疤/痣/胎记/特征性配饰），是用来跨镜头识别的
- 单角色签名 ≤ **260 字符**（超过会被可灵/海螺等模型的提示词预算挤掉）

---

## 3. SCENE_CARD · 场景卡（场景出图必加）

跨镜头保持同一地点的色调、光线、气氛。

### 槽位

```text
SCENE_CARD = "Location: {location}.
Time: {time_of_day}, {weather}.
Lighting: {lighting_setup}.
Color palette: {primary_colors}.
Atmosphere: {mood_keywords}.
Architectural/props details: {key_props}."
```

### 示例（古装剧客栈）

```text
SCENE_CARD = "Location: dimly-lit inn main hall, ancient China, wooden two-story structure with central hearth.
Time: night, light rain outside, occasional thunder.
Lighting: warm orange firelight from central hearth as key, cold blue moonlight from open door as rim.
Color palette: deep amber, burnt sienna, charcoal shadows, cold steel-blue accents.
Atmosphere: tense, quiet, anticipatory.
Architectural/props details: long wooden tables with worn benches, hanging paper lanterns (one flickering), weapon rack by door, rain-soaked traveler cloaks hanging near entrance."
```

### 关键约束

- **光线要给具体光源**（来自哪里、方向、色温），不要写"氛围光"这种空话
- **色板给 3-4 个具体颜色**，模型会照着压色调
- **关键道具列 2-4 件**，别超过——多了反而分散注意力
- 同一场景的**所有镜头**必须复用同一份 SCENE_CARD，逐字不改

---

## 4. SHOT_7ELEMENTS · 镜头七要素（必加）

VideoForge 的核心提示词公式。所有镜头描述按这 7 个槽位写：

```text
SHOT_7ELEMENTS = "{shot_size} of {subject_signature} {action_clause},
{camera_move}, {lens_focal},
{environment_one_liner},
{lighting_quick},
{time_of_day_quick},
{style_variant_marker}."
```

### 槽位说明

| 槽位 | 取值范围 | 示例 |
|---|---|---|
| `shot_size` | 大远景 / 远景 / 全景 / 中景 / 中近景 / 特写 / 大特写 / 过肩 / 主观视角 | `Medium close-up` |
| `subject_signature` | 复用 CHARACTER_SIGNATURE 主体部分（不带 name） | `a 35-year-old man with weathered tan skin and diagonal scar` |
| `action_clause` | 一句话动作，**不超过 18 词**，动词开头 | `crouches low behind a mossy boulder, gripping his broadsword` |
| `camera_move` | 固定 / 推近 / 拉远 / 横摇 / 跟拍 / 环绕 / 手持 / 斯坦尼康 / 航拍 | `slow dolly-in` |
| `lens_focal` | 24mm / 35mm / 50mm / 85mm / 135mm | `50mm prime` |
| `environment_one_liner` | 一句话环境，不复用 SCENE_CARD 全段 | `in a moonlit mountain pass` |
| `lighting_quick` | 一句话光线状态 | `rim-lit by cold blue moonlight` |
| `time_of_day_quick` | `at night` / `at golden hour` / `at blue hour` / `at dawn` | `at night` |
| `style_variant_marker` | 留空（默认 cinematic）或显式标 `anime style,` | ``（空） |

### 组装示例

把上面"李队长"的 CHARACTER_SIGNATURE 和客栈 SCENE_CARD 拼进一个镜头：

```text
[BASE_STYLE]

CHARACTER: 李队长, 35-year-old male, face: square jaw with weathered tan skin,
deep-set dark brown eyes, short-cropped black hair with grey temples,
distinctive: diagonal scar across left eyebrow,
wearing: dark grey linen tunic, weathered brown leather belt, dark wool trousers.

SHOT: Medium close-up of a 35-year-old man with weathered tan skin and diagonal scar
crouches low behind a long wooden table, hand resting on sword hilt,
slow dolly-in, 50mm prime,
in a dimly-lit ancient Chinese inn main hall,
rim-lit by cold blue moonlight from open door with warm orange firelight key from hearth,
at night.

[QUALITY_TAIL]
[NEGATIVE_PROMPT]
```

---

## 5. QUALITY_TAIL · 质量词尾段（必加）

**永远放在提示词最末尾**，不要前置——前置会干扰模型对主体的注意力。

```text
Cinematic quality, sharp focus on subject, natural motion blur on background,
film grain subtle, no motion distortion, no artifacts, no watermark, no text.
```

需要更高质感时追加（不超过 15 词）：

```text
High dynamic range, anamorphic lens flare subtle, ARRI Alexa look,
matched color, broadcast safe.
```

---

## 6. NEGATIVE_PROMPT · 负面清单（必加，单独通道）

**不要写进正面提示词**——会和"不要出现 XXX"互相干扰，反而更易出现。走模型的 `negative_prompt` 字段。

### 默认（cinematic 写实）

```text
modern equipment, cars, airplanes, smartphones, watches, glasses,
visible text, watermark, logo, signature, frame, border,
deformed hands, extra fingers, missing fingers, mutated hands,
blurry face, asymmetric eyes, double face,
low quality, jpeg artifacts, oversaturated, overexposed,
anime style, illustration, painting, cartoon, sketch,
plastic skin, doll-like, mannequin
```

### 变体（按风格）

- **anime 风格**：`photorealistic, 3d render, photo, real human skin`
- **古装剧**追加：`modern clothing, wristwatch, glasses, sneakers`
- **赛博朋克**追加：`natural lighting, daylight, classical architecture`
- **纪录片**追加：`dramatic lighting, cinematic color grading, posed`

---

## 7. 风格变体（Style Variants）

把第 1 节 BASE_STYLE 整段替换成下面对应版本即可。

### 7.1 anime / 二次元

```text
BASE_STYLE.anime = "Anime visual style. Clean line art with cel shading.
Vibrant saturated colors with flat color blocks. Stylized hair and eyes
with characteristic highlights. Studio Ghibli / Kyoto Animation influence.
16:9 composition, cinematic framing but 2D illustration."
```

### 7.2 chinese_ink / 国风水墨

```text
BASE_STYLE.chinese_ink = "Chinese ink painting style. Sumi-e brushwork
with rice paper texture. Negative space as compositional element.
Restrained color palette of ink black, raw umber, and occasional
cinnabar or jade green accents. Wabi-sabi atmosphere.
16:9 composition, asymmetric balance."
```

### 7.3 cyberpunk / 赛博朋克

```text
BASE_STYLE.cyberpunk = "Cyberpunk visual style. Neon-drenched night
cityscape. Volumetric fog with magenta and cyan light pollution.
Anamorphic lens flare and chromatic aberration on light sources.
Wet asphalt reflections. High contrast between deep shadows and
saturated neon highlights. Blade Runner 2049 color grading.
16:9 composition, low angle dutch tilt common."
```

### 7.4 documentary / 纪录片

```text
BASE_STYLE.documentary = "Documentary photography style. Handheld
verité aesthetic. Available light only, no artificial setups.
Slight grain from high ISO. Natural skin with visible pores and
imperfections. Unposed subjects, candid moments.
16:9 composition, photojournalistic framing."
```

### 7.5 3d_render / 游戏 CG

```text
BASE_STYLE.3d_render = "3D rendered cinematic. Unreal Engine 5 quality
with PBR materials and ray-traced global illumination. Subsurface
scattering on skin. Volumetric god rays and atmospheric depth.
Photorealistic but with stylized lighting. High polygon detail.
16:9 composition, cinematic depth of field."
```

---

## 8. 完整模板示例

### 8.1 角色定妆照（character_image）

```text
[BASE_STYLE]CHARACTER_SIGNATURE,

Full body portrait standing in neutral T-pose, head-on camera, soft studio
lighting from upper left, neutral grey seamless background,
clean character reference sheet style, multiple expressions in grid layout,
at studio.

[QUALITY_TAIL]
[NEGATIVE_PROMPT]
```

### 8.2 场景概念图（scene_image）

```text
[BASE_STYLE]SCENE_CARD,

Wide establishing shot of the location, no people in frame, deep focus
from foreground props to background architecture, golden hour sidelight
casting long shadows,
at golden hour.

[QUALITY_TAIL]
[NEGATIVE_PROMPT]
```

### 8.3 镜头首帧（shot_keyframe，给图生视频用）

```text
[BASE_STYLE][CHARACTER_SIGNATURE (without name)][SCENE_CARD (one-liner version)],

SHOT: {shot_size} of {subject_signature_short} {action_clause},
{camera_move} {lens_focal},
{environment_one_liner},
{lighting_quick}, {time_of_day_quick}.

[QUALITY_TAIL]
[NEGATIVE_PROMPT]
```

### 8.4 视频生成提示词（直接给可灵/海螺/Sora）

```text
[BASE_STYLE (去除静态构图相关词)]CHARACTER_SIGNATURE,

SHOT: {shot_size} of {subject_signature_short} {action_clause},
{camera_move}, {lens_focal},
{environment_one_liner},
{lighting_quick}, {time_of_day_quick}.

[QUALITY_TAIL（强调运动自然）]
[NEGATIVE_PROMPT]
```

视频模型特别提示：
- **不要在视频提示词里写"表情:XXX"这种键值对**——模型会把它们当文字字幕
- **运镜每个镜头只写一个主运动**，要两个就拆镜头
- **负面词放 negative_prompt**，不混进正文

---

## 9. 跨镜头一致性 · 3 条铁律

来自 VideoForge 的 13 维连续性矩阵，实战中最常踩坑的 3 条：

1. **同一角色 = 同一份 CHARACTER_SIGNATURE，逐字复用**
   不要每次"简化"或"换种说法"。镜头 1 写 `diagonal scar across left eyebrow`，镜头 20 也必须逐字一样。

2. **同一场景 = 同一份 SCENE_CARD，逐字复用**
   哪怕只是角色走一步，场景描述也整段原样复制。模型靠**重复的字面**保持一致。

3. **运镜一个镜头只有一个**
   "低角度跟拍，快速推近" = 两个主运动 = 画面会"被扯"。要两个效果就**拆成两个镜头**。

---

## 10. 失败排查（出图不对劲时查这里）

| 症状 | 原因 | 修复 |
|---|---|---|
| 角色脸每张不一样 | CHARACTER_SIGNATURE 没复用，被改写了 | 锁定一字不改，复制粘贴 |
| 角色穿了不该穿的衣服 | costume 描述被改 | 把 CHARACTER_SIGNATURE 写完整、所有镜头复制 |
| 白天晚上不统一 | SCENE_CARD 没复用 | 同场景所有镜头贴同一份 SCENE_CARD |
| 画面像 AI 油画不是电影 | BASE_STYLE 没加或位置错了 | BASE_STYLE 必须放最前 |
| 主体变形/手变 6 根 | NEGATIVE_PROMPT 缺 | 加 `deformed hands, extra fingers` |
| 镜头里出现"表情:紧张"字样 | 把分镜表的键值对当文字塞了 | 只写自然语言动作，键值对留在数据层 |
| 画面太"塑料"/皮肤不真实 | QUALITY_TAIL 没加 | 加 `film grain subtle, no plastic skin` |
| 颜色饱和度过高像动漫 | 误用了 BASE_STYLE.anime | 确认 BASE_STYLE 是 cinematic 默认版 |

---

## 11. 版本

- v1.0 · 2026-10-06 · 初版（从 `videoprompt.py` 6 维公式 + `cinematic_prompt` 150 条技法库提炼）
- 维护者：VideoForge 项目组
- 配套：`videoprompt.py` 的 `SHOT_SIZES` / `CAMERA_MOVES` / `LIGHTING` / `STYLE` 受控词表是单一事实来源——改这里要同步改那份代码
