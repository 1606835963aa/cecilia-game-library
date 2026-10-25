const TOKEN = document.querySelector('meta[name=token]').content;
const POLL_MS = 5000;
const GROUP_ORDER = ['在玩', '未玩', '通关', '弃坑'];
const SOURCES = {
  steam: { label: 'Steam', link: id => `https://store.steampowered.com/app/${id}/` },
  vndb: { label: 'VNDB', link: id => `https://vndb.org/${id}` },
  dlsite: { label: 'DLsite', link: id => `https://www.dlsite.com/maniax/work/=/product_id/${id}.html` },
  itch: { label: 'itch.io', link: id => id },
};

const ICONS = {
  play: '<svg viewBox="0 0 24 24" class="solid"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5Z"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
  home: '<svg viewBox="0 0 24 24"><path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/></svg>',
  star: '<svg viewBox="0 0 24 24" class="solid"><path d="m12 2.8 2.8 5.8 6.3.9-4.6 4.4 1.1 6.3L12 17.2l-5.6 3 1.1-6.3L2.9 9.5l6.3-.9Z"/></svg>',
  image: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>',
  external: '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17.5v.01"/></svg>',
  right: '<svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5 10 17l9-10"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
};

const ANDROID_HELP = {
  keep: '解压出来的 apk 放进安卓文件夹，游戏库里照样能看到。',
  archive: '压缩包不解开，原样挪进安卓文件夹，想玩的时候自己再解。',
  delete: '不玩安卓版就选这个。压缩包和 apk 直接删掉，不进回收站。',
};

const state = {
  games: [],
  statuses: [],
  platform: '全部',
  statusFilter: '全部',
  sort: localGet('sort') || 'recent',
  q: '',
  collapsed: new Set(JSON.parse(localGet('collapsed') || '[]')),
  route: { page: 'home' },
  lastJson: '',
  artKey: '',
  bust: {},        // 图片换过后加个版本号，让窗口重新读图
  pick: null,      // 正在选图片：{ id, kind }
  matchId: null,
  matchResults: [],
  viewer: 0,
  ctx: null,       // 右键菜单：{ id, items }
  focusName: false,
};
const loadedImages = new Set();
const SMALL_ART_WIDTH = 1100;
const smallArt = new Set();

const $ = (sel, root = document) => root.querySelector(sel);

function localGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function localSet(key, val) { try { localStorage.setItem(key, val); } catch { /* 存不了就算了 */ } }

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(method, path, body, contentType) {
  const opts = { method, headers: { 'X-Token': TOKEN } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = contentType || 'application/json';
    opts.body = contentType ? body : JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `出错了（${res.status}）`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), isError ? 4000 : 2400);
}

// ---------------------------------------------------------------- 格式化
function fmtDuration(sec) {
  if (!sec) return '没玩过';
  if (sec < 60) return '不到 1 分钟';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (!h) return `${m} 分钟`;
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

const fmtRunning = sec => (sec < 60 ? '刚开始' : fmtDuration(sec));
const parseTime = str => new Date(str.replace(' ', 'T'));

function fmtAgo(str) {
  if (!str) return '';
  const d = parseTime(str);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return '今天';
  if (days === 1) return '昨天';
  if (days < 7) return `${days} 天前`;
  if (days < 30) return `${Math.floor(days / 7)} 周前`;
  return fmtDate(d);
}

function fmtDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function fmtSize(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i >= 3 ? 2 : 0)} ${units[i]}`;
}

function hue(text) {
  let h = 0;
  for (const ch of text) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

const isCustom = name => (name || '').includes('-custom');
const isRunning = g => g.running_seconds != null;
const canPlay = g => g.platform === 'PC' && g.exe && !g.missing && !isRunning(g);
const firstLetter = g => esc([...g.name.trim()][0] || '?').toUpperCase();
const gradient = g => { const h = hue(g.name); return `linear-gradient(160deg,hsl(${h} 40% 34%),hsl(${(h + 50) % 360} 45% 16%))`; };
const game = id => state.games.find(g => g.id === id);
const genres = g => (g.genres ? g.genres.split('、') : []);

// ---------------------------------------------------------------- 图片
const imgUrl = (g, name) => `/covers/${encodeURIComponent(name)}?v=${state.bust[g.id] || 0}`;

// 图片读完再淡入；已经显示过的图直接显示，避免每次刷新都闪一下
function img(g, name, cls = '', attrs = '') {
  const url = imgUrl(g, name);
  return `<img class="${cls} ${loadedImages.has(url) ? 'ok' : ''}" src="${url}" alt="" ${attrs} onload="imgLoaded(this)" onerror="this.remove()">`;
}

function imgLoaded(el) {
  el.classList.add('ok');
  const url = el.getAttribute('src');
  loadedImages.add(url);
  // 横幅图太小（DLsite、itch.io 的宣传图只有几百像素宽）时，铺满会糊：背景改成模糊的，原图按原样摆在右边
  const isBg = el.classList.contains('bg') || el.classList.contains('sp-bg');
  if (isBg && el.naturalWidth < SMALL_ART_WIDTH && !smallArt.has(url)) {
    smallArt.add(url);
    const box = el.parentElement;
    box.classList.add('small-art');
    box.querySelector('.inset') || el.insertAdjacentHTML('afterend', `<img class="inset ok" src="${url}" alt="">`);
  }
}

function heroArt(g, cls) {
  const url = imgUrl(g, g.hero);
  return smallArt.has(url) ? `${img(g, g.hero, cls)}<img class="inset ok" src="${url}" alt="">` : img(g, g.hero, cls);
}

function blurBg(g, name, cls = 'blur') {
  return `<div class="${cls}" style="background-image:url('${imgUrl(g, name)}')"></div>`;
}

// ---------------------------------------------------------------- 筛选
function matches(g) {
  const q = state.q.trim().toLowerCase();
  return (state.platform === '全部' || g.platform === state.platform) &&
    (!q || [g.name, g.tags, g.genres, g.developer].some(t => (t || '').toLowerCase().includes(q)));
}

const SORTS = {
  recent: (a, b) => (b.last_played || '').localeCompare(a.last_played || '') || b.added_at.localeCompare(a.added_at),
  added: (a, b) => b.added_at.localeCompare(a.added_at),
  name: (a, b) => a.name.localeCompare(b.name, 'zh-CN'),
  playtime: (a, b) => b.play_seconds - a.play_seconds,
  size: (a, b) => b.size_bytes - a.size_bytes,
};

// ---------------------------------------------------------------- 左侧列表
function thumbHtml(g) {
  // 有封面优先用封面：很多游戏的 exe 图标是引擎自带的（比如 Godot 的蓝色小机器人）
  const name = g.cover || g.icon;
  if (name) return img(g, name, 'thumb');
  return `<span class="thumb letter" style="background:${gradient(g)}">${firstLetter(g)}</span>`;
}

function renderSidebar() {
  const list = state.games.filter(matches).sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
  const activeId = state.route.page === 'game' ? state.route.id : null;
  let html = `<a class="side-home ${state.route.page === 'home' ? 'active' : ''}" href="#/">${ICONS.home}<span>全部游戏</span></a>`;
  for (const status of GROUP_ORDER) {
    const items = list.filter(g => g.status === status);
    if (!items.length) continue;
    const collapsed = state.collapsed.has(status);
    html += `<button class="group-head ${collapsed ? 'collapsed' : ''}" data-group="${status}">${ICONS.chevron}${status}<span class="n">${items.length}</span></button>`;
    if (collapsed) continue;
    html += items.map(g => `
      <a data-gid="${g.id}" class="side-item ${g.id === activeId ? 'active' : ''} ${isRunning(g) ? 'running' : ''} ${g.missing ? 'missing' : ''}" href="#/game/${g.id}" title="${esc(g.name)}">
        ${thumbHtml(g)}<span class="label">${esc(g.name)}</span>${isRunning(g) ? '<span class="tag live">运行中</span>' : g.platform !== 'PC' ? `<span class="tag">${esc(g.platform)}</span>` : ''}
      </a>`).join('');
  }
  if (!list.length) html += `<div class="side-empty">${state.games.length ? '没有搜到' : '还没有游戏'}</div>`;
  $('#side-list').innerHTML = html;
}

// ---------------------------------------------------------------- 顶栏
function renderTopbar() {
  const total = state.games.reduce((n, g) => n + g.play_seconds, 0);
  const size = state.games.reduce((n, g) => n + (g.missing ? 0 : g.size_bytes), 0);
  $('#summary').textContent = `${state.games.length} 个游戏 · 玩了 ${fmtDuration(total)} · 占用 ${fmtSize(size)}`;
  const running = state.games.filter(isRunning);
  const np = $('#now-playing');
  np.hidden = !running.length;
  np.innerHTML = running.map(g => `<span class="pulse"></span>正在玩 <b>${esc(g.name)}</b> ${fmtRunning(g.running_seconds)}`).join('　');
  np.dataset.id = running.length ? running[0].id : '';
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === state.route.page));
}

// ---------------------------------------------------------------- 主页
function artHtml(g) {
  let inner;
  if (g.cover) {
    inner = img(g, g.cover, 'full', 'loading="lazy"');
  } else if (g.hero) {
    // 没有竖版封面：横幅大图模糊铺底，原图完整放在中间，下面是 Logo 或名字
    inner = `${blurBg(g, g.hero)}${img(g, g.hero, 'banner', 'loading="lazy"')}
      ${g.logo ? img(g, g.logo, 'logo', 'loading="lazy"') : `<div class="title">${esc(g.name)}</div>`}`;
  } else if (g.icon) {
    inner = `${blurBg(g, g.icon)}${img(g, g.icon, 'icon', 'loading="lazy"')}<div class="title">${esc(g.name)}</div>`;
  } else {
    inner = `<div class="blur" style="background:${gradient(g)};filter:none"></div><div class="letter">${firstLetter(g)}</div><div class="title">${esc(g.name)}</div>`;
  }
  let badge = '';
  if (isRunning(g)) badge = '<span class="badge live">运行中</span>';
  else if (g.scraping) badge = '<span class="badge"><i class="spinner"></i>找资料</span>';
  else if (g.platform === '安卓') badge = '<span class="badge">安卓</span>';
  const fab = canPlay(g) ? `<button class="play-fab" data-play="${g.id}" title="开始游戏">${ICONS.play}</button>` : '';
  return `<div class="art">${inner}<i class="sheen"></i>${badge}${fab}</div>`;
}

function tileHtml(g) {
  const meta = isRunning(g) ? `运行中 · ${fmtRunning(g.running_seconds)}`
    : g.play_seconds ? fmtDuration(g.play_seconds) : g.version ? `v${esc(g.version)}` : fmtSize(g.size_bytes);
  return `
    <a data-gid="${g.id}" class="tile ${g.missing ? 'missing' : ''} ${isRunning(g) ? 'running' : ''}" href="#/game/${g.id}" title="${esc(g.name)}">
      ${artHtml(g)}
      <div class="caption"><span class="name">${esc(g.name)}</span><span class="meta">${meta}</span></div>
    </a>`;
}

function spotlightHtml(g) {
  let bg;
  if (g.hero) bg = heroArt(g, 'sp-bg');
  else if (g.cover || g.icon) bg = blurBg(g, g.cover || g.icon, 'sp-bg soft');
  else bg = `<div class="sp-bg" style="background:${gradient(g)}"></div>`;
  const played = !!g.last_played;
  const meta = [
    isRunning(g) ? `正在运行 · ${fmtRunning(g.running_seconds)}` : played ? `上次运行 ${fmtAgo(g.last_played)}` : `${fmtAgo(g.added_at)}加入`,
    played && `游戏时间 ${fmtDuration(g.play_seconds)}`,
    g.version && `v${esc(g.version)}`,
  ].filter(Boolean).join('<i class="dot"></i>');
  return `
    <section class="spotlight ${g.hero && smallArt.has(imgUrl(g, g.hero)) ? 'small-art' : ''}" data-gid="${g.id}">
      ${bg}<div class="sp-shade"></div>
      ${!g.hero && g.cover ? img(g, g.cover, 'sp-cover') : ''}
      <div class="sp-body">
        <div class="kicker">${played ? '继续游戏' : '最近加入'}</div>
        ${g.logo ? img(g, g.logo, 'sp-logo', `title="${esc(g.name)}"`) : `<h2 class="sp-title">${esc(g.name)}</h2>`}
        <div class="sp-meta">${meta}</div>
        <div class="sp-actions">
          ${g.platform === 'PC' ? `<button class="btn-play" data-play="${g.id}" ${canPlay(g) ? '' : 'disabled'}>${ICONS.play}<span>${isRunning(g) ? '运行中' : '开始游戏'}</span></button>` : ''}
          <a class="btn glass" href="#/game/${g.id}">查看详情</a>
        </div>
      </div>
    </section>`;
}

function renderHome() {
  const view = $('#view');
  if (!state.games.length) {
    view.innerHTML = `<div class="home"><div class="empty-state"><h2>游戏库还是空的</h2>
      <p>把游戏放进 games 文件夹（压缩包会被自动解压工具解开），再点右上角的「扫描」。</p></div></div>`;
    return;
  }
  const base = state.games.filter(matches);
  const byRecent = base.filter(g => !g.missing).sort(SORTS.recent);
  const spot = byRecent.find(isRunning) || byRecent[0];
  const recent = base.filter(g => g.last_played && g !== spot).sort(SORTS.recent).slice(0, 10);
  const wall = base.filter(g => state.statusFilter === '全部' || g.status === state.statusFilter)
    .sort((a, b) => (a.missing - b.missing) || SORTS[state.sort](a, b));
  const scroll = view.scrollTop;
  view.innerHTML = `
    <div class="home">
      ${spot && !state.q ? spotlightHtml(spot) : ''}
      ${recent.length && !state.q ? `
      <div class="shelf-head"><h2>最近游戏</h2></div>
      <div class="recent">${recent.map(g => `<div class="slot"><div class="when">${fmtAgo(g.last_played)}</div>${tileHtml(g)}</div>`).join('')}</div>` : ''}
      <div class="shelf-head">
        <h2>${state.q ? `搜索「${esc(state.q)}」` : '全部游戏'}</h2><span class="n">${wall.length}</span>
        <div class="tools">
          <select id="status-filter" aria-label="状态">
            ${['全部', ...state.statuses].map(s => `<option value="${s}" ${s === state.statusFilter ? 'selected' : ''}>${s === '全部' ? '全部状态' : s}</option>`).join('')}
          </select>
          <select id="sort" aria-label="排序">
            ${[['recent', '按最近玩过'], ['added', '按加入时间'], ['name', '按名字'], ['playtime', '按游戏时间'], ['size', '按占用空间']]
              .map(([v, t]) => `<option value="${v}" ${v === state.sort ? 'selected' : ''}>${t}</option>`).join('')}
          </select>
        </div>
      </div>
      ${wall.length ? `<div class="wall">${wall.map(tileHtml).join('')}</div>` : '<div class="empty-state"><p>没有符合条件的游戏</p></div>'}
    </div>`;
  view.scrollTop = scroll;
}

// ---------------------------------------------------------------- 游戏页面
function heroHtml(g) {
  let bg;
  if (g.hero) bg = heroArt(g, 'bg');
  else if (g.cover || g.icon) bg = blurBg(g, g.cover || g.icon, 'bg soft');
  else bg = `<div class="bg" style="background:${gradient(g)}"></div>`;
  const sub = [g.version && `v${g.version}`, g.engine, g.platform].filter(Boolean).map(esc).join('<i class="dot"></i>');
  return `
    <section class="hero ${g.hero && smallArt.has(imgUrl(g, g.hero)) ? 'small-art' : ''}" id="hero" data-gid="${g.id}">
      ${bg}<div class="shade"></div>
      <button class="back" data-back>${ICONS.back}返回</button>
      <div class="hero-title">
        ${g.logo ? img(g, g.logo, 'logo', `title="${esc(g.name)}"`) : `<h1 id="hero-name">${esc(g.name)}</h1>`}
        <div class="sub">${sub}</div>
      </div>
    </section>`;
}

function actionBarHtml(g) {
  return `
    <div class="action-bar">
      ${g.platform === 'PC'
        ? `<button class="btn-play" id="play-btn" data-play="${g.id}">${ICONS.play}<span>开始游戏</span></button>`
        : '<button class="btn-play" disabled><span>安卓游戏</span></button>'}
      <div class="stat"><span class="k">上次运行</span><span class="v" id="st-last"></span></div>
      <div class="stat"><span class="k">游戏时间</span><span class="v" id="st-time"></span></div>
      <div class="stat"><span class="k">启动次数</span><span class="v" id="st-count"></span></div>
      <div class="spacer"></div>
      <button class="icon-btn" data-match title="匹配资料">${ICONS.search}</button>
      <button class="icon-btn" data-open-folder title="${g.platform === 'PC' ? '打开游戏文件夹' : '在文件夹中显示 apk'}">${ICONS.folder}</button>
    </div>`;
}

function aboutHtml(g) {
  const chips = genres(g).map(t => `<span class="chip">${esc(t)}</span>`).join('');
  let body;
  if (g.description) {
    body = `<p class="desc clamp" id="desc">${esc(g.description)}</p><button class="link" id="desc-more" hidden>展开全部</button>`;
  } else if (g.scraping) {
    body = '<p class="placeholder"><i class="spinner"></i>正在找这个游戏的资料……</p>';
  } else {
    body = `<div class="placeholder">还没有这个游戏的简介。<button class="link" data-match>匹配资料</button>，从 Steam 或 VNDB 找一下。</div>`;
  }
  return `
    <section class="card">
      <h3>关于这款游戏</h3>
      <div class="inner">${body}${chips ? `<div class="chips">${chips}</div>` : ''}</div>
    </section>`;
}

function shotsHtml(g) {
  if (!g.screenshots.length) return '';
  return `
    <section class="card">
      <h3>截图<span class="n">${g.screenshots.length}</span></h3>
      <div class="shots">${g.screenshots.map((s, i) => `<button class="shot" data-shot="${i}">${img(g, s, '', 'loading="lazy"')}</button>`).join('')}</div>
    </section>`;
}

function metaCardHtml(g) {
  const src = SOURCES[g.meta_source];
  const rows = [['开发者', esc(g.developer)], ['发行日期', esc(g.released)],
    ['资料来源', src ? `<a class="ext" href="${src.link(g.meta_id)}" target="_blank" rel="noreferrer">${src.label} ${ICONS.external}</a>` : '']]
    .filter(([, v]) => v);
  return `
    <section class="card">
      <h3>资料</h3>
      <div class="inner">
        ${rows.length ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`
          : `<p class="hint">${g.scraping ? '正在找资料……' : '没有匹配到网上的资料。'}</p>`}
        <div class="row-btns">
          <button class="btn" data-match>${ICONS.search}匹配资料</button>
          <button class="btn" data-rescrape ${g.scraping ? 'disabled' : ''}>${ICONS.refresh}自动获取</button>
        </div>
        ${src ? '<button class="link subtle" data-clear-meta>清除网上的资料</button>' : ''}
      </div>
    </section>`;
}

function pictureHtml(g, kind, label) {
  const name = g[kind];
  const preview = name ? img(g, name) : `<span class="empty">${kind === 'cover' ? '没有封面' : '没有背景'}</span>`;
  return `
    <div class="pic pic-${kind}">
      <div class="frame">${preview}</div>
      <div class="pic-label">${label}</div>
      <div class="pic-btns">
        <button class="link" data-pick="${kind}">更换</button>
        ${isCustom(name) ? `<button class="link subtle" data-reset="${kind}">恢复</button>` : ''}
      </div>
    </div>`;
}

function renderGame(id) {
  const g = game(id);
  const view = $('#view');
  if (!g) {
    view.innerHTML = '<div class="home"><div class="empty-state"><h2>找不到这个游戏</h2><p><a href="#/" class="link">回到主页</a></p></div></div>';
    return;
  }
  const isPC = g.platform === 'PC';
  state.artKey = artKey(g);
  view.innerHTML = `
    ${heroHtml(g)}
    ${actionBarHtml(g)}
    <div class="detail-body">
      <div class="col">
        ${g.suspicious.length || g.ad_files ? `
        <section class="card alert">
          <h3>${ICONS.warn}注意来路不明的文件</h3>
          <div class="inner">
            ${g.suspicious.length ? `<p>这些可执行文件不是游戏主程序，多半是资源站塞进来的，别随便双击：</p>
            <ul>${g.suspicious.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
            ${g.ad_files ? `<p>另有 ${g.ad_files} 个广告链接（.url 文件）。</p>` : ''}
          </div>
        </section>` : ''}
        ${aboutHtml(g)}
        ${shotsHtml(g)}
        <section class="card">
          <h3>游玩记录</h3>
          <ul class="sessions" id="sessions"><li class="muted">加载中……</li></ul>
        </section>
        <section class="card notes">
          <h3>备注</h3>
          <div class="inner field"><textarea data-field="notes" placeholder="玩到哪了、解压密码、攻略链接……">${esc(g.notes)}</textarea></div>
        </section>
      </div>

      <div class="col side-col">
        ${metaCardHtml(g)}
        <section class="card">
          <h3>游戏信息</h3>
          <div class="inner">
            <div class="field"><label>名字</label><input type="text" data-field="name" value="${esc(g.name)}"></div>
            <div class="field"><label>版本</label><input type="text" data-field="version" value="${esc(g.version)}" placeholder="比如 0.5.27"></div>
            <div class="field"><span class="k">状态</span>
              <div class="segmented">${state.statuses.map(s => `<button data-status="${s}" class="${s === g.status ? 'active' : ''}">${s}</button>`).join('')}</div>
            </div>
            <div class="field"><span class="k">评分</span>
              <div class="stars">${[1, 2, 3, 4, 5].map(n => `<button data-rate="${n}" class="${n <= g.rating ? 'on' : ''}" title="${n} 星">${ICONS.star}</button>`).join('')}</div>
            </div>
            <div class="field"><label>我的标签</label><input type="text" data-field="tags" value="${esc(g.tags)}" placeholder="用逗号分开：汉化, 3D, 收藏"></div>
          </div>
        </section>

        <section class="card">
          <h3>外观</h3>
          <div class="inner pictures">
            ${pictureHtml(g, 'cover', '封面')}
            ${pictureHtml(g, 'hero', '背景')}
          </div>
          <p class="hint pad">图片也可以直接拖到页面顶部的大图上，换成背景。</p>
        </section>

        <section class="card">
          <h3>文件</h3>
          <div class="inner">
            ${isPC ? `
            <div class="field"><label for="exe-select">主程序</label>
              <select id="exe-select"><option>${esc(g.exe || '（没找到）')}</option></select>
              <span class="hint">「开始游戏」运行的文件，认错了就换一个。</span>
            </div>` : ''}
            <dl class="kv">
              <dt>引擎</dt><dd>${esc(g.engine || '没认出来')}</dd>
              <dt>占用</dt><dd>${fmtSize(g.size_bytes)}</dd>
              <dt>位置</dt><dd class="path">${esc(g.path)}</dd>
              <dt>加入</dt><dd>${esc(g.added_at.slice(0, 10))}</dd>
            </dl>
          </div>
        </section>

        <div class="danger-zone">
          <div><button class="link" data-remove>从游戏库移除</button>
            <div class="hint">只删游戏库里的记录和游玩时长，不动硬盘上的文件。</div></div>
          <div><button class="link danger" data-delete-files>${ICONS.trash}删除游戏</button>
            <div class="hint">把${g.platform === 'PC' ? '游戏文件夹' : ' apk 文件'}（${fmtSize(g.size_bytes)}）移到回收站，游戏库里的记录也一起删掉。</div></div>
        </div>
      </div>
    </div>`;
  view.scrollTop = 0;
  const desc = $('#desc');
  if (desc && desc.scrollHeight > desc.clientHeight + 2) $('#desc-more').hidden = false;
  updateGameLive();
  loadSessions(g);
  if (isPC) loadExes(g);
  if (state.focusName) {
    state.focusName = false;
    const input = $('[data-field=name]');
    input.scrollIntoView({ block: 'center' });
    input.focus();
    input.select();
  }
}

// 资料或图片变了（比如后台刚找到资料）才需要整页重画
const artKey = g => [g.name, g.cover, g.hero, g.logo, g.description, g.meta_id, g.scraping, g.screenshots.length, state.bust[g.id]].join('|');

function updateGameLive() {
  const g = game(state.route.id);
  if (!g) return;
  const running = isRunning(g);
  $('#st-last').textContent = running ? '现在' : (fmtAgo(g.last_played) || '从未');
  $('#st-time').textContent = fmtDuration(g.play_seconds);
  $('#st-count').textContent = `${g.play_count} 次`;
  const btn = $('#play-btn');
  if (btn) {
    btn.disabled = !canPlay(g);
    btn.classList.toggle('running', running);
    $('span', btn).textContent = running ? `运行中 · ${fmtRunning(g.running_seconds)}` : g.missing ? '文件夹不在了' : !g.exe ? '没有主程序' : '开始游戏';
  }
  if ($('#sessions').dataset.key !== sessionsKey(g)) loadSessions(g);
}

// 启动次数或运行状态变了，游玩记录才需要重新加载
const sessionsKey = g => `${g.play_count}-${isRunning(g)}`;

async function loadSessions(g) {
  const el = $('#sessions');
  el.dataset.key = sessionsKey(g);
  try {
    const { sessions } = await api('GET', `/api/games/${g.id}/sessions`);
    if (state.route.id !== g.id) return;
    el.innerHTML = sessions.length
      ? sessions.map(s => {
        const d = parseTime(s.started_at);
        return `<li><span class="day">${fmtAgo(s.started_at)}</span><span class="time">${fmtDate(d)} ${s.started_at.slice(11, 16)}</span>
          <span class="len">${s.ended_at ? fmtDuration(s.seconds) : '进行中'}</span></li>`;
      }).join('')
      : `<li class="muted">${g.platform === 'PC' ? '还没从游戏库启动过。点「开始游戏」启动，会自动记录时长。' : '安卓游戏不在电脑上运行，没有游玩记录。'}</li>`;
  } catch (e) {
    el.innerHTML = `<li class="muted">${esc(e.message)}</li>`;
  }
}

async function loadExes(g) {
  try {
    const { exes, current } = await api('GET', `/api/games/${g.id}/exes`);
    const sel = $('#exe-select');
    if (!sel || state.route.id !== g.id) return;
    sel.innerHTML = exes.map(e => `<option value="${esc(e)}" ${e === current ? 'selected' : ''}>${esc(e)}</option>`).join('')
      || '<option>（文件夹里没有 exe）</option>';
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- 设置
async function renderSettings() {
  const view = $('#view');
  view.innerHTML = '<div class="page"><h1 class="page-title">设置</h1><div id="settings-body" class="settings"><p class="muted">读取中……</p></div></div>';
  try {
    renderSettingsBody(await api('GET', '/api/settings'));
  } catch (e) {
    $('#settings-body').innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

function toggleRow(key, title, hint, on) {
  return `
    <label class="setting-row">
      <span class="setting-text"><span class="label">${title}</span><span class="hint">${hint}</span></span>
      <input type="checkbox" class="switch" data-setting="${key}" ${on ? 'checked' : ''}>
    </label>`;
}

function renderSettingsBody(s) {
  const body = $('#settings-body');
  if (!body) return;
  body.innerHTML = `
    <section class="card">
      <h3>安卓版</h3>
      <div class="inner">
        <p class="hint">自动解压遇到安卓版（apk）时怎么处理，PC 版不受影响。</p>
        <div class="choices">${Object.entries(s.android_modes).map(([k, label]) => `
          <label class="choice">
            <input type="radio" name="android" value="${k}" ${s.android === k ? 'checked' : ''}>
            <span class="choice-text"><span class="label">${esc(label)}</span><span class="hint">${ANDROID_HELP[k] || ''}</span></span>
          </label>`).join('')}
        </div>
        <div class="setting-row">
          <span class="setting-text"><span class="label">安卓文件夹</span><span class="hint path">${esc(s.android_dir)}</span></span>
          <button class="btn" data-open-android>${ICONS.folder}打开</button>
        </div>
      </div>
    </section>
    <section class="card">
      <h3>自动解压</h3>
      <div class="inner">${toggleRow('delete_archive', '解压成功后删除压缩包', '关掉的话压缩包会留着，占双份空间。', s.delete_archive)}</div>
    </section>
    <section class="card">
      <h3>游戏资料</h3>
      <div class="inner">${toggleRow('auto_scrape', '新游戏自动联网找资料', '会把游戏名发给 Steam 和 VNDB 搜索。关掉后只从游戏文件里取，联网要在游戏页面手动点「匹配资料」。', s.auto_scrape)}</div>
    </section>
    <p class="hint">改完马上保存。正在运行的自动解压，下一轮扫描就会用上新设置。</p>`;
}

async function saveSettings(changes) {
  try {
    renderSettingsBody(await api('PATCH', '/api/settings', changes));
    toast('设置已保存');
  } catch (e) {
    toast(e.message, true);
    renderSettings();
  }
}

// ---------------------------------------------------------------- 游戏操作（游戏页面按钮和右键菜单共用）
function pickImage(id, kind) {
  state.pick = { id, kind };
  $('#image-file').click();
}

function setStatus(id, status) {
  view.querySelectorAll('[data-status]').forEach(b => b.classList.toggle('active', b.dataset.status === status));
  return saveField(id, 'status', status);
}

function rename(id) {
  state.focusName = true;
  if (state.route.page === 'game' && state.route.id === id) renderGame(id);
  else location.hash = `#/game/${id}`;
}

async function rescrape(id, btn) {
  const task = async () => {
    const r = await api('POST', `/api/games/${id}/rescrape`);
    state.bust[id] = Date.now();
    toast(r.message);
    await load(true);
  };
  if (btn) return busyButton(btn, '获取中', task);
  toast(`正在给「${game(id).name}」找资料……`);
  try { await task(); } catch (e) { toast(e.message, true); }
}

function openFolder(id) {
  return api('POST', `/api/games/${id}/open-folder`).catch(err => toast(err.message, true));
}

async function removeGame(id) {
  const ok = await ask(`把「${game(id).name}」从游戏库移除？`, '游玩时长会一起清掉，硬盘上的游戏文件不会删。', '移除');
  if (!ok) return;
  try {
    await api('DELETE', `/api/games/${id}`);
    toast('已移除');
    if (state.route.id === id) location.hash = '#/';
    load(true);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteGame(id) {
  const g = game(id);
  const what = g.platform === 'PC' ? '游戏文件夹' : 'apk 文件';
  const ok = await ask(`删除「${g.name}」？`,
    `${what}（${fmtSize(g.size_bytes)}）会移到回收站，游戏库里的记录、游玩时长和资料也一起删掉。文件可以去回收站还原，游戏库里的记录找不回来。`, '删除');
  if (!ok) return;
  try {
    await api('DELETE', `/api/games/${id}/files`);
    toast(`已删除「${g.name}」，文件在回收站`);
    if (state.route.id === id) location.hash = '#/';
    load(true);
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------- 右键菜单
function menuItems(g) {
  return [
    g.platform === 'PC' && { label: isRunning(g) ? '运行中' : '开始游戏', icon: ICONS.play, strong: true, disabled: !canPlay(g), run: () => launch(g.id) },
    { label: '查看详情', run: () => { location.hash = `#/game/${g.id}`; } },
    '-',
    { label: '标记为', sub: state.statuses.map(s => ({ label: s, checked: g.status === s, run: () => setStatus(g.id, s) })) },
    { label: '管理', sub: [
      { label: '匹配资料…', run: () => openMatch(g.id) },
      { label: '自动获取资料', disabled: g.scraping, run: () => rescrape(g.id) },
      '-',
      { label: '更换封面…', run: () => pickImage(g.id, 'cover') },
      { label: '更换背景…', run: () => pickImage(g.id, 'hero') },
      { label: '重命名', run: () => rename(g.id) },
      '-',
      { label: g.platform === 'PC' ? '打开游戏文件夹' : '在文件夹中显示', run: () => openFolder(g.id) },
    ] },
    '-',
    { label: '从游戏库移除', run: () => removeGame(g.id) },
    { label: '删除游戏…', danger: true, icon: ICONS.trash, run: () => deleteGame(g.id) },
  ].filter(Boolean);
}

function menuHtml(items, path = '') {
  return items.map((it, i) => {
    if (it === '-') return '<div class="ctx-sep"></div>';
    const key = path + i;
    return `
      <div class="ctx-item ${it.danger ? 'danger' : ''} ${it.strong ? 'strong' : ''} ${it.disabled ? 'disabled' : ''} ${it.sub ? 'has-sub' : ''}"
           role="menuitem" data-key="${key}" ${it.disabled ? 'aria-disabled="true"' : ''}>
        <span class="ctx-icon">${it.checked ? ICONS.check : it.icon || ''}</span>
        <span class="ctx-label">${esc(it.label)}</span>
        ${it.sub ? `${ICONS.right}<div class="ctx-sub" role="menu">${menuHtml(it.sub, key + '.')}</div>` : ''}
      </div>`;
  }).join('');
}

function openMenu(id, x, y) {
  const g = game(id);
  if (!g) return;
  const menu = $('#ctx');
  state.ctx = { id, items: menuItems(g) };
  menu.innerHTML = `<div class="ctx-title">${esc(g.name)}</div>${menuHtml(state.ctx.items)}`;
  menu.hidden = false;
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - width - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - height - 8)}px`;
  // 右边放不下子菜单就往左开
  menu.classList.toggle('flip', x + width + 200 > innerWidth);
}

function closeMenu() {
  if ($('#ctx').hidden) return;
  $('#ctx').hidden = true;
  state.ctx = null;
}

function menuItemByKey(key) {
  let items = state.ctx.items;
  let item;
  for (const i of key.split('.')) {
    item = items[+i];
    items = item.sub;
  }
  return item;
}

// ---------------------------------------------------------------- 匹配资料
function openMatch(id) {
  state.matchId = id;
  $('#match-q').value = game(id).name;
  $('#match').showModal();
  runSearch();
}

async function runSearch() {
  const q = $('#match-q').value.trim();
  const box = $('#match-results');
  if (!q) return;
  box.innerHTML = Array.from({ length: 4 }, () => '<div class="m-row skeleton"><div class="m-thumb"></div><div class="m-info"><i></i><i></i></div></div>').join('');
  try {
    const { results, errors } = await api('GET', `/api/search?q=${encodeURIComponent(q)}`);
    if (q !== $('#match-q').value.trim()) return;
    state.matchResults = results;
    const g = game(state.matchId);
    const note = errors.length ? `<div class="m-note">${errors.map(s => SOURCES[s].label).join('、')} 这次没连上，结果可能不全。</div>` : '';
    box.innerHTML = note + (results.length ? results.map((r, i) => {
      const current = g && g.meta_source === r.source && g.meta_id === r.id;
      const meta = [r.year, r.developer].filter(Boolean).map(esc).join('<i class="dot"></i>');
      return `
        <div class="m-row ${current ? 'current' : ''}">
          <div class="m-thumb ${r.source}">${r.thumb ? `<img src="${esc(r.thumb)}" alt="" referrerpolicy="no-referrer" onload="this.classList.add('ok')" onerror="this.remove()">` : ''}</div>
          <div class="m-info">
            <div class="m-title">${esc(r.title)}</div>
            ${r.alt && r.alt !== r.title ? `<div class="m-alt">${esc(r.alt)}</div>` : ''}
            <div class="m-meta"><span class="src ${r.source}">${SOURCES[r.source].label}</span>${meta}</div>
          </div>
          ${current ? '<span class="m-current">正在用</span>' : `<button class="btn btn-primary" data-use="${i}">用这个</button>`}
        </div>`;
    }).join('') : '<div class="m-empty">没有搜到。换个写法试试，比如英文原名、去掉版本号；也可以直接粘贴游戏的商店页面链接。</div>');
  } catch (e) {
    box.innerHTML = `<div class="m-empty">${esc(e.message)}</div>`;
  }
}

async function useMatch(btn) {
  const r = state.matchResults[+btn.dataset.use];
  const id = state.matchId;
  $('#match-results').querySelectorAll('button').forEach(b => { b.disabled = true; });
  btn.innerHTML = '<i class="spinner"></i>下载中';
  try {
    await api('POST', `/api/games/${id}/match`, { source: r.source, id: r.id });
    state.bust[id] = Date.now();
    $('#match').close();
    toast(`已换成「${r.title}」的资料`);
    await load(true);
  } catch (e) {
    toast(e.message, true);
    runSearch();
  }
}

// ---------------------------------------------------------------- 截图查看
function openViewer(i) {
  state.viewer = i;
  showShot();
  $('#viewer').showModal();
}

function showShot() {
  const g = game(state.route.id);
  const n = g.screenshots.length;
  state.viewer = (state.viewer + n) % n;
  $('#viewer-img').src = imgUrl(g, g.screenshots[state.viewer]);
  $('#viewer-count').textContent = `${state.viewer + 1} / ${n}`;
}

// ---------------------------------------------------------------- 路由与刷新
function parseRoute() {
  const m = location.hash.match(/^#\/game\/(\d+)/);
  if (m) state.route = { page: 'game', id: +m[1] };
  else state.route = { page: location.hash === '#/settings' ? 'settings' : 'home' };
}

function renderView() {
  if (state.route.page === 'game') renderGame(state.route.id);
  else if (state.route.page === 'settings') renderSettings();
  else renderHome();
}

function renderAll(full) {
  renderTopbar();
  renderSidebar();
  if (state.route.page === 'settings') return;
  if (full || state.route.page === 'home') return renderView();
  // 游戏页面：资料变了且没在打字时整页重画，否则只更新时长、按钮这些会变的地方
  const g = game(state.route.id);
  const typing = document.activeElement && document.activeElement.matches('#view input, #view textarea');
  if (g && artKey(g) !== state.artKey && !typing) renderGame(g.id);
  else updateGameLive();
}

async function load(full = false) {
  try {
    const data = await api('GET', '/api/games');
    const json = JSON.stringify(data);
    if (!full && json === state.lastJson) return;
    state.lastJson = json;
    state.games = data.games;
    state.statuses = data.statuses;
    renderAll(full);
  } catch (e) {
    toast(e.message, true);
  }
}

async function launch(id) {
  try {
    await api('POST', `/api/games/${id}/launch`);
    toast(`正在启动「${game(id).name}」`);
    setTimeout(() => load(), 1200);
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveField(id, field, value) {
  try {
    await api('PATCH', `/api/games/${id}`, { [field]: value });
    Object.assign(game(id), { [field]: value });
    state.lastJson = '';
    renderTopbar();
    renderSidebar();
    if (state.route.page === 'home') renderHome();
    if (field === 'name' && $('#hero-name')) $('#hero-name').textContent = value;
    toast('已保存');
  } catch (e) {
    toast(e.message, true);
  }
}

async function uploadImage(id, kind, file) {
  if (!file || !file.type.startsWith('image/')) return toast('请选一张图片', true);
  try {
    await api('POST', `/api/games/${id}/image/${kind}`, await file.arrayBuffer(), file.type);
    state.bust[id] = Date.now();
    toast(kind === 'cover' ? '封面已更换' : '背景已更换');
    await load(true);
  } catch (e) {
    toast(e.message, true);
  }
}

async function busyButton(btn, text, task) {
  const html = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<i class="spinner"></i>${text}`;
  try {
    await task();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = html;
  }
}

// ---------------------------------------------------------------- 事件
window.addEventListener('hashchange', () => { parseRoute(); renderSidebar(); renderTopbar(); renderView(); });

$('#side-list').addEventListener('click', e => {
  const head = e.target.closest('[data-group]');
  if (!head) return;
  const s = head.dataset.group;
  state.collapsed.has(s) ? state.collapsed.delete(s) : state.collapsed.add(s);
  localSet('collapsed', JSON.stringify([...state.collapsed]));
  renderSidebar();
});

$('#search').addEventListener('input', e => {
  state.q = e.target.value;
  renderSidebar();
  if (state.route.page === 'home') renderHome();
});
$('#platform').addEventListener('change', e => {
  state.platform = e.target.value;
  renderSidebar();
  if (state.route.page === 'home') renderHome();
});

$('#now-playing').addEventListener('click', e => {
  const id = e.currentTarget.dataset.id;
  if (id) location.hash = `#/game/${id}`;
});

$('#scan').addEventListener('click', async e => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.classList.add('spinning');
  try {
    const r = await api('POST', '/api/scan', { full: true });
    toast(r.added.length ? `新入库 ${r.added.length} 个：${r.added.join('、')}，正在后台找资料` : `扫描完成，没有新游戏（共 ${r.total} 个）`);
    await load(true);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.classList.remove('spinning');
  }
});

const view = $('#view');

view.addEventListener('click', async e => {
  const playBtn = e.target.closest('[data-play]');
  if (playBtn) {
    e.preventDefault();
    return launch(+playBtn.dataset.play);
  }
  const t = e.target.closest('button');
  if (!t) return;
  const id = state.route.id;
  if (t.hasAttribute('data-back')) return history.length > 1 ? history.back() : (location.hash = '#/');
  if (t.dataset.status) return setStatus(id, t.dataset.status);
  if (t.hasAttribute('data-open-android')) return api('POST', '/api/open-android-dir').catch(err => toast(err.message, true));
  if (t.dataset.rate) {
    const n = +t.dataset.rate === game(id).rating ? 0 : +t.dataset.rate;
    view.querySelectorAll('[data-rate]').forEach(b => b.classList.toggle('on', +b.dataset.rate <= n));
    return saveField(id, 'rating', n);
  }
  if (t.dataset.shot) return openViewer(+t.dataset.shot);
  if (t.id === 'desc-more') {
    $('#desc').classList.toggle('clamp');
    t.textContent = $('#desc').classList.contains('clamp') ? '展开全部' : '收起';
    return;
  }
  if (t.hasAttribute('data-match')) return openMatch(id);
  if (t.hasAttribute('data-rescrape')) return rescrape(id, t);
  if (t.hasAttribute('data-clear-meta')) {
    const ok = await ask('清除网上找来的资料？', '简介、截图和网上下载的图片会删掉，从游戏文件里取的背景和 Logo 会保留。', '清除');
    if (!ok) return;
    return busyButton(t, '清除中', async () => {
      await api('POST', `/api/games/${id}/clear-meta`);
      state.bust[id] = Date.now();
      await load(true);
    });
  }
  if (t.hasAttribute('data-open-folder')) return openFolder(id);
  if (t.dataset.pick) return pickImage(id, t.dataset.pick);
  if (t.dataset.reset) {
    try {
      await api('DELETE', `/api/games/${id}/image/${t.dataset.reset}`);
      state.bust[id] = Date.now();
      await load(true);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  if (t.hasAttribute('data-remove')) return removeGame(id);
  if (t.hasAttribute('data-delete-files')) return deleteGame(id);
});

view.addEventListener('change', async e => {
  const t = e.target;
  const id = state.route.id;
  if (t.id === 'sort') { state.sort = t.value; localSet('sort', t.value); return renderHome(); }
  if (t.id === 'status-filter') { state.statusFilter = t.value; return renderHome(); }
  if (t.name === 'android') return saveSettings({ android: t.value });
  if (t.dataset.setting) return saveSettings({ [t.dataset.setting]: t.checked });
  if (t.dataset.field) {
    const value = t.value.trim();
    if (value === (game(id)[t.dataset.field] ?? '')) return;
    return saveField(id, t.dataset.field, value);
  }
  if (t.id === 'exe-select') {
    try {
      await api('POST', `/api/games/${id}/exe`, { exe: t.value });
      toast('主程序已更换');
      load(true);
    } catch (err) {
      toast(err.message, true);
    }
  }
});

view.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.matches('input[data-field]')) e.target.blur();
});

$('#image-file').addEventListener('change', e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (state.pick) uploadImage(state.pick.id, state.pick.kind, file);
});

view.addEventListener('dragover', e => {
  const hero = e.target.closest('#hero');
  if (!hero) return;
  e.preventDefault();
  hero.classList.add('drop');
});
view.addEventListener('dragleave', e => {
  const hero = e.target.closest('#hero');
  if (hero && !hero.contains(e.relatedTarget)) hero.classList.remove('drop');
});
view.addEventListener('drop', e => {
  const hero = e.target.closest('#hero');
  if (!hero) return;
  e.preventDefault();
  hero.classList.remove('drop');
  uploadImage(state.route.id, 'hero', e.dataTransfer.files[0]);
});

// 文件拖到背景大图以外的地方时，窗口默认会直接打开这个文件、把游戏库页面换掉
for (const type of ['dragover', 'drop']) {
  window.addEventListener(type, e => e.preventDefault());
}

$('#match-form').addEventListener('submit', e => { e.preventDefault(); runSearch(); });
$('#match').addEventListener('click', e => {
  const use = e.target.closest('[data-use]');
  if (use) return useMatch(use);
  if (e.target.closest('[data-close]') || e.target === e.currentTarget) e.currentTarget.close();
});

$('#viewer').addEventListener('click', e => {
  const step = e.target.closest('[data-step]');
  if (step) { state.viewer += +step.dataset.step; return showShot(); }
  if (e.target.closest('[data-close]') || e.target === e.currentTarget) e.currentTarget.close();
});
$('#viewer').addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { state.viewer += e.key === 'ArrowLeft' ? -1 : 1; showShot(); }
});

document.addEventListener('contextmenu', e => {
  const target = e.target.closest('[data-gid]');
  if (!target || e.target.closest('input, textarea')) return;
  e.preventDefault();
  openMenu(+target.dataset.gid, e.clientX, e.clientY);
});
$('#ctx').addEventListener('click', e => {
  const el = e.target.closest('.ctx-item');
  if (!el || el.classList.contains('disabled') || el.classList.contains('has-sub')) return;
  const item = menuItemByKey(el.dataset.key);
  closeMenu();
  item.run();
});
// 子菜单超出窗口底部就往上挪
$('#ctx').addEventListener('mouseover', e => {
  const item = e.target.closest('.ctx-item.has-sub');
  if (!item) return;
  const sub = item.querySelector('.ctx-sub');
  sub.style.top = '';
  const over = sub.getBoundingClientRect().bottom - innerHeight + 8;
  if (over > 0) sub.style.top = `${-5 - over}px`;
});
document.addEventListener('mousedown', e => { if (!e.target.closest('#ctx')) closeMenu(); });
for (const type of ['blur', 'resize', 'hashchange']) window.addEventListener(type, closeMenu);
document.addEventListener('wheel', e => { if (!e.target.closest('#ctx')) closeMenu(); }, { passive: true });

function ask(title, text, okText) {
  const dialog = $('#confirm');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  $('#confirm-ok').textContent = okText;
  dialog.returnValue = '';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true }));
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && state.ctx) return closeMenu();
  if (e.target.matches('input, textarea, select') || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
  if (e.key === 'Escape' && state.route.page === 'game') location.hash = '#/';
});

parseRoute();
load(true);
setInterval(load, POLL_MS);
