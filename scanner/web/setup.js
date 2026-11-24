// Clonetron setup page: flat-field calibration and focus map, run as jobs.
const $ = id => document.getElementById(id);
let info = null;

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

function showInfo(i) {
  info = i;
  const miss = '<span class="missing">not done yet</span>';
  $('camMade').innerHTML = i.camera ? i.camera.made : miss;
  $('camExp').textContent = i.camera ? i.camera.exposure_ms + ' ms' : '–';
  if (!i.flat && i.camera) $('camMade').innerHTML += ' <span class="missing">(flat.npy missing)</span>';
  $('mapMade').innerHTML = i.map ? i.map.made : miss;
  $('mapSpots').textContent = i.map ? i.map.spots : '–';
  $('mapRange').textContent = i.map && i.map.range_um !== null ? i.map.range_um + ' µm' : '–';
  if (!$('mapZ').value) $('mapZ').value = i.z_default;
  const box = $('mapWells');
  if (!box.children.length) {
    i.order.forEach(w => {
      const l = document.createElement('label');
      l.innerHTML = `<input type="checkbox" value="${w}" checked> ${w}`;
      box.appendChild(l);
    });
  }
}

function showJob(d) {
  const j = d.job, log = $('log');
  if (!j) return;
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = `${j.title}  (started ${j.started})\n\n` + d.log.join('\n');
  if (atBottom) log.scrollTop = log.scrollHeight;
  const s = $('jobStatus');
  const words = {running: 'running', cancelling: 'stopping…', done: 'finished',
                 failed: 'failed', cancelled: 'cancelled'};
  s.textContent = words[j.status] || j.status;
  s.className = 'pill ' + ({done: 'ok', failed: 'bad', cancelled: 'bad'}[j.status] || 'warn');
  const running = j.status === 'running' || j.status === 'cancelling';
  $('cancelBtn').hidden = !running;
  $('ffBtn').disabled = $('mapBtn').disabled = running;
  if (!running && showJob.wasRunning) getJSON('/api/info').then(showInfo);   // refresh dates
  showJob.wasRunning = running;
}

async function start(kind, body) {
  const d = await post('/api/job/start', Object.assign({kind}, body || {}));
  if (d.error) { alert(d.error); return; }
  showJob(d);
}

$('ffBtn').onclick = () => {
  if (confirm('Is the plate out and the light pad on?')) start('flatfield');
};
$('mapBtn').onclick = () => {
  const wells = [...document.querySelectorAll('#mapWells input:checked')].map(c => c.value);
  if (!wells.length) { alert('Tick at least one well'); return; }
  if (confirm(`Make a new focus map for ${wells.join(', ')}? This replaces the current map.`))
    start('map', {z: +$('mapZ').value, wells});
};
$('cancelBtn').onclick = async () => {
  if (!confirm('Stop this job? The stage will halt where it is.')) return;
  const d = await post('/api/job/cancel');
  if (d.error) alert(d.error);
};

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
getJSON('/api/info').then(showInfo);
poll();
setInterval(poll, 1000);
