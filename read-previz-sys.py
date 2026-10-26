import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
text = open(r'D:\VideoForge-dev\backend\core\previz_design.py', encoding='utf-8').read()

i = text.find('_SYS = (')
j = text.find('def _make_llm(')
print('=== _SYS 与 schema 原文 ===')
print(text[i:j][:5200])
