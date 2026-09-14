const fs = require('fs');
const html = fs.readFileSync('D:/VideoForge-dev/frontend/index.html', 'utf8');
const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
let m, i = 0, bad = 0;
const vm = require('vm');
while ((m = re.exec(html))) {
  i++;
  const code = m[1];
  const line = html.slice(0, m.index).split('\n').length;
  try {
    new vm.Script(code, { filename: `inline#${i}@line${line}` });
    console.log(`  OK   inline script #${i} (起始 line ${line}, ${code.length} 字符)`);
  } catch (e) {
    bad++;
    console.log(`  FAIL inline script #${i} (起始 line ${line}): ${e.message}`);
  }
}
for (const f of ['js/api.js', 'js/app.js', 'js/chat.js']) {
  const p = 'D:/VideoForge-dev/frontend/' + f;
  try {
    new vm.Script(fs.readFileSync(p, 'utf8'), { filename: f });
    console.log(`  OK   ${f}`);
  } catch (e) {
    bad++;
    console.log(`  FAIL ${f}: ${e.message}`);
  }
}
console.log(bad === 0 ? '\n全部脚本语法通过' : `\n有 ${bad} 处语法错误`);
process.exit(bad === 0 ? 0 : 1);
