# -*- coding: utf-8 -*-
"""安全性测试：正常 JSON 中的单引号内容不能被修复逻辑破坏"""
import sys
sys.path.insert(0, r'D:\VideoForge-dev\backend')
sys.stdout.reconfigure(encoding='utf-8')
from core.llm import extract_json

cases = [
    ('{"text": "don\'t stop", "title": "OK"}', "don't stop"),
    ('{"logline": "it\'s a test", "scenes": []}', "it's a test"),
    ('{"note": "can\'t", "list": ["a\'b"]}', "can't"),
]
ok = 0
for raw, expect in cases:
    try:
        r = extract_json(raw)
        fine = expect in str(r)
        print(f'  {"✅" if fine else "⚠"} {raw[:45]:47} → {r}')
        ok += fine
    except Exception as e:
        print(f'  ❌ {raw[:45]} → {e}')
print(f'\n安全通过 {ok}/{len(cases)}')
