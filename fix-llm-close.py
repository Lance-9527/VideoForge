"""LLMClient 没有 close() 方法 —— 去掉那次调用（异常把结果都丢了）。"""
from pathlib import Path

p = Path(r'D:\VideoForge-dev\backend\core\scene_anchors.py')
text = p.read_text(encoding='utf-8')
old = """        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2000)
        data = extract_json(raw) if raw else None
        await llm.close()"""
new = """        raw = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2000)
        data = extract_json(raw) if raw else None
        # ★ LLMClient 没有 close() 方法（旧代码在这里抛异常，把已经拿到的结果也丢了）"""
if old in text:
    p.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('已去掉 llm.close()')
else:
    print('未匹配')

import py_compile
py_compile.compile(str(p), doraise=True)
print('语法 OK')
