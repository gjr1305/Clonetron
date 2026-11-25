// Clonetron Live: plate map with the objective's position, scale bar, front-panel drawer.
(() => {
  const $ = id => document.getElementById(id);
  const FOV_W_MM = 3.22;

  async function goWell(name) {
    const m = $('msg'); m.textContent = 'Going to ' + name + '…'; m.classList.remove('err');
    const r = await fetch('/api/well', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({well: name})});
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (d.error) { m.textContent = d.error; m.classList.add('err'); } else m.textContent = '';
  }
  const map = PlateMap($('platemap'), {onClick: goWell});

  // scale bar: 500 µm at the size the picture is shown
  function scale() {
    const img = $('live'), sb = $('scalebar');
    const shownW = Math.min(img.clientWidth, img.clientHeight * 4 / 3);   // contain, 4:3 picture
    sb.querySelector('.bar').style.width = (shownW / FOV_W_MM * 0.5) + 'px';
  }
  window.addEventListener('resize', scale);
  $('live').addEventListener('load', scale);
  setTimeout(scale, 500);

  $('fpToggle').onclick = () => {
    const open = $('fpBody').hidden;
    $('fpBody').hidden = !open;
    $('fpToggle').setAttribute('aria-expanded', open);
  };

  fetch('/api/info').then(r => r.json()).then(map.build);
  setInterval(async () => {
    try {
      const d = await (await fetch('/api/state')).json();
      if (d.x !== undefined) map.place(d.x, d.y);
    } catch (e) {}
  }, 400);
})();
