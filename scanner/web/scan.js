// Clonetron scan page: USB stick, new scan form, progress and stop.
const $ = id => document.getElementById(id);
let info = null, usbInfo = null, running = false;

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
const chosenWells = () => [...document.querySelectorAll('#wells input:checked')].map(c => c.value);

// ---- form
function buildWells(i) {
  const names = Object.keys(i.wells).sort((a, b) =>
    i.wells[a][0] - i.wells[b][0] || i.wells[a][1] - i.wells[b][1]);
  names.forEach(w => {
    const l = document.createElement('label');
    l.innerHTML = `<input type="checkbox" value="${w}" checked><span>${w}</span>`;
    l.querySelector('input').onchange = refreshForm;
    $('wells').appendChild(l);
  });
}
$('allBtn').onclick = () => { document.querySelectorAll('#wells input').forEach(c => c.checked = true); refreshForm(); };
$('noneBtn').onclick = () => { document.querySelectorAll('#wells input').forEach(c => c.checked = false); refreshForm(); };
document.querySelectorAll('input[name=mode]').forEach(r => r.onchange = refreshForm);

async function refreshForm() {
  const wells = chosenWells();
  let why = '';
  if (!wells.length) why = 'Tick at least one well.';
  const usbOk = usbInfo && usbInfo.stick && usbInfo.stick.writable;
  if (!usbOk) why = 'Plug in a USB stick. Scans are only saved to USB.';
  const mode = document.querySelector('input[name=mode]:checked').value;
  if (mode === 'map' && info && !info.map) why = why || 'There is no focus map yet: make one on the Setup page, or choose "Focus once".';
  if (wells.length) {
    const e = await getJSON('/api/estimate?wells=' + wells.join(','));
    let line = `${wells.length} well${wells.length > 1 ? 's' : ''} · about ${e.photos} photos · ~${e.minutes} min · needs ~${(e.mb / 1000).toFixed(1)} GB`;
    if (usbOk && usbInfo.stick.free < e.mb * 1e6) why = `Not enough space on the stick (${gb(usbInfo.stick.free)} free).`;
    $('est').textContent = line;
  } else $('est').textContent = '–';
  $('why').textContent = running ? '' : why;
  $('startBtn').disabled = running || !!why;
}

$('startBtn').onclick = async () => {
  const body = {kind: 'scan', name: $('name').value.trim(), wells: chosenWells(),
                mode: document.querySelector('input[name=mode]:checked').value,
                reshoot: $('reshoot').checked};
  $('startBtn').disabled = true;
  const d = await post('/api/job/start', body);
  if (d.error) { $('why').textContent = d.error; $('startBtn').disabled = false; return; }
  poll();
};
$('stopBtn').onclick = async () => {
  if (!confirm('Stop the scan? Photos taken so far stay on the USB stick.')) return;
  const d = await post('/api/job/cancel');
  if (d.error) alert(d.error);
};

// ---- USB
function showUsb(u) {
  usbInfo = u;
  const box = $('usbBox');
  if (u.stick) {
    const s = u.stick, pct = 100 * (1 - s.free / s.total);
    box.className = 'msg';
    box.innerHTML = `<b>${s.label}</b><br>${gb(s.free)} free of ${gb(s.total)}
      <div class="sharpbar" style="margin-top:6px"><div style="width:${pct}%"></div></div>` +
      (s.writable ? '' : '<br><span class="missing">Can\'t write to it (write-protected?)</span>');
  } else if (u.unmounted && u.unmounted.length) {
    box.className = 'msg err';
    box.textContent = 'A stick is plugged in but couldn\'t be opened. Try unplugging it and plugging it back in.';
  } else {
    box.className = 'msg err';
    box.textContent = 'No USB stick. Plug one into the Pi.';
  }
  $('ejectBtn').hidden = !u.stick;
  $('simPlugBtn').hidden = !u.sim_ejected;
  $('recent').innerHTML = u.recent && u.recent.length
    ? u.recent.map(r => `<div class="recent"><span class="mono">${r.name}</span><span>${r.photos} photos</span></div>`).join('')
    : 'No scans yet.';
}
$('ejectBtn').onclick = async () => {
  $('usbBox').textContent = 'Finishing writing and ejecting…';
  const d = await post('/api/usb/eject');
  if (d.error) alert(d.error);
  else { $('usbBox').className = 'msg'; $('usbBox').innerHTML = '<b>Safe to remove.</b>'; }
  usbInfo = d; $('ejectBtn').hidden = true; $('simPlugBtn').hidden = !d.sim_ejected;
  refreshForm();
};
$('simPlugBtn').onclick = async () => { showUsb(await post('/api/usb/simplug')); refreshForm(); };

// ---- progress
function showJob(d) {
  const j = d.job;
  const isScan = j && j.kind === 'scan';
  const active = j && (j.status === 'running' || j.status === 'cancelling');
  running = !!active;
  $('progressCard').hidden = !isScan;
  if (!isScan) { $('formCard').hidden = false; return; }
  $('formCard').hidden = !!active;
  const p = j.progress || {};
  $('jobTitle').textContent = j.title + ' (started ' + j.started + ')';
  const words = {running: 'scanning', cancelling: 'stopping…', done: 'finished',
                 failed: 'stopped with a problem', cancelled: 'stopped'};
  $('jobStatus').textContent = words[j.status] || j.status;
  $('jobStatus').className = 'pill ' + ({done: 'ok', failed: 'bad', cancelled: 'bad'}[j.status] || 'warn');
  $('stopBtn').hidden = !active;
  $('latest').parentElement.hidden = !active;
  const total = p.total || 0;
  $('bar').style.width = total ? (100 * p.taken / total) + '%' : '0';
  $('pPhotos').textContent = total ? `${p.taken} of ${total}` : (p.taken || '–');
  $('pWell').textContent = active ? (p.well || 'getting ready…') : '–';
  $('pLeft').textContent = active ? (p.left_min != null ? `about ${Math.ceil(p.left_min)} min` : 'working it out…') : '–';
  $('pFolder').textContent = p.folder || '–';
  const log = $('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = (d.log || []).join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (!active && showJob.was) { getJSON('/api/usb').then(showUsb); }
  if (!active && j.status !== 'running') $('formCard').hidden = false;
  showJob.was = active;
}

async function poll() {
  try {
    const st = await getJSON('/api/state');
    const pill = $('st');
    pill.textContent = st.busy && st.action ? st.action : st.state;
    pill.className = 'pill ' + (st.busy ? 'warn' : st.state === 'Alarm' ? 'bad' : 'ok');
    $('sim').hidden = !st.sim;
    showJob(await getJSON('/api/job'));
  } catch (e) {
    if (e.message !== 'login') { $('st').textContent = 'offline'; $('st').className = 'pill bad'; }
  }
}
async function pollUsb() {
  if (running) return;
  try { showUsb(await getJSON('/api/usb')); refreshForm(); } catch (e) {}
}

getJSON('/api/info').then(i => { info = i; buildWells(i); pollUsb(); });
poll();
setInterval(poll, 1000);
setInterval(pollUsb, 3000);
