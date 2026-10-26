import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
root = r'D:\VideoForge-dev\backend'
for dp, dn, fn in os.walk(root):
    if '__pycache__' in dp:
        continue
    for f in fn:
        if not f.endswith('.py'):
            continue
        p = os.path.join(dp, f)
        t = open(p, encoding='utf-8', errors='replace').read()
        for m in re.finditer(r'_progress', t):
            line = t.count('\n', 0, m.start()) + 1
            snippet = t.split('\n')[line - 1].strip()[:120]
            if 'get(' in snippet or '=' in snippet or 'params' in snippet:
                print(f'{p.replace(root, "")}:{line}  {snippet}')
