# -*- coding: utf-8 -*-
"""验证 extract_json 对 LLM 各类畸形输出的容错能力"""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'D:\VideoForge-dev\backend')
from core.llm import extract_json

CASES = [
    ('标准 JSON', '{"title":"A","scenes":[{"scene_number":1}]}'),
    ('markdown 包裹', '```json\n{"title":"B","scenes":[]}\n```'),
    ('无语言标记代码块', '```\n{"title":"C"}\n```'),
    ('前后说明文字', '好的，这是剧本：\n{"title":"D","scenes":[{"scene_number":1}]}\n希望满意！'),
    ('尾随逗号', '{"title":"E","scenes":[{"scene_number":1},]}'),
    ('单引号', "{'title':'F','scenes':[]}"),
    ('中文引号', '{"title":"G","logline":"测试“引号”"}'),
    ('Python 字面量', '{"title":"H","active":True,"extra":None,"flag":False}'),
    ('带注释', '{\n  // 标题\n  "title":"I",\n  /* 场次 */\n  "scenes":[]\n}'),
    ('嵌套截断', '{"title":"J","scenes":[{"scene_number":1,"title":"场次1","actions":["动作1","动作2'),
    ('被截断（值缺失）', '{"title":"K","logline":"测试","scenes":[{"scene_number":1},'),
    ('BOM + 零宽字符', '\ufeff{"title":"L"}\u200b'),
    ('数组根', '[{"scene_number":1}]'),
    ('多段对象（取第一段）', '{"title":"M"} 然后 {"title":"N"}'),
    ('转义引号', '{"title":"O","logline":"他说\\"你好\\""}'),
]

ok = 0
for name, raw in CASES:
    try:
        r = extract_json(raw)
        preview = json.dumps(r, ensure_ascii=False)[:70]
        print(f'  ✅ {name:22} → {preview}')
        ok += 1
    except Exception as e:
        print(f'  ❌ {name:22} → {str(e)[:80]}')

print()
print(f'通过 {ok}/{len(CASES)}')
