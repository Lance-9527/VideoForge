const fs = require('fs'), zlib = require('zlib');
const f = 'C:/Users/Administrator/.dsh/sessions/--D-KnowledgeVault--/session-12234d56-99b3-4b1a-bf00-a6f01bbc8d3f/session.v3.jsonl.zstd';
const chunks = [];
fs.createReadStream(f)
  .pipe(zlib.createZstdDecompress())
  .on('data', (d) => chunks.push(d))
  .on('error', (e) => { console.log('STREAM-ERR', e.message); report(); })
  .on('end', report);

function report() {
  const buf = Buffer.concat(chunks).toString('utf8');
  console.log('decompressed bytes:', buf.length);
  console.log('--- 首个 zstd 帧内容（前 400 字符） ---');
  console.log(buf.slice(0, 400));
  const tally = (re) => { const o = {}; for (const m of buf.matchAll(re)) o[m[1]] = (o[m[1]] || 0) + 1; return o; };
  console.log('model fields   :', JSON.stringify(tally(/"model"\s*:\s*"([^"]+)"/g)));
  console.log('provider fields:', JSON.stringify(tally(/"provider"\s*:\s*"([^"]+)"/g)));
  console.log('deepseek ids   :', JSON.stringify(tally(/deepseek-[a-z0-9.\-]+/g)));
}
