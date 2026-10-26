import io,sys,re
sys.stdout.reconfigure(encoding="utf-8")
p="core/v2api.py"
L=io.open(p,encoding="utf-8",errors="replace").read().splitlines()
print(f"=== v2api.py {len(L)} 行 ===")
for i,l in enumerate(L,1):
    if re.search(r"^(async )?def |@router\.|APIRouter\(|status_code=|\"failed\"|\"cancelled\"|cancel", l):
        print(f"{i}: {l.strip()[:150]}")
