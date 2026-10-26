"""Generate VideoForge brand assets from the user's logo image.

- mark only (transparent) for the in-app brand mark
- white rounded-tile icons for Windows/macOS/tray
- base64 PNG for the sidebar slot (client bundle can only ship JS)
"""
import base64
import io
import os

from PIL import Image, ImageDraw

SRC = r'C:\Users\Administrator\.dsh\attachments\v1\objects\16\16475091820d5ef725619517882fcafa3b417d2daced270a057e141ce62d88d1'
OUT = r'D:\VideoForge-dsh\brand'
RES = r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources'
os.makedirs(OUT, exist_ok=True)

im = Image.open(SRC).convert('RGBA')
px = im.load()
w, h = im.size

# ① 只取上面那块（V 图形），下面那块是 "VideoForge" 字标
MARK_TOP, MARK_BOTTOM = 290, 880
minx, miny, maxx, maxy = w, h, 0, 0
for y in range(MARK_TOP, MARK_BOTTOM):
    for x in range(0, w):
        r, g, b, a = px[x, y]
        if a > 10 and (r < 246 or g < 246 or b < 246):
            minx = min(minx, x); maxx = max(maxx, x)
            miny = min(miny, y); maxy = max(maxy, y)
print('mark bbox', minx, miny, maxx, maxy)

mark = im.crop((minx, miny, maxx + 1, maxy + 1))
mw, mh = mark.size
print('mark size', mw, mh)

# ② 白底 → 透明（保留柔和边缘与火光）
mp = mark.load()
for y in range(mh):
    for x in range(mw):
        r, g, b, a = mp[x, y]
        whiteness = min(r, g, b)
        if whiteness >= 250:
            mp[x, y] = (r, g, b, 0)
        elif whiteness >= 236:
            # 抗锯齿边缘：按接近白色的程度给 alpha
            alpha = int(max(0, min(255, (250 - whiteness) * 255 // 14)))
            mp[x, y] = (r, g, b, alpha)

# ③ 方形画布
side = max(mw, mh)
pad = int(side * 0.06)
canvas_side = side + pad * 2
canvas = Image.new('RGBA', (canvas_side, canvas_side), (0, 0, 0, 0))
canvas.paste(mark, ((canvas_side - mw) // 2, (canvas_side - mh) // 2), mark)
canvas.save(os.path.join(OUT, 'mark-transparent-1024.png'))
canvas.resize((512, 512), Image.LANCZOS).save(os.path.join(RES, 'icon.png'))
print('icon.png (mark on transparent) written')

# ④ 白底圆角瓦片（Windows/macOS/tray）：白瓦片 + 居中 mark
def tile(size, radius_ratio=0.22, bg=(255, 255, 255, 255), inner=0.80):
    tile_im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile_im)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=bg)
    inner_size = int(size * inner)
    m = canvas.resize((inner_size, inner_size), Image.LANCZOS)
    off = (size - inner_size) // 2
    tile_im.paste(m, (off, off), m)
    return tile_im

for name, size in (('icon-windows.png', 512), ('icon-macos.png', 512)):
    tile(size).save(os.path.join(RES, name))
    print(name, 'written')

# ⑤ 托盘 ico
frames = [tile(s) for s in (16, 20, 24, 32, 48, 64)]
frames[-1].save(os.path.join(RES, 'tray-windows.ico'), format='ICO', sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64)])
print('tray-windows.ico written')

# ⑥ 侧栏用 64px 白瓦片（base64 内联进 client bundle）
small = tile(64, radius_ratio=0.24, inner=0.84)
buf = io.BytesIO()
small.save(buf, format='PNG', optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode('ascii')
with open(os.path.join(OUT, 'mark-64.base64.txt'), 'w', encoding='ascii') as fh:
    fh.write(b64)
print('sidebar base64 length', len(b64))
