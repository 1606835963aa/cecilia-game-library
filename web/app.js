const TOKEN = document.querySelector('meta[name=token]').content;
const POLL_MS = 5000;
const GROUP_ORDER = ['在玩', '未玩', '通关', '弃坑'];
const SOURCES = {
  steam: { label: 'Steam', link: id => `https://store.steampowered.com/app/${id}/` },
  vndb: { label: 'VNDB', link: id => `https://vndb.org/${id}` },
  dlsite: { label: 'DLsite', link: id => `https://www.dlsite.com/maniax/work/=/product_id/${id}.html` },
  itch: { label: 'itch.io', link: id => id },
  subscribestar: { label: 'SubscribeStar', link: id => id },
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
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  archive: '<svg viewBox="0 0 24 24"><path d="M21 8v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8"/><path d="M1 3h22v5H1zM10 12h4"/></svg>',
  phone: '<svg viewBox="0 0 24 24"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M11 18h2"/></svg>',
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  close: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
};

const ANDROID_HELP = {
  keep: '解压出来的 apk 放进安卓文件夹，游戏库里照样能看到。',
  archive: '压缩包不解开，原样挪进安卓文件夹，想玩的时候自己再解。',
  delete: '不玩安卓版就选这个。压缩包和 apk 直接删掉，不进回收站。',
};

const state = {
  games: [],
  statuses: [],
  statusFilter: '全部',
  sort: localGet('sort') || 'added',
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
const canPlay = g => !g.is_wishlist && g.platform === 'PC' && g.exe && !g.missing && !isRunning(g);
const firstLetter = g => esc([...g.name.trim()][0] || '?').toUpperCase();
// 没有任何图的游戏用名字算一个颜色；纸艺皮肤用浅一些的粉彩色
const gradient = g => {
  const h = hue(g.name);
  return document.documentElement.dataset.theme === 'paper'
    ? `linear-gradient(160deg,hsl(${h} 38% 86%),hsl(${(h + 40) % 360} 30% 72%))`
    : `linear-gradient(160deg,hsl(${h} 40% 34%),hsl(${(h + 50) % 360} 45% 16%))`;
};
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
  return !q || [g.name, g.tags, g.genres, g.developer].some(t => (t || '').toLowerCase().includes(q));
}

const SORTS = {
  recent: (a, b) => (b.last_played || '').localeCompare(a.last_played || '') || SORTS.added(a, b),
  added: (a, b) => b.added_at.localeCompare(a.added_at) || b.id - a.id,
  name: (a, b) => a.name.localeCompare(b.name, 'zh-CN'),
  playtime: (a, b) => b.play_seconds - a.play_seconds,
  size: (a, b) => b.size_bytes - a.size_bytes,
};

function compareGames(a, b) {
  return (a.missing - b.missing) || (SORTS[state.sort] || SORTS.added)(a, b);
}

// ---------------------------------------------------------------- 左侧列表
function thumbHtml(g) {
  // 有封面优先用封面：很多游戏的 exe 图标是引擎自带的（比如 Godot 的蓝色小机器人）
  const name = g.cover || g.icon;
  if (name) return img(g, name, 'thumb');
  return `<span class="thumb letter" style="background:${gradient(g)}">${firstLetter(g)}</span>`;
}

function renderSidebar() {
  const list = state.games.filter(matches).sort(compareGames);
  const activeId = state.route.page === 'game' ? state.route.id : null;
  let html = `<a class="side-home ${state.route.page === 'home' ? 'active' : ''}" href="#/">${ICONS.home}<span>全部游戏</span></a>`;
  for (const status of ['愿望单', ...GROUP_ORDER, '安卓']) {
    const items = list.filter(g => status === '愿望单' ? g.is_wishlist
      : !g.is_wishlist && (status === '安卓' ? g.platform === '安卓' : g.platform !== '安卓' && g.status === status));
    if (!items.length) continue;
    const collapsed = state.collapsed.has(status);
    html += `<button class="group-head ${collapsed ? 'collapsed' : ''}" data-group="${status}" aria-expanded="${!collapsed}">${ICONS.chevron}${status}<span class="n">${items.length}</span></button>`;
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
  const meta = g.is_wishlist ? `愿望单${g.latest_version ? ' · v' + esc(g.latest_version) : ''}${g.version_comparison === 'newer' ? ' · 有新版' : ''}` : isRunning(g) ? `运行中 · ${fmtRunning(g.running_seconds)}`
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
          ${g.platform === 'PC' && !g.is_wishlist ? `<button class="btn-play" data-play="${g.id}" ${canPlay(g) ? '' : 'disabled'}>${ICONS.play}<span>${isRunning(g) ? '运行中' : '开始游戏'}</span></button>` : ''}
          <a class="btn glass" href="#/game/${g.id}">查看详情</a>
        </div>
      </div>
    </section>`;
}

function renderHome() {
  const view = $('#view');
  const wishPage = state.route.page === 'wishlist';
  if (!state.games.length && !wishPage) {
    view.innerHTML = `<div class="home"><div class="empty-state"><h2>游戏库还是空的</h2>
      <p>把压缩包放进下载文件夹，再点右上角「刷新」；也可以先去愿望单记录想玩的游戏。</p><a class="btn" href="#/wishlist">记录想玩的游戏</a></div></div>`;
    return;
  }
  const base = state.games.filter(matches).filter(g => !wishPage || g.is_wishlist);
  const byRecent = base.filter(g => !g.missing && !g.is_wishlist).sort(SORTS.recent);
  const spot = byRecent.find(isRunning) || byRecent[0];
  const recent = base.filter(g => g.last_played && g !== spot).sort(SORTS.recent).slice(0, 10);
  const wall = base.filter(g => wishPage || state.statusFilter === '全部' || (state.statusFilter === '愿望单' ? g.is_wishlist : !g.is_wishlist && g.status === state.statusFilter))
    .sort(compareGames);
  const scroll = view.scrollTop;
  view.innerHTML = `
    <div class="home">
      ${spot && !state.q ? spotlightHtml(spot) : ''}
      ${recent.length && !state.q ? `
      <div class="shelf-head"><h2>最近游戏</h2></div>
      <div class="recent">${recent.map(g => `<div class="slot"><div class="when">${fmtAgo(g.last_played)}</div>${tileHtml(g)}</div>`).join('')}</div>` : ''}
      <div class="shelf-head">
        <h2>${state.q ? `搜索「${esc(state.q)}」` : wishPage ? '愿望单' : '全部游戏'}</h2><span class="n">${wall.length}</span>
        <div class="tools">
          ${wishPage ? '<button class="btn btn-primary" data-add-wish>添加游戏</button>' : `<select id="status-filter" aria-label="状态">
            ${['全部', '愿望单', ...state.statuses].map(s => `<option value="${s}" ${s === state.statusFilter ? 'selected' : ''}>${s === '全部' ? '全部状态' : s}</option>`).join('')}
          </select>`}
        </div>
      </div>
      ${wall.length ? `<div class="wall">${wall.map(tileHtml).join('')}</div>` : `<div class="empty-state"><p>${wishPage ? '先搜名字、保存资料，想玩的时候再下载。' : '没有符合条件的游戏'}</p></div>`}
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
  if (g.is_wishlist) return `<div class="action-bar wish-actions">
    <span class="wish-label">愿望单 · 尚未下载</span>
    <button class="btn btn-primary" data-wish-check>检查版本</button>
    <button class="btn" data-wish-attach>关联已下载游戏</button>
    <div class="spacer"></div><a class="btn" href="${esc(SOURCES[g.meta_source].link(g.meta_id))}" target="_blank" rel="noopener noreferrer">${ICONS.external}打开官网</a>
  </div>`;
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
    body = `<div class="placeholder">还没有这个游戏的简介。点<button class="link" data-match>匹配资料</button>搜索，或粘贴游戏官网、作者主页的链接。</div>`;
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
        ${src && !g.is_wishlist ? '<button class="link subtle" data-clear-meta>清除网上的资料</button>' : ''}
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

// 两个版本并排优先，检查为主要操作，历史为次要；沿用文字色和 12、24px 间距。
function versionCard(g) {
  if (!g.wishlisted_at) return '';
  const labels = {newer: '发现新版', same: '与加入时相同', older: '官网版本号较小', changed: '版本有变化', unknown: '待确认版本'};
  const note = g.version_status === 'unknown' ? '这次官网没有明确标注版本，保留上次记录。' : g.version_status === 'error' ? '这次没有检查成功，保留上次记录。' : g.version_status === 'manual' ? '最近版本由你手动填写。' : g.version_evidence;
  return `<section class="card version-card">
    <h3>版本对比 <span class="version-result">${labels[g.version_comparison]}</span></h3>
    <div class="inner">
      <div class="version-pair">
        <div class="field"><label for="wish-start">加入时版本</label><input id="wish-start" type="text" data-field="wish_version" value="${esc(g.wish_version)}" maxlength="80" placeholder="未提供，可手动补填"></div>
        <div class="field"><label for="wish-current">最近记录版本</label><input id="wish-current" type="text" data-wish-version value="${esc(g.latest_version)}" maxlength="80" placeholder="未提供，可手动补填"></div>
      </div>
      <p class="wish-help">${esc(note || '官网未提供版本号。')}</p>
      <p class="wish-help">${g.wishlisted_at.slice(0,10)} 加入${g.version_checked_at ? ' · 最近检查 ' + esc(g.version_checked_at) : ''}</p>
      ${!g.is_wishlist ? `<button class="btn" data-wish-check ${g.scraping ? 'disabled' : ''}>${ICONS.refresh}检查版本</button>` : ''}
      ${g.version_history.length ? `<details class="version-history"><summary>版本记录（${g.version_history.length}）</summary><ul>${[...g.version_history].reverse().map(item => `<li><b>${esc(item.version)}</b><span>${esc(item.checked_at)} · ${esc(item.evidence || '官网')}</span></li>`).join('')}</ul></details>` : ''}
    </div>
  </section>`;
}

function renderGame(id) {
  const g = game(id);
  const view = $('#view');
  if (!g) {
    view.innerHTML = '<div class="home"><div class="empty-state"><h2>找不到这个游戏</h2><p><a href="#/" class="link">回到主页</a></p></div></div>';
    return;
  }
  const isPC = g.platform === 'PC' && !g.is_wishlist;
  state.artKey = artKey(g);
  view.innerHTML = `
    ${heroHtml(g)}
    ${actionBarHtml(g)}
    <div class="detail-body">
      <div class="col">
        ${versionCard(g)}
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
            ${!g.is_wishlist ? `<div class="field"><label>${g.wishlisted_at ? '本地版本' : '版本'}</label><input type="text" data-field="version" value="${esc(g.version)}" placeholder="比如 0.5.27"></div>` : ''}
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

        ${!g.is_wishlist ? `<section class="card">
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
        </section>` : ''}

        <div class="danger-zone">
          <div><button class="link" data-remove>从游戏库移除</button>
            <div class="hint">只删游戏库里的记录和游玩时长，不动硬盘上的文件。</div></div>
          ${!g.is_wishlist ? `<div><button class="link danger" data-delete-files>${ICONS.trash}删除游戏</button>
            <div class="hint">把${g.platform === 'PC' ? '游戏文件夹' : ' apk 文件'}（${fmtSize(g.size_bytes)}）移到回收站，游戏库里的记录也一起删掉。</div></div>` : ''}
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
const artKey = g => [g.name, g.cover, g.hero, g.logo, g.description, g.meta_id, g.scraping, g.is_wishlist, g.wish_version, g.latest_version, g.version_status, g.version_checked_at, g.screenshots.length, state.bust[g.id]].join('|');

function updateGameLive() {
  const g = game(state.route.id);
  if (!g) return;
  if (g.is_wishlist) return;
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

let savedPasswords = [];

// 目录用途和路径优先，打开、更改为次要操作；沿用 --text、--text-2 和 8、12、24px 间距。
function directoryRow(kind, label, path, canPick) {
  return `<div class="directory-row">
    <span class="setting-text"><span class="label">${label}</span><span class="directory-path">${esc(path)}</span></span>
    <div class="directory-actions">
      <button class="btn" data-open-dir="${kind}">${ICONS.folder}打开</button>
      <button class="btn" data-pick-dir="${kind}" ${canPick ? '' : 'disabled title="请在软件窗口中更改"'}>更改</button>
    </div>
  </div>`;
}

function renderSettingsBody(s) {
  const body = $('#settings-body');
  if (!body) return;
  savedPasswords = s.passwords;
  body.innerHTML = `
    <section class="card">
      <h3>皮肤</h3>
      <div class="inner">
        <div class="skins">${Object.entries(s.themes).map(([k, label]) => `
          <label class="skin">
            <input type="radio" name="theme" value="${k}" ${s.theme === k ? 'checked' : ''}>
            <span class="skin-preview ${k}"><i class="p-nav"></i><i class="p-side"></i><i class="p-hero"></i><i class="p-tile"></i><i class="p-tile"></i><i class="p-tile"></i></span>
            <span class="skin-name">${esc(label)}</span>
          </label>`).join('')}
        </div>
      </div>
    </section>
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
    <section class="card extract-settings">
      <h3>自动解压</h3>
      <div class="inner">
        <div class="directory-list">
          ${directoryRow('downloads', '下载文件夹 · 压缩包放这里', s.download_dirs.join('、'), s.native_window)}
          ${directoryRow('games', '游戏文件夹 · 解压结果放这里', s.games_dir, s.native_window)}
        </div>
        <p class="hint">新解压的游戏统一放进 Game_000001 这样的编号目录，游戏库仍显示游戏名。</p>
        ${toggleRow('delete_archive', '解压成功后删除压缩包', '所有层都解开后才删除；关掉则原包保留在下载文件夹。', s.delete_archive)}
      </div>
    </section>
    <section class="card">
      <h3>解压密码 <span class="n">${s.passwords.length}</span></h3>
      <div class="inner">
        <p class="hint password-hint">每一层分别试密码，内外层可以不同；也会从包内说明里找线索。导入时手动输对的密码会记到这里。</p>
        ${s.passwords.length ? `<ol class="pw-list">${s.passwords.map((p, i) => `
          <li><code>${esc(p)}</code><button class="icon-btn" data-pw-remove="${i}" title="删除这个密码" aria-label="删除这个密码">${ICONS.trash}</button></li>`).join('')}
        </ol>` : '<p class="muted">还没有密码。下载页面上写的解压密码，可以加在这里。</p>'}
        <form id="pw-form" class="pw-add field">
          <input type="text" id="pw-input" placeholder="输入解压密码，回车添加" autocomplete="off" spellcheck="false">
          <button class="btn">${ICONS.plus}添加</button>
        </form>
      </div>
    </section>
    <section class="card">
      <h3>游戏资料</h3>
      <div class="inner">${toggleRow('auto_scrape', '自动联网找资料', '从资料网站和订阅平台公开主页找资料；点刷新也会补查没匹配上的游戏。关掉后只读取游戏文件，需要时可以手动匹配。', s.auto_scrape)}</div>
    </section>
    <section class="card" id="itch-account-card">
      <h3>itch.io 账号</h3>
      <div class="inner" id="itch-account"></div>
    </section>
    <p class="hint">改完马上保存。正在运行的自动解压，下一轮扫描就会用上新设置。</p>`;
  renderItchAccount(s.itch_account);
}

// 登录状态优先，登录或官网设置是主要操作；沿用 --text、--text-2 和 8、12、16px 间距。
const itchUi = { account: null, json: '', polling: false };

function renderItchAccount(account) {
  if (!account) return;
  itchUi.account = account;
  itchUi.json = JSON.stringify(account);
  const container = $('#itch-account');
  if (!container) return;
  const signed = account.status === 'signed_in';
  let hint = signed ? '搜索会使用这个账号，自动找资料和手动匹配都能用。' : '在官网窗口登录后，开启成人内容并保存。';
  if (signed && account.adult !== true) hint = '在官网设置中开启成人内容并保存，再点「检查登录」。';
  if (!account.available) hint = '请用「打开游戏库.bat」打开软件窗口后登录。';
  const disabled = !account.available || account.clearing;
  container.innerHTML = `
    <div class="itch-status" role="status" aria-live="polite">
      ${account.checking ? '<i class="spinner"></i>' : `<span class="itch-dot ${signed ? 'signed' : ''}"></span>`}
      <span>${esc(account.checking ? '正在检查登录……' : account.message)}</span>
    </div>
    <p class="itch-hint">${esc(hint)}</p>
    <div class="itch-actions">
      <button class="btn btn-primary" data-itch="${signed ? 'settings' : 'login'}" ${disabled ? 'disabled' : ''}>${signed ? '官网设置' : account.status === 'expired' ? '重新登录' : '登录 itch.io'}</button>
      <button class="btn" data-itch="check" ${disabled || account.checking ? 'disabled' : ''}>检查登录</button>
      <button class="link subtle" data-itch="logout" ${disabled ? 'disabled' : ''}>退出并清除登录</button>
    </div>`;
}

async function pollItchAccount() {
  if (itchUi.polling || state.route.page !== 'settings') return;
  itchUi.polling = true;
  try {
    const account = await api('GET', '/api/itch/account');
    const json = JSON.stringify(account);
    if (json !== itchUi.json) {
      itchUi.json = json;
      renderItchAccount(account);
    }
  } catch { /* 下次再检查，不打断正在修改的其他设置 */ }
  finally { itchUi.polling = false; }
}

async function itchAction(action, btn) {
  await busyButton(btn, action === 'logout' ? '清除中' : '处理中', async () => {
    const method = action === 'logout' ? 'DELETE' : 'POST';
    const path = action === 'check' ? '/api/itch/check' : '/api/itch/login';
    const account = await api(method, path, action === 'settings' ? { settings: true } : undefined);
    renderItchAccount(account);
    if (action === 'logout') toast('itch.io 已退出');
  });
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


async function pickDirectory(kind, btn) {
  await busyButton(btn, '选择中', async () => {
    const result = await api('POST', '/api/directories/pick', { kind });
    if (!result.cancelled) {
      renderSettingsBody(result);
      toast('文件夹已更新');
    }
  });
}

async function addPassword(input) {
  const password = input.value.trim();
  if (!password) return input.focus();
  try {
    renderSettingsBody(await api('POST', '/api/passwords', { password }));
    toast('密码已添加');
    $('#pw-input').focus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function removePassword(i) {
  try {
    renderSettingsBody(await api('POST', '/api/passwords/remove', { password: savedPasswords[i] }));
    toast('密码已删除');
  } catch (e) {
    toast(e.message, true);
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
    if (r.game_id && r.game_id !== id) location.hash = `#/game/${r.game_id}`;
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
  if (g.is_wishlist) return [
    {label: '查看详情', run: () => { location.hash = `#/game/${g.id}`; }},
    {label: '检查版本', icon: ICONS.refresh, run: () => checkWish(g.id)},
    {label: '关联已下载游戏', run: () => openWishAttach(g.id)},
    {label: '移除记录', run: () => removeGame(g.id)},
  ];
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
  $('#match-title').textContent = id == null ? '添加到愿望单' : '匹配资料';
  $('#match-help').textContent = id == null ? '输入名字或官网链接，选中作品后保存封面、简介和版本，不下载游戏文件。' : '输入游戏名或商店页面链接，选中正确的作品。';
  $('#wish-baseline').hidden = id != null;
  $('#wish-baseline-input').value = '';
  $('#match-q').value = id == null ? '' : game(id).name;
  $('#match-results').innerHTML = '';
  $('#match').showModal();
  if (id != null) runSearch();
  else $('#match-q').focus();
}

async function runSearch() {
  const q = $('#match-q').value.trim();
  const box = $('#match-results');
  if (!q) return;
  box.innerHTML = Array.from({ length: 4 }, () => '<div class="m-row skeleton"><div class="m-thumb"></div><div class="m-info"><i></i><i></i></div></div>').join('');
  try {
    const context = state.matchId == null ? '' : `&game_id=${state.matchId}`;
    const { results, errors, itch_account: account } = await api('GET', `/api/search?q=${encodeURIComponent(q)}${context}`);
    if (q !== $('#match-q').value.trim()) return;
    state.matchResults = results;
    const g = game(state.matchId);
    const itchIssue = account && (['expired', 'verification', 'error'].includes(account.status) || account.status === 'signed_in' && account.adult === false);
    const otherErrors = errors.filter(s => s !== 'itch' || !itchIssue);
    let note = otherErrors.length ? `<div class="m-note">${otherErrors.map(s => SOURCES[s].label).join('、')} 这次没连上，结果可能不全。</div>` : '';
    if (itchIssue) note += `<div class="m-note">itch.io：${esc(account.message)}。<button class="link" data-itch-settings>去设置</button></div>`;
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
  btn.innerHTML = '<i class="spinner"></i>获取中';
  try {
    const result = await api('POST', id == null ? '/api/wishlist' : `/api/games/${id}/match`, { source: r.source, id: r.id, version: $('#wish-baseline-input').value.trim() });
    const actualId = result.game_id || id;
    state.bust[actualId] = Date.now();
    $('#match').close();
    toast(id == null ? result.added ? '已加入愿望单' : '游戏库里已经有这款游戏' : `已换成「${r.title}」的资料`);
    await load(true);
    if (id == null || actualId !== id) location.hash = `#/game/${actualId}`;
  } catch (e) {
    toast(e.message, true);
    runSearch();
  }
}

async function checkWish(id, btn) {
  const task = async () => {
    const result = await api('POST', `/api/wishlist/${id}/check`);
    await load(true);
    toast(result.message);
  };
  if (btn) return busyButton(btn, '检查中', task);
  try { await task(); } catch (err) { toast(err.message, true); }
}

let wishAttachId = null;
function openWishAttach(id) {
  wishAttachId = id;
  const local = state.games.filter(g => !g.is_wishlist && !g.missing && !isRunning(g));
  $('#wish-local').innerHTML = local.length ? local.map(g => `<option value="${g.id}">${esc(g.name)}${g.version ? ' · v' + esc(g.version) : ''}</option>`).join('') : '<option value="">暂时没有可关联的本地游戏</option>';
  $('#wish-attach-ok').disabled = !local.length;
  $('#wish-attach').showModal();
}

$('#wish-attach').addEventListener('click', e => {
  if (e.target.closest('[data-close]')) e.currentTarget.close();
});
$('#wish-attach-form').addEventListener('submit', e => {
  e.preventDefault();
  busyButton($('#wish-attach-ok'), '关联中', async () => {
    const result = await api('POST', `/api/wishlist/${wishAttachId}/attach`, {local_id: +$('#wish-local').value});
    $('#wish-attach').close();
    await load(true);
    location.hash = `#/game/${result.game_id}`;
    toast('已关联，版本记录和备注已保留');
  });
});

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
  else state.route = { page: location.hash === '#/settings' ? 'settings' : location.hash === '#/wishlist' ? 'wishlist' : 'home' };
}

function renderView() {
  if (state.route.page === 'game') renderGame(state.route.id);
  else if (state.route.page === 'settings') renderSettings();
  else renderHome();
}

function renderAll(full) {
  renderTopbar();
  renderSidebar();
  // 设置页只在整页刷新时画一次，定时刷新游戏列表时不重画，免得冲掉正在改的选项
  if (state.route.page === 'settings') return full && renderView();
  if (full || ['home', 'wishlist'].includes(state.route.page)) return renderView();
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
    if (field === 'wish_version') await load(true);
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

// ---------------------------------------------------------------- 刷新与解压
const refreshing = { job: null, timer: null, starting: false, polling: false };

function renderRefresh() {
  const job = refreshing.job;
  if (!job) return;
  const btn = $('#scan');
  btn.classList.toggle('spinning', job.running);
  btn.setAttribute('aria-busy', String(job.running));
  btn.querySelector('span').textContent = job.running ? '刷新中' : '刷新';
  btn.title = job.running ? '正在刷新，点击查看进度' : '检查新压缩包、自动解压，再更新游戏列表和占用空间';
  $('#refresh-message').textContent = job.message || '正在检查下载文件夹';
  const active = job.running ? `
    <div class="i-row job refresh-row">
      <span class="i-state"><i class="spinner"></i></span>
      <div><div class="refresh-name">${esc(job.file || job.message)}</div>
        ${job.file ? `<div class="i-msg">${esc(job.message)}</div>` : ''}</div>
    </div>` : '';
  const rows = [...job.results].reverse().map(item => `
    <div class="i-row job refresh-row">
      <span class="i-state ${item.ok ? 'done' : 'failed'}">${item.ok ? ICONS.check : ICONS.warn}</span>
      <div><div class="refresh-name">${esc(item.file)}</div>
        <div class="i-msg ${item.ok ? '' : 'failed'}">${esc(item.message)}</div></div>
    </div>`).join('');
  $('#refresh-results').innerHTML = active + rows || `<div class="refresh-empty">${job.phase === 'error' ? ICONS.warn : ICONS.check}<span>${job.phase === 'error' ? '刷新没能完成' : job.waiting ? '有文件还在下载，下载完再刷新' : '下载文件夹已检查'}</span></div>`;
}

async function pollRefresh(quiet = false) {
  clearTimeout(refreshing.timer);
  if (refreshing.polling) return;
  refreshing.polling = true;
  try {
    const previous = refreshing.job;
    const job = await api('GET', '/api/refresh');
    // 页面刚打开时的旧查询可能晚于点击刷新返回，不能把新任务盖回旧状态。
    if (refreshing.job && job.id < refreshing.job.id) {
      refreshing.timer = setTimeout(pollRefresh, 1000);
      return;
    }
    refreshing.job = job;
    if (job.id) renderRefresh();
    if (!job.running && previous?.running) {
      await load(true);
      if (!quiet && !$('#refresh-dialog').open) toast(job.message, job.phase === 'error' || job.failed > 0);
    }
    if (job.running) refreshing.timer = setTimeout(pollRefresh, 1000);
  } catch {
    refreshing.timer = setTimeout(pollRefresh, 3000);
  } finally {
    refreshing.polling = false;
  }
}

async function startRefresh() {
  const dialog = $('#refresh-dialog');
  if (refreshing.job?.running) {
    if (!dialog.open) dialog.showModal();
    return;
  }
  if (refreshing.starting) return;
  refreshing.starting = true;
  $('#scan').disabled = true;
  try {
    refreshing.job = await api('POST', '/api/refresh');
    renderRefresh();
    if (!dialog.open) dialog.showModal();
    pollRefresh();
  } catch (e) {
    toast(e.message, true);
  } finally {
    refreshing.starting = false;
    $('#scan').disabled = false;
  }
}

// ---------------------------------------------------------------- 导入
const IMPORT_POLL_MS = 1000;
const IMPORT_ICONS = { folder: 'folder', archive: 'archive', apk: 'phone', unknown: 'warn' };
const FOLDER_MODES = [
  ['move', '挪进游戏库', '放进游戏库的文件夹，原来的位置就没有了'],
  ['copy', '复制一份', '原来的留着不动，硬盘会多占一份空间'],
  ['link', '留在原处', '文件不动，游戏库记住它在哪儿，以后挪走了会显示找不到'],
];
const imp = {
  phase: 'closed',     // inspecting：正在识别 / review：勾选要导入的 / jobs：看进度
  items: [],
  checked: new Set(),
  mode: localGet('importMode') || 'move',
  deleteArchive: localGet('importDelete') === '1',
  jobs: [],
  finished: new Set(), // 已经处理过“做完了”的任务编号
  timer: null,
};
const jobActive = j => j.state === 'waiting' || j.state === 'running';

async function importPaths(paths) {
  if (!paths || !paths.length) return;
  closeImportMenu();
  // 正在勾选时又拖进来一批，就加到列表里
  if (!(imp.phase === 'review' && $('#import').open)) { imp.items = []; imp.checked.clear(); }
  imp.phase = 'inspecting';
  renderImport();
  if (!$('#import').open) $('#import').showModal();
  try {
    const { items } = await api('POST', '/api/import/inspect', { paths });
    for (const it of items) {
      if (imp.items.some(x => x.key === it.key)) continue;
      imp.items.push(it);
      if (!it.blocked && !it.duplicate) imp.checked.add(it.key);
    }
  } catch (e) {
    toast(e.message, true);
  }
  imp.phase = 'review';
  renderImport();
}
window.importDropped = importPaths;  // 窗口那边拿到拖进来的文件路径后调这个

function importItemHtml(it) {
  const meta = [];
  if (it.kind === 'archive') meta.push(it.parts > 1 ? `压缩包 · ${it.parts} 个分卷` : '压缩包');
  if (it.kind === 'folder') meta.push(it.engine || '游戏文件夹');
  if (it.kind === 'apk') meta.push('安卓');
  if (it.version) meta.push(`v${it.version}`);
  if (it.size) meta.push(fmtSize(it.size));
  if (it.exe) meta.push(`主程序 ${it.exe}`);
  const tag = it.blocked ? `<span class="i-tag no">${esc(it.blocked)}</span>`
    : it.duplicate ? '<span class="i-tag warn">库里已有同名游戏</span>'
    : it.in_library_dir ? '<span class="i-tag ok">已在游戏库文件夹里</span>' : '';
  return `
    <label class="i-row ${it.blocked ? 'blocked' : ''}">
      <input type="checkbox" data-key="${esc(it.key)}" ${imp.checked.has(it.key) ? 'checked' : ''} ${it.blocked ? 'disabled' : ''}>
      <span class="i-icon ${it.kind}">${ICONS[IMPORT_ICONS[it.kind]]}</span>
      <div class="i-info">
        <div class="i-name">${esc(it.name)}</div>
        ${meta.length ? `<div class="i-meta">${meta.map(esc).join('<i class="dot"></i>')}</div>` : ''}
        <div class="i-path" title="${esc(it.path)}">${esc(it.path)}</div>
      </div>
      ${tag}
    </label>`;
}

function importJobHtml(j) {
  const icon = { waiting: ICONS.clock, running: '<i class="spinner"></i>', done: ICONS.check, failed: ICONS.close }[j.state];
  const pct = j.progress == null ? null : Math.round(j.progress * 100);
  const msg = j.state === 'running' && pct != null ? `${j.message} ${pct}%` : j.message;
  const bar = j.state === 'running' ? `<div class="i-bar ${pct == null ? 'busy' : ''}"><i style="${pct == null ? '' : `width:${pct}%`}"></i></div>` : '';
  const retry = j.state === 'failed' && j.need_password ? `
    <form class="i-retry" data-retry="${j.id}">
      <input type="text" placeholder="解压密码" autocomplete="off" spellcheck="false" required>
      <button class="btn btn-primary">重试</button>
    </form>` : '';
  const go = j.state === 'done' && j.game_id ? `<button class="btn" data-goto="${j.game_id}">查看</button>` : '';
  return `
    <div class="i-row job">
      <span class="i-state ${j.state}">${icon}</span>
      <span class="i-icon ${j.kind}">${ICONS[IMPORT_ICONS[j.kind]]}</span>
      <div class="i-info">
        <div class="i-name">${esc(j.name)}</div>
        <div class="i-msg ${j.state === 'failed' ? 'failed' : ''}">${esc(msg)}</div>
        ${bar}${retry}
      </div>
      ${go}
    </div>`;
}

function renderImport() {
  const list = $('#import-list');
  const go = $('#import-go');
  const cancel = $('#import-cancel');
  if (imp.phase === 'inspecting') {
    $('#import-sub').textContent = '正在识别……';
    if (!imp.items.length) list.innerHTML = '<div class="i-empty"><i class="spinner"></i>正在识别</div>';
    $('#import-opts').innerHTML = '';
    $('#import-hint').textContent = '';
    cancel.hidden = false;
    go.disabled = true;
    return;
  }
  if (imp.phase === 'review') {
    const ok = imp.items.filter(it => !it.blocked).length;
    $('#import-sub').textContent = imp.items.length
      ? `认出 ${imp.items.length} 个，能导入 ${ok} 个` : '';
    list.innerHTML = imp.items.length ? imp.items.map(importItemHtml).join('') : '<div class="i-empty">没认出能导入的东西</div>';
    cancel.hidden = false;
    renderImportOptions();
    return;
  }
  const active = imp.jobs.filter(jobActive).length;
  const failed = imp.jobs.filter(j => j.state === 'failed').length;
  $('#import-sub').textContent = active ? '可以关掉这个窗口，导入会在后台继续'
    : failed ? `导入完了，有 ${failed} 个没成功` : '全部导入完了';
  // 输入框正在打字时不重画，免得把输进去的密码冲掉
  if (!list.contains(document.activeElement) || document.activeElement.tagName !== 'INPUT') {
    list.innerHTML = imp.jobs.map(importJobHtml).join('');
  }
  $('#import-opts').innerHTML = '';
  $('#import-hint').textContent = `${imp.jobs.length - active} / ${imp.jobs.length} 完成`;
  cancel.hidden = true;
  go.disabled = false;
  go.textContent = active ? '在后台继续' : '完成';
}

function renderImportOptions() {
  const sel = imp.items.filter(it => imp.checked.has(it.key));
  const needMode = sel.some(it => it.kind !== 'archive' && !it.in_library_dir);
  const needDelete = sel.some(it => it.kind === 'archive');
  $('#import-opts').innerHTML = (needMode ? `
    <div>
      <div class="opt-label">游戏文件夹放哪儿</div>
      <div class="seg">${FOLDER_MODES.map(([v, title, help]) => `
        <label><input type="radio" name="import-mode" value="${v}" ${imp.mode === v ? 'checked' : ''}><b>${title}</b><small>${help}</small></label>`).join('')}
      </div>
    </div>` : '') + (needDelete ? `
    <label class="check"><input type="checkbox" id="import-delete" ${imp.deleteArchive ? 'checked' : ''}>解压成功后，把原压缩包移到回收站</label>` : '');
  const size = sel.reduce((n, it) => n + (it.size || 0), 0);
  $('#import-hint').textContent = sel.length ? `已选 ${sel.length} 个 · ${fmtSize(size)}` : '';
  const go = $('#import-go');
  go.disabled = !sel.length;
  go.textContent = sel.length ? `导入 ${sel.length} 个` : '导入';
}

function renderImportButton() {
  const btn = $('#import-btn');
  const active = imp.jobs.filter(jobActive);
  const failed = imp.jobs.filter(j => j.state === 'failed');
  btn.classList.toggle('busy', !!active.length);
  btn.classList.toggle('failed', !active.length && !!failed.length);
  if (active.length) {
    const total = imp.jobs.length;
    const done = total - active.length;
    const running = active.find(j => j.state === 'running');
    const share = (done + (running && running.progress ? running.progress : 0)) / total;
    btn.innerHTML = `<i class="spinner"></i><span>正在导入 ${Math.min(done + 1, total)}/${total}</span><i class="bar" style="width:${share * 100}%"></i>`;
  } else if (failed.length) {
    btn.innerHTML = `${ICONS.warn}<span>${failed.length} 个没导入成功</span>`;
  } else {
    btn.innerHTML = `${ICONS.plus}<span>导入游戏</span>`;
  }
}

async function pollImports(quiet = false) {
  clearTimeout(imp.timer);
  let jobs;
  try {
    ({ jobs } = await api('GET', '/api/import/jobs'));
  } catch {
    imp.timer = setTimeout(pollImports, 3000);
    return;
  }
  const newlyDone = jobs.filter(j => !jobActive(j) && !imp.finished.has(j.id));
  newlyDone.forEach(j => imp.finished.add(j.id));
  imp.jobs = jobs;
  if (newlyDone.some(j => j.state === 'done')) load();
  if (imp.phase === 'jobs' && $('#import').open) renderImport();
  renderImportButton();
  if (jobs.some(jobActive)) {
    imp.timer = setTimeout(pollImports, IMPORT_POLL_MS);
    return;
  }
  // 窗口关着时导入做完了，提示一下结果
  if (quiet || !newlyDone.length || $('#import').open) return;
  const failed = jobs.filter(j => j.state === 'failed').length;
  if (failed) toast(`有 ${failed} 个没导入成功，点左下角看看原因`, true);
  else toast(jobs.length === 1 ? `「${jobs[0].name}」${jobs[0].message}` : `${jobs.length} 个都导入好了`);
}

async function startImport() {
  const items = imp.items.filter(it => imp.checked.has(it.key) && !it.blocked);
  if (!items.length) return;
  try {
    await api('POST', '/api/import/start', { items, mode: imp.mode, delete_archive: imp.deleteArchive });
  } catch (e) {
    return toast(e.message, true);
  }
  imp.phase = 'jobs';
  imp.items = [];
  imp.checked.clear();
  renderImport();
  pollImports();
}

function showImportJobs() {
  closeImportMenu();
  imp.phase = 'jobs';
  renderImport();
  if (!$('#import').open) $('#import').showModal();
}

async function pickImport(what) {
  closeImportMenu();
  try {
    const { paths } = await api('POST', '/api/import/pick', { what });
    importPaths(paths);
  } catch (e) {
    toast(e.message, true);
  }
}

function toggleImportMenu() {
  const menu = $('#import-menu');
  if (!menu.hidden) return closeImportMenu();
  const old = menu.querySelector('[data-import-jobs]');
  if (old) old.remove();
  if (imp.jobs.length) {
    const active = imp.jobs.filter(jobActive).length;
    menu.insertAdjacentHTML('afterbegin', `
      <button role="menuitem" data-import-jobs>${active ? '<i class="spinner"></i>' : ICONS.check}
        <span>查看导入进度<small>${active ? `还有 ${active} 个在排队或进行中` : '上一批已经做完了'}</small></span>
      </button>`);
  }
  menu.hidden = false;
  $('#import-btn').setAttribute('aria-expanded', 'true');
}

function closeImportMenu() {
  $('#import-menu').hidden = true;
  $('#import-btn').setAttribute('aria-expanded', 'false');
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
  if (['home', 'wishlist'].includes(state.route.page)) renderHome();
});
$('#sort').value = Object.prototype.hasOwnProperty.call(SORTS, state.sort) ? state.sort : 'added';
state.sort = $('#sort').value;
$('#sort').addEventListener('change', e => {
  state.sort = e.target.value;
  localSet('sort', state.sort);
  renderSidebar();
  if (['home', 'wishlist'].includes(state.route.page)) renderHome();
});

$('#now-playing').addEventListener('click', e => {
  const id = e.currentTarget.dataset.id;
  if (id) location.hash = `#/game/${id}`;
});

$('#scan').addEventListener('click', startRefresh);
$('#refresh-dialog').addEventListener('click', e => {
  if (e.target.closest('[data-close]')) e.currentTarget.close();
});
$('#refresh-settings').addEventListener('click', () => {
  $('#refresh-dialog').close();
  location.hash = '#/settings';
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
  if (t.hasAttribute('data-add-wish')) return openMatch(null);
  if (t.hasAttribute('data-wish-check')) return checkWish(state.route.id, t);
  if (t.hasAttribute('data-wish-attach')) return openWishAttach(state.route.id);
  if (t.dataset.itch) return itchAction(t.dataset.itch, t);
  if (t.dataset.pickDir) return pickDirectory(t.dataset.pickDir, t);
  if (t.dataset.openDir) {
    try { await api('POST', `/api/directories/${t.dataset.openDir}/open`); }
    catch (err) { toast(err.message, true); }
    return;
  }
  const id = state.route.id;
  if (t.hasAttribute('data-back')) return history.length > 1 ? history.back() : (location.hash = '#/');
  if (t.dataset.status) return setStatus(id, t.dataset.status);
  if (t.hasAttribute('data-open-android')) return api('POST', '/api/open-android-dir').catch(err => toast(err.message, true));
  if (t.dataset.pwRemove) return removePassword(+t.dataset.pwRemove);
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
  if (t.hasAttribute('data-wish-version')) {
    try {
      await api('PATCH', `/api/wishlist/${id}/version`, {version: t.value.trim()});
      await load(true);
      toast('版本已记录');
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (t.id === 'status-filter') { state.statusFilter = t.value; return renderHome(); }
  if (t.name === 'android') return saveSettings({ android: t.value });
  if (t.name === 'theme') {
    document.documentElement.dataset.theme = t.value;
    renderSidebar();
    return saveSettings({ theme: t.value });
  }
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

view.addEventListener('submit', e => {
  if (e.target.id !== 'pw-form') return;
  e.preventDefault();
  addPassword($('#pw-input'));
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
  e.stopPropagation();  // 拖到背景大图上是换背景，不当成导入
  endDrag();
  hero.classList.remove('drop');
  uploadImage(state.route.id, 'hero', e.dataTransfer.files[0]);
});

// 文件拖进窗口：盖一层“松手导入”的提示。拖到背景大图上时不盖，那里是换背景
const dropOverlay = $('#drop-overlay');
let dragDepth = 0;
const draggingFiles = e => !!e.dataTransfer && [...e.dataTransfer.types].includes('Files');
function endDrag() {
  dragDepth = 0;
  dropOverlay.hidden = true;
  $('#import').classList.remove('dragging');
}
window.addEventListener('dragenter', e => {
  if (!draggingFiles(e)) return;
  dragDepth++;
  dropOverlay.hidden = false;
  $('#import').classList.add('dragging');
});
window.addEventListener('dragleave', e => {
  if (draggingFiles(e) && --dragDepth <= 0) endDrag();
});
// 不拦下来的话，窗口默认会直接打开拖进来的文件、把游戏库页面换掉
window.addEventListener('dragover', e => {
  e.preventDefault();
  dropOverlay.classList.toggle('off', !!(e.target.closest && e.target.closest('#hero')));
});
window.addEventListener('drop', e => {
  e.preventDefault();
  endDrag();
  if (!window.pywebview && draggingFiles(e)) toast('在浏览器里打开时拖不进来，请在游戏库窗口里拖', true);
});

// 自绘标题栏的三个按钮；窗口那边最大化、还原后会调 setMaximized 换图标
window.setMaximized = on => {
  document.documentElement.classList.toggle('maximized', on);
  $('[data-win="max"]').title = on ? '向下还原' : '最大化';
};
$('.win-ctrls').addEventListener('click', e => {
  const btn = e.target.closest('[data-win]');
  if (btn) api('POST', `/api/window/${btn.dataset.win}`).catch(err => toast(err.message, true));
});

$('#import-btn').addEventListener('click', toggleImportMenu);
$('#import-menu').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.hasAttribute('data-import-jobs')) return showImportJobs();
  pickImport(btn.dataset.importPick);
});
document.addEventListener('mousedown', e => { if (!e.target.closest('.side-foot')) closeImportMenu(); });

const importDialog = $('#import');
importDialog.addEventListener('change', e => {
  const t = e.target;
  if (t.dataset.key) {
    t.checked ? imp.checked.add(t.dataset.key) : imp.checked.delete(t.dataset.key);
    return renderImportOptions();
  }
  if (t.name === 'import-mode') { imp.mode = t.value; localSet('importMode', t.value); }
  if (t.id === 'import-delete') { imp.deleteArchive = t.checked; localSet('importDelete', t.checked ? '1' : '0'); }
});
importDialog.addEventListener('click', e => {
  if (e.target.closest('[data-close]')) return importDialog.close();
  if (e.target.closest('#import-go')) return imp.phase === 'review' ? startImport() : importDialog.close();
  const goto = e.target.closest('[data-goto]');
  if (goto) {
    importDialog.close();
    location.hash = `#/game/${goto.dataset.goto}`;
  }
});
importDialog.addEventListener('submit', async e => {
  e.preventDefault();
  const form = e.target.closest('[data-retry]');
  if (!form) return;
  const password = form.querySelector('input').value.trim();
  form.querySelector('button').disabled = true;
  try {
    await api('POST', `/api/import/${form.dataset.retry}/retry`, { password });
    form.querySelector('input').blur();
    pollImports();
  } catch (err) {
    toast(err.message, true);
    form.querySelector('button').disabled = false;
  }
});
importDialog.addEventListener('close', () => {
  // 全做完了才清掉记录；还在导入的话关掉窗口也照样在后台跑
  if (imp.phase === 'jobs' && !imp.jobs.some(jobActive)) {
    api('POST', '/api/import/clear').then(() => pollImports(true)).catch(() => {});
  }
  imp.phase = 'closed';
  imp.items = [];
  imp.checked.clear();
});

$('#match-form').addEventListener('submit', e => { e.preventDefault(); runSearch(); });
$('#match').addEventListener('click', e => {
  if (e.target.closest('[data-itch-settings]')) {
    e.currentTarget.close();
    location.hash = '#/settings';
    return;
  }
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
  if (e.key === 'Escape' && !$('#import-menu').hidden) return closeImportMenu();
  if (e.target.matches('input, textarea, select') || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
  if (e.key === 'Escape' && state.route.page === 'game') location.hash = '#/';
});

parseRoute();
load(true);
setInterval(load, POLL_MS);
pollImports(true);
pollRefresh(true);
setInterval(pollItchAccount, 2000);
