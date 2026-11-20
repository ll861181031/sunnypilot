(function () {
  'use strict';
  var KEY = 'd3-settings';
  var OPTS = {
    paint: [
      { id: 'model', name: '原车颜色', model: true },
      { id: 'red', name: '赤焰红', color: '#9a0c13', metal: 0.3, rough: 0.34, acc: '#ff4b55' },
      { id: 'white', name: '珍珠白', color: '#e6e4df', metal: 0.12, rough: 0.3, acc: '#9fd0ff' },
      { id: 'black', name: '午夜黑', color: '#0f1013', metal: 0.5, rough: 0.28, acc: '#b9c2d0' },
      { id: 'blue', name: '电光蓝', color: '#1a44c4', metal: 0.45, rough: 0.3, acc: '#5b8cff' },
      { id: 'green', name: '极光绿', color: '#13705a', metal: 0.45, rough: 0.3, acc: '#3fd9a6' },
      { id: 'orange', name: '落日橙', color: '#e0571a', metal: 0.25, rough: 0.32, acc: '#ff8a3d' },
      { id: 'custom', name: '自定义', hex: 'paintHex', metal: 0.3, rough: 0.32 }
    ],
    rim: [
      { id: 'model', name: '原车颜色', model: true },
      { id: 'silver', name: '亮银', color: '#d9dce1', metal: 1, rough: 0.22 },
      { id: 'black', name: '亮黑', color: '#1a1b1e', metal: 0.7, rough: 0.25 },
      { id: 'gold', name: '香槟金', color: '#c9a96a', metal: 1, rough: 0.28 },
      { id: 'gun', name: '枪灰', color: '#5a5f67', metal: 0.9, rough: 0.3 },
      { id: 'custom', name: '自定义', hex: 'rimHex', metal: 0.9, rough: 0.25 }
    ],
    caliper: [
      { id: 'model', name: '原车颜色', model: true },
      { id: 'red', name: '红', color: '#c8141b' },
      { id: 'yellow', name: '黄', color: '#e8b20e' },
      { id: 'blue', name: '蓝', color: '#1f5fd6' },
      { id: 'black', name: '黑', color: '#1a1a1c' },
      { id: 'custom', name: '自定义', hex: 'calHex' }
    ],
    finish: ['preset', 'gloss', 'metal', 'pearl', 'satin', 'matte', 'frost', 'candy', 'chrome', 'flip'],
    theme: ['auto', 'light', 'dark'],
    bg: ['studio', 'warm', 'cool', 'image'],
    vBase: ['cream', 'walnut', 'black', 'silver'],
    vDisc: ['black', 'red', 'blue', 'clear'],
    vRpm: ['16', '20', '33'],
    quality: ['eco', 'std', 'high', 'ultra'],
    fps: ['30', '60'],
    npLines: ['3', '5', '7', '9', '10'],
    npColor: ['default', 'pink', 'yellow', 'orange', 'cyan', 'green', 'purple', 'red'],
    npColorLight: ['default', 'pink', 'yellow', 'orange', 'cyan', 'green', 'purple', 'red']
  };
  var FINISH = {
    gloss: { metal: 0.15, rough: 0.28, coat: 1, flake: 0 },
    metal: { metal: 0.62, rough: 0.3, coat: 1, flake: 0.08 },
    pearl: { metal: 0.22, rough: 0.24, coat: 1, iri: 0.8, thick: [120, 380], flake: 0.05 },
    satin: { metal: 0.35, rough: 0.46, coat: 0.4, ccr: 0.35, flake: 0.04 },
    matte: { metal: 0.08, rough: 0.72, coat: 0, flake: 0 },
    frost: { metal: 0.78, rough: 0.52, coat: 0, flake: 0.06 },
    candy: { metal: 0.5, rough: 0.14, coat: 1, flake: 0.06 },
    chrome: { metal: 1, rough: 0.06, coat: 1, flake: 0 },
    flip: { metal: 0.55, rough: 0.24, coat: 1, iri: 1, thick: [250, 900], flake: 0.06 }
  };
  var QUAL = {
    eco: { ratio: 1, shadow: 0, flake: false, lowp: true, note: '像素比 1，不画真实阴影、车漆闪点，最省显卡' },
    std: { ratio: 1.5, shadow: 1024, flake: true, lowp: true, note: '真实影棚反射、车身阴影、金属漆闪点和清漆纹理' },
    high: { ratio: 1.5, shadow: 2048, flake: true, reflect: true, bloom: true, note: '在标准上加地面反射、灯光辉光，转车时更费显卡' },
    ultra: { ratio: 2, shadow: 2048, flake: true, reflect: true, bloom: true, ao: true, dof: true, note: '再加环境光遮蔽、景深、像素比 2，最费显卡，车机上转车可能会卡' }
  };
  var STATS = window.D3CarStat.STATS;
  var HEX = { paintHex: '#3a7bd5', rimHex: '#b8bcc4', calHex: '#2bb673' };
  var deg = function (v) { return Math.round(v * 180 / Math.PI) + '°'; };
  var NUM = {
    bgDim: { min: 0, max: 0.7, step: 0.05, def: 0.25, fmt: function (v) { return Math.round(v * 100) + '%'; } },
    viewAz: { min: -3.15, max: 3.15, step: 0.01, def: -0.72, fmt: deg },
    viewEl: { min: 0.04, max: 0.62, step: 0.01, def: 0.2, fmt: deg },
    viewZoom: { min: 0.7, max: 1.4, step: 0.05, def: 1, fmt: function (v) { return Math.round(v * 100) + '%'; } },
    viewFov: { min: 18, max: 50, step: 1, def: 27, fmt: function (v) { return v + '°'; } },
    camDist: { min: 0.6, max: 1.8, step: 0.05, def: 1, fmt: function (v) { return v.toFixed(2) + '×'; } },
    camPitch: { min: 4, max: 50, step: 1, def: 11, fmt: function (v) { return v + '°'; } },
    camAhead: { min: 0, max: 10, step: 0.5, def: 4, fmt: function (v) { return v + ' 米'; } },
    camFov: { min: 24, max: 64, step: 1, def: 36, fmt: function (v) { return v + '°'; } },
    dtScale: { min: 0.6, max: 1.5, step: 0.05, def: 0.8, fmt: function (v) { return Math.round(v * 100) + '%'; } },
    npScale: { min: 0.6, max: 1.5, step: 0.05, def: 1, fmt: function (v) { return v.toFixed(2) + '×'; } },
    npTop: { min: 0, max: 100, step: 1, def: 38, fmt: function (v) { return Math.round(v) + '%'; } },
    npOpacity: { min: 0.3, max: 1, step: 0.05, def: 0.9, fmt: function (v) { return Math.round(v * 100) + '%'; } },
    npHeight: { min: 1.2, max: 3.4, step: 0.1, def: 2.2, fmt: function (v) { return v.toFixed(1) + 'rem'; } },
    npWidth: { min: 2, max: 8, step: 0.1, def: 4, fmt: function (v) { return v.toFixed(1) + 'rem'; } },
    npFont: { min: 0.14, max: 0.34, step: 0.01, def: 0.22, fmt: function (v) { return v.toFixed(2) + 'rem'; } }
  };
  var VIEW = ['viewAz', 'viewEl', 'viewZoom', 'viewFov'];
  var CAM = ['camDist', 'camPitch', 'camAhead', 'camFov'];
  var DEF = { paint: 'red', rim: 'silver', caliper: 'red', finish: 'preset', paintHex: HEX.paintHex, rimHex: HEX.rimHex, calHex: HEX.calHex, theme: 'auto',
    bg: 'studio', bgDim: 0.25, viewAz: -0.72, viewEl: 0.2, viewZoom: 1, viewFov: 27, stats: ['bat', 'fuel', 'range', 'temp'],
    vinyl: true, vBase: 'cream', vDisc: 'black', vRpm: '20',
    labels: true, intro: true, drive: false, camDist: 1, camPitch: 11, camAhead: 4, camFov: 36,
    quality: 'eco', fps: '60', fsBar: false,
    showNet: true, showDate: true, showLunar: true, showTime: true, showAdas: true, showRoad: true, showCam: true,
    showMediaCtl: true, showMusic: true, npLeft: false, npBg: true, npScale: 1, npTop: 38, npOpacity: 0.9, npHeight: 2.2,
    npWidth: 4, npFont: 0.22, npLines: '5', npColor: 'default', npColorLight: 'default', dtScale: 0.8 };
  var BOOL = { labels: 1, intro: 1, drive: 1, vinyl: 1, fsBar: 1, showNet: 1, showDate: 1, showLunar: 1, showTime: 1,
    showAdas: 1, showRoad: 1, showCam: 1, showMediaCtl: 1, showMusic: 1, npLeft: 1, npBg: 1 };
  var s = load(), fns = [];
  var modelPaint = null;
  var modelPart = { rim: null, caliper: null };
  var hueKeep = {};

  function ok(k, v) {
    if (BOOL[k]) return typeof v === 'boolean';
    if (NUM[k]) return typeof v === 'number' && v >= NUM[k].min && v <= NUM[k].max;
    if (HEX[k]) return typeof v === 'string' && /^#[0-9a-f]{6}$/.test(v);
    if (k === 'stats') return Array.isArray(v) && v.length >= 1 && v.length <= 4 && v.every(function (x, i) { return v.indexOf(x) === i && STATS.some(function (o) { return o[0] === x; }); });
    var o = OPTS[k];
    if (!o) return false;
    return o.some(function (x) { return (x.id || x) === v; });
  }
  function load() {
    var r = {}, raw = null;
    try { raw = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
    if (raw && raw.quality === 'smooth') raw.quality = 'std';
    if (raw && raw.quality === 'eco' && raw.fps === undefined) raw.fps = '30';
    for (var k in DEF) r[k] = raw && ok(k, raw[k]) ? raw[k] : DEF[k];
    return r;
  }
  function find(k, id) { var o = OPTS[k]; for (var i = 0; i < o.length; i++) if (o[i].id === id) return o[i]; return o[0]; }

  function hex2hsl(h) {
    var r = parseInt(h.slice(1, 3), 16) / 255, g = parseInt(h.slice(3, 5), 16) / 255, b = parseInt(h.slice(5, 7), 16) / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn, hh = 0, ss = 0;
    if (d) {
      ss = d / (1 - Math.abs(2 * l - 1));
      hh = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      hh = (hh * 60 + 360) % 360;
    }
    return [Math.round(hh), Math.round(ss * 100), Math.round(l * 100)];
  }
  function hsl2hex(h, sat, l) {
    sat /= 100; l /= 100;
    var c = (1 - Math.abs(2 * l - 1)) * sat, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = l - c / 2, rgb = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return '#' + rgb.map(function (v) { var n = Math.round((v + m) * 255); return (n < 16 ? '0' : '') + n.toString(16); }).join('');
  }
  function accOf(h) { var a = hex2hsl(h); return a[1] < 12 ? '#b9c2d0' : hsl2hex(a[0], Math.max(60, a[1]), Math.max(58, Math.min(70, a[2] + 20))); }

  var api = window.D3Set = {
    opts: OPTS, STATS: STATS,
    get: function (k) { return s[k]; },
    pick: function (k) {
      var o = find(k, s[k]), r = { id: o.id, name: o.name, color: o.hex ? s[o.hex] : o.color, metal: o.metal, rough: o.rough, acc: o.acc, coat: 1 };
      if (o.model && k !== 'paint') { r.model = true; r.color = modelPart[k] || o.color || '#9a9da3'; }
      if (o.model && k === 'paint') { var mp = modelPaint || { color: '#9a0c13', css: '#9a0c13', metal: 0.3, rough: 0.34, coat: 1 }; r.model = true; r.color = mp.color; r.acc = accOf(mp.css);
        r.metal = mp.metal >= 0.5 ? 0.6 : 0.3; r.rough = 0.34; r.coat = 1; }
      if (o.hex && k === 'paint') r.acc = accOf(r.color);
      if (k === 'paint') {
        var f = FINISH[s.finish] || {};
        if (f.metal !== undefined) { r.metal = f.metal; r.rough = f.rough; r.coat = f.coat; }
        r.ccr = f.ccr || 0.03; r.iri = f.iri || 0; r.thick = f.thick || [250, 900];
        r.flake = f.flake !== undefined ? f.flake : r.metal >= 0.4 ? 0.06 : 0;
      }
      return r;
    },
    set: function (k, v) {
      if (NUM[k]) v = Math.round(Math.round(v / NUM[k].step) * NUM[k].step * 100) / 100;
      if (!ok(k, v) || JSON.stringify(s[k]) === JSON.stringify(v)) return;
      s[k] = v;
      try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {}
      ui();
      emit(k);
    },
    fx: function () { return QUAL[s.quality]; },
    modelPaint: function (mp) { modelPaint = mp; ui(); },
    modelPart: function (k, css) { modelPart[k] = css || null; ui(); },
    on: function (fn) { fns.push(fn); return function () { var i = fns.indexOf(fn); if (i >= 0) fns.splice(i, 1); }; },
    isOpen: function () { return open; },
    close: function () { toggle(false); }
  };
  function emit(k) { fns.slice().forEach(function (f) { f(k); }); }

  var $ = function (id) { return document.getElementById(id); };
  var panel = $('set'), btn = $('setBtn'), open = false;
  var fsMode = '';
  $('rowFsBar').hidden = !CardFullscreen.bind($('fsBtn'), {
    ready: function (m) { fsMode = m; document.getElementById('app').classList.add('has-fs'); if (m === 'small' && s.fsBar) CardFullscreen.bar(false); },
    fail: function (c, m, t) { if (window.D3) D3.toast(t); }
  });
  api.on(function (k) { if (k === 'fsBar' && fsMode === 'small') CardFullscreen.bar(s.fsBar ? false : true); });
  var dbg = window.uiplus && window.uiplus.__debug && window.D3Debug;
  $('rowDebug').hidden = !dbg;
  if (dbg) {
    api.on(function (k) { if (k === 'act:debug') { toggle(false); D3Debug.open(); } });
    $('cDemo').addEventListener('click', function (e) { e.stopPropagation(); if (window.D3DebugPanel && D3DebugPanel.isOpen()) D3DebugPanel.close(); else D3Debug.open(); });
  }
  function balls(k, box) {
    OPTS[k].forEach(function (o) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'ball' + (o.hex ? ' custom' : o.model ? ' model' : ''); b.setAttribute('data-k', k); b.setAttribute('data-v', o.id); b.setAttribute('aria-label', o.name);
      if (!o.hex && !o.model) b.style.backgroundColor = o.color;
      box.appendChild(b);
    });
    var hx = find(k, 'custom').hex, wrap = document.createElement('div');
    wrap.className = 'hsl'; wrap.id = 'hsl-' + k;
    [['h', '色相', 360], ['s', '饱和度', 100], ['l', '亮度', 100]].forEach(function (c) {
      var lb = document.createElement('label');
      lb.className = 'rng';
      lb.innerHTML = '<span>' + c[1] + '</span><input type="range" min="0" max="' + c[2] + '" step="1" data-hsl="' + hx + '" data-c="' + c[0] + '"><b></b>';
      wrap.appendChild(lb);
    });
    box.parentNode.appendChild(wrap);
  }
  balls('paint', $('setPaint')); balls('rim', $('setRim')); balls('caliper', $('setCaliper'));
  (function () {
    var box = $('setStats'), g = '';
    STATS.forEach(function (o) {
      if (o[2] !== g) { g = o[2]; var h = document.createElement('span'); h.className = 'tags-g'; h.textContent = g; box.appendChild(h); }
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'tag'; b.setAttribute('data-k', 'stats'); b.setAttribute('data-v', o[0]); b.textContent = o[1];
      box.appendChild(b);
    });
  })();
  function ui() {
    var all = panel.querySelectorAll('[data-k]');
    for (var i = 0; i < all.length; i++) {
      var e = all[i], k = e.getAttribute('data-k'), v = e.getAttribute('data-v');
      if (BOOL[k]) e.classList.toggle('on', s[k]);
      else if (NUM[k]) {
        if (document.activeElement !== e) e.value = s[k];
        e.style.setProperty('--p', ((s[k] - NUM[k].min) / (NUM[k].max - NUM[k].min) * 100).toFixed(1) + '%');
        $('setV-' + k).textContent = NUM[k].fmt(s[k]);
      } else if (k === 'stats') {
        var n = s.stats.indexOf(v);
        e.classList.toggle('on', n >= 0);
        e.setAttribute('data-n', n >= 0 ? n + 1 : '');
      } else e.classList.toggle('on', s[k] === v);
    }
    ['paint', 'rim', 'caliper'].forEach(function (k) {
      var o = find(k, 'custom'), h = s[o.hex], a = hex2hsl(h), on = s[k] === 'custom', w = $('hsl-' + k);
      if (a[1] < 3 && hueKeep[o.hex] !== undefined) a[0] = hueKeep[o.hex];
      panel.querySelector('.ball.custom[data-k="' + k + '"]').style.setProperty('--c', h);
      w.className = 'hsl' + (on ? ' on' : '');
      var ins = w.querySelectorAll('input');
      for (var j = 0; j < 3; j++) {
        if (document.activeElement !== ins[j]) ins[j].value = a[j];
        ins[j].style.setProperty('--p', (a[j] / (j ? 100 : 360) * 100).toFixed(1) + '%');
        ins[j].nextSibling.textContent = j ? a[j] + '%' : a[j] + '°';
      }
      w.style.setProperty('--hue', a[0]);
    });
    var mb = panel.querySelector('.ball.model[data-k="paint"]');
    mb.style.setProperty('--c', modelPaint ? modelPaint.css : '#9a0c13');
    mb.disabled = !modelPaint;
    ['rim', 'caliper'].forEach(function (k) { var b = panel.querySelector('.ball.model[data-k="' + k + '"]'); b.style.setProperty('--c', modelPart[k] || '#9a9da3'); b.disabled = !modelPart[k]; });
    $('setQualNote').textContent = QUAL[s.quality].note;
    $('setPaintName').textContent = api.pick('paint').name;
    $('setRimName').textContent = api.pick('rim').name;
    $('setCaliperName').textContent = api.pick('caliper').name + (s.caliper === 'custom' || s.caliper === 'model' ? '' : '色');
    $('setBgImg').className = 'set-more' + (s.bg === 'image' ? ' on' : '');
    $('setVinylMore').className = 'set-more' + (s.vinyl ? ' on' : '');
    panel.style.setProperty('--acc', api.pick('paint').acc);
  }
  function toggle(on) {
    if (on === open) return;
    open = on;
    panel.className = 'set' + (on ? ' on' : '');
    if (on && window.D3DebugPanel) D3DebugPanel.close();
    if (on && window.D3CarInfo) D3CarInfo.close();
    btn.classList.toggle('on', on);
    $('app').classList.toggle('set-open', on);
    if (on) { var rows = panel.querySelector('.set-body').children; for (var i = 0; i < rows.length; i++) rows[i].style.animationDelay = Math.min(i * 0.035, 0.6).toFixed(3) + 's'; }
    emit('open');
  }
  btn.addEventListener('click', function () { toggle(!open); });
  $('setClose').addEventListener('click', function () { toggle(false); });
  panel.addEventListener('click', function (e) {
    if (e.target === panel) { toggle(false); return; }
    var act = e.target.closest ? e.target.closest('[data-act]') : null;
    if (act) { emit('act:' + act.getAttribute('data-act')); return; }
    if (e.target.getAttribute('data-reset') === 'view') { VIEW.forEach(function (k) { api.set(k, NUM[k].def); }); return; }
    if (e.target.getAttribute('data-reset') === 'cam') { CAM.forEach(function (k) { api.set(k, NUM[k].def); }); return; }
    if (e.target.getAttribute('data-reset') === 'dt') { api.set('dtScale', NUM.dtScale.def); return; }
    var t = e.target.closest ? e.target.closest('[data-k]') : null;
    if (!t || NUM[t.getAttribute('data-k')] || t.tagName === 'INPUT') return;
    var k = t.getAttribute('data-k'), v = t.getAttribute('data-v');
    if (k === 'stats') {
      var l = s.stats.slice(), i = l.indexOf(v);
      if (i >= 0) { if (l.length > 1) l.splice(i, 1); } else { if (l.length >= 4) l.pop(); l.push(v); }
      api.set('stats', l);
      return;
    }
    if (k === 'bg' && v === 'image') { emit('act:bgUse'); return; }
    api.set(k, BOOL[k] ? !s[k] : v);
  });
  panel.addEventListener('input', function (e) {
    var k = e.target.getAttribute('data-k'), hx = e.target.getAttribute('data-hsl');
    if (NUM[k]) api.set(k, parseFloat(e.target.value));
    else if (hx) {
      var ins = e.target.parentNode.parentNode.querySelectorAll('input'), v = [];
      for (var j = 0; j < 3; j++) v.push(parseFloat(ins[j].value));
      if (e.target.getAttribute('data-c') === 'h') { if (v[1] < 25) v[1] = ins[1].value = 70; if (v[2] > 85 || v[2] < 15) v[2] = ins[2].value = 50; }
      hueKeep[hx] = v[0];
      api.set(hx, hsl2hex(v[0], v[1], v[2]));
    }
  });
  Object.keys(NUM).forEach(function (k) { var e = panel.querySelector('[data-k="' + k + '"]'); e.min = NUM[k].min; e.max = NUM[k].max; e.step = NUM[k].step; });
  ui();

  (function () {
    var LC = ['pink', 'yellow', 'orange', 'cyan', 'green', 'purple', 'red'];
    function npCss() {
      var r = document.documentElement.style;
      r.setProperty('--np-scale', s.npScale);
      r.setProperty('--np-left', s.npLeft ? '.4rem' : 'auto');
      r.setProperty('--np-right', s.npLeft ? 'auto' : '.4rem');
      r.setProperty('--np-origin', s.npLeft ? 'left' : 'right');
      r.setProperty('--np-top', s.npTop + '%');
      r.setProperty('--np-opacity', s.npOpacity);
      r.setProperty('--np-height', s.npHeight + 'rem');
      r.setProperty('--np-width', s.npWidth + 'rem');
      r.setProperty('--np-font', s.npFont + 'rem');
      r.setProperty('--np-bg', s.npBg ? 'var(--chip)' : 'transparent');
      r.setProperty('--np-border', s.npBg ? '1px solid var(--line)' : '0');
      r.setProperty('--np-shadow', s.npBg ? '0 .04rem .16rem rgba(0,0,0,.12)' : 'none');
      var b = document.body;
      for (var i = 0; i < LC.length; i++) b.classList.remove('lc-' + LC[i]);
      if (s.npColor !== 'default') b.classList.add('lc-' + s.npColor);
      if (s.npColorLight !== 'default' && s.npColorLight !== s.npColor) b.classList.add('lc-' + s.npColorLight);
    }
    api.on(function (k) { if (k === 'showMusic' || /^np/.test(k)) npCss(); });
    npCss();

    var qWrap = $('quickWrap'), qToggle = $('quickToggle'), qClose = $('quickClose');
    if (qWrap && qToggle) {
      qToggle.addEventListener('click', function () { qWrap.classList.add('open'); });
      if (qClose) qClose.addEventListener('click', function () { qWrap.classList.remove('open'); });
      qWrap.addEventListener('click', function (e) { if (e.target === qWrap) qWrap.classList.remove('open'); });
    }
    var switches = [
      { id: 'wcSwitch', field: 'energy.wirelessCharging', api: 'vehicle.energy.setWirelessCharging' },
      { id: 'acPower', field: 'ac.power', api: 'vehicle.ac.setPower' },
      { id: 'acCompressor', field: 'ac.compressor', api: 'vehicle.ac.setCompressor' },
      { id: 'frontDefrost', field: 'ac.frontDefrost', api: 'vehicle.ac.setFrontDefrost' },
      { id: 'rearDefrost', field: 'ac.rearDefrost', api: 'vehicle.ac.setRearDefrost' },
      { id: 'lowBeam', field: 'light.lowBeam', api: 'vehicle.light.setLowBeam' },
      { id: 'highBeam', field: 'light.highBeam', api: 'vehicle.light.setHighBeam' },
      { id: 'daytimeRunning', field: 'light.daytimeRunning', api: 'vehicle.light.setDaytimeRunning' },
      { id: 'frontFog', field: 'light.frontFog', api: 'vehicle.light.setFrontFog' },
      { id: 'rearFog', field: 'light.rearFog', api: 'vehicle.light.setRearFog' },
      { id: 'hazard', field: 'light.hazard', api: 'vehicle.light.setHazard' }
    ];
    switches.forEach(function (sw) {
      var el = $(sw.id);
      if (!el) return;
      if (window.D3Data && D3Data.onCar) D3Data.onCar(function (c) {
        var v = c[sw.field];
        el.parentElement.style.display = (v === undefined || v === null) ? 'none' : '';
        el.classList.toggle('on', v === true);
      });
      el.addEventListener('click', function () {
        var on = !el.classList.contains('on');
        var o = window.uiplus, fn = null;
        if (o && o.vehicle) { try { var p = o.vehicle; sw.api.split('.').forEach(function (seg) { p = p == null ? null : p[seg]; }); fn = p; } catch (e) { fn = null; } }
        if (!fn) return;
        el.classList.toggle('on', on);
        try { fn(on); } catch (e) {}
      });
    });
    var seatBtns = [
      { id: 'driverHeating', field: 'seat.driver.heating', pos: 'driver', fn: 'cycleHeating' },
      { id: 'passengerHeating', field: 'seat.passenger.heating', pos: 'passenger', fn: 'cycleHeating' },
      { id: 'driverVent', field: 'seat.driver.ventilation', pos: 'driver', fn: 'cycleVentilation' },
      { id: 'passengerVent', field: 'seat.passenger.ventilation', pos: 'passenger', fn: 'cycleVentilation' }
    ];
    seatBtns.forEach(function (sb) {
      var el = $(sb.id);
      if (!el) return;
      if (window.D3Data && D3Data.onCar) D3Data.onCar(function (c) {
        var v = c[sb.field];
        el.parentElement.style.display = (v === undefined || v === null) ? 'none' : '';
        el.classList.toggle('on', v === true);
      });
      el.addEventListener('click', function () {
        var o = window.uiplus, seat = o && o.vehicle && o.vehicle.seat;
        if (!seat || typeof seat[sb.fn] !== 'function') return;
        try { seat[sb.fn](sb.pos); } catch (e) {}
      });
    });

    (function () {
      var car = function () { return (window.D3Data && D3Data.car && D3Data.car()) || {}; };
      var ac = function () { return (window.uiplus && window.uiplus.vehicle && window.uiplus.vehicle.ac) || null; };
      var send = function (name, arg) {
        var a = ac();
        if (!a || typeof a[name] !== 'function') return;
        try { if (arg === undefined) a[name](); else a[name](arg); } catch (e) {}
      };
      var bind = function (id, fn) { var el = $(id); if (el) el.addEventListener('click', fn); };
      var has = function (k) { return car()[k] !== undefined; };
      var num = function (k) { var v = car()[k]; return typeof v === 'number' ? v : null; };
      var pick = $('acPick'), pickList = $('acPickList'), pickTitle = $('acPickTitle');
      var pickClose = function () { if (pick) pick.classList.remove('open'); };
      var pickOpen = function (title, items, cur, apply) {
        if (!pick || !pickList) { apply(cur); return; }
        pickTitle.textContent = title;
        pickList.innerHTML = '';
        items.forEach(function (v) {
          var b = document.createElement('button');
          b.type = 'button'; b.className = 'acp-i' + (v === cur ? ' on' : ''); b.textContent = v;
          b.addEventListener('click', function () { pickClose(); apply(v); });
          pickList.appendChild(b);
        });
        var bar = document.querySelector('.stats'), panel = pick.querySelector('.acp-panel');
        if (bar && panel) {
          var r = bar.getBoundingClientRect(), h = window.innerHeight || document.documentElement.clientHeight;
          panel.style.bottom = Math.round(Math.max(0, h - r.top) + 8) + 'px';
        }
        pick.classList.add('open');
        var onEl = pickList.querySelector('.acp-i.on');
        if (onEl) pickList.scrollTop = Math.max(0, onEl.offsetTop - pickList.clientHeight / 2 + onEl.offsetHeight / 2);
      };
      if (pick) {
        pick.addEventListener('click', function (e) { if (e.target === pick) pickClose(); });
        pick.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
      }
      bind('acOn', function () { if (has('ac.power')) send('togglePower'); });
      bind('acCirc', function () {
        var cur = car()['ac.circulation'];
        var norm = (cur === 'inside' || cur === true || cur === 1) ? 'inside' : ((cur === 'outside' || cur === false || cur === 0) ? 'outside' : null);
        var next = (norm === 'inside') ? 'outside' : 'inside';
        var abs = function () { send('setCirculation', next); };
        var a = ac();
        if (a && typeof a.toggleCirculation === 'function') {
          try { var p = a.toggleCirculation(); if (p && typeof p.then === 'function') p.then(null, abs); else abs(); } catch (e) { abs(); }
        } else abs();
      });
      bind('acTempBtn', function () {
        var cur = num('ac.driverTemperature');
        if (cur === null) return;
        var items = [];
        for (var t = 33; t >= 17; t--) items.push(t);
        pickOpen('设定温度', items, Math.round(cur), function (v) { send('setDriverTemperature', v); });
      });
      bind('acFanBtn', function () {
        var cur = num('ac.fanLevel');
        if (cur === null) return;
        var items = [];
        for (var f = 7; f >= 1; f--) items.push(f);
        pickOpen('风量', items, Math.round(cur), function (v) { send('setFanLevel', v); });
      });
    })();

    (function () {
      var pick = $('netPick'), list = $('netPickList'), panel = pick ? pick.querySelector('.acp-panel') : null;
      if (!pick || !list) return;
      var timer = null;
      var WST = { 0: '关闭中', 1: '已关闭', 2: '开启中', 3: '已开启', 4: '未知' };
      var BST = { 10: '已关闭', 11: '开启中', 12: '已开启', 13: '关闭中' };
      var BCN = { 0: '未连接', 1: '连接中', 2: '已连接', 3: '断开中' };
      var CST = { 0: '已断开', 1: '连接中', 2: '已连接', 3: '已挂起', 4: '断开中' };
      var SIMT = { 0: '未知', 1: '无 SIM 卡', 2: '需 PIN', 3: '需 PUK', 4: '网络锁定', 5: '就绪', 6: '未就绪', 7: '永久停用', 8: '卡错误', 9: '卡受限', 10: '已加载', 11: '已插入' };
      var TRT = { wifi: 'Wi-Fi', cellular: '移动网络', ethernet: '有线网络', vpn: 'VPN', bluetooth: '蓝牙共享', other: '其他', none: '没有联网' };
      function S() { return (window.D3Data && D3Data.sys) ? (D3Data.sys() || {}) : {}; }
      function el(tag, cls, txt) { var n = document.createElement(tag); if (cls) n.className = cls; if (txt !== undefined && txt !== null) n.textContent = txt; return n; }
      function nm(s, k) { return (s[k] === null || s[k] === undefined) ? null : s[k]; }
      function num(s, k) { return typeof nm(s, k) === 'number' ? nm(s, k) : null; }
      function onText(v) { return v === true ? '已开启' : (v === false ? '已关闭' : '未知'); }
      function connText(v) { return v === true ? '已连接' : (v === false ? '未连接' : '未知'); }
      function code(map, v) { return (typeof v === 'number' && map[v]) ? map[v] : null; }
      function bars(level) {
        var box = el('span', 'npt-bars'), n = (typeof level === 'number') ? Math.max(0, Math.min(4, Math.round(level))) : null;
        for (var i = 0; i < 4; i++) box.appendChild(el('i', (n !== null && i < n) ? 'on' : ''));
        return box;
      }
      function lvText(level) { return (typeof level === 'number') ? level + '/4' : '未知'; }
      function join(a, sep) { var out = []; (a || []).forEach(function (t) { if (t !== null && t !== undefined && t !== '') out.push(t); }); return out.join(sep || ' · '); }
      function band(freq) { return (freq === null || freq === undefined) ? null : (freq >= 5000 ? '5 GHz' : (freq >= 2400 ? '2.4 GHz' : null)); }
      function line(parts) {
        var d = el('div', 'npt-s');
        (parts || []).forEach(function (p) { if (!p) return; d.appendChild(typeof p === 'string' ? document.createTextNode(p) : p); });
        return d;
      }
      function row(title, head, nodes) {
        var r = el('div', 'npt-r'), h = el('div', 'npt-h');
        h.appendChild(el('span', 'npt-n', title));
        h.appendChild(el('em', '', head));
        r.appendChild(h);
        (nodes || []).forEach(function (n) { if (n) r.appendChild(n); });
        return r;
      }
      function wifiRow(s) {
        var q = line([bars(s.wifiLevel), '信号 ' + lvText(s.wifiLevel),
          (num(s, 'wifiRssi') === null ? '' : ' · ' + s.wifiRssi + ' dBm'),
          (num(s, 'wifiLink') === null ? '' : ' · ' + s.wifiLink + ' Mbps'),
          (band(num(s, 'wifiFreq')) ? ' · ' + band(num(s, 'wifiFreq')) : '')]);
        return row('Wi-Fi', join([code(WST, s.wifiState) || onText(nm(s, 'wifiOn')), connText(nm(s, 'wifiConn'))]),
          [line([s.wifiSsid || '未连接热点']), q]);
      }
      function cellRow(s) {
        var q = line([bars(s.cellLevel), '信号 ' + lvText(s.cellLevel),
          (num(s, 'cellDbm') === null ? '' : ' · ' + s.cellDbm + ' dBm')]);
        var sim = code(SIMT, s.cellSim);
        var simBad = sim && sim !== '就绪' && sim !== '已插入' && sim !== '已加载';
        return row('移动网络', (nm(s, 'cellOn') === false) ? '已关闭' : join([code(CST, s.cellState) || onText(nm(s, 'cellOn'))]),
          [line([join([s.cellType, s.cellOp, s.cellRoam === true ? '漫游中' : null]) || '未注册网络']),
           q, (simBad ? line(['SIM：' + sim]) : null)]);
      }
      function btRow(s) {
        return row('蓝牙', join([code(BST, s.btState) || onText(nm(s, 'btOn')), code(BCN, s.btConn) || connText(nm(s, 'btConn'))]),
          [line(['媒体 ' + (code(BCN, s.btMedia) || '未知') + '　电话 ' + (code(BCN, s.btPhone) || '未知')])]);
      }
      function paint() {
        if (!pick.classList.contains('open')) return;
        var s = S(), frag = document.createDocumentFragment();
        var sum = el('div', 'npt-sum');
        var tr = (s.transport === null || s.transport === undefined) ? '未知' : (TRT[s.transport] || s.transport);
        sum.textContent = '当前联网：' + join([tr,
          (s.netValid === true ? '可正常上网' : (s.netValid === false ? '未验证' : null)),
          (s.netMetered === true ? '计费网络' : null), (s.netVpn === true ? 'VPN' : null), (s.netPortal === true ? '需门户登录' : null)]);
        frag.appendChild(sum);
        frag.appendChild(wifiRow(s));
        frag.appendChild(cellRow(s));
        frag.appendChild(btRow(s));
        frag.appendChild(el('div', 'npt-f', '接口只读：只能查看，不能开关'));
        list.innerHTML = '';
        list.appendChild(frag);
      }
      function refresh() {
        var d = window.D3Data;
        if (!d || typeof d.sysDetail !== 'function') return;
        try { d.sysDetail().then(paint, paint); } catch (e) {}
      }
      function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
      function close() { pick.classList.remove('open'); stopTimer(); }
      function open() {
        pick.classList.add('open');
        var head = document.querySelector('.c-head');
        if (head && panel) {
          var hr = head.getBoundingClientRect();
          panel.style.top = Math.round(hr.bottom + 8) + 'px';
        }
        refresh();
        stopTimer();
        timer = setInterval(refresh, 3000);
      }
      var sysBtn = $('cSys');
      if (sysBtn) sysBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (pick.classList.contains('open')) close(); else open();
      });
      if (pick) {
        pick.addEventListener('click', function (e) { if (e.target === pick) close(); });
        pick.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
      }
    })();
  })();
})();
