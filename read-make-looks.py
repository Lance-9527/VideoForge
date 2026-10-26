import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
path = r'D:\VideoForge-dev\backend\core\agent_tools_stages.py'
text = open(path, encoding='utf-8').read()
i = text.find('def t_make_looks')
if i < 0:
    i = text.find('def t_make_character_look')
print(text[i:i + 4200])
