"""把 SOP 那条改成单条（用 + 拼接），JS 才合法。"""
from pathlib import Path

p = Path(r'D:\VideoForge-dsh\deepseek-harness-master\packages\video\vf-video\lib\index.js')
lines = p.read_text(encoding='utf-8').split('\n')
for i, l in enumerate(lines):
    if '相邻两镜动作必须接得上' in l:
        start = i
        break
else:
    raise SystemExit('没找到 SOP 那条')

# 找到这条的结尾（含逗号）
end = start
while end < len(lines) and not lines[end].rstrip().endswith(','):
    end += 1
print('替换行区间:', start + 1, '→', end + 1)
for j in range(start, end + 1):
    print('  ', lines[j][:110])

new_block = [
    "    '· ★ 同一场景的**相邻两镜动作必须接得上**：上一镜的结束状态 = 下一镜的起点'",
    "      + '（不要让下一镜重新「走入/转身/拿起」上一镜已经做完的事 —— 那叫时间倒流，'",
    "      + '接龙预检会判成冲突并把该镜降级成独立镜，白白损失连续性）。',",
]
lines[start:end + 1] = new_block
p.write_text('\n'.join(lines), encoding='utf-8')
print('已改成单条拼接')
