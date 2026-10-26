"""白膜规格生成器升级：移植 Proma 的实战经验（2026-10-22）。

来源（Proma 训练产物，直接可用）：
  · `.proma/agent-workspaces/default/workspace-files/wuxia_previz/PROMPT_SPEC.md`
      —— 4 段式（起点状态/人物动作/摄影机运动/终点状态）+ 镜头任务表 + 12 种拍法
  · `.../PREVIZ_SPEC.md` —— 白膜规范（空间关系+运镜路径+时间节奏；骨骼局部系、步态、
      脚踝反解、步幅上限≤0.34m 等物理约束）
  · `D:\KnowledgeVault\AI短剧-白模预演-previz-调研报告.md`
      —— 白膜风格 prompt 措辞、预处理要求（统一 clay 材质、必须有地板与接触点、角色命名复述）、
         Techviz 几何体检（主体出画/穿墙/超框）

落到我们的 `previz_design`（LLM → JSON 规格 → 通用 Blender 渲染器）：
  ① 每镜加「镜头任务 + 4 段式 + 时间打点」字段（模型照着写，规格更专业）；
  ② normalize_spec 增加**几何体检**（出画/穿墙/超框）与自动钳制；
  ③ 渲染后导出**白膜规格说明**（blockout 的做法：白膜视频 + 每机位静帧 + 自动 prompt.txt），
     让用户与下游出片都能看到"白膜到底锁定了什么"。

本脚本只改 `core/previz_design.py`（并新增 `core/previz_notes.py` 导出规格说明）。
"""
import shutil
import time
from pathlib import Path

STAMP = time.strftime('%Y%m%d-%H%M%S')
ROOT = Path(r'D:\VideoForge-dev\backend\core')

# ── ① previz_design：提示词 + schema + 字段 ──────────────────────────────
p = ROOT / 'previz_design.py'
text = p.read_text(encoding='utf-8')
shutil.copyfile(p, p.with_suffix(p.suffix + '.bak-' + STAMP))

old_sys = '''_SYS = (
    "你是电影预演（previz）的**技术美术**。你的输出会被一个确定性的 Blender 脚本"
    "直接读成 3D 场景，所以只需要**摆放与机位**，不要写文学描写，不要写代码。\\n"
    "坐标系：+X 右、+Y 远、+Z 上；地面 Z=0；单位=米；人物默认面向 -Y（rot=180）。\\n"
    "相机默认在 -Y 侧（负 y）看向 +Y 方向，y 约为 -2 ~ -8，高度 1.2~1.8。"
)'''

new_sys = '''_SYS = (
    "你是电影预演（previz）的**技术美术**。你的输出会被一个确定性的 Blender 脚本"
    "直接读成 3D 场景，所以只需要**摆放与机位**，不要写文学描写，不要写代码。\\n"
    "坐标系：+X 右、+Y 远、+Z 上；地面 Z=0；单位=米；人物默认面向 -Y（rot=180）。\\n"
    "相机默认在 -Y 侧（负 y）看向 +Y 方向，y 约为 -2 ~ -8，高度 1.2~1.8。\\n"
    "\\n"
    "★ 白膜是什么（Proma 训练沉淀，规范见 PREVIZ_SPEC.md）：\\n"
    "  白膜 = **空间关系 + 运镜路径 + 时间节奏**的三维预演，给下游视频模型当运动参考。\\n"
    "  它**不是**成片：没有材质、没有景深、没有布料动力学。成本恒为 0（本地 Blender 渲染）。\\n"
    "\\n"
    "★ 每镜先定**镜头任务**，再选拍法（顺序反了就会写出空话）：\\n"
    "  建立空间=谁在哪、什么环境 → 大全景/全景/固定机位\\n"
    "  对峙蓄力=双方关系、实力差距 → 中景/推近\\n"
    "  出招动作=谁先动手、怎么动 → 中景跟拍/摇摄\\n"
    "  撞击结果=碰撞结果 → 特写/移焦\\n"
    "  表演情绪=情绪变化 → 特写/环绕\\n"
    "\\n"
    "★ 每镜按 **4 段式**写：起点状态 / 人物动作 / 摄影机运动 / 终点状态。\\n"
    "  起点状态 = 机位(距离/高度/角度/焦距) + 人物空间关系 + 关键道具位置\\n"
    "  人物动作 = 只写动作（具体动词 + 方向 + 重心变化），不写镜头\\n"
    "  摄影机运动 = 只写镜头（起始位置 + 拍法/幅度 + 落点 + 起止焦距），不写人物\\n"
    "  终点状态 = 机位 + 人物姿态 + 道具状态 + 环境反馈\\n"
    "\\n"
    "★ 时间打点：把动作拆成 2~3 段（例：0-3s approach / 3-5s turn / 5-8s reveal），"
    "每段给一个人物位置与相机位置，落点必须**衔接**（上一段落点 = 下一段起点）。\\n"
    "  人物移动步幅 ≤ 0.34m/步（实测超过就会腿被拉直、像木偶）。\\n"
    "  人物必须有**接触点**（脚踩在 Z=0 地面上），不许浮空。\\n"
    "\\n"
    "★ 只摆剧本里真的出现的东西与角色；角色名要**原样复述**剧本里的名字（下游按名字对参考图）。"
)'''
if old_sys in text:
    text = text.replace(old_sys, new_sys, 1)
    print('① _SYS 已升级（白膜定义 + 镜头任务 + 4 段式 + 打点 + 步幅/接触点）')
else:
    print('① 未匹配 _SYS')

old_schema = '''  "note":"一句话：这镜在演什么"} ]}
硬要求：'''
new_schema = '''  "note":"一句话：这镜在演什么",
  "purpose":"建立空间|对峙蓄力|出招动作|撞击结果|表演情绪",
  "start_state":"起点状态：机位 + 人物空间关系 + 关键道具位置（一句话）",
  "beats":[{"start":0,"end":72,"action":"这段的动作（只写动作）",
            "cast_pos":[0,0],"camera_pos":[0,-4.2,1.55],"focus":[0,0,1.15]}],
  "end_state":"终点状态：机位 + 人物姿态 + 道具状态 + 环境反馈（一句话）"} ]}
硬要求：'''
if old_schema in text:
    text = text.replace(old_schema, new_schema, 1)
    print('① schema 已加 purpose/start_state/beats/end_state')
else:
    print('① 未匹配 schema')

# 硬要求补两条
old_hard = '''· 只摆剧本里真的提到的东西（没提到就别加车、别加树）。"""'''
new_hard = '''· 只摆剧本里真的提到的东西（没提到就别加车、别加树）；
· beats 的段要**时间递增且首尾相接**（第 N 段的 end = 第 N+1 段的 start）；
  人物在 beats 里的位移 ≤ 0.34m/步；相机 moves 的第一帧必须是 1 且落点与 beats 对得上；
· purpose 必填（决定这一镜的任务）；start_state / end_state 各一句话，不超过 60 字。"""'''
if old_hard in text:
    text = text.replace(old_hard, new_hard, 1)
    print('① 硬要求已补（beats 衔接 / 步幅 / purpose）')
else:
    print('① 未匹配硬要求')

p.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(p), doraise=True)
print('① 语法 OK')
