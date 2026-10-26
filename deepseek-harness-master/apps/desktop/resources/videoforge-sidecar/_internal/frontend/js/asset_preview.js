/* ══════════════════════════════════════════════════════════════════
 * VideoForge · 资产页的媒体渲染（2026-09-27 第 54 轮）
 *
 * 为什么要单独一个文件（用户第 2 / 5 / 6 / 9 / 11 条）：
 *   用户的原话是「本地 qwen 出图模型在软件里面没配置好 key，我需要在**资产区域**
 *   可以自行操作重新生成、修改和生成，不止是在对话中生成」，以及
 *   「资产还包括已经生成的视频」「资产要跟剧本的实际情况对上」
 *   「白膜视频我没看到你在做，也没看到结果」。
 *
 *   后端已经把这些都准备好了（`GET /api/projects/{pid}/asset-media`：
 *   角色图 / 场景图 / **已生成的镜头片段** / **白膜** / **成片** /
 *   剧本依据 / 音色），也做了逐项验收（验收项 25/26）。
 *   差的只是"画出来"。
 *
 *   把"画出来"这件事故意**放在独立文件**里，而不是塞进 `app.js` 的资产页：
 *     ① 资产页有 1000+ 行、还牵动 tab/路由，动它的风险远大于收益；
 *     ② 这个文件只做一件事：给一个容器 id + 项目 id，把媒体排出来；
 *     ③ 它坏不坏可以**静态查**（`tests/test_frontend_wiring.py` 会检查
 *        `this.xxx` 有没有写错），下一轮接上去只要两行。
 *
 * 用法（下一轮在资产页里加这两行即可）：
 *     ① index.html 里加：<script src="/js/asset_preview.js"></script>
 *     ② 资产页渲染完（或切 tab）后调用：
 *          AssetPreview.render(State.currentProject && State.currentProject.id, 'assetMediaBox')
 *     ③ 页面里放一个容器：<div id="assetMediaBox"></div>
 * ══════════════════════════════════════════════════════════════════ */
const AssetPreview = {
  /** 最近一次渲染用的数据（调试/自检用） */
  lastData: null,

  /** 把本地路径变成界面能直接用的 URL（后端已经给好了 url，这里只兜底） */
  mediaUrl(path, url) {
    if (url) return url;
    const s = String(path || '');
    if (!s) return '';
    if (/^https?:\/\//.test(s) || s.indexOf('/api/') === 0) return s;
    return '/api/media?path=' + encodeURIComponent(s);
  },

  /** 一条资产的媒体卡（图/视频/白膜/成片都走它） */
  mediaCell(item, label) {
    const acts = (item.pid && item.id && item.kind_of)
      ? '<span class="ap-act" data-pid="' + item.pid + '" data-id="' + item.id
        + '" data-kind="' + item.kind_of + '" title="重新出一张（本机出图约 2~4 分钟）">↻ 重出</span>'
        + '<span class="ap-edit" data-pid="' + item.pid + '" data-name="' + String(item.label_raw || '')
        + '" data-kind="' + item.kind_of + '" title="拿现有图改一处（不换脸）">✎ 改一改</span>'
      : '';
    const url = this.mediaUrl(item.path, item.url);
    const missing = item.exists === false || item.has_image === false;
    const tag = missing ? '<span class="ap-badge ap-missing">文件不在了</span>' : '';
    const name = String(label || item.name || '').slice(0, 40);
    const isVideo = (item.kind === 'video') || /\.(mp4|mov|webm|mkv|m4v)$/i.test(url);
    const inner = missing
      ? '<div class="ap-ph">⚠</div>'
      : (isVideo
        ? '<video src="' + url + '" preload="metadata" muted></video><span class="ap-tag">▶ 视频</span>'
        : '<img src="' + url + '" loading="lazy" alt="' + name + '">');
    return '<div class="ap-cell" data-media="' + url + '" data-kind="'
      + (isVideo ? 'video' : 'image') + '">' + inner + tag + acts
      + '<span class="ap-name">' + name + '</span></div>';
  },

  /** 一块（角色 / 场景 / 片段 / 白膜 / 成片）*/
  section(title, hint, cells, empty) {
    if (!cells) cells = '';
    const body = cells || '<div class="ap-empty">' + (empty || '还没有') + '</div>';
    return '<div class="ap-section"><div class="ap-head"><b>' + title + '</b>'
      + (hint ? '<span class="ap-hint">' + hint + '</span>' : '') + '</div>'
      + '<div class="ap-row">' + body + '</div></div>';
  },

  /** 把一整个项目画进容器。pid 为空时提示用户先选项目。 */
  async render(pid, boxId) {
    const box = document.getElementById(boxId || 'assetMediaBox');
    if (!box) return;
    if (!pid) {
      box.innerHTML = '<div class="ap-empty">先选一个项目 —— 在对话里说「做一个…」'
        + '或者从左边点一个项目，这里就会显示它的角色、场景、片段、白膜和成片。</div>';
      return;
    }
    box.innerHTML = '<div class="ap-loading">正在读资产…</div>';
    let d = null;
    try {
      // ★ 用 `fetch` 直连（**不要**写 `API.request` —— `request` 是 api.js 里的
      //   模块级函数，不在 `API` 对象上；写成 API.request 会抛 TypeError，
      //   表现为"资产页永远空白"。这一处是 `tests/test_frontend_wiring.py`
      //   当场抓出来的，记在这里免得以后有人改回去）。
      const resp = await fetch('/api/projects/' + encodeURIComponent(pid) + '/asset-media');
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error((body && (body.message || body.detail)) || ('HTTP ' + resp.status));
      }
      d = (body && body.data) || body || {};
    } catch (e) {
      box.innerHTML = '<div class="ap-empty">读资产失败：' + String(e.message || e) + '</div>';
      return;
    }
    this.lastData = d;
    const chars = (d.characters || []).map(a => this.mediaCell(
      { url: a.url, path: a.image, has_image: a.has_image, kind: 'image',
        pid: pid, id: a.id, kind_of: 'character', label_raw: a.name },
      a.name + (a.voice_id ? '（' + String(a.voice_id).split(':').pop().replace('Neural', '') + '）' : '')));
    const scenes = (d.scenes || []).map(a => this.mediaCell(
      { url: a.url, path: a.image, has_image: a.has_image, kind: 'image',
        pid: pid, id: a.id, kind_of: 'scene', label_raw: a.name }, a.name));
    const clips = [];
    const previz = [];
    (d.shots || []).forEach(s => {
      (s.videos || []).forEach(v => clips.push(this.mediaCell(v, '#' + (s.index + 1) + ' 片段')));
      if (s.previz) previz.push(this.mediaCell(s.previz, '#' + (s.index + 1) + ' 白膜'));
    });
    const films = (d.films || []).map(f => this.mediaCell(f, f.name));

    box.innerHTML = this.section('角色', '点一下放大看；这里就是你剧本里的人物',
      chars.join(''), '还没有角色 —— 在对话里说「提取角色」')
      + this.section('场景', '', scenes.join(''), '还没有场景')
      + this.section('镜头片段', '已生成的视频（按条计费的那一步的产物）',
        clips.join(''), '还没有生成过片段')
      + this.section('白膜', '花钱前唯一能改运镜的机会（本机渲，免费）',
        previz.join(''), '还没有渲过白膜')
      + this.section('成片', '', films.join(''), '还没有合成过成片')
      + '<div class="ap-foot">' + String(d.note || '') + '</div>'
      + '<div class="ap-prov" id="apProv">正在读本机出图配置…</div>';

    // 点缩略图 → 全屏（与对话里同一套交互）
    if (!box.dataset.bound) {
      box.dataset.bound = '1';
      box.addEventListener('click', (e) => {
        const ed = e.target.closest && e.target.closest('.ap-edit[data-name]');
        if (ed) { e.stopPropagation(); this.editLook(ed, box); return; }
        const act = e.target.closest && e.target.closest('.ap-act[data-id]');
        if (act) { e.stopPropagation(); this.regen(act, box); return; }
        const cell = e.target.closest && e.target.closest('.ap-cell[data-media]');
        if (!cell || !cell.dataset.media) return;
        this.openMedia(cell.dataset.media, cell.dataset.kind || 'image');
      });
    }
    // ★ 本机出图配置：端点和 Key 是**代码内置默认值**，界面上不显示的话
    //   用户会以为"没配好"（这正是他第 2 条的原话）。
    try {
      const pr = await fetch('/api/image-provider');
      const pb = await pr.json().catch(() => ({}));
      const pd = (pb && pb.data) || {};
      const h = pd.health || {};
      const el = document.getElementById('apProv');
      if (el) {
        // ★ 第 61 轮（用户要求「让出图模型可以选择」）：这里放一个**下拉框**，
        //   选完立刻保存（PUT /api/settings）并回读确认 —— 不是只改个界面状态。
        let opts = '';
        let items = [];
        try {
          const lr = await fetch('/api/image-providers');
          const lb = await lr.json().catch(() => ({}));
          items = ((lb && lb.data) || {}).items || [];
        } catch (e) { /* 清单拿不到就只显示当前值 */ }
        items.forEach(function (it) {
          const mark = it.usable ? '' : '（缺 Key）';
          opts += '<option value="' + it.key + '"' + (it.current ? ' selected' : '') + '>'
            + it.label + mark + '</option>';
        });
        el.innerHTML = '出图模型：'
          + (opts ? '<select id="apProvSel" class="ap-sel">' + opts + '</select>'
                  : '<b>' + String(pd.label || pd.resolved || '') + '</b>')
          + '　<span class="ap-hint">地址 <code>' + String(pd.endpoint || '') + '</code>'
          + '　Key ' + String(pd.key_source || '') + '</span>'
          + '　服务 <b>' + (h.ok ? '✓ 在跑' : '✗ 没起来') + '</b>'
          + '<div class="ap-hint">换一个之后，**新的**出图就走它；已经出好的图不受影响。'
          + '（本机一张约 2~4 分钟；云端按张计费）</div>';
        const sel = document.getElementById('apProvSel');
        if (sel) {
          sel.addEventListener('change', async () => {
            const want = sel.value;
            sel.disabled = true;
            try {
              await fetch('/api/settings', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ image_provider: want }),
              });
              // 回读确认真的存上了（这个仓库栽过"保存被静默丢掉"）
              const back = await fetch('/api/settings').then(x => x.json()).catch(() => ({}));
              const got = ((back && back.data) || {}).image_provider || '';
              if (String(got).toLowerCase() !== String(want).toLowerCase()) {
                if (window.Toast && Toast.warn) Toast.warn('没保存上（当前仍是 ' + got + '）');
              } else if (window.Toast && Toast.success) {
                Toast.success('已切换出图模型');
              }
            } catch (e) {
              if (window.Toast && Toast.warn) Toast.warn('切换失败：' + (e.message || e));
            } finally {
              sel.disabled = false;
              this.render(pid, box && box.id);
            }
          });
        }
      }
    } catch (e) { /* 面板拿不到不影响资产显示 */ }

    // 开放给自检脚本（打包冒烟可以用它验证渲染出来了东西）
    try {
      window.__AP_LAST = {
        characters: (d.characters || []).length,
        scenes: (d.scenes || []).length,
        clips: clips.length, previz: previz.length,
        films: (d.films || []).length,
        note: String(d.note || ''),
      };
    } catch (e) { /* 自检钩子失败不影响显示 */ }
  },

  /** ★ 第 2 条：在**资产区自己**重新生成一张（不用回对话里说）。 */
  async regen(btn, box) {
    const pid = btn.dataset.pid;
    const id = btn.dataset.id;
    const kind = btn.dataset.kind;
    if (!pid || !id) return;
    if (!confirm('重新出一张？\n\n本机出图一张大约 2~4 分钟，期间请别关页面。'
      + '\n（出的是参考图；不满意可以再点一次，或到对话里说「把某某改成…」）')) return;
    const old = btn.textContent;
    btn.textContent = '⏳ 出图中…';
    btn.classList.add('ap-busy');
    // 秒表：本机实测 144~265 秒，不能让用户干等
    const t0 = Date.now();
    const timer = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      btn.textContent = '⏳ ' + (s < 60 ? s + ' 秒' : Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒');
    }, 1000);
    try {
      const url = '/api/projects/' + encodeURIComponent(pid) + '/'
        + (kind === 'scene' ? 'scenes' : 'characters') + '/' + encodeURIComponent(id)
        + '/generate-image';
      const r = await fetch(url, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const body = await r.json().catch(() => ({}));
      const ok = r.ok && !(body && body.data && body.data.ok === false);
      const msg = (body && (body.message || (body.data && body.data.error)))
        || (ok ? '出好了' : ('HTTP ' + r.status));
      btn.textContent = ok ? '✓ 已重出' : '✗ 失败';
      btn.title = String(msg).slice(0, 200);
      if (window.Toast && Toast.success && ok) Toast.success('参考图已重出');
      if (window.Toast && Toast.warn && !ok) Toast.warn(String(msg).slice(0, 120));
      await this.render(pid, box && box.id);
    } catch (e) {
      btn.textContent = '✗ 失败';
      btn.title = String(e.message || e).slice(0, 200);
      console.warn('[asset_preview] 重出失败：', e);
    } finally {
      clearInterval(timer);
      setTimeout(() => { btn.textContent = old; btn.classList.remove('ap-busy'); }, 2500);
    }
  },

  /** ★ 第 2 条：一句话改图（只改你要求的地方，其余不动 —— 重画会换脸）。 */
  async editLook(btn, box) {
    const pid = btn.dataset.pid;
    const name = btn.dataset.name || '';
    const kind = btn.dataset.kind || 'character';
    const what = prompt('要改哪一处？（只改这一处，长相/构图/背景保持不动）\n'
      + '例如：换成灰蓝军装，加绑腿　/　背景改成夜晚', '');
    if (!what || !String(what).trim()) return;
    const old = btn.textContent;
    btn.textContent = '⏳ 改图中…';
    const t0 = Date.now();
    const timer = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      btn.textContent = '⏳ ' + (s < 60 ? s + ' 秒' : Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒');
    }, 1000);
    try {
      const r = await fetch('/api/projects/' + encodeURIComponent(pid) + '/refine-look', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind, name: name, instruction: String(what).trim() }),
      });
      const body = await r.json().catch(() => ({}));
      const d = (body && body.data) || {};
      const ok = r.ok && d.ok !== false;
      const msg = String(d.error || body.message || ('HTTP ' + r.status));
      btn.textContent = ok ? '✓ 改好了' : '✗ 失败';
      btn.title = msg.slice(0, 200);
      if (ok && window.Toast && Toast.success) Toast.success('改好了');
      if (!ok && window.Toast && Toast.warn) Toast.warn(msg.slice(0, 120));
      await this.render(pid, box && box.id);
    } catch (e) {
      btn.textContent = '✗ 失败';
      console.warn('[asset_preview] 改图失败：', e);
    } finally {
      clearInterval(timer);
      setTimeout(() => { btn.textContent = old; }, 2500);
    }
  },

  /** 全屏看（灯箱）：和对话里那张图/视频同一套观感 */
  openMedia(url, kind) {
    let ov = document.getElementById('apOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'apOverlay';
      ov.className = 'ap-overlay';
      ov.addEventListener('click', (e) => {
        if (e.target === ov || (e.target.classList && e.target.classList.contains('ap-close'))) {
          ov.style.display = 'none';
          const v = ov.querySelector('video');
          if (v) v.pause();
        }
      });
      document.body.appendChild(ov);
    }
    ov.innerHTML = '<span class="ap-close">✕ 关闭</span>'
      + (kind === 'video' ? '<video src="' + url + '" controls autoplay></video>'
        : '<img src="' + url + '" alt="预览">');
    ov.style.display = 'flex';
  },
};

window.AssetPreview = AssetPreview;


/* ══════════════════════════════════════════════════════════════════
 * 自挂载引导（第 54 轮）：**不改 app.js 也能用**。
 *
 * 为什么这么接：资产页有 1000+ 行、还牵着 tab 与路由，直接去改它的渲染函数
 * 风险大；而这个文件只需要"页面上有一块地方给我画"。
 * 所以：容器不存在就**自己造一个**塞进资产页，然后在合适的时机渲染：
 *   · 页面加载完           · 点左侧「资产」        · 地址栏 hash/page 变化
 * 任何一步失败都只是"这块区域没出现"，**不会影响资产页原有功能**（全程 try/catch）。
 * ══════════════════════════════════════════════════════════════════ */
(function () {
  function currentPid() {
    try {
      if (window.State && State.currentProject && State.currentProject.id) {
        return State.currentProject.id;
      }
    } catch (e) { /* 拿不到就当没有 */ }
    return '';
  }

  function ensureBox() {
    let box = document.getElementById('assetMediaBox');
    if (box) return box;
    const page = document.getElementById('assetPage')
      || (document.getElementById('assetPageTitle') && document.getElementById('assetPageTitle').parentElement);
    if (!page) return null;           // 资产页还没渲染出来，等下一次时机
    box = document.createElement('div');
    box.id = 'assetMediaBox';
    const tabs = document.getElementById('assetTabs') || page.firstElementChild;
    if (tabs && tabs.parentElement === page) page.insertBefore(box, tabs.nextSibling);
    else page.insertBefore(box, page.firstChild);
    return box;
  }

  function draw() {
    try {
      const page = document.getElementById('assetPage');
      // 只在资产页可见时才画（省一次请求，也不打扰别的页）
      const visible = page && page.offsetParent !== null
        && !(page.classList && page.classList.contains('hidden'));
      if (!visible && !document.getElementById('assetMediaBox')) return;
      const box = ensureBox();
      if (box) AssetPreview.render(currentPid(), 'assetMediaBox');
    } catch (e) {
      console.warn('[asset_preview] 自挂载失败（不影响资产页本身）：', e);
    }
  }

  function hook() {
    draw();
    document.addEventListener('click', (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      // 点左侧导航（含「资产」）或资产页的 tab → 稍后重画
      if (t.closest('[data-page]') || t.closest('.nav-item') || t.closest('#assetTabs')
          || t.closest('button') || t.closest('a')) {
        setTimeout(draw, 120);
      }
    }, true);
    window.addEventListener('hashchange', () => setTimeout(draw, 120));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hook);
  } else {
    hook();
  }
})();
