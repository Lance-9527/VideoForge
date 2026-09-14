const fs = require('fs'), path = require('path');
const root = 'C:/Users/Administrator/dsh-install-015rc1/node_modules/.pnpm/@deepseek-ai+dsh-web-app@0._ec05738df2a3daded5b0c92e6228d771';
const hits = [];
function walk(d) {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) { walk(p); continue; }
    if (!/\.(js|mjs|cjs|json)$/.test(e.name)) continue;
    let s;
    try { s = fs.readFileSync(p, 'utf8'); } catch { continue; }
    for (const needle of ['deepseek-flash', 'deepseek-official']) {
      let i = -1;
      while ((i = s.indexOf(needle, i + 1)) >= 0) {
        hits.push({ file: path.relative(root, p), needle, ctx: s.slice(Math.max(0, i - 130), i + 130).replace(/\s+/g, ' ') });
      }
    }
  }
}
walk(root);
const seen = new Set();
for (const h of hits) {
  const k = h.needle + h.ctx.slice(0, 90);
  if (seen.has(k)) continue;
  seen.add(k);
  console.log('[' + h.needle + '] ' + h.file);
  console.log('   ' + h.ctx + '\n');
}
console.log('total hits:', hits.length, ' unique contexts:', seen.size);
