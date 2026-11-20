(function () {
  'use strict';
  var T = window.THREE;
  var pages = [], shown = [], cur = 0, locked = false, W = 0, H = 0, renderer = null, canvas = null, raf = 0, timer = 0, lastT = 0, lastDraw = 0;
  var live = null, ready = false, seq = 0, visible = true, hostVisible = true, started = false, sliding = false, slideTimer = 0;
  var strip = document.getElementById('strip'), dotsEl = document.getElementById('dots'), toastT = 0;
  var KEY = 'd3-page';

  (function () {
    var LO = '#ifdef D3_MP\nprecision mediump float;\n#endif\n', HI = '#ifdef D3_MP\nprecision highp float;\n#endif\n';
    var keep = ['uv_pars_fragment', 'uv2_pars_fragment', 'iridescence_fragment', 'cube_uv_reflection_fragment', 'normal_pars_fragment', 'transmission_pars_fragment',
      'shadowmap_pars_fragment', 'bumpmap_pars_fragment', 'normalmap_pars_fragment', 'logdepthbuf_pars_fragment', 'clipping_planes_pars_fragment'];
    var c = T.ShaderChunk, b = c.bsdfs;
    b = b.replace('float D_GGX( const in float alpha, const in float dotNH ) {\n\tfloat a2 = pow2( alpha );',
      'float D_GGX( const in float alpha, const in float dotNH ) {\n#ifdef D3_MP\n\tfloat a2 = max( pow2( alpha ), 6.2e-5 );\n#else\n\tfloat a2 = pow2( alpha );\n#endif');
    b = b.split('return F * ( V * D );').join('#ifdef D3_MP\n\treturn F * min( V * D, 8192.0 );\n#else\n\treturn F * ( V * D );\n#endif');
    c.bsdfs = b;
    ['standard', 'physical'].forEach(function (k) {
      var s = T.ShaderLib[k], f = s.fragmentShader.replace('varying vec3 vViewPosition;', 'varying highp vec3 vViewPosition;')
        .replace('#include <common>', '#include <common>\n#ifdef D3_MP\n#undef EPSILON\n#define EPSILON 1e-4\n#endif');
      keep.forEach(function (n) { f = f.split('#include <' + n + '>').join(HI + '#include <' + n + '>\n' + LO); });
      s.fragmentShader = '#if defined( D3_LOWP ) && ! defined( USE_IRIDESCENCE )\n#define D3_MP\n#endif\n' + LO + f;
    });
  })();

  function now() { return Date.now(); }
  function rem() { return parseFloat(getComputedStyle(document.documentElement).fontSize) || 50; }

  function invalidate(fps) {
    if (!fps) dirty = true;
    if (!started || !visible || !ready || raf) return;
    fps = Math.min(fps || 60, D3Set.get('fps') === '30' ? 30 : 60);
    if (fps < 55) {
      if (timer) return;
      var wait = Math.max(0, 1000 / fps - (performance.now() - lastDraw));
      timer = setTimeout(function () { timer = 0; if (!raf && started && visible && ready) raf = requestAnimationFrame(frame); }, wait);
      return;
    }
    clearTimeout(timer); timer = 0;
    raf = requestAnimationFrame(frame);
  }
  function frame(t) {
    raf = 0;
    if (!live || !ready) return;
    var dt = lastT ? Math.min(0.1, (t - lastT) / 1000) : 1 / 60;
    lastT = t;
    var f = live.update ? live.update(dt) : 0, need = f === true ? 60 : f || 0;
    var act = !!(g && g.mode === 'rot') || !!pinch || !!mpan;
    if (post) {
      if (post.bokeh) post.bokeh.uniforms.focus.value = live.focusDist ? live.focusDist() : 10;
      post.c.render(dt);
    } else if (!act && live.still === true && renderer.capabilities.isWebGL2 && !renderer.shadowMap.needsUpdate) {
      if (still.ok && !dirty && stillSame()) stillDraw();
      else if (need) stillSave();
      else { still.ok = false; renderer.render(live.scene, live.camera); }
    } else {
      still.ok = false;
      if ((need || act) && live.fast === true && !soft && renderer.capabilities.isWebGL2) fastDraw();
      else renderer.render(live.scene, live.camera);
    }
    dirty = false;
    if (!need && !act) { stillDrop(); fastDrop(); }
    drawn++;
    stashFree(live.id);
    if (live.after) live.after();
    if (canvas.style.opacity !== '1') canvas.style.opacity = '1';
    lastDraw = performance.now();
    if (need) invalidate(need); else lastT = 0;
  }
  function stopFrames() { cancelAnimationFrame(raf); raf = 0; clearTimeout(timer); timer = 0; lastT = 0; }

  var OV = 1, dirty = true, v2a = new T.Vector2(), v2b = new T.Vector2(), m4 = new T.Matrix4();
  function quad(fs, uniforms) {
    var m = new T.ShaderMaterial({ uniforms: uniforms, vertexShader: 'void main() { gl_Position = vec4(position.xy, 0.0, 1.0); }', fragmentShader: fs, blending: T.NoBlending, toneMapped: false });
    var q = new T.Mesh(new T.PlaneGeometry(2, 2), m), sc = new T.Scene();
    q.frustumCulled = false;
    sc.add(q);
    return { m: m, sc: sc, cam: new T.Camera() };
  }
  var still = { buf: null, q: null, dm: {}, ok: false, scene: null, m: new T.Matrix4() };
  function stillBuf(w, h) {
    if (still.buf && still.buf.w === w && still.buf.h === h) return;
    stillDrop();
    var dt = new T.DepthTexture(w, h);
    dt.type = T.UnsignedIntType;
    var rt = new T.WebGLRenderTarget(w, h, { depthTexture: dt, stencilBuffer: false, minFilter: T.NearestFilter, magFilter: T.NearestFilter, generateMipmaps: false });
    var color = new T.FramebufferTexture(w, h, T.RGBAFormat);
    if (!still.q) {
      still.q = quad('uniform sampler2D tC;\nuniform sampler2D tD;\nuniform vec2 uS;\nvoid main() {\n  vec2 uv = gl_FragCoord.xy / uS;\n  gl_FragColor = texture2D(tC, uv);\n  gl_FragDepthEXT = texture2D(tD, uv).r;\n}',
        { tC: { value: null }, tD: { value: null }, uS: { value: new T.Vector2() } });
      still.q.m.depthFunc = T.AlwaysDepth;
      still.q.m.extensions.fragDepth = true;
    }
    var u = still.q.m.uniforms;
    u.tC.value = color; u.tD.value = dt; u.uS.value.set(w, h);
    still.buf = { w: w, h: h, color: color, depth: dt, rt: rt };
  }
  function stillDrop() {
    still.ok = false;
    if (!still.buf) return;
    still.buf.rt.dispose(); still.buf.depth.dispose(); still.buf.color.dispose();
    still.buf = null;
  }
  function stillSame() {
    var c = live.camera, a = still.m.elements, sz = renderer.getDrawingBufferSize(v2a);
    if (!still.buf || still.scene !== live.scene || sz.x !== still.buf.w || sz.y !== still.buf.h) return false;
    var b = m4.multiplyMatrices(c.projectionMatrix, c.matrixWorldInverse).elements;
    for (var i = 0; i < 16; i++) if (Math.abs(a[i] - b[i]) > 1e-7) return false;
    return true;
  }
  function depthMat(m) {
    var d = still.dm[m.id], at = m.alphaTest || 0, key = [m.side, at > 0, at > 0 && !!m.map, at > 0 && !!m.alphaMap].join();
    if (!d) d = still.dm[m.id] = new T.MeshBasicMaterial({ colorWrite: false });
    if (d.userData.k !== key) { d.userData.k = key; d.needsUpdate = true; }
    d.side = m.side; d.visible = m.visible !== false && m.depthWrite !== false;
    d.depthTest = m.depthTest; d.depthFunc = m.depthFunc;
    d.polygonOffset = m.polygonOffset; d.polygonOffsetFactor = m.polygonOffsetFactor; d.polygonOffsetUnits = m.polygonOffsetUnits;
    d.alphaTest = at; d.map = at > 0 ? m.map || null : null; d.alphaMap = at > 0 ? m.alphaMap || null : null; d.opacity = at > 0 ? m.opacity : 1;
    return d;
  }
  function stillSave() {
    var sc = live.scene, cam = live.camera, mask = cam.layers.mask, ac = renderer.autoClear, sz = renderer.getDrawingBufferSize(v2a);
    stillBuf(sz.x, sz.y);
    var hide = [], swap = [], used = {};
    sc.traverse(function (o) { if (o.isLight) o.layers.enable(OV); });
    cam.layers.set(0);
    renderer.render(sc, cam);
    renderer.copyFramebufferToTexture(v2b.set(0, 0), still.buf.color);
    sc.traverse(function (o) {
      if (!o.visible || !o.layers.test(cam.layers)) return;
      if (o.isMesh && o.material) {
        var arr = Array.isArray(o.material), l = (arr ? o.material : [o.material]).map(function (m) { used[m.id] = 1; return depthMat(m); });
        swap.push(o, o.material); o.material = arr ? l : l[0];
      } else if (o.isLine || o.isPoints || o.isSprite) { hide.push(o); o.visible = false; }
    });
    var bg = sc.background;
    sc.background = null;
    renderer.setRenderTarget(still.buf.rt);
    renderer.clear();
    renderer.render(sc, cam);
    renderer.setRenderTarget(null);
    sc.background = bg;
    for (var i = 0; i < swap.length; i += 2) swap[i].material = swap[i + 1];
    hide.forEach(function (o) { o.visible = true; });
    for (var k in still.dm) if (!used[k]) { still.dm[k].dispose(); delete still.dm[k]; }
    cam.layers.set(OV);
    renderer.autoClear = false;
    renderer.render(sc, cam);
    renderer.autoClear = ac; cam.layers.mask = mask;
    still.ok = true; still.scene = sc;
    still.m.multiplyMatrices(cam.projectionMatrix, cam.matrixWorldInverse);
  }
  function stillDraw() {
    var cam = live.camera, mask = cam.layers.mask, ac = renderer.autoClear;
    renderer.render(still.q.sc, still.q.cam);
    cam.layers.set(OV);
    renderer.autoClear = false;
    renderer.render(live.scene, cam);
    renderer.autoClear = ac; cam.layers.mask = mask;
  }
  function stillFree() {
    stillDrop();
    for (var k in still.dm) still.dm[k].dispose();
    still.dm = {}; still.scene = null;
  }
  var FXAA = [
    'uniform sampler2D tC;', 'uniform vec2 uR;',
    'float L(vec4 c) { return dot(c.rgb, vec3(0.299, 0.587, 0.114)) * 0.7 + c.a * 0.3; }',
    'void main() {',
    '  vec2 uv = gl_FragCoord.xy * uR;',
    '  vec4 cM = texture2D(tC, uv);',
    '  float lM = L(cM), lNW = L(texture2D(tC, uv + vec2(-1.0, 1.0) * uR)), lNE = L(texture2D(tC, uv + uR)),',
    '    lSW = L(texture2D(tC, uv - uR)), lSE = L(texture2D(tC, uv + vec2(1.0, -1.0) * uR));',
    '  float mn = min(lM, min(min(lNW, lNE), min(lSW, lSE))), mx = max(lM, max(max(lNW, lNE), max(lSW, lSE)));',
    '  if (mx - mn < max(0.04, mx * 0.12)) { gl_FragColor = cM; return; }',
    '  vec2 d = vec2(-((lNW + lNE) - (lSW + lSE)), (lNW + lSW) - (lNE + lSE));',
    '  float red = max((lNW + lNE + lSW + lSE) * 0.03125, 1.0 / 128.0);',
    '  d = clamp(d / (min(abs(d.x), abs(d.y)) + red), vec2(-8.0), vec2(8.0)) * uR;',
    '  vec4 a = 0.5 * (texture2D(tC, uv - d / 6.0) + texture2D(tC, uv + d / 6.0));',
    '  vec4 b = a * 0.5 + 0.25 * (texture2D(tC, uv - d * 0.5) + texture2D(tC, uv + d * 0.5));',
    '  float lb = L(b);',
    '  gl_FragColor = lb < mn || lb > mx ? a : b;',
    '}'].join('\n');
  var fast = { rt: null, q: null }, soft = false;
  function fastDraw() {
    var sz = renderer.getDrawingBufferSize(v2a);
    if (!fast.rt || fast.rt.width !== sz.x || fast.rt.height !== sz.y) {
      fastDrop();
      var rt = fast.rt = new T.WebGLRenderTarget(sz.x, sz.y, { depthTexture: new T.DepthTexture(sz.x, sz.y), stencilBuffer: false, minFilter: T.LinearFilter, magFilter: T.LinearFilter, generateMipmaps: false });
      rt.isXRRenderTarget = true; rt.texture.encoding = T.sRGBEncoding;
      rt.texture.internalFormat = 'RGBA8';
      if (!fast.q) { fast.q = quad(FXAA, { tC: { value: null }, uR: { value: new T.Vector2() } }); fast.q.m.depthTest = false; fast.q.m.depthWrite = false; }
      fast.q.m.uniforms.tC.value = rt.texture; fast.q.m.uniforms.uR.value.set(1 / sz.x, 1 / sz.y);
    }
    renderer.setRenderTarget(fast.rt);
    renderer.render(live.scene, live.camera);
    renderer.setRenderTarget(null);
    renderer.render(fast.q.sc, fast.q.cam);
  }
  function fastDrop() {
    if (!fast.rt) return;
    fast.rt.depthTexture.dispose(); fast.rt.dispose();
    fast.rt = null;
  }

  function enabled(p) { return p.id !== 'vinyl' || D3Set.get('vinyl'); }
  function refresh() {
    var id = shown[cur] ? shown[cur].id : null;
    shown = pages.filter(enabled);
    pages.forEach(function (p) { p.el.style.display = enabled(p) ? '' : 'none'; });
    var i = 0;
    shown.forEach(function (p, k) { if (p.id === id) i = k; });
    cur = i;
    dotsEl.innerHTML = '';
    if (shown.length > 1) shown.forEach(function (p, k) { var d = document.createElement('i'); d.addEventListener('click', function () { go(k); }); dotsEl.appendChild(d); });
    resize();
    dots();
    if (started && !sliding && shown[cur] !== live) activate(cur);
  }

  function activate(i) {
    var p = shown[i];
    if (live === p) return;
    deactivate();
    live = p; ready = false;
    var s = ++seq;
    canvas.style.opacity = '0';
    p.el.insertBefore(canvas, p.el.querySelector('.ui'));
    D3Data.use(visible ? p.needs || [] : []);
    Promise.resolve(p.load(app)).then(function () {
      if (s !== seq) return;
      ready = true;
      makePost();
      if (p.resize) p.resize(W, H, W < H);
      if (!visible && p.wake) p.wake(false);
      if (D3Set.isOpen() && p.focus) p.focus(true);
      invalidate();
    }, function (e) { if (s === seq) console.error('3D 页面加载失败', e); });
  }
  function deactivate() {
    if (!live) return;
    seq++;
    stopFrames();
    var p = live;
    live = null; ready = false;
    dropPost();
    stillFree(); fastDrop();
    D3Data.use([]);
    unloading = p.id;
    p.unload();
    unloading = '';
    renderer.renderLists.dispose();
    renderer.clear();
  }

  function place(x, anim) {
    strip.style.transition = anim ? 'transform .42s cubic-bezier(.22, .8, .24, 1)' : 'none';
    strip.style.transform = 'translate3d(' + x.toFixed(1) + 'px,0,0)';
  }
  function go(i, ms) {
    i = Math.max(0, Math.min(shown.length - 1, i));
    var changed = i !== cur;
    cur = i;
    dots();
    if (changed) { try { localStorage.setItem(KEY, shown[cur].id); } catch (e) {} }
    var instant = ms === 0 || !W;
    place(-cur * W, !instant);
    sliding = !instant;
    clearTimeout(slideTimer);
    if (instant) settle();
    else slideTimer = setTimeout(function () { if (sliding) settle(); }, 520);
  }
  function settle() {
    sliding = false;
    if (started && shown[cur] !== live) activate(cur);
  }
  strip.addEventListener('transitionend', function (e) { if (e.target === strip && sliding) settle(); });
  function dots() {
    for (var i = 0; i < dotsEl.children.length; i++) dotsEl.children[i].className = i === cur ? 'on' : '';
    if (shown[cur]) document.getElementById('app').setAttribute('data-at', shown[cur].id);
  }

  var g = null;
  function start(x, y, target) {
    if (target && target.closest && target.closest('button, .set, .guide, .busy')) return;
    var edge = !locked && (x < W * 0.1 || x > W * 0.9);
    g = { x0: x, y0: y, x: x, y: y, t0: now(), lt: now(), vx: 0, mode: '', rot: !sliding && !edge && ready && live === shown[cur] && live.grab && live.grab(x, y) };
    if (sliding) {
      var m = new WebKitCSSMatrix(getComputedStyle(strip).transform);
      g.base = m.m41; place(m.m41, false); sliding = false;
    } else g.base = -cur * W;
  }
  function move(x, y) {
    if (!g) return;
    var dx = x - g.x, dy = y - g.y, t = now();
    g.vx = dx / Math.max(1, t - g.lt); g.lt = t;
    g.x = x; g.y = y;
    if (!g.mode) {
      var ax = Math.abs(x - g.x0), ay = Math.abs(y - g.y0);
      if (ax < 8 && ay < 8) return;
      g.mode = g.rot ? 'rot' : ax > ay && !locked && shown.length > 1 ? 'swipe' : 'none';
      if (g.mode === 'rot' && live.press) live.press(g.x0, g.y0);
    }
    if (g.mode === 'rot') { live.drag(dx, dy, x, y); invalidate(); }
    else if (g.mode === 'swipe') {
      var p = -(g.base + x - g.x0) / W;
      p = Math.max(0, Math.min(shown.length - 1, p));
      place(-p * W, false);
    }
  }
  function end(x, y) {
    if (!g) return;
    var s = g;
    g = null;
    if (s.mode === 'rot') { live.release(s.vx, x, y); invalidate(); return; }
    if (s.mode === 'swipe') {
      var p = -(s.base + x - s.x0) / W, target = Math.round(p);
      if (s.vx < -0.45) target = Math.floor(p) + 1;
      else if (s.vx > 0.45) target = Math.ceil(p) - 1;
      go(Math.max(cur - 1, Math.min(cur + 1, target)));
      return;
    }
    if (s.base !== -cur * W) { go(cur); return; }
    if (!s.mode && ready && live && live.tap && now() - s.t0 < 500) live.tap(x, y);
  }
  var touchedAt = 0;
  function touch(e) { touchedAt = now(); return e.changedTouches && e.changedTouches[0]; }
  var pinch = null;
  function canPinch(target) { return locked && ready && live && live.pinch && !(target && target.closest && target.closest('button, .set, .guide, .busy')); }
  function two(e) {
    var a = e.touches[0], b = e.touches[1];
    return { d: Math.sqrt(Math.pow(a.clientX - b.clientX, 2) + Math.pow(a.clientY - b.clientY, 2)), x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
  }
  document.addEventListener('touchstart', function (e) {
    var t = touch(e);
    if (e.touches.length === 2 && canPinch(e.touches[0].target) && canPinch(e.touches[1].target)) {
      if (g) { var s = g; g = null; if (s.mode === 'rot') live.release(0, s.x, s.y, true); }
      pinch = two(e);
      return;
    }
    if (t && !g && !pinch) start(t.clientX, t.clientY, e.target);
  }, { passive: true });
  document.addEventListener('touchmove', function (e) {
    if (pinch) {
      if (e.touches.length >= 2) { var p = two(e); live.pinch(pinch.d > 0 ? p.d / pinch.d : 1, p.x, p.y, p.x - pinch.x, p.y - pinch.y); pinch = p; invalidate(); }
      e.preventDefault();
      return;
    }
    var t = touch(e);
    if (t && g) { move(t.clientX, t.clientY); if (g && g.mode && g.mode !== 'none') e.preventDefault(); }
  }, { passive: false });
  document.addEventListener('touchend', function (e) {
    if (pinch) { touchedAt = now(); if (e.touches.length < 2) pinch = null; return; }
    var t = touch(e);
    if (t) end(t.clientX, t.clientY);
  });
  document.addEventListener('touchcancel', function () { pinch = null; if (g) { var s = g; g = null; if (s.mode === 'rot') live.release(0, s.x, s.y, true); else go(cur); } });
  var sl = null;
  document.addEventListener('touchstart', function (e) {
    var el = e.target;
    if (!el || el.type !== 'range' || e.touches.length !== 1 || !el.closest('.set-body')) return;
    e.preventDefault();
    var p = e.touches[0];
    sl = { el: el, box: el.closest('.set-body'), x: p.clientX, y: p.clientY, top: 0, v: +el.value, mode: '' };
    sl.top = sl.box.scrollTop;
  }, { passive: false, capture: true });
  document.addEventListener('touchmove', function (e) {
    if (!sl) return;
    var p = e.touches[0], dx = p.clientX - sl.x, dy = p.clientY - sl.y;
    if (!sl.mode && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) sl.mode = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    if (sl.mode === 'y') sl.box.scrollTop = sl.top - dy;
    if (sl.mode === 'x') {
      var el = sl.el, min = +el.min, max = +el.max, st = +el.step || 1, w = el.getBoundingClientRect().width || 1;
      var v = Math.max(min, Math.min(max, Math.round((sl.v + dx / w * (max - min) - min) / st) * st + min));
      v = +v.toFixed(6);
      if (+el.value !== v) { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }
    }
    e.preventDefault();
  }, { passive: false, capture: true });
  document.addEventListener('touchend', function () { if (sl && sl.mode === 'x') sl.el.dispatchEvent(new Event('change', { bubbles: true })); sl = null; }, true);
  document.addEventListener('touchcancel', function () { if (sl && sl.mode === 'x') sl.el.dispatchEvent(new Event('change', { bubbles: true })); sl = null; }, true);
  var mouse = false, mpan = null;
  document.addEventListener('mousedown', function (e) {
    if (now() - touchedAt <= 1000) return;
    if (e.button === 2 && canPinch(e.target)) { mpan = { x: e.clientX, y: e.clientY }; return; }
    if (e.button === 0 && !g) { mouse = true; start(e.clientX, e.clientY, e.target); }
  });
  document.addEventListener('mousemove', function (e) {
    if (mpan) { if (live && live.pinch) live.pinch(1, e.clientX, e.clientY, e.clientX - mpan.x, e.clientY - mpan.y); mpan = { x: e.clientX, y: e.clientY }; invalidate(); return; }
    if (mouse) move(e.clientX, e.clientY);
  });
  document.addEventListener('mouseup', function (e) { if (mpan && e.button === 2) { mpan = null; return; } if (mouse) { mouse = false; end(e.clientX, e.clientY); } });
  document.addEventListener('contextmenu', function (e) { if (locked) e.preventDefault(); });
  document.addEventListener('wheel', function (e) {
    if (!canPinch(e.target)) return;
    e.preventDefault();
    live.pinch(Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), e.clientX, e.clientY, 0, 0);
    invalidate();
  }, { passive: false });

  function resize() {
    var w = window.innerWidth, h = window.innerHeight;
    if (window.VIEWPORT && VIEWPORT.typing() && w === W && h < H) return;
    W = w; H = h;
    if (!W || !H) return;
    var portrait = W < H;
    document.documentElement.className = portrait ? 'portrait' : 'landscape';
    shown.forEach(function (p, i) { p.el.style.left = (i * W) + 'px'; p.el.style.width = W + 'px'; });
    place(-cur * W, false); sliding = false;
    if (!renderer) return;
    renderer.setSize(W, H, false);
    if (post) post.c.setSize(W, H);
    if (live && ready && live.resize) live.resize(W, H, portrait);
    invalidate();
  }
  function setVisible() {
    var v = !document.hidden && hostVisible;
    if (v === visible) return;
    visible = v;
    if (live) {
      D3Data.use(v ? live.needs || [] : []);
      if (ready && live.wake) live.wake(v);
    }
    if (v) invalidate(); else stopFrames();
  }
  document.addEventListener('visibilitychange', setVisible);
  var vis = window.uiplus && ((window.uiplus.lifecycle && window.uiplus.lifecycle.onVisibilityChange) || window.uiplus.onVisibilityChange);
  if (typeof vis === 'function') {
    try { vis(function (e) { hostVisible = !e || e.visible !== false; setVisible(); }); } catch (e) {}
  }

  var hdr = null;
  var NEUTRAL = {
    top: [400, [0.5, 14.0, 0.5]], room: [[0.0, 13.2, 0.0], [31.5, 28.5, 31.5]],
    boxes: [[[-10.906, -1.0, 1.846], -0.195, [2.328, 7.905, 4.651]], [[-5.607, -0.754, -0.758], 0.994, [1.970, 1.534, 3.955]], [[6.167, -0.16, 7.803], 0.561, [3.927, 6.285, 3.687]],
      [[-2.017, 0.018, 6.124], 0.333, [2.002, 4.566, 2.064]], [[2.291, -0.756, -2.621], -0.286, [1.546, 1.552, 1.496]], [[-2.193, -0.369, -5.547], 0.516, [3.875, 3.487, 2.986]]],
    lights: [[80, [-14.0, 10.0, 8.0], [0.1, 2.5, 2.5]], [80, [-14.0, 14.0, -4.0], [0.1, 2.5, 2.5]], [23, [14.0, 12.0, 0.0], [0.1, 5.0, 5.0]], [16, [0.0, 9.0, 14.0], [5.0, 5.0, 0.1]],
      [80, [7.0, 8.0, -14.0], [2.5, 2.5, 0.1]], [80, [-7.0, 16.0, -14.0], [2.5, 2.5, 0.1]], [1, [0.0, 20.0, 0.0], [0.1, 0.1, 0.1]]]
  };
  function neutralScene() {
    var sc = new T.Scene(), g = new T.BoxGeometry(), mats = [], add = function (m, p, s, ry) { var o = new T.Mesh(g, m); o.position.fromArray(p); o.scale.fromArray(s); o.rotation.y = ry || 0; sc.add(o); };
    g.deleteAttribute('uv');
    sc.position.y = -3.5;
    var top = new T.PointLight(0xffffff, NEUTRAL.top[0], 28, 2);
    top.position.fromArray(NEUTRAL.top[1]);
    sc.add(top);
    var roomM = new T.MeshStandardMaterial({ metalness: 0, side: T.BackSide }), boxM = new T.MeshStandardMaterial({ metalness: 0 });
    mats.push(roomM, boxM);
    add(roomM, NEUTRAL.room[0], NEUTRAL.room[1]);
    NEUTRAL.boxes.forEach(function (b) { add(boxM, b[0], b[2], b[1]); });
    NEUTRAL.lights.forEach(function (l) { var m = new T.MeshBasicMaterial(); m.color.setScalar(l[0]); mats.push(m); add(m, l[1], l[2]); });
    sc.userData.free = function () { g.dispose(); mats.forEach(function (m) { m.dispose(); }); };
    return sc;
  }
  function env(kind) {
    var pm = new T.PMREMGenerator(renderer), rt = null;
    if (kind === 'neutral') {
      var sc = neutralScene(), pc = renderer.physicallyCorrectLights;
      renderer.physicallyCorrectLights = true;
      try { rt = pm.fromScene(sc, 0.04); } catch (e) { console.warn('中性环境生成失败，用简易影棚', e); rt = null; }
      renderer.physicallyCorrectLights = pc;
      sc.userData.free();
    }
    if (kind === 'studio' && (hdr || window.D3_ENV_HDR) && T.RGBELoader) {
      try {
        if (!hdr) {
          var b = atob(window.D3_ENV_HDR), u = new Uint8Array(b.length), l = new T.RGBELoader();
          for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
          l.setDataType(T.HalfFloatType);
          hdr = l.parse(u.buffer);
          window.D3_ENV_HDR = null;
        }
        var tex = new T.DataTexture(hdr.data, hdr.width, hdr.height, T.RGBAFormat, T.HalfFloatType);
        tex.mapping = T.EquirectangularReflectionMapping; tex.minFilter = tex.magFilter = T.LinearFilter; tex.generateMipmaps = false; tex.flipY = true; tex.needsUpdate = true;
        rt = pm.fromEquirectangular(tex);
        tex.dispose();
      } catch (e) { console.warn('影棚环境图解不开，用简易影棚', e); rt = null; }
    }
    if (!rt) { var room = new T.RoomEnvironment(); rt = pm.fromScene(room, 0.04); room.dispose(); }
    pm.dispose();
    return rt;
  }
  var stash = {}, unloading = '', drawn = 0;
  function stashFree(id, all) {
    var l = stash[id];
    if (!l || !l.length) return;
    stash[id] = l.filter(function (x) { if (all || x.f < drawn) { x.m.dispose(); return false; } return true; });
  }
  function dispose(root) {
    var seen = [], id = live ? live.id : unloading;
    function tex(m) {
      for (var k in m) { var v = m[k]; if (v && v.isTexture) { if (seen.indexOf(v) < 0) { seen.push(v); v.dispose(); } m[k] = null; } }
      (stash[id] = stash[id] || []).push({ m: m, f: drawn });
    }
    root.traverse(function (o) {
      if (o.geometry) o.geometry.dispose();
      if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach(function (m) { if (seen.indexOf(m) < 0) { seen.push(m); tex(m); } });
    });
    if (root.environment) root.environment.dispose();
    if (root.background && root.background.isTexture) root.background.dispose();
  }
  function canvasTex(w, h, draw, color) {
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    draw(c.getContext('2d'), w, h);
    var t = new T.CanvasTexture(c);
    if (color !== false) t.encoding = T.sRGBEncoding;
    return t;
  }
  var app = {
    invalidate: invalidate, rem: rem, env: env, dispose: dispose, canvasTex: canvasTex, loadPost: loadPost, rtSpec: function () { return rtSpec(); },
    size: function () { return { w: W, h: H }; },
    renderer: function () { return renderer; }
  };

  function pixelRatio() { renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, D3Set.fx().ratio)); }

  var post = null, postLoad = null;
  function loadPost() {
    if (T.EffectComposer) return Promise.resolve();
    if (!postLoad) postLoad = new Promise(function (ok, no) {
      var s = document.createElement('script');
      s.src = 'three-post.js';
      s.onload = ok;
      s.onerror = function () { postLoad = null; no(new Error('three-post.js 加载失败')); };
      document.head.appendChild(s);
    });
    return postLoad;
  }
  var FINAL = {
    uniforms: { tDiffuse: { value: null } },
    vertexShader: 'varying vec2 vUv;\nvoid main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
    fragmentShader: 'uniform sampler2D tDiffuse;\nvarying vec2 vUv;\nvoid main() {\n  vec4 c = texture2D(tDiffuse, vUv);\n' +
      '  vec3 p = max(c.rgb, 0.0);\n  float a = clamp(max(c.a, max(p.r, max(p.g, p.b))), 0.0, 1.0);\n  if (a <= 0.0) { gl_FragColor = vec4(0.0); return; }\n' +
      '  vec3 l = min(p / a, 1.0);\n  vec3 s = mix(pow(l, vec3(0.41666)) * 1.055 - vec3(0.055), l * 12.92, vec3(lessThanEqual(l, vec3(0.0031308))));\n  gl_FragColor = vec4(s * a, a);\n}'
  };
  var rtSpecC = null;
  function rtSpec() {
    if (rtSpecC) return rtSpecC;
    var c = renderer.capabilities, gl = renderer.getContext(), half = c.isWebGL2 && renderer.extensions.has('EXT_color_buffer_float'), n = 0;
    if (c.isWebGL2) {
      try {
        var all = [half ? gl.RGBA16F : gl.RGBA8, gl.DEPTH_COMPONENT16, gl.DEPTH_COMPONENT24].map(function (f) { return Array.prototype.slice.call(gl.getInternalformatParameter(gl.RENDERBUFFER, f, gl.SAMPLES) || []); });
        [4, 2].some(function (k) { if (all.every(function (l) { return l.indexOf(k) >= 0; })) { n = k; return true; } return false; });
      } catch (er) { n = 0; }
    }
    return (rtSpecC = { type: half ? T.HalfFloatType : T.UnsignedByteType, samples: n });
  }

  function makePost() {
    dropPost();
    var f = D3Set.fx();
    if (!live || !live.scene || !(f.bloom || f.ao || f.dof)) return;
    if (!T.EffectComposer) {
      var p0 = live;
      loadPost().then(function () { if (live === p0 && ready) { makePost(); invalidate(); } }, function (e) { console.warn('后期效果加载失败，按标准画质画', e); });
      return;
    }
    var sp = rtSpec(), rt = new T.WebGLRenderTarget(1, 1, { type: sp.type, samples: sp.samples });
    var c = new T.EffectComposer(renderer, rt), sc = live.scene, cam = live.camera, bokeh = null;
    c.setPixelRatio(renderer.getPixelRatio());
    c.setSize(W, H);
    if (f.ao) {
      var ao = new T.SSAOPass(sc, cam, W, H);
      ao.kernelRadius = 0.9; ao.minDistance = 0.0002; ao.maxDistance = 0.015;
      c.addPass(ao);
    } else c.addPass(new T.RenderPass(sc, cam));
    if (f.dof && live.focusDist) {
      bokeh = new T.BokehPass(sc, cam, { focus: 10, aperture: 0.0012, maxblur: 0.004 });
      c.addPass(bokeh);
    }
    if (f.bloom) c.addPass(new T.UnrealBloomPass(new T.Vector2(W, H), 0.32, 0.5, 0.86));
    c.addPass(new T.ShaderPass(FINAL));
    post = { c: c, bokeh: bokeh };
  }
  function dropPost() {
    if (!post) return;
    post.c.passes.forEach(function (p) { if (p.dispose) p.dispose(); if (p.noiseTexture) p.noiseTexture.dispose(); });
    post.c.dispose();
    post = null;
  }
  function quality() {
    pixelRatio();
    renderer.setSize(W, H, false);
    renderer.shadowMap.enabled = D3Set.fx().shadow > 0;
    if (live) { var c0 = cur; deactivate(); activate(c0); }
  }
  D3Set.on(function (k) {
    if (k === 'quality' && renderer) quality();
    if (k === 'open' && live && ready && live.focus) live.focus(D3Set.isOpen());
    if (k === 'vinyl') refresh();
  });

  function boot() {
    T.ColorManagement.legacyMode = false;
    try {
      renderer = new T.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    } catch (e) { renderer = null; }
    resize();
    if (!renderer) { document.getElementById('nogl').className = 'nogl on'; return; }
    pixelRatio();
    renderer.setClearColor(0x000000, 0);
    renderer.shadowMap.enabled = D3Set.fx().shadow > 0;
    renderer.shadowMap.type = T.PCFSoftShadowMap;
    renderer.shadowMap.autoUpdate = false;
    renderer.outputEncoding = T.sRGBEncoding;
    renderer.toneMapping = T.ACESFilmicToneMapping;
    canvas = renderer.domElement;
    try { var dbg = renderer.getContext().getExtension('WEBGL_debug_renderer_info'); soft = !!dbg && /SwiftShader|llvmpipe|Software/i.test(renderer.getContext().getParameter(dbg.UNMASKED_RENDERER_WEBGL)); } catch (e) {}
    canvas.className = 'gl';
    canvas.addEventListener('webglcontextlost', function (e) { e.preventDefault(); started = false; stopFrames(); document.getElementById('nogl').className = 'nogl on lost'; });
    canvas.addEventListener('webglcontextrestored', function () { location.reload(); });
    refresh();
    try {
      var s = localStorage.getItem(KEY), n = parseInt(s, 10), id = n >= 0 && pages[n] ? pages[n].id : s;
      shown.forEach(function (p, k) { if (p.id === id) cur = k; });
    } catch (e) {}
    started = true;
    resize();
    dots();
    window.addEventListener('resize', resize);
    setVisible();
    activate(cur);
  }

  window.D3 = {
    add: function (p) { pages.push(p); shown = pages.filter(enabled); },
    lock: function (on) { locked = on; },
    toast: function (msg, ms) {
      var e = document.getElementById('toast');
      clearTimeout(toastT);
      if (!msg) { e.className = 'toast'; return; }
      e.innerHTML = '';
      var p = document.createElement('p');
      p.textContent = msg;
      e.appendChild(p);
      e.className = 'toast on';
      toastT = setTimeout(function () { e.className = 'toast'; }, ms || 3200);
    },
    page: function () { return cur; },
    go: go,
    needs: function (p) { if (p === live && visible) D3Data.use(p.needs || []); },
    focusRect: function () { return live && ready && live.focusRect ? live.focusRect() : null; },
    bench: function (n, warm) {
      if (!live || !ready) return Promise.resolve(null);
      var gl = renderer.getContext(), px = new Uint8Array(4), info = gl.getExtension('WEBGL_debug_renderer_info'), calls = 0, tris = 0, cpu = 0, qs = [];
      var tq = renderer.capabilities.isWebGL2 ? gl.getExtension('EXT_disjoint_timer_query_webgl2') : null;
      var spin = function () { if (live.drag) { if (live.grab) live.grab(W / 2, H / 2); live.drag(4, 0, W / 2, H / 2); } };
      var one = function (q) {
        renderer.info.reset();
        spin(); raf = 0;
        if (q) gl.beginQuery(tq.TIME_ELAPSED_EXT, q);
        var t = performance.now();
        frame(performance.now());
        var d = performance.now() - t;
        if (q) gl.endQuery(tq.TIME_ELAPSED_EXT);
        return d;
      };
      renderer.info.autoReset = false;
      for (var k = 0; k < (warm || 3); k++) { one(null); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); }
      for (var i = 0; i < n; i++) {
        var q = tq ? gl.createQuery() : null;
        cpu += one(q);
        if (q) qs.push(q);
        calls += renderer.info.render.calls; tris += renderer.info.render.triangles;
      }
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
      renderer.info.autoReset = true;
      stopFrames();
      var out = { cpu: Math.round(cpu / n * 100) / 100, gpu: null, calls: Math.round(calls / n), tris: Math.round(tris / n), px: renderer.domElement.width * renderer.domElement.height,
        name: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : '' };
      if (!tq) return Promise.resolve(out);
      return new Promise(function (res) {
        var tries = 0;
        (function poll() {
          var ok = qs.every(function (x) { return gl.getQueryParameter(x, gl.QUERY_RESULT_AVAILABLE); });
          if (!ok && ++tries < 200) { setTimeout(poll, 20); return; }
          var sum = 0, m = 0;
          if (ok && !gl.getParameter(tq.GPU_DISJOINT_EXT)) qs.forEach(function (x) { sum += gl.getQueryParameter(x, gl.QUERY_RESULT) / 1e6; m++; });
          qs.forEach(function (x) { gl.deleteQuery(x); });
          out.gpu = m ? Math.round(sum / m * 100) / 100 : null;
          res(out);
        })();
      });
    },
    info: function () {
      var m = renderer ? renderer.info.memory : {};
      return { live: live ? live.id : '', ready: ready, geometries: m.geometries, textures: m.textures, programs: renderer ? renderer.info.programs.length : 0, az: live && live.view ? Math.round(live.view.az * 1000) / 1000 : null, el: live && live.view ? Math.round(live.view.el * 1000) / 1000 : null,
        spot: live && ready && live.spot ? live.spot() : null,
        post: post ? post.c.passes.map(function (p) { return p.constructor.name.replace('Pass', ''); }).join(',') : '', tone: renderer ? renderer.toneMapping : null, ratio: renderer ? renderer.getPixelRatio() : 0, shadowMap: renderer ? renderer.shadowMap.enabled : false,
        state: live && ready && live.info ? live.info() : null };
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(boot, 0); });
  else setTimeout(boot, 0);
})();
