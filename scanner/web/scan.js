// Clonetron Scan: pick wells on the plate map, start a scan to USB, watch it fill in.
const $ = id => document.getElementById(id);
let info = null, usbInfo = null, running = false, showingResult = false, flatAt = null, ffRunning = false;
const chosen = new Set();

async function getJSON(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  return r.json();
}
async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(body || {})});
  if (r.status === 401) { location.href = '/login'; return {}; }
  return r.json();
}
const gb = b => (b / 1e9).toFixed(1) + ' GB';

// ---- plate map: click to choose wells
const map = PlateMap($('platemap'), {onClick: w => {
  if (running) return;
  chosen.has(w) ? chosen.delete(w) : chosen.add(w);
  paintChoice(); refreshForm();
}});
function paintChoice() { map.names().forEach(w => map.mark(w, chosen.has(w) ? 'selected' : '', 0)); }
$('allBtn').onclick = () => { map.names().forEach(w => chosen.add(w)); paintChoice(); refreshForm(); };
$('noneBtn').onclick = () => { chosen.clear(); paintChoice(); refreshForm(); };

const mode = 'map';            // every scan maps the focus first

function orderedWells() { return (info ? info.order : []).filter(w => chosen.has(w)); }

async function refreshForm() {
  const wells = orderedWells();
  let why = '';
  const usbOk = usbInfo && usbInfo.stick && usbInfo.stick.writable;
  if (!wells.length) why = 'Choose at least one well on the plate.';
  else if (!usbOk) why = 'Plug a USB stick into the Pi. Scans are only saved to USB.';
  if (!flatAt && !why) why = 'Do the flat-field first (step 1).';
  if (wells.length) {
    const e = await getJSON('/api/estimate?wells=' + wells.join(','));
    $('est').innerHTML = `${wells.length} well${wells.length > 1 ? 's' : ''} &nbsp; ~${e.photos} photos<br>~${e.minutes} min &nbsp; ~${(e.mb / 1000).toFixed(1)} GB`;
    if (!why && usbOk && usbInfo.stick.free < e.mb_needed * 1e6) why = `Needs ${(e.mb_needed / 1000).toFixed(1)} GB free while scanning (the photos are packed into one file at the end). The stick has ${gb(usbInfo.stick.free)}.`;
  } else $('est').textContent = 'No wells chosen';
  $('why').textContent = why;
  $('startBtn').disabled = running || !!why;
}

$('startBtn').onclick = async () => {
  $('startBtn').disabled = true;
  const d = await post('/api/job/start', {kind: 'scan', name: $('name').value.trim(), wells: orderedWells(),
                                           reshoot: $('reshoot').checked});
  if (d.error) { $('why').textContent = d.error; $('startBtn').disabled = false; return; }
  poll();
};
$('stopBtn').onclick = async () => {
  if (!confirm('Stop the scan? Photos taken so far stay on the USB stick.')) return;
  const d = await post('/api/job/cancel');
  if (d.error) alert(d.error);
};
$('newBtn').onclick = () => { showingResult = false; $('progressCard').hidden = true; $('formCard').hidden = false; paintChoice(); };

// ---- USB
function showUsb(u) {
  usbInfo = u;
  const box = $('usbBox');
  if (u.stick) {
    const s = u.stick, used = 100 * (1 - s.free / s.total);
    box.innerHTML = `<div class="usb"><b>${s.label}</b> &nbsp;${gb(s.free)} free of ${gb(s.total)}</div>
      <div class="usbbar"><div style="width:${used}%"></div></div>` + (s.writable ? '' : '<p class="why">Can\'t write to it. Is it write-protected?</p>');
  } else if (u.unmounted && u.unmounted.length) {
    box.innerHTML = `<span class="why">${u.reason || 'A stick is plugged in but could not be opened. Unplug it and plug it back in.'}</span>`;
  } else box.innerHTML = '<span class="why">No USB stick. Plug one into the Pi.</span>';
  $('ejectBtn').hidden = !u.stick;
  $('simPlugBtn').hidden = !u.sim_ejected;
  $('recent').innerHTML = u.recent && u.recent.length
    ? u.recent.map(r => {
        // 2026-11-26_004718_GL261_4Gy.clonescan -> "GL261_4Gy" and "26 Nov 00:47"
        const m = r.name.replace(/\.clonescan$/, '').match(/^(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)\d\d_?(.*)$/);
        const mon = m ? 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')[+m[2] - 1] : '';
        const title = m ? (m[6] || 'Unnamed plate') : r.name;
        const when = m ? `${+m[3]} ${mon} ${m[4]}:${m[5]}` : '';
        const what = r.packed ? (r.bad ? '<span class="warn">damaged file</span>'
                                       : `${r.photos} photos, ${r.mb >= 1000 ? (r.mb / 1000).toFixed(1) + ' GB' : r.mb + ' MB'}` + (r.complete ? '' : ', <span class="warn">stopped early</span>'))
                              : `${r.photos} photos, <span class="warn">still a folder</span>`;
        return `<div class="recent" title="${r.name}"><b>${title}</b><span>${when} · ${what}</span></div>`;
      }).join('')
    : 'No scans yet.';
}
$('ejectBtn').onclick = async () => {
  $('usbBox').textContent = 'Finishing writing and ejecting…';
  const d = await post('/api/usb/eject');
  if (d.error) alert(d.error); else $('usbBox').innerHTML = '<b>Safe to remove.</b>';
  usbInfo = d; $('ejectBtn').hidden = true; $('simPlugBtn').hidden = !d.sim_ejected; refreshForm();
};
$('simPlugBtn').onclick = async () => { showUsb(await post('/api/usb/simplug')); refreshForm(); };

// ---- step 1: flat-field after every restart
function showFlat(st, j) {
  if (st.flat_since_start !== flatAt) {
    flatAt = st.flat_since_start;
    getJSON('/api/info').then(i => { info = i; refreshForm(); });
  }
  const ff = j && j.kind === 'flatfield' ? j : null;
  ffRunning = !!(ff && (ff.status === 'running' || ff.status === 'cancelling'));
  const scanActive = j && j.kind === 'scan' && (j.status === 'running' || j.status === 'cancelling');
  const needFlat = !flatAt && !scanActive && !showingResult;
  $('flatCard').hidden = !needFlat;
  if (needFlat) $('formCard').hidden = true;
  $('ffBtn').disabled = ffRunning;
  $('ffBtn').textContent = ffRunning ? 'Calibrating…' : (ff && ff.status === 'failed' ? 'Try again, plate is out' : 'Calibrate, plate is out');
  $('ffStatus').hidden = !ff;
  if (ff) {
    $('ffStatus').textContent = {running: 'calibrating', cancelling: 'stopping…', done: 'done', failed: 'problem', cancelled: 'stopped'}[ff.status] || ff.status;
    $('ffStatus').className = 'status ' + ({done: 'ok', failed: 'bad', cancelled: 'bad'}[ff.status] || 'warn');
    $('ffLine').textContent = (d => d[d.length - 1] || '')(ffLog);
  }
  $('ffLast').textContent = info && info.camera ? `Last done ${info.camera.made}. It still has to be redone after a restart.` : '';
  $('flatOk').textContent = flatAt ? `Flat-field done ${flatAt.slice(11)} ✓` : '';
}
let ffLog = [];
$('ffBtn').onclick = async () => {
  if (!confirm('Is the plate out, the light pad on and the lid closed?')) return;
  const d = await post('/api/job/start', {kind: 'flatfield'});
  if (d.error) alert(d.error);
};

// ---- progress
function showJob(d) {
  const j = d.job;
  if (j && j.kind === 'flatfield') ffLog = d.log || [];
  const isScan = j && j.kind === 'scan';
  const active = isScan && (j.status === 'running' || j.status === 'cancelling');
  running = !!active;
  if (active) showingResult = true;
  $('progressCard').hidden = !(active || (isScan && showingResult));
  $('formCard').hidden = !$('progressCard').hidden;
  $('pickRow').hidden = active;
  $('plateSub').textContent = active ? 'Scan progress by well' : 'Click wells to include them';
  if (!isScan || $('progressCard').hidden) return;

  const p = j.progress || {};
  const total = p.total || 0;
  const focusing = active && p.step === 'focus' && !p.packing;
  const pct = total ? Math.round(100 * p.taken / total) : 0;
  $('pPhase').textContent = p.packing ? 'packing into one file' : !active ? '–'
    : focusing ? `focusing ${Math.min(p.focus_done || 0, p.focus_total || 7)} / ${p.focus_total || 7} spots` : 'taking photos';
  $('jobTitle').textContent = j.title;
  $('jobStarted').textContent = 'Started ' + j.started;
  $('pct').innerHTML = `${j.status === 'done' ? 100 : pct}<small>%</small>`;
  $('bar').style.width = (j.status === 'done' ? 100 : pct) + '%';
  $('pWell').textContent = p.packing ? 'all done' : active ? (p.well || 'starting') : '–';
  $('pPhotos').textContent = total ? `${p.taken} / ${total}` : (p.taken || '–');
  $('pLeft').textContent = p.packing ? '~1–3 min' : active ? (p.left_min != null ? `~${Math.ceil(p.left_min)} min` : '…') : '–';
  const words = {running: 'scanning', cancelling: 'stopping…', done: 'finished', failed: 'stopped: problem', cancelled: 'stopped'};
  $('jobStatus').textContent = p.packing
    ? 'packing into one file' + (p.packed ? ` ${Math.round(100 * p.packed[0] / p.packed[1])}%` : '…')
    : words[j.status] || j.status;
  const base = f => f.split(/[\\/]/).pop();
  $('pFolder').textContent = p.file ? 'Saved as ' + base(p.file)
    : p.folder ? 'Saving to ' + base(p.folder) : '';
  $('stopBtn').hidden = !active || !!p.packing;
  $('newBtn').hidden = active;

  // the plate fills in well by well
  const wells = p.wells || [];
  const idx = wells.indexOf(p.well);
  const perWell = total && wells.length ? total / wells.length : 0;
  map.names().forEach(w => {
    const k = wells.indexOf(w);
    if (k < 0) return map.mark(w, '', 0);
    if (j.status === 'done' || (idx >= 0 && k < idx)) return map.mark(w, 'done', 1);
    if (k === idx) return map.mark(w, 'scanning', perWell ? (p.taken - k * perWell) / perWell : 0);
    map.mark(w, 'pending', 0);
  });

  const log = $('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = (d.log || []).join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (!active && showJob.was) getJSON('/api/usb').then(showUsb);
  showJob.was = active;
}

async function poll() {
  try {
    const st = await getJSON('/api/state');
    showBusy(st);
    const pill = $('st');
    pill.textContent = st.busy && st.action ? st.action : st.state;
    pill.className = 'chip ' + (st.busy ? 'warn' : st.state === 'Alarm' ? 'bad' : 'ok');
    $('sim').hidden = !st.sim;
    if (!running) map.place(st.x, st.y);
    const jd = await getJSON('/api/job');
    showJob(jd);
    showFlat(st, jd.job);
  } catch (e) {
    if (e.message !== 'login') { $('st').textContent = 'offline'; $('st').className = 'chip bad'; }
  }
}
async function pollUsb() {
  if (running) return;
  try { showUsb(await getJSON('/api/usb')); refreshForm(); } catch (e) {}
}

getJSON('/api/info').then(i => {
  info = i; map.build(i);
  i.order.forEach(w => chosen.add(w));
  paintChoice(); pollUsb();
});
poll();
setInterval(poll, 1000);
setInterval(pollUsb, 3000);
