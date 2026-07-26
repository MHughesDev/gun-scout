/* Shared search shell for all three engines (guns / parts / ammo).
   The page is fully data-driven by /api/<vertical>/schema: the left form is
   built from schema.inputs, the results table/grid + sort options from
   schema.columns. Everything else (polling, sort, grid/table, watch, CSV,
   lightbox, history, health) is vertical-agnostic. */
const $ = id => document.getElementById(id);

/* All three engines live on one page; the segmented picker (#vertPicker) swaps
   the active engine in place — no full reload. The URL path is kept in sync via
   pushState so refresh / deep-links / back still land on the right engine.
   VERTICAL is mutable state, seeded from the path the page was opened at. */
const VERT_PATHS = {guns: '/', parts: '/parts', ammo: '/ammo'};
let VERTICAL = {'/': 'guns', '/parts': 'parts', '/ammo': 'ammo'}[location.pathname] || 'guns';
let VERTICALS = [];   // [{id,label,path}] from /api/verticals, for the picker

let SCHEMA = null, COLUMNS = [], PRESETS = {};
let PUBLIC = false;   // hosted mode: operator-only UI (health, raw errors) hidden
let currentSearch = null, lastId = 0, rows = [], pollTimer = null;
let sortKey = null, sortDir = 1;
const SITE_LABELS = {};

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

/* ---------------- combobox (unchanged behavior) ---------------- */
function comboScore(value, q) {
  const v = value.toLowerCase();
  if (v === q) return 5;
  if (v.startsWith(q)) return 4;
  if (new RegExp('\\b' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).test(v)) return 3;
  if (v.includes(q)) return 2;
  let i = 0;
  for (const ch of v) if (ch === q[i] && ++i === q.length) return 1;
  return 0;
}
function makeCombo(inputId, values) {
  const input = $(inputId);
  const wrap = input.parentElement;
  const btn = wrap.querySelector('.combo-btn');
  const list = wrap.querySelector('.combo-list');
  let active = -1;
  const mark = (v, q) => {
    const i = q ? v.toLowerCase().indexOf(q) : -1;
    return i < 0 ? esc(v)
      : esc(v.slice(0, i)) + '<b>' + esc(v.slice(i, i + q.length)) + '</b>' + esc(v.slice(i + q.length));
  };
  function render(filter) {
    const q = (filter || '').toLowerCase();
    active = -1;
    let html;
    if (!q) {
      html = values.map(v => `<div data-v="${esc(v)}">${esc(v)}</div>`).join('');
    } else {
      const ranked = values.map((v, i) => ({v, i, s: comboScore(v, q)}))
        .sort((a, b) => b.s - a.s || a.i - b.i);
      const nHit = ranked.filter(r => r.s > 0).length;
      html = ranked.map((r, i) =>
        (i === nHit ? `<div class="sep">${nHit ? 'other presets' : 'no close match — free text is fine'}</div>` : '')
        + `<div class="${r.s ? '' : 'dim'}" data-v="${esc(r.v)}">${r.s ? mark(r.v, q) : esc(r.v)}</div>`
      ).join('');
    }
    list.innerHTML = html || '<div class="none">no presets — free text is fine</div>';
    list.scrollTop = 0; list.hidden = false;
  }
  const close = () => { list.hidden = true; active = -1; };
  input.addEventListener('input', () => render(input.value.trim()));
  input.addEventListener('focus', () => render(input.value.trim()));
  input.addEventListener('keydown', e => {
    const items = [...list.querySelectorAll('div[data-v]')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (list.hidden) { render(input.value.trim()); return; }
      e.preventDefault();
      active = e.key === 'ArrowDown' ? Math.min(active + 1, items.length - 1) : Math.max(active - 1, 0);
      items.forEach((el, i) => el.classList.toggle('active', i === active));
      if (items[active]) items[active].scrollIntoView({block: 'nearest'});
    } else if (e.key === 'Enter' && !list.hidden && active >= 0) {
      e.preventDefault(); input.value = items[active].dataset.v; close();
    } else if (e.key === 'Escape') close();
  });
  btn.onclick = () => { if (list.hidden) { render(''); input.focus(); } else close(); };
  list.addEventListener('mousedown', e => {
    const v = e.target.dataset && e.target.dataset.v;
    if (v != null) { input.value = v; close(); }
    e.preventDefault();
  });
  document.addEventListener('mousedown', e => { if (!wrap.contains(e.target)) close(); });
}

/* ---------------- build the form from schema.inputs ---------------- */
function comboMarkup(id, ph) {
  return `<div class="combo"><input id="${id}" placeholder="${esc(ph || '')}" autocomplete="off">
    <button class="combo-btn" type="button" tabindex="-1">▾</button>
    <div class="combo-list" hidden></div></div>`;
}
function fieldMarkup(f) {
  if (f.type === 'text')
    return `<label>${esc(f.label)}</label><input id="${f.id}" placeholder="${esc(f.placeholder || '')}">`;
  if (f.type === 'select') {
    const opts = (f.options || []).map(o => {
      const val = typeof o === 'object' ? o.value : o;
      const lab = typeof o === 'object' ? o.label : o;
      const sel = String(f.default ?? '') === String(val) ? ' selected' : '';
      return `<option value="${esc(val)}"${sel}>${esc(lab)}</option>`;
    }).join('');
    return `<label>${esc(f.label)}</label><select id="${f.id}">${opts}</select>`;
  }
  if (f.type === 'combo')
    return `<label>${esc(f.label)}</label>${comboMarkup(f.id, f.placeholder)}`;
  if (f.type === 'range')
    return `<label>${esc(f.label)}</label><div class="row2">
      ${comboMarkup(f.min_id, f.min_placeholder || 'min')}
      ${comboMarkup(f.max_id, f.max_placeholder || 'max')}</div>`;
  if (f.type === 'checkbox')
    return `<label class="chk" style="display:flex;align-items:center;gap:7px;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text)">
      <input type="checkbox" id="${f.id}" style="width:auto"${f.default ? ' checked' : ''}> ${esc(f.label)}</label>`;
  return '';
}
function buildForm() {
  $('formFields').innerHTML = SCHEMA.inputs.map(f =>
    `<div class="field">${fieldMarkup(f)}${f.note ? `<div class="note">${esc(f.note)}</div>` : ''}</div>`
  ).join('');
  for (const f of SCHEMA.inputs) {
    if (f.type === 'combo') makeCombo(f.id, PRESETS[f.presets] || []);
    if (f.type === 'range') {
      makeCombo(f.min_id, f.num_presets || []);
      makeCombo(f.max_id, f.num_presets || []);
    }
  }
}

/* collect the form into a flat criteria object; the backend routes any
   vertical-specific keys into filters{} */
function criteria() {
  const c = {vertical: VERTICAL,
             sites: [...document.querySelectorAll('.site:checked')].map(cb => cb.value)};
  for (const f of SCHEMA.inputs) {
    if (f.type === 'range') {
      const lo = ($(f.min_id).value || '').trim(), hi = ($(f.max_id).value || '').trim();
      if (lo) c[f.min_id] = lo;
      if (hi) c[f.max_id] = hi;
    } else if (f.type === 'checkbox') {
      c[f.id] = $(f.id).checked;
    } else {
      const v = ($(f.id).value || '').trim();
      if (v) c[f.id] = v;
    }
  }
  return c;
}
function fillForm(c) {
  for (const f of SCHEMA.inputs) {
    if (f.type === 'range') {
      $(f.min_id).value = c[f.min_id] != null ? c[f.min_id] : '';
      $(f.max_id).value = c[f.max_id] != null ? c[f.max_id] : '';
    } else if (f.type === 'checkbox') {
      $(f.id).checked = c[f.id] != null ? !!c[f.id] : !!f.default;
    } else {
      // value may be a top-level criteria field or inside filters{}
      const v = c[f.id] != null ? c[f.id] : (c.filters && c.filters[f.id]);
      $(f.id).value = v != null ? v : (f.default != null && f.type === 'select' ? f.default : '');
    }
  }
}

/* ---------------- build result columns from schema.columns ---------------- */
function buildColumns() {
  $('theadRow').innerHTML = COLUMNS.map(c =>
    `<th ${c.sortable ? `data-k="${c.key}"` : ''}>${esc(c.label)}</th>`).join('');
  $('sortSel').innerHTML = '<option value="">as received</option>' +
    COLUMNS.filter(c => c.sortable && c.render !== 'image')
      .map(c => `<option value="${c.key}">${esc(c.label || c.key)}</option>`).join('');
  document.querySelectorAll('#theadRow th[data-k]').forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    setSort(k, sortKey === k ? -sortDir : 1);
  });
}

/* ---------------- value formatting per render type ---------------- */
function money(v) { return '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2}); }
function priceHtml(l) {
  const isAuction = l.listing_type === 'auction' || l.listing_type === 'auction_buynow';
  const parts = [];
  if (l.price != null) parts.push(`<span class="price">${money(l.price)}</span>`);
  if (isAuction) {
    const bid = l.current_bid != null ? money(l.current_bid) : '—';
    const n = l.bid_count != null ? ` · ${l.bid_count} bid${l.bid_count === 1 ? '' : 's'}` : '';
    parts.push(`<span class="bid" title="current auction bid — not an asking price">bid ${bid}${n}</span>`);
  }
  return parts.join(' ') || '—';
}
function pprHtml(l) {
  if (l.price_per_round == null) return '';
  const v = Number(l.price_per_round);
  const s = v < 1 ? '¢' + (v * 100).toFixed(1) : '$' + v.toFixed(2);
  return `<span class="ppr" title="cost per round">${s}/rd</span>`;
}
function stockHtml(l) {
  if (l.in_stock == null) return '';
  return l.in_stock ? '<span class="stock-in">in stock</span>'
                    : '<span class="stock-out">out</span>';
}
function listedAgo(l) {
  if (l.posted_at == null) return '';
  const s = Date.now() / 1000 - l.posted_at;
  if (s < 0) return '';
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  const d = Math.floor(s / 86400);
  if (d < 30) return `${d}d ago`;
  if (d < 365) return `${Math.floor(d / 30)}mo ago`;
  return `${Math.floor(d / 365)}y ago`;
}
function endsIn(l) {
  if (l.ends_at == null) return '';
  const left = l.ends_at * 1000 - Date.now();
  if (left <= 0) return 'ended';
  const d = Math.floor(left / 86400000), h = Math.floor(left % 86400000 / 3600000),
        m = Math.floor(left % 3600000 / 60000);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}
/* inner HTML for one column cell */
function cell(col, l) {
  const v = l[col.key];
  switch (col.render) {
    case 'image': return l.image ? `<img src="${esc(l.image)}" loading="lazy" referrerpolicy="no-referrer" alt="">` : '';
    case 'title': return `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>${l.is_new ? '<span class="badge-new">NEW</span>' : ''}`;
    case 'price': return priceHtml(l);
    case 'pricePerRound': return pprHtml(l);
    case 'stock': return stockHtml(l);
    case 'ends': return endsIn(l);
    case 'listed': return listedAgo(l);
    case 'site': return SITE_LABELS[l.site] || l.site;
    case 'chip': return v ? `<span class="chip">${esc(v)}</span>` : '';
    case 'num': return v != null && v !== '' ? esc(v + (col.suffix || '')) : '';
    case 'inches': return v != null ? v + (col.suffix || '"') : '';
    default: return esc(v ?? '');
  }
}

/* ---------------- table + grid rendering ---------------- */
function rowHtml(l) {
  return '<tr>' + COLUMNS.map(c => {
    const cls = c.render === 'title' ? ' class="title"'
      : c.render === 'ends' ? ' class="ends"'
      : c.render === 'site' ? ' class="site-tag"' : '';
    return `<td${cls}>${cell(c, l)}</td>`;
  }).join('') + '</tr>';
}
function cardHtml(l) {
  const imgCol = COLUMNS.find(c => c.grid === 'image');
  const priceCol = COLUMNS.find(c => c.grid === 'price');
  const chips = COLUMNS.filter(c => c.grid === 'chip').map(c => cell(c, l)).filter(Boolean);
  const badges = COLUMNS.filter(c => c.grid === 'badge').map(c => cell(c, l)).filter(Boolean);
  const metaCols = COLUMNS.filter(c => c.grid === 'meta');
  const metas = metaCols.map(c => cell(c, l)).filter(Boolean);
  const img = (imgCol && l.image)
    ? `<img src="${esc(l.image)}" loading="lazy" referrerpolicy="no-referrer" alt="">`
    : '<div class="noimg">no photo</div>';
  const priceLine = priceCol ? cell(priceCol, l) : priceHtml(l);
  return `
    <div class="card" data-url="${esc(l.url)}" title="Open listing">
      <div class="card-img">${img}</div>
      <div class="card-body">
        <a class="card-title" href="${esc(l.url)}" target="_blank" rel="noopener" title="${esc(l.title)}">${esc(l.title)}</a>
        ${chips.length ? `<div class="card-chips">${chips.join(' ')}</div>` : ''}
        <div class="card-price">${priceLine}
          ${badges.join(' ')}
          ${l.is_new ? '<span class="badge-new">NEW</span>' : ''}</div>
        <div class="card-meta">
          <span class="site-tag">${esc(metas[0] || '')}</span>
          <span class="ends">${esc(metas.slice(1).join(' · '))}</span>
        </div>
      </div>
    </div>`;
}

/* ---------------- current view (filter + sort) ---------------- */
const hiddenSites = new Set();
function currentView() {
  let view = rows.filter(l => !hiddenSites.has(l.site));
  const q = $('quickFilter').value.trim().toLowerCase();
  if (q) {
    view = view.filter(l => COLUMNS.map(c => l[c.key])
      .filter(v => typeof v === 'string').join(' ').toLowerCase().includes(q)
      || `${l.title} ${l.site}`.toLowerCase().includes(q));
  }
  if (sortKey) {
    view = view.slice().sort((a, b) => {
      let x = a[sortKey], y = b[sortKey];
      if (x == null) return 1; if (y == null) return -1;
      if (typeof x === 'string') { x = x.toLowerCase(); y = (y || '').toLowerCase(); }
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
  }
  return view;
}
function primaryPrice(l) { return VERTICAL === 'ammo' ? l.price_per_round : l.price; }
function renderStats(view) {
  const prices = view.map(primaryPrice).filter(p => p != null).sort((a, b) => a - b);
  const sites = new Set(view.map(l => l.site)).size;
  let s = `${view.length} listing${view.length === 1 ? '' : 's'} · ${sites} site${sites === 1 ? '' : 's'}`;
  if (prices.length) {
    const median = prices[Math.floor(prices.length / 2)];
    const fmt = VERTICAL === 'ammo'
      ? v => (v < 1 ? '¢' + (v * 100).toFixed(1) + '/rd' : '$' + v.toFixed(2) + '/rd')
      : v => '$' + Number(v).toLocaleString();
    s += ` · ${fmt(prices[0])} low · ${fmt(median)} median`;
  }
  $('stats').textContent = s;
}

let viewMode = localStorage.getItem('gs_view_' + VERTICAL) || (SCHEMA ? SCHEMA.defaults.view : 'table');
function setView(mode) {
  viewMode = mode;
  localStorage.setItem('gs_view_' + VERTICAL, mode);
  document.querySelectorAll('#viewToggle button').forEach(b => b.classList.toggle('active', b.dataset.view === mode));
  renderRows();
}
function renderRows() {
  if (!rows.length) {
    $('tbl').style.display = 'none'; $('grid').style.display = 'none';
    $('empty').style.display = ''; $('toolbar').style.display = 'none';
    updateHScroll(); return;
  }
  $('empty').style.display = 'none'; $('toolbar').style.display = '';
  const view = currentView();
  renderStats(view);
  if (viewMode === 'grid') {
    $('tbl').style.display = 'none'; $('grid').style.display = '';
    $('grid').innerHTML = view.map(cardHtml).join('');
  } else {
    $('grid').style.display = 'none'; $('tbl').style.display = '';
    $('tbody').innerHTML = view.map(rowHtml).join('');
  }
  updateHScroll();
}

/* ---------------- search + poll ---------------- */
async function runSearch() {
  const c = criteria();
  clearInterval(pollTimer);
  rows = []; lastId = 0; renderRows();
  $('empty').textContent = 'Searching…';
  $('empty').style.display = ''; $('tbl').style.display = 'none';
  $('messages').innerHTML = '';
  $('searchBtn').disabled = true;
  if (isNarrow()) collapseFilters(true);
  const r = await fetch(`/api/${VERTICAL}/search`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(c),
  });
  currentSearch = (await r.json()).search_id;
  rememberSearch(c);
  pollTimer = setInterval(poll, 1200);
  poll();
}
async function poll() {
  if (currentSearch == null) return;
  const r = await fetch(`/api/search/${currentSearch}?after=${lastId}`);
  if (!r.ok) return;
  const s = await r.json();
  renderStatus(s.clients, s.status);
  if (s.listings.length) {
    for (const l of s.listings) { rows.push(l); lastId = Math.max(lastId, l.id); }
    renderRows();
  }
  if (s.status === 'done') {
    clearInterval(pollTimer);
    $('searchBtn').disabled = false;
    if (!rows.length) $('empty').textContent = 'No matches found.';
    onSearchDone();
  }
}
function onSearchDone() {
  const nNew = rows.filter(l => l.is_new).length;
  if (watchMinutes > 0) {
    scheduleWatch();
    if (nNew > 0) {
      document.title = `(${nNew} new) ${SCHEMA.label} — Gun Scout`;
      if (Notification.permission === 'granted')
        new Notification('Gun Scout', {body: `${nNew} new ${VERTICAL} listing${nNew > 1 ? 's' : ''} found`});
    }
  }
}

/* ---------------- status pills ---------------- */
let lastClients = [], lastStatus = 'done';
function renderStatus(clients, overall) {
  lastClients = clients; lastStatus = overall;
  const spin = overall !== 'done' ? '<div class="spinner" title="searching"></div>' : '';
  if (!clients.length && overall === 'done')
    $('statusBar').innerHTML = '<span class="site-tag">No data sources configured for this engine yet.</span>';
  else
    $('statusBar').innerHTML = spin + clients.map(c => `
      <div class="pill ${c.status} ${hiddenSites.has(c.site) ? 'off' : ''}"
           data-site="${esc(c.site)}" title="Click to hide/show this site's results">
        <div class="dot"></div>${SITE_LABELS[c.site] || c.site}
        <small>${c.status} · ${c.found} found</small></div>`).join('');
  document.querySelectorAll('#statusBar .pill').forEach(p => p.onclick = () => {
    const s = p.dataset.site;
    if (hiddenSites.has(s)) hiddenSites.delete(s); else hiddenSites.add(s);
    renderStatus(lastClients, lastStatus); renderRows();
  });
  // raw scraper diagnostics (HTTP codes, tracebacks) are operator info — the
  // hosted app shows only the pills, plus 'unavailable', whose message is
  // plain English a visitor should actually see
  $('messages').innerHTML = clients
    .filter(c => c.message && (!PUBLIC || c.status === 'unavailable'))
    .map(c => `<div class="msg">⚠ ${SITE_LABELS[c.site] || c.site}: ${esc(c.message)}</div>`).join('');
}

/* ---------------- sort ---------------- */
function setSort(key, dir) { sortKey = key || null; sortDir = dir; syncSortUI(); renderRows(); }
function syncSortUI() {
  $('sortSel').value = sortKey || '';
  const btn = $('sortDirBtn');
  btn.disabled = !sortKey;
  btn.textContent = sortDir === 1 ? '▲' : '▼';
  btn.title = sortDir === 1 ? 'Ascending — click for descending' : 'Descending — click for ascending';
}

/* ---------------- watch mode ---------------- */
let watchMinutes = 0, watchTimer = null, watchDeadline = 0, watchTick = null;
function scheduleWatch() {
  clearTimeout(watchTimer); clearInterval(watchTick);
  watchDeadline = Date.now() + watchMinutes * 60000;
  watchTimer = setTimeout(() => { if (!$('searchBtn').disabled) runSearch(); else scheduleWatch(); }, watchMinutes * 60000);
  watchTick = setInterval(() => {
    const left = Math.max(0, watchDeadline - Date.now());
    const m = Math.floor(left / 60000), sec = Math.floor((left % 60000) / 1000);
    $('watchStatus').textContent = `next in ${m}:${String(sec).padStart(2, '0')}`;
  }, 1000);
}

/* ---------------- history (client-side, this session only) ----------------
   Only the INPUTS of past searches are remembered — never results — and only
   in this browser's sessionStorage, so history dies with the session and the
   server keeps nothing about who searched for what. Clicking an item just
   prefills the form. */
const HIST_MAX = 8;
function histKey() { return 'gs_hist_' + VERTICAL; }
function histList() {
  try { return JSON.parse(sessionStorage.getItem(histKey())) || []; }
  catch (e) { return []; }
}
function rememberSearch(c) {
  const sig = JSON.stringify(c);
  const list = histList().filter(h => JSON.stringify(h.criteria) !== sig);
  list.unshift({criteria: c, at: Date.now()});
  try { sessionStorage.setItem(histKey(), JSON.stringify(list.slice(0, HIST_MAX))); }
  catch (e) { /* storage unavailable — history is a nicety, searching still works */ }
  loadHistory();
}
function loadHistory() {
  const list = histList();
  $('historyList').innerHTML = list.map((h, i) => {
    const c = h.criteria;
    const bits = [c.keyword, c.manufacturer, c.caliber,
      c.condition && c.condition !== 'both' ? c.condition : '',
      c.price_max != null ? '≤$' + c.price_max : ''].filter(Boolean);
    const when = new Date(h.at).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
    return `<div class="hist-item" data-i="${i}" title="Prefill these filters">${esc(bits.join(' · ') || '(broad search)')}
      <small>${when}</small></div>`;
  }).join('');
  document.querySelectorAll('.hist-item').forEach(el => el.onclick = () => {
    const h = histList()[parseInt(el.dataset.i, 10)];
    if (!h) return;
    fillForm(h.criteria);
    document.querySelectorAll('.site').forEach(cb =>
      cb.checked = !h.criteria.sites || !h.criteria.sites.length || h.criteria.sites.includes(cb.value));
  });
}

/* ---------------- CSV ---------------- */
$('csvBtn').onclick = () => {
  const cols = COLUMNS.map(c => c.key).filter(k => k !== 'image').concat(['url']);
  const lines = [cols.join(',')];
  for (const l of currentView()) {
    lines.push(cols.map(k => {
      let v = l[k]; if (v == null) v = '';
      v = String(v).replace(/"/g, '""');
      return /[",\n]/.test(v) ? `"${v}"` : v;
    }).join(','));
  }
  const blob = new Blob([lines.join('\n')], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `gun_scout_${VERTICAL}_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.csv`;
  a.click(); URL.revokeObjectURL(a.href);
};
$('quickFilter').oninput = () => renderRows();

/* ---------------- lightbox ---------------- */
const lightbox = $('lightbox');
function openLightbox(src) { lightbox.querySelector('img').src = src; lightbox.hidden = false; }
/* grid: a click anywhere on a card opens the listing (no photo lightbox) */
$('grid').addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card || !card.dataset.url) return;
  if (e.target.closest('a')) return;   // the title link already navigates itself
  window.open(card.dataset.url, '_blank', 'noopener');
});
$('tbody').addEventListener('click', e => { if (e.target.matches('td img')) openLightbox(e.target.src); });
lightbox.onclick = () => { lightbox.hidden = true; lightbox.querySelector('img').src = ''; };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !lightbox.hidden) lightbox.onclick(); });

/* ---------------- sticky horizontal scrollbar ---------------- */
const hscroll = $('hscroll'), tblWrap = $('tblWrap');
hscroll.onscroll = () => { tblWrap.scrollLeft = hscroll.scrollLeft; };
tblWrap.onscroll = () => { hscroll.scrollLeft = tblWrap.scrollLeft; };
let wheelAxis = null, wheelAxisReset = null;
window.addEventListener('wheel', e => {
  if (hscroll.style.display === 'none') return;
  let dx = e.deltaX, dy = e.deltaY;
  if (e.shiftKey && !dx) { dx = dy; dy = 0; }
  clearTimeout(wheelAxisReset);
  wheelAxisReset = setTimeout(() => { wheelAxis = null; }, 250);
  if (wheelAxis === null && (dx || dy)) wheelAxis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
  if (wheelAxis !== 'x') return;
  tblWrap.scrollLeft += dx * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? tblWrap.clientWidth : 1);
  if (e.cancelable) e.preventDefault();
}, {passive: false, capture: true});
function updateHScroll() {
  const need = $('tbl').style.display !== 'none' && tblWrap.scrollWidth > tblWrap.clientWidth;
  hscroll.style.display = need ? 'block' : 'none';
  hscroll.firstElementChild.style.width = tblWrap.scrollWidth + 'px';
}
window.addEventListener('resize', updateHScroll);

/* ---------------- health ---------------- */
const STATUS_WORDS = {ok: 'ok', degraded: 'degraded', blocked: 'blocked',
  schema_changed: 'STRUCTURE CHANGED', error: 'error', schema: 'STRUCTURE CHANGED'};
function renderHealth(list) {
  list = list.filter(h => !h.vertical || h.vertical === VERTICAL);
  $('healthPanel').innerHTML = list.map(h => `
    <div class="hrow ${h.status}"><div class="dot"></div>
      <b>${esc(h.label || SITE_LABELS[h.site] || h.site)}</b>
      <span>${STATUS_WORDS[h.status] || h.status}</span>
      <small>${h.status === 'ok' ? h.found + ' hits · ' + h.elapsed_ms + 'ms' : ''}</small></div>
    ${h.message ? `<div class="hmsg">${esc(h.message)}</div>` : ''}`).join('');
}
$('healthBtn').onclick = async () => {
  $('healthBtn').disabled = true; $('healthBtn').textContent = 'Checking…';
  try { renderHealth(await (await fetch('/api/health', {method: 'POST'})).json()); }
  finally { $('healthBtn').disabled = false; $('healthBtn').textContent = 'Check site health'; }
};

/* ---------------- clear search (client-side only) ----------------
   Resets the form to defaults and empties the results table. Deletes
   nothing: history keeps its entries and the server (which only ever holds
   in-flight results transiently) is not called at all. */
$('clearSearchBtn').onclick = () => {
  clearInterval(pollTimer); clearTimeout(watchTimer); clearInterval(watchTick);
  watchMinutes = 0; $('watchSel').value = '0'; $('watchStatus').textContent = '';
  document.title = SCHEMA.label + ' — Gun Scout';
  currentSearch = null; rows = []; lastId = 0; hiddenSites.clear();
  lastClients = []; lastStatus = 'done';
  $('statusBar').innerHTML = ''; $('messages').innerHTML = '';
  $('quickFilter').value = '';
  fillForm({});   // every input back to its schema default
  document.querySelectorAll('.site').forEach(cb => cb.checked = true);
  renderRows();
  $('empty').innerHTML = 'Search cleared. Set your filters and hit <b>Search</b>.';
  $('searchBtn').disabled = false;
};

/* ---------------- wiring that needs the schema ---------------- */
$('searchBtn').onclick = runSearch;
$('watchSel').onchange = () => {
  watchMinutes = parseInt($('watchSel').value, 10);
  clearTimeout(watchTimer); clearInterval(watchTick);
  $('watchStatus').textContent = ''; document.title = SCHEMA.label + ' — Gun Scout';
  if (watchMinutes > 0) { if (Notification.permission === 'default') Notification.requestPermission(); scheduleWatch(); }
};
$('sortSel').onchange = () => setSort($('sortSel').value, 1);
$('sortDirBtn').onclick = () => setSort(sortKey, -sortDir);

/* header nav — the vertical picker handles engine switching, so the header
   only carries the cross-engine pages */
function loadNav() {
  $('appNav').innerHTML = '<a href="/" class="active">Search</a>'
    + '<a href="/stats">Stats</a><a href="/ballistics">Ballistics</a>';
}

/* ---------------- mobile filter drawer ----------------
   Below 760px the filter rail stacks above the results, which would bury them
   a full screen down — so it collapses on search and the header button reopens it. */
const isNarrow = () => window.matchMedia('(max-width: 760px)').matches;
function syncFiltersToggle() {
  $('filtersToggle').textContent =
    document.body.classList.contains('filters-collapsed') ? 'Filters ▾' : 'Filters ▴';
}
function collapseFilters(on) {
  document.body.classList.toggle('filters-collapsed', on);
  syncFiltersToggle();
}
$('filtersToggle').onclick = () =>
  collapseFilters(!document.body.classList.contains('filters-collapsed'));
function buildVertPicker() {
  $('vertPicker').innerHTML = VERTICALS.map(v =>
    `<button data-v="${esc(v.id)}"${v.id === VERTICAL ? ' class="active"' : ''}>${esc(v.label)}</button>`
  ).join('');
  document.querySelectorAll('#vertPicker button').forEach(b =>
    b.onclick = () => switchVertical(b.dataset.v));
}
function markActiveVert() {
  document.querySelectorAll('#vertPicker button')
    .forEach(b => b.classList.toggle('active', b.dataset.v === VERTICAL));
}
async function loadSites() {
  const sites = await (await fetch(`/api/${VERTICAL}/clients`)).json();
  $('siteList').innerHTML = sites.length ? sites.map(s => {
    SITE_LABELS[s.name] = s.label;
    return `<label><input type="checkbox" class="site" value="${s.name}" checked> ${esc(s.label)}</label>`;
  }).join('') : '<span class="site-tag">No sources yet — coming soon.</span>';
}

/* ---------------- swap the active engine in place ---------------- */
async function switchVertical(vid) {
  if (vid === VERTICAL || !VERT_PATHS[vid]) return;
  VERTICAL = vid;
  history.pushState({vertical: vid}, '', VERT_PATHS[vid]);
  await loadVertical();
}
window.addEventListener('popstate', () => {
  const vid = {'/': 'guns', '/parts': 'parts', '/ammo': 'ammo'}[location.pathname] || 'guns';
  if (vid !== VERTICAL) { VERTICAL = vid; loadVertical(); }
});

/* (re)build the whole engine-specific UI + state for the current VERTICAL */
async function loadVertical() {
  // stop anything the previous engine had running
  clearInterval(pollTimer); clearTimeout(watchTimer); clearInterval(watchTick);
  watchMinutes = 0; $('watchSel').value = '0'; $('watchStatus').textContent = '';
  currentSearch = null; rows = []; lastId = 0; hiddenSites.clear();
  lastClients = []; lastStatus = 'done';
  $('statusBar').innerHTML = ''; $('messages').innerHTML = '';
  $('healthPanel').innerHTML = ''; $('searchBtn').disabled = false;

  SCHEMA = await (await fetch(`/api/${VERTICAL}/schema`)).json();
  COLUMNS = SCHEMA.columns; PRESETS = SCHEMA.presets;
  document.title = SCHEMA.label + ' — Gun Scout';
  $('appSub').textContent = SCHEMA.label.toLowerCase() + ' search';
  markActiveVert();
  buildForm(); buildColumns();
  // defaults from schema (view is remembered per engine)
  sortKey = SCHEMA.defaults.sort.key || null; sortDir = SCHEMA.defaults.sort.dir || 1;
  viewMode = localStorage.getItem('gs_view_' + VERTICAL) || SCHEMA.defaults.view;
  document.querySelectorAll('#viewToggle button')
    .forEach(b => b.classList.toggle('active', b.dataset.view === viewMode));
  syncSortUI();
  $('empty').innerHTML = 'Set your filters and hit <b>Search</b>.';
  renderRows();
  await loadSites();
  if (!PUBLIC) fetch('/api/health').then(r => r.json()).then(renderHealth);
  loadHistory();
}

(async () => {
  $('appTitle').textContent = 'Gun Scout';
  loadNav();
  document.querySelectorAll('#viewToggle button')
    .forEach(b => b.onclick = () => setView(b.dataset.view));
  try { PUBLIC = !!(await (await fetch('/api/config')).json()).public; }
  catch (e) { /* default: dev mode */ }
  if (PUBLIC) { $('healthBtn').style.display = 'none'; $('healthPanel').style.display = 'none'; }
  VERTICALS = await (await fetch('/api/verticals')).json();
  buildVertPicker();
  await loadVertical();
})();
