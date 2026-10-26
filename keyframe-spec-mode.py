"""关键帧改成「AI 写实预演图」模式（2026-10-22 实测后的架构结论）。

实测依据（都在本轮做过）：
  ① 平台**严格认** `role=first_frame`：去掉参考图后，成片首帧 = 白膜首帧，构图 100% 一致。
  ② 但白膜是 Blender 灰模，直接当首帧 → 成片也是灰模。
  ③ 图像模型（Qwen-Image-Edit）**保不住灰模的布局**：喂灰模 + 写实场景图，它跟的是写实图；
     只喂灰模，它也重画成另一个场景。⇒ "用图像模型把灰模重绘成写实"这条路不成立。
  ④ 于是架构变成（也更符合"白膜 = 用户点头的构图"这个本意）：
       分镜 → **AI 写实关键帧**（= 用户要确认的那张图）→ 当 first_frame 出片
       ⇒ 成片首帧 = 用户确认过的那张图，**一致性由平台保证**（见 ①）。

所以关键帧不再是"灰模的重绘"，而是**按分镜规格生成的写实预演图**：
  提示词 = 我们编译的三段式 video_prompt 的前段（画面质感 + 场景 + 主体）
  参考图 = 角色图 + 场景图（保长相与氛围）
"""

from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\keyframe.py')
text = p.read_text(encoding='utf-8')

old_doc = """    所以中间必须加一步：用图像模型把白膜**重绘成写实关键帧**（构图取自白膜，长相来自参考图），
    再拿关键帧当首帧出片。只花一张图的钱，且关键帧可缓存复用。
    \"\"\""""
new_doc = """    所以中间必须加一步：**AI 写实关键帧** —— 按分镜规格生成一张写实预演图，
    这张图就是**用户要点头确认的那张**，出片时它当 first_frame，
    于是"用户看到的构图 = 成片的构图"由平台本身保证（见上面 ①）。

    ★ 修正过一次方向：原以为"把白膜灰模重绘成写实"可行，实测**不可行**
      （图像模型保不住灰模布局，喂了写实参考图就跟参考图）。
      结论：关键帧 = 文生图（分镜规格 + 角色/场景参考），白膜灰模仍保留，
      但它只作运镜/遮挡参考，**不再当首帧来源**。
    \"\"\""""
text = text.replace(old_doc, new_doc, 1)

old_keyframe_prompt = """KEYFRAME_PROMPT = (
    "严格保持第一张参考图的**构图、机位、景别、透视与所有物体的位置关系**不变，"
    "只把画面重绘成写实电影质感。"
    "第二张参考图是同一场景的写实参考，第三张及之后是人物参考：请还原它们的场景氛围与人物长相，"
    "但人物的站位必须与第一张图一致。"
    "画面干净、无任何文字与水印。"
)"""
new_keyframe_prompt = """KEYFRAME_PROMPT = (
    "这是短片的**首帧定妆图**：按下面的画面设定生成一张写实电影剧照。"
    "构图、机位、景别、人物站位与道具位置都要明确，"
    "画面干净、无任何文字与水印（招牌也不要出现字）。"
)"""
text = text.replace(old_keyframe_prompt, new_keyframe_prompt, 1)

# 关键帧输入：分镜规格（提示词）+ 角色/场景参考图（保长相与氛围）
old_refs = """    refs = _collect_refs(db, shot)
    # ★ 默认**只喂白膜首帧**：构图必须只有一个来源。
    #   上一版把写实场景图一起喂进去，图像模型跟的是那张照片（构图跑偏），
    #   场景/人物信息应该走**提示词**（我们已经把场景锚定与角色锚定写进去了）。
    if not use_extra_refs:
        refs = []"""
new_refs = """    # ★ 关键帧的素材：**角色图 + 场景图**（保长相与氛围），
    #   构图由提示词（分镜规格）决定；白膜灰模**不再**当参考图
    #   （实测图像模型保不住它的布局，喂了反而跑偏）。
    refs = _collect_refs(db, shot) if use_extra_refs else []"""
text = text.replace(old_refs, new_refs, 1)

old_call = """            reference_images=[previz] + refs[:2],"""
new_call = """            reference_images=(refs[:2] if use_extra_refs else []),"""
text = text.replace(old_call, new_call, 1)

# previz 现在不是必需（关键帧不再从它重绘），但仍作为"用户看过的白膜"保留
old_guard = """    previz = str(shot.get("previz_path") or "").strip()
    if previz == "" or not os.path.isfile(previz):
        return {"ok": False, "error": "没有白膜首帧，跳过关键帧"}"""
new_guard = """    previz = str(shot.get("previz_path") or "").strip()
    if not prompt:
        # 没给提示词就用镜头的概述兜底（不该发生：出片前一定会先编译提示词）
        prompt = KEYFRAME_PROMPT + "\\n" + str(shot.get("layer1_overview") or "")"""
text = text.replace(old_guard, new_guard, 1)

# 去掉下面重复的 prompt 兜底
text = text.replace("""    if not prompt:
        prompt = KEYFRAME_PROMPT

    provider""", """    provider""", 1)

p.write_text(text, encoding='utf-8')
import py_compile
py_compile.compile(str(p), doraise=True)
print('keyframe.py 已改为「AI 写实预演图」模式，语法 OK', p.stat().st_size, 'B')
