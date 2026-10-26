import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
path = r'D:\VideoForge-dev\backend\core\previz_design.py'
text = open(path, encoding='utf-8').read()

print('=== 顶层定义')
for m in re.finditer(r'^(?:[A-Z_]{3,} =|def |async def |class )(.{0,80})', text, re.M):
    print('  ', m.group(0).strip()[:100])

print('\n=== 提示词相关片段')
for key in ('PROMPT', 'SYSTEM_PROMPT', '请你', '要求：', 'JSON'):
    i = text.find(key)
    if i >= 0:
        print(f'--- 命中 {key} @ {i}')
        print(text[max(0, i - 200): i + 900].replace('\n', ' | ')[:1100])
        print()
