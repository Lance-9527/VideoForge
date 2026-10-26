import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
# 找 make_human 的定义
root = r'D:\VideoForge-dev\backend\data\previz_blender'
import os
for f in sorted(os.listdir(root)):
    if not f.endswith('.py'):
        continue
    t = open(os.path.join(root, f), encoding='utf-8', errors='replace').read()
    if 'def make_human' in t:
        i = t.find('def make_human')
        print('##', f, '@', i)
        print(t[i:i + 1800])
        break
