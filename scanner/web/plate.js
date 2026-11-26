// Clonetron plate map: the six wells drawn to scale in stage coordinates,
// with the objective's position. Used by the Live, Scan and Setup pages.
// X is negative towards the wells (left); Y is positive towards you.
function PlateMap(svg, opts) {
  const NS = 'http://www.w3.org/2000/svg';
  const FOV = [3.22, 2.41];
  opts = opts || {};
  let wells = {}, radius = 17.4, dot, pulse, fov, circles = {};

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    (parent || svg).appendChild(e);
    return e;
  }

  function build(info) {
    wells = info.wells; radius = info.well_radius;
    const xs = Object.values(wells).map(w => -w[0]), ys = Object.values(wells).map(w => w[1]);
    const pad = radius + 6;
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
    svg.setAttribute('viewBox', `${minX} ${minY} ${maxX - minX} ${maxY - minY}`);
    svg.style.aspectRatio = `${(maxX - minX) / (maxY - minY)}`;
    el('rect', {x: minX + 2, y: minY + 2, width: maxX - minX - 4, height: maxY - minY - 4, rx: 4,
      fill: 'none', stroke: 'rgba(255,255,255,.12)', 'stroke-width': .5});
    for (const [name, [wx, wy]] of Object.entries(wells)) {
      const g = el('g', {class: 'wellg', 'data-w': name, tabindex: 0, role: 'button', 'aria-label': 'Well ' + name});
      const c = el('circle', {cx: -wx, cy: wy, r: radius, class: 'well'}, g);
      el('circle', {cx: -wx, cy: wy, r: radius, class: 'ring', pathLength: 100}, g);
      const t = el('text', {x: -wx, y: wy, class: 'wl'}, g); t.textContent = name;
      circles[name] = g;
      if (opts.onClick) {
        g.addEventListener('click', () => opts.onClick(name));
        g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); opts.onClick(name); } });
      }
    }
    pulse = el('circle', {r: 2.2, class: 'pulse'});
    fov = el('rect', {width: FOV[0], height: FOV[1], class: 'fov'});
    dot = el('circle', {r: 1.1, class: 'dot'});
    place(9999, 9999);
  }

  function place(x, y) {
    if (!dot) return;
    for (const e of [dot, pulse]) { e.setAttribute('cx', x); e.setAttribute('cy', y); }
    fov.setAttribute('x', x - FOV[0] / 2); fov.setAttribute('y', y - FOV[1] / 2);
    for (const [name, g] of Object.entries(circles)) {
      const [wx, wy] = wells[name];
      g.classList.toggle('here', Math.hypot(x + wx, y - wy) < radius);
    }
  }

  // state: '', 'selected', 'pending', 'scanning', 'done'; frac fills the progress ring
  function mark(name, state, frac) {
    const g = circles[name]; if (!g) return;
    g.classList.remove('selected', 'pending', 'scanning', 'done');
    if (state) g.classList.add(state);
    const ring = g.querySelector('.ring');
    ring.style.strokeDasharray = `${Math.max(0, Math.min(1, frac || 0)) * 100} 100`;
  }

  return {build, place, mark, names: () => Object.keys(wells)};
}


// While a job has the camera and no photo is showing yet, put a sharp text card
// over the dark picture (title of the job and its latest log line).
function showBusy(st) {
  let el = document.getElementById('busycard');
  if (!el) {
    el = document.createElement('div');
    el.id = 'busycard'; el.className = 'busycard';
    el.innerHTML = '<div class="bt"></div><div class="bl"></div>';
    document.body.appendChild(el);
  }
  const on = st.cam_busy && !st.showing_photo;
  el.hidden = !on;
  if (on) {
    el.querySelector('.bt').textContent = (st.job && st.job.title) || 'Busy';
    el.querySelector('.bl').textContent = st.job_line || '';
  }
}
