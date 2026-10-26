import io,sys
sys.stdout.reconfigure(encoding="utf-8")
L=io.open("core/v2api.py",encoding="utf-8",errors="replace").read().splitlines()
print("=== get_job (536-560) ===")
for j in range(535,560): print(f"{j+1}: {L[j]}")
print("\n=== cancel_job (560-575) ===")
for j in range(559,575): print(f"{j+1}: {L[j]}")
