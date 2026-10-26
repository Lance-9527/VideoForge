import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
path = r'D:\VideoForge-dev\backend\data\previz_blender\render_spec.py'
text = open(path, encoding='utf-8', errors='replace').read()
i = text.find('for c in (S.get("cast") or [])[:6]')
print(text[max(0, i - 1800): i + 1500])
