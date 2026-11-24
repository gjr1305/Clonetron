// Clonetron scanner app: live view and jogging.
const $ = id => document.getElementById(id);
const STEPS = [0.01, 0.02, 0.05, 0.1, 1, 5, 10];
let step = 1, sharpMax = 1;

function setStep(s) {
  step = s;
  document.querySelectorAll('#steps button').forEach(b => b.classList.toggle('on', +b.dataset.s === s));
}
document.querySelectorAll('#steps button').forEach(b => b.onclick = () => setStep(+b.dataset.s));

function msg(t, err) { const m = $('msg'); m.textContent = t || ''; m.classList.toggle('err', !!err); }

async function post(url, body) {
  try {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body || {})});
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (d.error) msg(d.error, true); else msg('');
    show(d);
  } catch (e) { msg('Lost connection to the scanner', true); }
}

function jog(axis, sign) { post('/api/jog', {axis, dist: sign * step}); }
function home() { msg('Homing…'); post('/api/home'); }
function stop() { post('/api/stop'); msg('Stopped'); }

document.querySelectorAll('[data-j]').forEach(b => {
  const [a, s] = b.dataset.j.split(',');
  b.onclick = () => jog(a, +s);
});
$('homeBtn').onclick = home;
$('stopBtn').onclick = stop;

function show(d) {
  if (!d || d.x === undefined) return;
  if (d.msg) msg(d.msg);
  const jb = $('jobbanner');
  if (jb) {
    const running = d.job && (d.job.status === 'running' || d.job.status === 'cancelling');
    jb.style.display = running ? 'block' : 'none';
    if (running) jb.innerHTML = `<b>${d.job.title}</b> is running, so the camera and stage are in use. <a href="/setup">See progress</a>`;
  }
  $('px').textContent = `${Math.abs(d.x).toFixed(2)} ${d.x < 0 ? 'left' : d.x > 0 ? 'right' : ''}`;
  $('py').textContent = `${Math.abs(d.y).toFixed(2)} ${d.y > 0 ? 'towards' : d.y < 0 ? 'away' : ''}`;
  $('pz').textContent = d.z.toFixed(3);
  const st = $('st');
  const label = d.busy && d.action ? d.action : d.state;
  st.textContent = label;
  st.className = 'pill ' + (d.state.startsWith('Error') || d.state === 'Alarm' ? 'bad'
                            : d.busy ? 'warn' : 'ok');
  $('banner').style.display = d.state === 'Alarm' ? 'block' : 'none';
  $('sim').hidden = !d.sim;
  sharpMax = Math.max(sharpMax * 0.995, d.sharp, 1);
  $('sbar').style.width = Math.min(100, 100 * d.sharp / sharpMax) + '%';
  $('snum').textContent = d.sharp.toFixed(0);
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    if (r.status === 401) { location.href = '/login'; return; }
    show(await r.json());
  } catch (e) { $('st').textContent = 'offline'; $('st').className = 'pill bad'; }
}
setInterval(poll, 400);
poll();

// keyboard
const KEYS = {a: ['X', -1], d: ['X', 1], w: ['Y', -1], s: ['Y', 1], r: ['Z', 1], f: ['Z', -1]};
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const k = e.key.toLowerCase();
  if (KEYS[k]) { e.preventDefault(); jog(...KEYS[k]); }
  else if (k === 'h') home();
  else if (k === ' ') { e.preventDefault(); stop(); }
  else if (k === '[' || k === ']') {
    const i = STEPS.indexOf(step) + (k === ']' ? 1 : -1);
    if (i >= 0 && i < STEPS.length) setStep(STEPS[i]);
  }
});

// reconnect the live picture if it drops
$('live').onerror = () => setTimeout(() => { $('live').src = '/stream.mjpg?' + Date.now(); }, 1500);

// wells, laid out like the plate (rows of two, as they sit on the stage)
fetch('/api/info').then(r => r.json()).then(info => {
  const p = $('plate'); if (!p) return;
  const names = Object.keys(info.wells).sort((a, b) =>
    info.wells[a][0] - info.wells[b][0] || info.wells[a][1] - info.wells[b][1]);
  names.forEach(w => {
    const b = document.createElement('button');
    b.textContent = w; b.title = 'Go to the centre of ' + w;
    b.onclick = () => { msg('Going to ' + w + '…'); post('/api/well', {well: w}); };
    p.appendChild(b);
  });
});
$('afBtn').onclick = () => { msg('Autofocusing… (20–60 s)'); post('/api/autofocus'); };
