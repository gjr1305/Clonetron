// Clonetron Setup: flat-field calibration and focus map, run as jobs.
const $ = id => document.getElementById(id);
const chosen = new Set();
let running = false;

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

const map = PlateMap($('platemap'), {onClick: w => {
  if (running) return;
  chosen.has(w) ? chosen.delete(w) : chosen.add(w);
  paint();
}});
function paint() { map.names().forEach(w => map.mark(w, chosen.has(w) ? 'selected' : '', 0)); }

function showInfo(i) {
  const miss = '<span style="color:var(--warn)">not done</span>';
  $('camMade').innerHTML = i.camera ? i.camera.made : miss;
  $('camExp').textContent = i.camera ? i.camera.exposure_ms + ' ms' : '–';
  $('mapMade').innerHTML = i.map ? i.map.made : miss;
  $('mapSpots').textContent = i.map ? i.map.spots : '–';
  $('mapRange').textContent = i.map && i.map.range_um !== null ? i.map.range_um + ' µm' : '–';
  if (!$('mapZ').value) $('mapZ').value = i.z_default;
}

function showJob(d) {
  const j = d.job;
  if (!j || j.kind === 'scan') return;
  running = j.status === 'running' || j.status === 'cancelling';
  $('jobTitle').textContent = j.title + ' · started ' + j.started;
  const words = {running: 'running', cancelling: 'stopping…', done: 'finished', failed: 'failed', cancelled: 'cancelled'};
  $('jobStatus').textContent = words[j.status] || j.status;
  $('jobStatus').className = 'status ' + ({done: 'ok', failed: 'bad', cancelled: 'bad'}[j.status] || 'warn');
  $('cancelBtn').hidden = !running;
  $('ffBtn').disabled = $('mapBtn').disabled = running;
  const log = $('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = d.log.join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;
  if (running && !showJob.was) $('logWrap').open = true;
  if (!running && showJob.was) getJSON('/api/info').then(showInfo);
  showJob.was = running;
}

async function start(kind, body) {
  const d = await post('/api/job/start', Object.assign({kind}, body || {}));
  if (d.error) { alert(d.error); return; }
  showJob(d);
}
$('ffBtn').onclick = () => { if (confirm('Is the plate out and the light pad on?')) start('flatfield'); };
$('mapBtn').onclick = () => {
  const wells = [...chosen];
  if (!wells.length) { alert('Click at least one well on the plate'); return; }
  if (confirm(`Make a new focus map for ${wells.join(', ')}? This replaces the current map.`))
    start('map', {z: +$('mapZ').value, wells});
};
$('cancelBtn').onclick = async () => {
  if (!confirm('Stop this job? The stage halts where it is.')) return;
  const d = await post('/api/job/cancel'); if (d.error) alert(d.error);
};

async function poll() {
  try {
    const st = await getJSON('/api/state');
    $('st').textContent = st.busy && st.action ? st.action : st.state;
    $('st').className = 'chip ' + (st.busy ? 'warn' : st.state === 'Alarm' ? 'bad' : 'ok');
    $('sim').hidden = !st.sim;
    map.place(st.x, st.y);
    showJob(await getJSON('/api/job'));
  } catch (e) {
    if (e.message !== 'login') { $('st').textContent = 'offline'; $('st').className = 'chip bad'; }
  }
}
getJSON('/api/info').then(i => { map.build(i); i.order.forEach(w => chosen.add(w)); paint(); showInfo(i); });
poll();
setInterval(poll, 1000);
