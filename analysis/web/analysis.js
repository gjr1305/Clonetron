// Clonetron Analysis: browse scans, stitch, count and correct colonies.
const $ = id => document.getElementById(id);
const WELL_ROWS = [['FL', 'ML', 'BL'], ['FR', 'MR', 'BR']];
const DEFAULT_PARAMS = {sensitivity: 4, min_area_mm2: 0.05, join_um: 40};

async function api(url, body) {
  const opt = body === undefined ? {} : {method: 'POST', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-Clonetron': '1'}};
  const r = await fetch(url, opt);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'Something went wrong');
  return d;
}
const q = s => encodeURIComponent(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ------------------------------------------------------------------ library
let curPath = null;
async function showLibrary(path) {
  scan = null;
  $('library').hidden = false;
  ['viewer', 'plateDock', 'wellDock', 'hud'].forEach(id => $(id).hidden = true);
  $('emptybg').hidden = false;
  $('navScans').classList.add('on'); $('navScan').classList.remove('on');
  const places = await api('/api/places');
  const chips = [];
  places.drives.forEach(d => chips.push(`<button class="usb" data-p="${esc(d)}">USB: ${esc(d)}</button>`));
  places.recent.forEach(d => chips.push(`<button data-p="${esc(d)}">${esc(shortPath(d))}</button>`));
  chips.push(`<button data-p="${esc(places.home)}">Home folder</button>`);
  $('places').innerHTML = chips.join('');
  $('places').querySelectorAll('button').forEach(b => b.onclick = () => listFolder(b.dataset.p));
  listFolder(path || curPath || places.drives[0] || places.recent[0] || places.home);
}
function shortPath(p) { const parts = p.split(/[\\/]/).filter(Boolean); return parts.slice(-2).join(' / '); }

async function listFolder(path) {
  let d;
  try { d = await api('/api/browse?path=' + q(path)); }
  catch (e) { $('listing').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  curPath = d.path;
  $('pathInput').value = d.path;
  $('upBtn').disabled = !d.parent;
  $('upBtn').onclick = () => d.parent && listFolder(d.parent);
  const folderIcon = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.5 3h4l1.5 1.5h7.5v8.5h-13z"/></svg>';
  const rows = d.scans.map((s, i) => {
    const tags = [];
    if (s.bad) tags.push('<span class="tag warn">damaged</span>');
    else {
      if (s.complete === false) tags.push('<span class="tag warn">stopped early</span>');
      tags.push(s.counted ? '<span class="tag done">counted</span>' : s.stitched ? '<span class="tag done">stitched</span>' : '<span class="tag">new</span>');
      if (!s.file) tags.push('<span class="tag">folder</span>');
    }
    const meta = [s.date && `${s.date} ${s.time || ''}`, s.wells && `${s.wells.length} wells`,
                  s.photos && `${s.photos} photos`, s.mb != null && (s.mb >= 1000 ? `${(s.mb / 1000).toFixed(1)} GB` : `${s.mb} MB`)].filter(Boolean).join(' · ');
    return `<div class="scanrow" tabindex="0" data-i="${i}"><b>${esc(s.plate || s.name)}</b><span class="meta">${esc(meta)}</span><span class="tags">${tags.join('')}</span></div>`;
  });
  const dirs = d.dirs.map(n => `<div class="dirrow" data-n="${esc(n)}">${folderIcon}${esc(n)}</div>`);
  $('listing').innerHTML = rows.join('') + dirs.join('') ||
    '<div class="empty">No scans or folders here. Scans are in the Clonetron folder on the scanner\'s USB stick.</div>';
  $('listing').querySelectorAll('.scanrow').forEach(el => {
    const open = () => { api('/api/remember', {path: d.path}); openScan(d.scans[+el.dataset.i].path); };
    el.onclick = open; el.onkeydown = e => { if (e.key === 'Enter') open(); };
  });
  $('listing').querySelectorAll('.dirrow').forEach(el => el.onclick = () =>
    listFolder(d.path + (d.path.includes('\\') ? '\\' : '/') + el.dataset.n));
}
$('goBtn').onclick = () => listFolder($('pathInput').value.trim());
$('pathInput').onkeydown = e => { if (e.key === 'Enter') listFolder($('pathInput').value.trim()); };
$('navScans').onclick = e => { e.preventDefault(); showLibrary(); };
$('navScan').onclick = e => { e.preventDefault(); if (scan) openScan(scan.path); };

// ------------------------------------------------------------------ one scan
let scan = null, well = null, cols = [], wellData = {}, undo = [], selected = null, tool = 'select';
let params = Object.assign({}, DEFAULT_PARAMS, JSON.parse(localStorage.getItem('ct-params') || '{}'));

async function openScan(path, keepWell) {
  scan = await api('/api/scan?path=' + q(path));
  $('library').hidden = true;
  $('plateDock').hidden = false;
  $('navScan').hidden = false; $('navScan').textContent = shortTitle();
  $('navScans').classList.remove('on'); $('navScan').classList.add('on');
  $('scanTitle').textContent = shortTitle();
  $('scanSub').textContent = (scan.made || '') + (scan.manifest && scan.manifest.complete === false ? ' · stopped early' : '');
  const withPhotos = scan.wells.filter(w => w.photos);
  const anyStitched = !withPhotos.some(w => !w.stitched), allCounted = withPhotos.every(w => w.counted);
  $('analyseBtn').textContent = !anyStitched ? 'Stitch and count' : allCounted ? 'Re-stitch' : 'Count colonies';
  $('analyseBtn').onclick = () => !anyStitched ? runJob('/api/analyse', {path, params})
    : allCounted ? (confirm('Stitch the photos again? Counts and your edits are kept.') && runJob('/api/stitch', {path, redo: true}))
    : runJob('/api/count', {path, params});
  const total = scan.wells.reduce((s, w) => s + (w.count || 0), 0);
  $('plateKv').innerHTML = `<span>Wells</span><span>${scan.wells.length}</span>` +
    `<span>Colonies</span><span>${scan.wells.some(w => w.counted) ? total : '–'}</span>` +
    `<span>Checked</span><span>${scan.wells.filter(w => w.reviewed).length} / ${scan.wells.length}</span>` +
    (scan.has_flat ? '' : `<span>Flat-field</span><span style="color:var(--warn)">missing</span>`);
  drawWellGrid();
  const target = (keepWell && scan.wells.find(w => w.name === keepWell && w.stitched)) || scan.wells.find(w => w.stitched);
  if (target) selectWell(target.name);
  else {
    $('viewer').hidden = true; $('wellDock').hidden = true; $('hud').hidden = true; $('emptybg').hidden = false;
  }
}
function shortTitle() {
  const m = scan.name.match(/^\d{4}-\d\d-\d\d_\d{6}_?(.*)$/);
  return (m && m[1]) || scan.name;
}

function drawWellGrid() {
  const have = Object.fromEntries(scan.wells.map(w => [w.name, w]));
  $('wellgrid').innerHTML = WELL_ROWS.flat().map(n => {
    const w = have[n];
    const cls = ['wellbtn', w && w.counted ? 'has' : '', w && w.reviewed ? 'ok' : '', n === well ? 'on' : ''].join(' ');
    const note = !w ? '' : w.reviewed ? 'checked' : !w.photos ? 'no photos' : !w.stitched ? 'not stitched' : '';
    return `<button class="${cls}" data-w="${n}" ${w && w.stitched ? '' : 'disabled'} title="${n}${w ? ': ' + w.photos + ' photos' : ''}">
      <span>${n}</span><b>${w && w.counted ? w.count : w && w.stitched ? '·' : ''}</b>${note ? `<small>${note}</small>` : ''}</button>`;
  }).join('');
  $('wellgrid').querySelectorAll('button').forEach(b => b.onclick = () => selectWell(b.dataset.w));
}

async function selectWell(name) {
  well = name; selected = null; undo = [];
  const w = scan.wells.find(x => x.name === name);
  drawWellGrid();
  $('viewer').hidden = false; $('wellDock').hidden = false; $('hud').hidden = false; $('emptybg').hidden = true;
  $('wellTitle').textContent = 'Well ' + name;
  wellData = await api(`/api/colonies?path=${q(scan.path)}&well=${name}`);
  cols = wellData.colonies || [];
  const img = $('wellimg');
  img.onload = () => {
    $('overlay').setAttribute('width', img.naturalWidth); $('overlay').setAttribute('height', img.naturalHeight);
    $('overlay').setAttribute('viewBox', `0 0 ${img.naturalWidth} ${img.naturalHeight}`);
    fit(); drawCols();
  };
  img.src = `/api/image?path=${q(scan.path)}&well=${name}&t=${Date.now()}`;
  $('reviewed').checked = !!wellData.reviewed;
  ppm = (w.info && w.info.px_per_mm) || wellData.px_per_mm || 1;
  wellPanel();
}
let ppm = 1;

function wellPanel() {
  const live = cols.filter(c => !c.removed);
  const counted = wellData.colonies !== undefined;
  $('wellCount').innerHTML = counted ? `${live.length}<small> colonies</small>` : '–';
  $('wellSub').textContent = counted ? (wellData.reviewed ? 'Checked by eye' : 'Not checked yet') : 'Not counted yet';
  const d = live.map(c => c.diameter_mm).sort((a, b) => a - b);
  const med = d.length ? d[Math.floor(d.length / 2)] : null;
  const man = live.filter(c => c.source === 'manual').length, rem = cols.filter(c => c.removed).length;
  $('wellKv').innerHTML = counted ? `<span>Median size</span><span>${med ? med.toFixed(2) + ' mm' : '–'}</span>` +
    `<span>Found</span><span>${cols.filter(c => c.source !== 'manual').length}</span>` +
    `<span>Added by you</span><span>${man}</span><span>Removed by you</span><span>${rem}</span>` : '';
  $('undoBtn').disabled = !undo.length;
  $('removeBtn').disabled = !selected;
  $('removeBtn').textContent = selected && selected.removed ? 'Put back' : 'Remove';
  $('toolHelp').textContent = tool === 'add'
    ? 'Drag from a colony\'s centre to its edge to add it. Right-drag moves the picture.'
    : 'Click a colony to select it, then Remove (or press Delete). Drag to move, scroll to zoom.';
}

// ------------------------------------------------------------------ drawing colonies
function drawCols() {
  const svg = $('overlay');
  svg.innerHTML = cols.map((c, i) =>
    `<polygon class="col ${c.source === 'manual' ? 'manual' : ''} ${c.removed ? 'removed' : ''} ${c === selected ? 'sel' : ''}"
      data-i="${i}" points="${c.poly.map(p => p.join(',')).join(' ')}"/>`).join('');
  svg.classList.toggle('hideout', !$('showOut').checked);
  wellPanel();
}
function save() {
  clearTimeout(save.t);
  save.t = setTimeout(() => api('/api/colonies', {path: scan.path, well, colonies: cols, px_per_mm: ppm,
    reviewed: $('reviewed').checked}).then(() => {
      const w = scan.wells.find(x => x.name === well);
      w.count = cols.filter(c => !c.removed).length; w.counted = true; w.reviewed = $('reviewed').checked;
      wellData.colonies = cols; wellData.reviewed = w.reviewed;
      drawWellGrid(); wellPanel();
    }), 300);
}
function edit(fn) {
  undo.push(JSON.stringify(cols)); if (undo.length > 50) undo.shift();
  fn(); drawCols(); save();
}
$('undoBtn').onclick = () => { if (!undo.length) return; cols = JSON.parse(undo.pop()); selected = null; drawCols(); save(); };
$('removeBtn').onclick = () => { if (selected) edit(() => { selected.removed = !selected.removed; }); };
$('reviewed').onchange = save;
$('showOut').onchange = drawCols;
document.querySelectorAll('#toolSeg button').forEach(b => b.onclick = () => {
  tool = b.dataset.t;
  document.querySelectorAll('#toolSeg button').forEach(x => x.classList.toggle('on', x === b));
  $('viewer').classList.toggle('add', tool === 'add');
  wellPanel();
});
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || !scan || $('viewer').hidden) return;
  if ((e.key === 'Delete' || e.key === 'Backspace') && selected) { $('removeBtn').click(); e.preventDefault(); }
  if (e.key === 'z' && (e.ctrlKey || e.metaKey)) { $('undoBtn').click(); e.preventDefault(); }
  if (e.key === 'a') document.querySelector('#toolSeg [data-t=add]').click();
  if (e.key === 's' || e.key === 'Escape') document.querySelector('#toolSeg [data-t=select]').click();
});

// ------------------------------------------------------------------ zoom and pan
let view = {x: 0, y: 0, s: 1};
function apply() {
  $('pan').style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.s})`;
  $('hZoom').textContent = Math.round(view.s * 100) + '%';
}
const phone = () => matchMedia('(max-width: 820px)').matches;
function fit() {
  const img = $('wellimg'), r = $('viewer').getBoundingClientRect(), W = r.width, H = r.height;
  const side = phone() ? 0 : W > 1440 ? 600 : 520, top = phone() ? 0 : 100;
  const s = Math.min((W - side) / img.naturalWidth, (H - top) / img.naturalHeight);
  view.s = Math.max(s, 0.02);
  view.x = (W - img.naturalWidth * view.s) / 2; view.y = (H - img.naturalHeight * view.s) / 2 + (phone() ? 0 : 20);
  apply();
}
const vrect = () => $('viewer').getBoundingClientRect();
function zoomAt(cx, cy, k) {
  const s2 = Math.min(Math.max(view.s * k, 0.02), 4);
  view.x = cx - (cx - view.x) * (s2 / view.s); view.y = cy - (cy - view.y) * (s2 / view.s); view.s = s2; apply();
}
$('fitBtn').onclick = fit;
$('oneBtn').onclick = () => { const r = vrect(); zoomAt(r.width / 2, r.height / 2, 1 / view.s); };
const toImg = e => { const r = vrect(); return [(e.clientX - r.left - view.x) / view.s, (e.clientY - r.top - view.y) / view.s]; };

const vw = $('viewer');
vw.addEventListener('wheel', e => { e.preventDefault(); const r = vrect(); zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015)); }, {passive: false});
// two-finger pinch on phones and tablets
const touches = new Map();
vw.addEventListener('touchmove', e => {
  if (e.touches.length !== 2) return;
  e.preventDefault();
  const [a, b] = e.touches, r = vrect();
  const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  const cx = (a.clientX + b.clientX) / 2 - r.left, cy = (a.clientY + b.clientY) / 2 - r.top;
  if (touches.d) zoomAt(cx, cy, d / touches.d);
  touches.d = d; drag = null;
}, {passive: false});
vw.addEventListener('touchend', () => { touches.d = 0; });
vw.addEventListener('contextmenu', e => e.preventDefault());
let drag = null;
vw.addEventListener('pointerdown', e => {
  const [ix, iy] = toImg(e);
  if (tool === 'add' && e.button === 0) {
    drag = {mode: 'draw', x0: ix, y0: iy, r: 0};
    $('overlay').insertAdjacentHTML('beforeend', `<circle class="draft" id="draft" cx="${ix}" cy="${iy}" r="0"/>`);
  } else {
    const hit = e.target.closest && e.target.closest('.col');
    drag = {mode: 'pan', sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y, moved: false, hit};
    vw.classList.add('dragging');
  }
  vw.setPointerCapture(e.pointerId);
});
vw.addEventListener('pointermove', e => {
  const [ix, iy] = toImg(e);
  $('hPos').textContent = `${(ix / ppm).toFixed(2)}, ${(iy / ppm).toFixed(2)} mm`;
  const over = e.target.closest && e.target.closest('.col');
  const c = over ? cols[+over.dataset.i] : selected;
  $('hCol').textContent = c ? `#${c.id} · ${c.diameter_mm.toFixed(2)} mm${c.removed ? ' · removed' : ''}` : '–';
  if (!drag) return;
  if (drag.mode === 'pan') {
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    view.x = drag.vx + dx; view.y = drag.vy + dy; apply();
  } else {
    drag.r = Math.hypot(ix - drag.x0, iy - drag.y0);
    $('draft').setAttribute('r', drag.r);
  }
});
vw.addEventListener('pointerup', e => {
  vw.classList.remove('dragging');
  if (!drag) return;
  const d = drag; drag = null;
  if (d.mode === 'pan' && !d.moved) {
    selected = d.hit ? cols[+d.hit.dataset.i] : null; drawCols();
  } else if (d.mode === 'draw') {
    $('draft') && $('draft').remove();
    const r = Math.max(d.r, 0.1 * ppm);                  // a click alone adds a 0.2 mm colony
    edit(() => {
      const poly = Array.from({length: 24}, (_, k) => [+(d.x0 + r * Math.cos(k * Math.PI / 12)).toFixed(1),
                                                        +(d.y0 + r * Math.sin(k * Math.PI / 12)).toFixed(1)]);
      const area = Math.PI * (r / ppm) ** 2;
      const c = {id: Math.max(0, ...cols.map(c => c.id)) + 1, x: +d.x0.toFixed(1), y: +d.y0.toFixed(1),
                 area_mm2: +area.toFixed(4), diameter_mm: +(2 * r / ppm).toFixed(3), poly, source: 'manual'};
      cols.push(c); selected = c;
    });
  }
});
addEventListener('resize', () => scan && !$('viewer').hidden && fit());

// ------------------------------------------------------------------ settings, export
function showParams() {
  $('joinU').value = params.join_um; $('joinUV').textContent = params.join_um + ' µm';
  $('sens').value = params.sensitivity; $('sensV').textContent = params.sensitivity;
  $('minA').value = params.min_area_mm2; $('minAV').textContent = params.min_area_mm2 + ' mm²';
}
$('sens').oninput = () => { params.sensitivity = +$('sens').value; localStorage.setItem('ct-params', JSON.stringify(params)); showParams(); };
$('joinU').oninput = () => { params.join_um = +$('joinU').value; localStorage.setItem('ct-params', JSON.stringify(params)); showParams(); };
$('minA').oninput = () => { params.min_area_mm2 = +$('minA').value; localStorage.setItem('ct-params', JSON.stringify(params)); showParams(); };
showParams();
const confirmRecount = () => confirm('Count again with these settings? Colonies you removed come back; colonies you added are kept.');
$('recountBtn').onclick = () => confirmRecount() && runJob('/api/count', {path: scan.path, params});
$('recountWellBtn').onclick = () => confirmRecount() && runJob('/api/count', {path: scan.path, params, wells: [well]});
$('freshBtn').onclick = () => confirm('Start over? Every well is stitched again and counted from scratch with the current settings. All your edits (added and removed colonies, "checked by eye") are lost.')
  && runJob('/api/analyse', {path: scan.path, params, fresh: true});
$('exportBtn').onclick = async () => {
  try {
    const d = await api('/api/export', {path: scan.path});
    $('exportMsg').textContent = 'Saved counts.csv and colonies.csv in ' + d.folder;
  } catch (e) { $('exportMsg').textContent = e.message; }
};
$('folderBtn').onclick = async () => {
  const d = await api('/api/show', {path: scan.path});
  $('exportMsg').textContent = 'Results are in ' + d.folder;
};

// ------------------------------------------------------------------ background jobs
async function runJob(url, body) {
  try { await api(url, body); } catch (e) { alert(e.message); return; }
  pollJob();
}
async function pollJob() {
  const j = await api('/api/job');
  const running = j.status === 'running';
  $('jobcard').hidden = j.status === 'idle' || (!running && pollJob.dismissed === j.started);
  $('jobchip').hidden = !running;
  if (running) $('jobchip').textContent = j.title;
  $('jobTitle').textContent = j.title;
  $('jobStatus').textContent = {running: 'working…', done: 'finished', failed: 'problem'}[j.status] || '';
  $('jobStatus').className = 'status ' + ({done: 'ok', failed: 'bad'}[j.status] || 'warn');
  const p = j.progress;
  $('jobBar').style.width = (j.status === 'done' ? 100 : p ? Math.round(100 * p[0] / p[1]) : 4) + '%';
  const log = $('jobLog'); log.textContent = (j.log || []).join('\n'); log.scrollTop = log.scrollHeight;
  if (running) setTimeout(pollJob, 700);
  else if (pollJob.was) {
    if (scan && j.path === scan.path) openScan(scan.path, well);
    if (j.status === 'done') setTimeout(() => { pollJob.dismissed = j.started; $('jobcard').hidden = true; }, 4000);
  }
  pollJob.was = running;
}

showLibrary();
api('/api/job').then(j => { if (j.status !== 'running') pollJob.dismissed = j.started; pollJob(); });
