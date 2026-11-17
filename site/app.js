/* FTB Quests web viewer. No build step, no dependencies. */
(() => {
  'use strict';

  const U = 48;               // pixels per quest-grid unit at zoom 1
  const MIN_Z = 0.15, MAX_Z = 4;
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  let DB, ICONS, questIndex = new Map(), dependents = new Map(), chapterById = new Map(), chapterByFile = new Map();
  let current = null;               // current chapter
  let view = { x: 0, y: 0, z: 1 };  // pan/zoom
  let nodes = new Map();            // questId -> element on current map
  let lineEls = [];                 // {line, from, to}

  // ------------------------------------------------------------ assets
  function splitId(id) { const i = id.indexOf(':'); return i < 0 ? ['minecraft', id] : [id.slice(0, i), id.slice(i + 1)]; }
  function texUrl(ref) {
    let [ns, p] = splitId(ref);
    if (!p.startsWith('textures/')) p = 'textures/' + p;
    if (!p.endsWith('.png')) p += '.png';
    return `assets/textures/${ns}/${p}`;
  }
  function iconUrl(icon) {
    if (!icon) return null;
    if (icon.texture) return texUrl(icon.texture);
    if (icon.gui) return `assets/gui/${icon.gui}.png`;
    if (icon.item && ICONS.has(icon.item)) { const [ns, p] = splitId(icon.item); return `assets/icons/${ns}/${p}.png`; }
    return null;
  }
  function iconEl(icon, cls = '') {
    const url = iconUrl(icon);
    if (url) { const i = el('img', cls); i.src = url; i.alt = ''; i.loading = 'lazy'; i.draggable = false; return i; }
    const d = el('span', 'noicon ' + cls, '?');
    return d;
  }

  // ------------------------------------------------------------ text formatting (& codes)
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const NAMED = { black: '0', dark_blue: '1', dark_green: '2', dark_aqua: '3', dark_red: '4', dark_purple: '5', gold: '6', gray: '7', dark_gray: '8', blue: '9', green: 'a', aqua: 'b', red: 'c', light_purple: 'd', yellow: 'e', white: 'f' };

  /** Convert a string with Minecraft '&' formatting codes to HTML. */
  function fmt(s) {
    if (s == null) return '';
    s = String(s).replace(/§/g, '&');
    let out = '', color = null, styles = new Set(), buf = '';
    const flush = () => {
      if (!buf) return;
      const cls = [];
      if (color) cls.push('c' + color);
      styles.forEach(x => cls.push('f' + x));
      out += cls.length ? `<span class="${cls.join(' ')}">${esc(buf)}</span>` : esc(buf);
      buf = '';
    };
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (ch === '\\' && s[i + 1] === '&') { buf += '&'; i++; continue; }
      if (ch === '&' && i + 1 < s.length) {
        const c = s[i + 1].toLowerCase();
        if (/[0-9a-f]/.test(c)) { flush(); color = c; styles = new Set(); i++; continue; }
        if (/[k-o]/.test(c)) { flush(); styles.add(c); i++; continue; }
        if (c === 'r') { flush(); color = null; styles = new Set(); i++; continue; }
      }
      buf += ch;
    }
    flush();
    return out;
  }
  const plain = s => String(s || '').replace(/\\&/g, '&').replace(/[&§][0-9a-fk-or]/gi, '');

  /** JSON text component (used by some FTB descriptions) -> HTML */
  function component(c) {
    if (c == null) return '';
    if (typeof c === 'string') return fmt(c);
    if (Array.isArray(c)) return c.map(component).join('');
    let inner = c.translate ? esc(c.fallback || c.translate) : fmt(c.text || '');
    if (c.extra) inner += component(c.extra);
    const cls = [];
    if (c.color && NAMED[c.color]) cls.push('c' + NAMED[c.color]);
    if (c.bold) cls.push('fl'); if (c.italic) cls.push('fo'); if (c.underlined) cls.push('fn'); if (c.strikethrough) cls.push('fm');
    let style = c.color && c.color.startsWith('#') ? ` style="color:${esc(c.color)}"` : '';
    let html = `<span class="${cls.join(' ')}"${style}>${inner}</span>`;
    const ev = c.clickEvent || c.click_event;
    if (ev && ev.action === 'open_url' && /^https?:/i.test(ev.value || ev.url || '')) html = `<a href="${esc(ev.value || ev.url)}" target="_blank" rel="noopener">${html}</a>`;
    else if (ev && ev.action === 'change_page' && questIndex.has(String(ev.value))) html = `<a href="#" data-goto="${esc(ev.value)}">${html}</a>`;
    return html;
  }

  /** Description lines -> array of pages (each an HTML string). */
  function renderDescription(lines) {
    const pages = [[]];
    for (const raw of lines) {
      const line = raw == null ? '' : String(raw);
      if (line.trim() === '{@pagebreak}') { pages.push([]); continue; }
      const page = pages[pages.length - 1];
      const img = line.match(/^\s*\{image:([^\s}]+)([^}]*)\}\s*$/);
      if (img) {
        const props = Object.fromEntries([...img[2].matchAll(/(\w+):(\S+)/g)].map(m => [m[1], m[2]]));
        const align = { '0': 'left', '1': 'center', '2': 'right', left: 'left', center: 'center', right: 'right' }[props.align] || 'center';
        const w = parseFloat(props.width) || 0, h = parseFloat(props.height) || 0;
        const size = w && h ? ` width="${w * 2}" height="${h * 2}"` : '';
        page.push(`<div class="img" style="text-align:${align}"><img src="${esc(texUrl(img[1]))}" alt=""${size} loading="lazy"></div>`);
        continue;
      }
      const t = line.trim();
      if ((t.startsWith('[') || t.startsWith('{"')) && t.length > 2) {
        try { page.push(`<p>${component(JSON.parse(t))}</p>`); continue; } catch { /* not JSON, fall through */ }
      }
      page.push(`<p>${fmt(line)}</p>`);
    }
    return pages.map(p => p.join('')).filter((p, i, a) => p || a.length === 1);
  }

  // ------------------------------------------------------------ tooltip
  const tip = $('#tooltip');
  let tipTimer = 0;
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y - 10;
    if (left + r.width > innerWidth - 6) left = Math.max(6, x - r.width - 14);
    if (top + r.height > innerHeight - 6) top = innerHeight - r.height - 6;
    tip.style.left = left + 'px'; tip.style.top = Math.max(6, top) + 'px';
  }
  function hideTip() { tip.hidden = true; clearTimeout(tipTimer); }
  function bindTip(node, htmlFn) {
    node.addEventListener('pointermove', e => { if (e.pointerType === 'mouse') showTip(htmlFn(), e.clientX, e.clientY); });
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('click', e => {
      if (e.pointerType && e.pointerType !== 'mouse') {
        const r = node.getBoundingClientRect();
        showTip(htmlFn(), r.right, r.top);
        clearTimeout(tipTimer); tipTimer = setTimeout(hideTip, 2500);
      }
    });
  }
  const modName = id => id && id.includes(':') ? `<div class="c9 fo">${esc(id.split(':')[0])}</div>` : '';

  // ------------------------------------------------------------ chapter list
  function buildChapterList() {
    const list = $('#chapter-list');
    list.innerHTML = '';
    const groups = [{ id: '', title: '' }, ...DB.groups];
    for (const g of groups) {
      const chs = DB.chapters.filter(c => (c.group || '') === g.id);
      if (!chs.length) continue;
      const wrap = el('div', 'group');
      if (g.id) {
        const h = el('button', 'group-h mc', `<span class="arrow">▼</span><span>${fmt(g.title)}</span>`);
        const key = 'grp-collapsed-' + g.id;
        if (store.get(key) === '1') wrap.classList.add('collapsed');
        h.onclick = () => { wrap.classList.toggle('collapsed'); store.set(key, wrap.classList.contains('collapsed') ? '1' : '0'); };
        wrap.append(h);
      }
      const items = el('div', 'group-items');
      for (const c of chs) {
        const b = el('button', 'ch');
        b.dataset.id = c.id;
        b.append(iconEl(c.icon, 'pixel'));
        b.append(el('span', 'name mc', fmt(c.title)));
        b.append(el('span', 'count', String(c.quests.length)));
        b.title = plain(c.title) + (c.subtitle.length ? ' — ' + c.subtitle.map(plain).join(' ') : '');
        b.onclick = () => { go(c); document.body.classList.remove('drawer'); };
        items.append(b);
      }
      wrap.append(items);
      list.append(wrap);
    }
  }

  // ------------------------------------------------------------ map
  const viewport = $('#viewport'), world = $('#world'), svg = $('#lines');

  function applyView() {
    world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`;
    $('#zoom-ind').textContent = Math.round(view.z * 100) + '%';
  }

  function shapeUrl(shape, part) { return `assets/gui/shapes/${shape}/${part}.png`; }

  function questNode(q, opts = {}) {
    const shape = opts.shape || q.shape || 'circle';
    const size = (opts.size || q.size || 1) * U;
    const n = el('div', 'q shape-' + shape + (opts.linked ? ' linked' : ''));
    n.style.width = n.style.height = size + 'px';
    n.style.left = ((opts.x ?? q.x) * U - size / 2) + 'px';
    n.style.top = ((opts.y ?? q.y) * U - size / 2) + 'px';
    if (shape !== 'none') {
      const bg = el('div', 'bg'); bg.style.backgroundImage = `url(${shapeUrl(shape, 'background')})`;
      const ol = el('div', 'ol'); ol.style.backgroundImage = `url(${shapeUrl(shape, 'outline')})`;
      const hl = el('div', 'hl'); hl.style.webkitMaskImage = hl.style.maskImage = `url(${shapeUrl(shape, 'shape')})`;
      n.append(bg, ol, hl);
    }
    n.append(iconEl(q.icon));
    if (q.repeatable) { const b = el('span', 'badge mc', ''); n.append(b); }
    n.dataset.id = q.id;
    return n;
  }

  function questTip(q, chapter) {
    let h = `<div>${fmt(q.title)}</div>`;
    if (q.subtitle) h += `<div class="tt-sub">${fmt(q.subtitle)}</div>`;
    if (chapter && chapter !== current) h += `<div class="tt-dim">in ${esc(plain(chapter.title))}</div>`;
    const flags = [];
    if (q.repeatable) flags.push('Repeatable');
    if (q.optional) flags.push('Optional');
    if (flags.length) h += `<div class="tt-dim">${flags.join(' · ')}</div>`;
    return h;
  }

  function renderChapter(c) {
    current = c;
    nodes.clear(); lineEls = [];
    $('#quests').innerHTML = ''; svg.innerHTML = ''; $('#images').innerHTML = '';
    $('#chapter-title .t').innerHTML = fmt(c.title);
    $('#chapter-title .s').innerHTML = c.subtitle.map(fmt).join(' &nbsp;·&nbsp; ');
    document.title = `${plain(c.title)} — ${DB.siteTitle}`;
    document.querySelectorAll('.ch').forEach(b => b.classList.toggle('sel', b.dataset.id === c.id));
    const sel = document.querySelector('.ch.sel');
    if (sel) { sel.closest('.group')?.classList.remove('collapsed'); sel.scrollIntoView({ block: 'nearest' }); }

    // chapter images (decorations)
    for (const im of [...c.images].sort((a, b) => a.order - b.order)) {
      const i = el('img', 'cimg'); i.src = texUrl(im.texture); i.alt = '';
      i.style.width = im.w * U + 'px'; i.style.height = im.h * U + 'px';
      i.style.left = (im.x - im.w / 2) * U + 'px'; i.style.top = (im.y - im.h / 2) * U + 'px';
      const filters = [];
      if (im.rotation) i.style.transform = `rotate(${im.rotation}deg)`;
      if (im.color != null && im.color !== 0xFFFFFF) {
        const r = (im.color >> 16) & 255, g = (im.color >> 8) & 255, b = im.color & 255;
        filters.push(`brightness(${((r + g + b) / 3 / 255).toFixed(2)})`);
      }
      if (filters.length) i.style.filter = filters.join(' ');
      if (im.alpha != null && im.alpha < 255) i.style.opacity = (im.alpha / 255).toFixed(2);
      i.style.zIndex = im.order < 0 ? -1 : 0;
      $('#images').append(i);
    }

    // dependency lines (same chapter only, like in-game)
    const pos = new Map(c.quests.map(q => [q.id, q]));
    const NS = 'http://www.w3.org/2000/svg';
    for (const q of c.quests) {
      if (q.hideLines) continue;
      for (const d of q.deps) {
        const dq = pos.get(d);
        if (!dq) continue;
        const l = document.createElementNS(NS, 'line');
        l.setAttribute('x1', dq.x * U); l.setAttribute('y1', dq.y * U);
        l.setAttribute('x2', q.x * U); l.setAttribute('y2', q.y * U);
        l.setAttribute('stroke-width', U * 0.1);
        svg.append(l);
        lineEls.push({ line: l, from: d, to: q.id });
      }
    }
    svg.style.setProperty('--dash', (U * 0.22) + 'px');

    // quests
    const frag = document.createDocumentFragment();
    for (const q of c.quests) {
      const n = questNode(q);
      bindQuestNode(n, q, c);
      nodes.set(q.id, n);
      frag.append(n);
    }
    for (const l of c.links) {
      const ref = questIndex.get(l.quest);
      if (!ref) continue;
      const n = questNode(ref.quest, { x: l.x, y: l.y, size: l.size, shape: l.shape || ref.quest.shape, linked: true });
      bindQuestNode(n, ref.quest, ref.chapter);
      frag.append(n);
    }
    $('#quests').append(frag);
    markSearchMatches();
  }

  function bindQuestNode(n, q, chapter) {
    bindTip(n, () => questTip(q, chapter));
    n.addEventListener('pointerenter', () => highlightLines(q.id));
    n.addEventListener('pointerleave', () => highlightLines(null));
    n.addEventListener('click', e => { if (!dragMoved) openQuest(q.id); });
  }

  function highlightLines(id) {
    for (const { line, from, to } of lineEls) {
      line.classList.toggle('req', id != null && to === id);
      line.classList.toggle('for', id != null && from === id);
      line.classList.toggle('dim', id != null && to !== id && from !== id);
      if (id != null && (to === id || from === id)) svg.append(line); // bring to front
    }
  }

  function bounds(c) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y, w, h) => { x0 = Math.min(x0, x - w / 2); y0 = Math.min(y0, y - h / 2); x1 = Math.max(x1, x + w / 2); y1 = Math.max(y1, y + h / 2); };
    c.quests.forEach(q => add(q.x, q.y, q.size || 1, q.size || 1));
    c.links.forEach(l => add(l.x, l.y, l.size || 1, l.size || 1));
    c.images.forEach(i => add(i.x, i.y, i.w, i.h));
    if (!isFinite(x0)) return { x0: -2, y0: -2, x1: 2, y1: 2 };
    return { x0, y0, x1, y1 };
  }

  function fit() {
    if (!current) return;
    const b = bounds(current);
    const vw = viewport.clientWidth, vh = viewport.clientHeight, pad = 40;
    const w = (b.x1 - b.x0) * U, h = (b.y1 - b.y0) * U;
    view.z = Math.max(MIN_Z, Math.min(1.6, (vw - pad * 2) / w, (vh - pad * 2) / h));
    view.x = vw / 2 - ((b.x0 + b.x1) / 2) * U * view.z;
    view.y = vh / 2 - ((b.y0 + b.y1) / 2) * U * view.z;
    applyView();
  }

  function centerOn(q, zoom) {
    if (zoom) view.z = Math.max(view.z, zoom);
    view.x = viewport.clientWidth / 2 - q.x * U * view.z;
    view.y = viewport.clientHeight / 2 - q.y * U * view.z;
    applyView();
  }

  function zoomAt(factor, cx, cy) {
    const nz = Math.max(MIN_Z, Math.min(MAX_Z, view.z * factor));
    const k = nz / view.z;
    view.x = cx - (cx - view.x) * k;
    view.y = cy - (cy - view.y) * k;
    view.z = nz;
    applyView();
  }

  // pan / zoom input
  let dragMoved = false;
  const pointers = new Map();
  let pinch = null;
  viewport.addEventListener('wheel', e => {
    e.preventDefault();
    const r = viewport.getBoundingClientRect();
    const f = Math.exp(-(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY) * 0.0015);
    zoomAt(f, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  viewport.addEventListener('pointerdown', e => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY });
    dragMoved = false;
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) };
    }
  });
  addEventListener('pointermove', e => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (Math.hypot(e.clientX - p.sx, e.clientY - p.sy) > 4) {
      if (!dragMoved) { dragMoved = true; viewport.classList.add('dragging'); hideTip(); try { viewport.setPointerCapture(e.pointerId); } catch { } }
    }
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const r = viewport.getBoundingClientRect();
      zoomAt(d / pinch.d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      pinch.d = d;
      view.x += dx / 2; view.y += dy / 2; applyView();
    } else if (dragMoved) {
      view.x += dx; view.y += dy; applyView();
    }
  });
  const endPointer = e => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!pointers.size) { viewport.classList.remove('dragging'); setTimeout(() => { dragMoved = false; }, 0); }
  };
  addEventListener('pointerup', endPointer);
  addEventListener('pointercancel', endPointer);

  // ------------------------------------------------------------ quest view
  const qv = $('#quest-view');
  let qvPages = [], qvPage = 0, openId = null;

  function slot(icon, count, tipHtml, opts = {}) {
    const s = el(opts.onClick ? 'button' : 'div', 'slot' + (opts.onClick ? ' clickable' : ''));
    s.append(iconEl(icon));
    if (count && count > 1) s.append(el('span', 'cnt', count >= 1000 ? (count / 1000).toFixed(count % 1000 ? 1 : 0) + 'k' : String(count)));
    if (opts.badge) { const b = el('img', opts.badgeCls || 'chk'); b.src = opts.badge; b.alt = ''; s.append(b); }
    bindTip(s, () => tipHtml);
    if (opts.onClick) s.addEventListener('click', e => { hideTip(); opts.onClick(e); });
    return s;
  }

  function taskTip(t) {
    let h = `<div>${fmt(t.name)}</div>`;
    if (t.type === 'item' && t.item) {
      if (t.name !== t.item.name) h += `<div class="tt-sub">${fmt(t.item.name)}</div>`;
      if (t.item.extra) h += `<div class="tt-sub">${esc(t.item.extra)}</div>`;
      if (t.item.count > 1) h += `<div class="tt-sub">Amount: ${t.item.count}</div>`;
      if (t.consume) h += `<div class="tt-sub c6">Consumes items</div>`;
      h += modName(t.item.id);
    } else if (t.type === 'kill') {
      h += `<div class="tt-sub">Kill ${esc(t.entity)} × ${t.count}</div>`;
    } else if (t.type === 'stat') {
      h += `<div class="tt-sub">Reach ${t.count}</div>`;
    } else if (t.detail) {
      h += `<div class="tt-sub">${fmt(t.detail)}</div>`;
    }
    h += `<div class="tt-dim">${esc(taskTypeName(t.type))} task</div>`;
    return h;
  }
  const taskTypeName = ty => ({ item: 'Item', kill: 'Kill', checkmark: 'Checkmark', advancement: 'Advancement', biome: 'Biome', dimension: 'Dimension', structure: 'Structure', stat: 'Stat', observation: 'Observation', location: 'Location', xp: 'XP' }[ty] || ty);

  function rewardTip(r) {
    let h = `<div>${fmt(r.name)}</div>`;
    if (r.type === 'item' && r.item) {
      if (r.name !== r.item.name) h += `<div class="tt-sub">${fmt(r.item.name)}</div>`;
      if (r.item.extra) h += `<div class="tt-sub">${esc(r.item.extra)}</div>`;
      if (r.item.count > 1) h += `<div class="tt-sub">Amount: ${r.item.count}</div>`;
      h += modName(r.item.id);
    }
    if (r.table) h += `<div class="tt-dim">Click to see possible rewards</div>`;
    if (r.auto) h += `<div class="tt-dim">Claimed automatically</div>`;
    return h;
  }

  function questLink(id) {
    const ref = questIndex.get(id);
    if (!ref) return null;
    const b = el('button', 'qlink mc');
    b.append(iconEl(ref.quest.icon, 'pixel'));
    b.append(el('span', '', fmt(ref.quest.title)));
    if (ref.chapter !== current) b.append(el('span', 'ch-n', '— ' + esc(plain(ref.chapter.title))));
    b.onclick = () => openQuest(id);
    return b;
  }

  function openQuest(id, { pan = true } = {}) {
    const ref = questIndex.get(id);
    if (!ref) return;
    const { quest: q, chapter } = ref;
    if (chapter !== current) { renderChapter(chapter); fit(); }
    if (pan) {
      const n = nodes.get(id);
      document.querySelectorAll('.q.focus').forEach(x => x.classList.remove('focus'));
      if (n) n.classList.add('focus');
    }
    openId = id;
    hideTip();
    results.hidden = true;
    const p = qv.querySelector('.qv-panel');
    $('.qv-icon', p).replaceChildren(iconEl(q.icon, 'pixel'));
    $('#qv-title').innerHTML = fmt(q.title);
    const flags = $('.qv-flags', p); flags.innerHTML = '';
    if (q.repeatable) flags.append(el('span', 'flag', 'Repeatable'));
    if (q.optional) flags.append(el('span', 'flag', 'Optional'));

    const ts = $('.qv-slots.tasks', p); ts.innerHTML = '';
    for (const t of q.tasks) {
      const badge = t.type === 'checkmark' ? 'assets/gui/accept_gray.png' : null;
      ts.append(slot(t.icon, t.type === 'item' ? t.item?.count : t.count, taskTip(t), { badge }));
    }
    if (!q.tasks.length) ts.append(el('span', 'none mc', 'None'));

    const rs = $('.qv-slots.rewards', p); rs.innerHTML = '';
    for (const r of q.rewards) {
      const onClick = r.table && DB.tables[r.table] ? () => openTable(r) : null;
      rs.append(slot(r.icon, r.type === 'item' ? r.item?.count : 0, rewardTip(r), { onClick, badge: r.table ? 'assets/gui/money_bag.png' : null, badgeCls: 'tbl' }));
    }
    if (!q.rewards.length) rs.append(el('span', 'none mc', 'None'));

    $('.qv-subtitle', p).innerHTML = fmt(q.subtitle);
    qvPages = renderDescription(q.description);
    qvPage = 0;
    showPage();

    // dependencies, including ones in other chapters
    const deps = $('.qv-deps', p); deps.innerHTML = '';
    const addRow = (label, cls, ids) => {
      const links = ids.map(questLink).filter(Boolean);
      if (!links.length) return;
      const row = el('div', 'row');
      row.append(el('span', 'lbl ' + cls, label));
      links.forEach(l => row.append(l));
      deps.append(row);
    };
    const reqLabel = q.depMode === 'one_completed' || q.minDeps === 1 ? 'Requires one of:' : q.minDeps > 1 ? `Requires ${q.minDeps} of:` : 'Requires:';
    addRow(reqLabel, 'req', q.deps);
    addRow('Unlocks:', 'for', dependents.get(id) || []);

    qv.hidden = false;
    $('.qv-body', p).scrollTop = 0;
    const want = `#/${chapter.filename}/${id}`;
    if (location.hash !== want) history.replaceState(null, '', want);
  }

  function showPage() {
    const p = qv.querySelector('.qv-panel');
    $('.qv-desc', p).innerHTML = qvPages[qvPage] || '';
    const pg = $('.qv-pages', p);
    pg.hidden = qvPages.length < 2;
    $('.pg-num', p).textContent = `${qvPage + 1} / ${qvPages.length}`;
    $('.pg-prev', p).disabled = qvPage === 0;
    $('.pg-next', p).disabled = qvPage >= qvPages.length - 1;
  }
  $('.pg-prev').onclick = () => { if (qvPage > 0) { qvPage--; showPage(); } };
  $('.pg-next').onclick = () => { if (qvPage < qvPages.length - 1) { qvPage++; showPage(); } };
  qv.querySelector('.qv-desc').addEventListener('click', e => {
    const a = e.target.closest('a[data-goto]');
    if (a) { e.preventDefault(); openQuest(a.dataset.goto); }
  });

  function closeQuest() {
    qv.hidden = true; openId = null; hideTip();
    document.querySelectorAll('.q.focus').forEach(x => x.classList.remove('focus'));
    if (current) history.replaceState(null, '', `#/${current.filename}`);
  }
  qv.addEventListener('click', e => { if (e.target === qv || e.target.closest('.qv-close')) closeQuest(); });

  // reward table popup
  const tv = $('#table-view');
  function openTable(r) {
    const tab = DB.tables[r.table];
    const p = tv.querySelector('.tv-panel');
    $('h3', p).innerHTML = fmt(tab.title);
    const total = tab.rewards.reduce((s, x) => s + (x.weight || 0), 0) || 1;
    const how = { choice: 'Choose one of these rewards', random: 'One random reward from this table', loot: 'Loot crate with rewards from this table', all_table: 'You get all of these rewards' }[r.type] || '';
    $('.tv-sub', p).textContent = how;
    const list = $('.tv-list', p); list.innerHTML = '';
    for (const x of tab.rewards) {
      const row = el('div', 'tv-row');
      const tipH = `<div>${fmt(x.name)}</div>` + (x.item && x.item.name !== x.name ? `<div class="tt-sub">${fmt(x.item.name)}</div>` : '') + modName(x.item?.id);
      row.append(slot(x.icon, x.item?.count, tipH));
      row.append(el('span', 'nm mc', fmt(x.name)));
      if (r.type === 'random' || r.type === 'loot') row.append(el('span', 'w', ((x.weight || 0) / total * 100).toFixed(1).replace(/\.0$/, '') + '%'));
      list.append(row);
    }
    tv.hidden = false;
  }
  tv.addEventListener('click', e => { if (e.target === tv || e.target.closest('.qv-close')) { tv.hidden = true; hideTip(); } });

  // ------------------------------------------------------------ search
  const search = $('#search'), results = $('#search-results');
  let searchIndex = [], activeResult = -1, matchSet = new Set();

  function buildSearchIndex() {
    for (const c of DB.chapters) for (const q of c.quests) {
      const parts = [q.title, q.subtitle, ...q.description,
        ...q.tasks.flatMap(t => [t.name, t.item?.name, t.item?.id, t.entity]),
        ...q.rewards.flatMap(r => [r.name, r.item?.name, r.item?.id, ...(r.table && DB.tables[r.table] ? DB.tables[r.table].rewards.map(x => x.name) : [])])];
      searchIndex.push({ q, c, title: plain(q.title).toLowerCase(), text: parts.filter(Boolean).map(plain).join('\n').toLowerCase() });
    }
  }

  function runSearch() {
    const term = search.value.trim().toLowerCase();
    matchSet = new Set();
    if (!term) { results.hidden = true; markSearchMatches(); return; }
    const words = term.split(/\s+/);
    const hits = searchIndex.filter(e => words.every(w => e.text.includes(w)))
      .map(e => ({ ...e, score: (e.title === term ? 0 : e.title.startsWith(term) ? 1 : e.title.includes(term) ? 2 : 3) }))
      .sort((a, b) => a.score - b.score);
    hits.forEach(h => matchSet.add(h.q.id));
    markSearchMatches();
    results.innerHTML = '';
    activeResult = -1;
    if (!hits.length) results.append(el('div', 'sr-empty', 'No quests found'));
    for (const h of hits.slice(0, 80)) {
      const b = el('button', 'sr');
      b.append(iconEl(h.q.icon, 'pixel'));
      const tx = el('span', 'tx');
      tx.append(el('span', 'a mc', fmt(h.q.title)));
      let where = plain(h.c.title);
      if (h.score === 3) {
        const i = h.text.indexOf(words[0]);
        const snip = h.text.slice(Math.max(0, i - 30), i + 50).replace(/\n/g, ' · ');
        where += ' — …' + snip + '…';
      }
      tx.append(el('span', 'b', esc(where)));
      b.append(tx);
      b.onclick = () => { results.hidden = true; openQuest(h.q.id); };
      results.append(b);
    }
    if (hits.length > 80) results.append(el('div', 'sr-empty', `${hits.length - 80} more… refine your search`));
    results.hidden = false;
  }
  function markSearchMatches() {
    nodes.forEach((n, id) => {
      n.classList.toggle('match', matchSet.has(id));
      n.classList.toggle('faded', matchSet.size > 0 && !matchSet.has(id));
    });
  }
  let searchT = 0;
  search.addEventListener('input', () => { clearTimeout(searchT); searchT = setTimeout(runSearch, 120); });
  search.addEventListener('focus', () => { if (search.value.trim()) results.hidden = false; });
  search.addEventListener('keydown', e => {
    const items = [...results.querySelectorAll('.sr')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      activeResult = Math.max(0, Math.min(items.length - 1, activeResult + (e.key === 'ArrowDown' ? 1 : -1)));
      items.forEach((x, i) => x.classList.toggle('active', i === activeResult));
      items[activeResult]?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      (items[activeResult] || items[0])?.click();
    } else if (e.key === 'Escape') {
      search.value = ''; runSearch(); search.blur();
    }
  });
  document.addEventListener('pointerdown', e => { if (!e.target.closest('#search-wrap')) results.hidden = true; });

  // ------------------------------------------------------------ routing
  function go(c, questId) {
    const target = questId ? `#/${c.filename}/${questId}` : `#/${c.filename}`;
    if (location.hash !== target) history.pushState(null, '', target);
    route();
  }
  function route() {
    const m = location.hash.match(/^#\/([^/]+)(?:\/([0-9A-Fa-f]+))?/);
    let c = m && chapterByFile.get(decodeURIComponent(m[1]));
    if (!c) c = DB.chapters[0];
    if (c !== current) { renderChapter(c); fit(); results.hidden = true; }
    const qid = m && m[2] && m[2].toUpperCase();
    if (qid && questIndex.has(qid)) {
      openQuest(qid);
      const q = questIndex.get(qid).quest;
      if (questIndex.get(qid).chapter === c) centerOn(q);
    } else if (!qv.hidden) {
      qv.hidden = true;
    }
  }
  addEventListener('popstate', route);
  addEventListener('hashchange', route);

  // ------------------------------------------------------------ misc ui
  $('#fit-btn').onclick = fit;
  $('#menu-btn').onclick = () => document.body.classList.toggle('drawer');
  $('#font-btn').onclick = () => {
    document.documentElement.classList.toggle('pixel');
    store.set('pixel-font', document.documentElement.classList.contains('pixel') ? '1' : '0');
  };
  if (store.get('pixel-font') !== '0') document.documentElement.classList.add('pixel');
  viewport.addEventListener('pointerdown', () => document.body.classList.remove('drawer'));
  addEventListener('keydown', e => {
    if (e.target === search) return;
    if (e.key === 'Escape') {
      if (!tv.hidden) { tv.hidden = true; hideTip(); }
      else if (!qv.hidden) closeQuest();
      else document.body.classList.remove('drawer');
    } else if (e.key === '/' || (e.key === 'k' && (e.ctrlKey || e.metaKey))) {
      e.preventDefault(); search.focus(); search.select();
    } else if ((e.key === 'f' || e.key === 'F') && qv.hidden && !e.ctrlKey && !e.metaKey) {
      fit();
    } else if (!qv.hidden && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      if (e.key === 'ArrowRight' && qvPage < qvPages.length - 1) { qvPage++; showPage(); }
      if (e.key === 'ArrowLeft' && qvPage > 0) { qvPage--; showPage(); }
    } else if (qv.hidden && (e.key === '+' || e.key === '=' || e.key === '-')) {
      zoomAt(e.key === '-' ? 1 / 1.2 : 1.2, viewport.clientWidth / 2, viewport.clientHeight / 2);
    }
  });
  let resizeT = 0;
  addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (qv.hidden) fit(); }, 150); });

  // ------------------------------------------------------------ boot
  fetch('data/quests.json').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(data => {
    DB = data;
    ICONS = new Set(DB.icons);
    for (const c of DB.chapters) {
      chapterById.set(c.id, c); chapterByFile.set(c.filename, c);
      for (const q of c.quests) questIndex.set(q.id, { quest: q, chapter: c });
    }
    for (const c of DB.chapters) for (const q of c.quests) for (const d of q.deps) {
      if (!dependents.has(d)) dependents.set(d, []);
      dependents.get(d).push(q.id);
    }
    document.title = DB.siteTitle;
    $('.pack-title').innerHTML = fmt(DB.title);
    const pi = $('.pack-icon'); const u = iconUrl(DB.icon); if (u) pi.src = u; else pi.remove();
    buildChapterList();
    buildSearchIndex();
    search.value = '';  // browsers restore old input on reload
    route();
    $('#loading').hidden = true;
  }).catch(err => {
    $('#loading').textContent = 'Could not load quest data (' + err.message + '). Run tools/build.py first.';
  });
})();
