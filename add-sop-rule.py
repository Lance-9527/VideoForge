"""把「相邻镜动作不能重复/倒流」写进 vf-video 的 SOP（来自本轮 4 镜实测的发现）。

实测：农夫与蛇的镜1 已经"蹲下"，镜2 又"走入→蹲下"——时间倒流。
接龙预检把这一对判成"首帧与动作冲突"并自动降级独立镜（安全带生效），
但根子在分镜：**相邻两镜的动作必须接得上**（上一镜的结束状态 = 下一镜的起点）。

同时补一条 SOP：接龙镜要"从当前姿势继续"。
"""
from pathlib import Path

p = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\index.js')
text = p.read_text(encoding='utf-8')
anchor = "    '· 用户说「确认：<阶段>」= 同意执行该阶段；「我要改一下：<阶段>」= 先问清改什么再动手；「跳过：<阶段>」= 记住跳过原因并进入下一步。',"
add = anchor + """
    '· ★ 同一场景的**相邻两镜动作必须接得上**：上一镜的结束状态 = 下一镜的起点'
    '（不要让下一镜重新"走入/转身/拿起"上一镜已经做完的事 —— 那叫时间倒流，'
    '接龙预检会判成冲突并把该镜降级成独立镜，白白损失连续性）。',"""
if '时间倒流' in text:
    print('已存在该 SOP')
elif anchor in text:
    text = text.replace(anchor, add, 1)
    p.write_text(text, encoding='utf-8')
    print('① SOP 已加（相邻两镜动作要接得上）')
else:
    print('① 未匹配 SOP 锚点')

# 顺带把「从当前姿势继续」写进接龙镜的动作提示（在 core.js 的 relay_action_hint 已加，这里只确认）
core = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\core.js')
ct = core.read_text(encoding='utf-8')
print('② core.js 里 relay_action_hint 存在:', 'relay_action_hint' in ct)
