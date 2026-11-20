(function () {
  'use strict';
  var T = window.THREE;
  var CELL = 24;
  var st = null;

  function start(ctx) {
    stop();
    st = { ctx: ctx, sel: {}, cnt: {}, over: {}, proj: null, mode: 'brush', size: 22, angle: 30, undo: [], stroke: null, adj: {}, mat: null, lastP: null, raf: 0 };
    st.mat = new T.MeshBasicMaterial({ color: 0xffb020, transparent: true, opacity: 0.6, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -3, side: T.DoubleSide });
  }
  function stop() {
    if (!st) return;
    Object.keys(st.over).forEach(function (k) { drop(st.over[k]); });
    st.mat.dispose();
    cancelAnimationFrame(st.raf);
    st = null;
  }
  function list() { return st.ctx.meshes; }

  var mvp = new T.Matrix4(), camKey = '';
  function key() { var c = st.ctx.camera; return c.matrixWorld.elements.join(',') + c.projectionMatrix.elements.join(',') + st.ctx.W() + 'x' + st.ctx.H(); }
  function shown(o) { for (var p = o; p; p = p.parent) if (!p.visible) return false; return true; }
  function depthMap(W, H) {
    var c = st.ctx, r = c.renderer, rt = new T.WebGLRenderTarget(W, H), dm = new T.MeshDepthMaterial({ depthPacking: T.RGBADepthPacking, side: T.DoubleSide }), keep = [];
    var px = new Uint8Array(W * H * 4), old = r.getRenderTarget(), cc = r.getClearColor(new T.Color()), ca = r.getClearAlpha();
    c.root.traverse(function (o) {
      if (!o.isMesh && !o.isSprite && !o.isPoints && !o.isLine) return;
      keep.push([o, o.material, o.visible]);
      if (o.isMesh && o.userData.mid !== undefined && !o.isInstancedMesh) o.material = dm; else o.visible = false;
    });
    try {
      r.setRenderTarget(rt);
      r.setClearColor(0xffffff, 1);
      r.clear();
      r.render(c.root, c.camera);
      r.readRenderTargetPixels(rt, 0, 0, W, H, px);
    } finally {
      keep.forEach(function (k) { k[0].material = k[1]; k[0].visible = k[2]; });
      r.setRenderTarget(old);
      r.setClearColor(cc, ca);
      rt.dispose(); dm.dispose();
    }
    return px;
  }
  var UNPACK = [255 / 256 / 16777216 / 255, 255 / 256 / 65536 / 255, 255 / 256 / 256 / 255, 255 / 256 / 255];
  function viewDist(d, cam) { return cam.near * cam.far / ((cam.far - cam.near) * d - cam.far) * -1; }
  function project() {
    var c = st.ctx, W = Math.max(1, Math.round(c.W())), H = Math.max(1, Math.round(c.H())), cam = c.camera, k = key();
    if (st.proj && camKey === k) return st.proj;
    camKey = k;
    var depth = depthMap(W, H), tol = 0.012 * c.len(), gw = Math.ceil(W / CELL), gh = Math.ceil(H / CELL);
    var meshes = [], cellOf = [], ent = [], e = mvp.elements, v = new T.Vector3();
    list().forEach(function (m, mid) {
      if (!m || !m.isMesh || m.isInstancedMesh || m.isSkinnedMesh || !shown(m)) return;
      var g = m.geometry, pos = g.attributes.position, ix = g.index, nt = (ix ? ix.count : pos.count) / 3, sx = new Float32Array(nt), sy = new Float32Array(nt), dz = new Float32Array(nt);
      m.updateMatrixWorld();
      mvp.multiplyMatrices(cam.projectionMatrix, cam.matrixWorldInverse).multiply(m.matrixWorld);
      var li = meshes.push({ m: m, mid: mid, sx: sx, sy: sy, dz: dz, nt: nt }) - 1;
      for (var t = 0; t < nt; t++) {
        var x = 0, y = 0, z = 0;
        for (var j = 0; j < 3; j++) { var i = ix ? ix.getX(t * 3 + j) : t * 3 + j; x += pos.getX(i); y += pos.getY(i); z += pos.getZ(i); }
        x /= 3; y /= 3; z /= 3;
        var cw = e[3] * x + e[7] * y + e[11] * z + e[15];
        sx[t] = -1;
        if (cw <= 1e-6) continue;
        var nx = (e[0] * x + e[4] * y + e[8] * z + e[12]) / cw, ny = (e[1] * x + e[5] * y + e[9] * z + e[13]) / cw, nz = (e[2] * x + e[6] * y + e[10] * z + e[14]) / cw;
        if (nx < -1 || nx > 1 || ny < -1 || ny > 1 || nz < -1 || nz > 1) continue;
        var px = (nx * 0.5 + 0.5) * W, py = (0.5 - ny * 0.5) * H, ix2 = Math.min(W - 1, px | 0), iy2 = Math.min(H - 1, (H - 1 - py) | 0), o = (iy2 * W + ix2) * 4;
        var bd = depth[o] * UNPACK[0] + depth[o + 1] * UNPACK[1] + depth[o + 2] * UNPACK[2] + depth[o + 3] * UNPACK[3];
        var me = viewDist(nz * 0.5 + 0.5, cam), seen = bd >= 0.9999 ? Infinity : viewDist(bd, cam);
        if (me - seen > tol) continue;
        sx[t] = px; sy[t] = py; dz[t] = me;
        cellOf.push(Math.min(gh - 1, (py / CELL) | 0) * gw + Math.min(gw - 1, (px / CELL) | 0)); ent.push(li, t);
      }
    });
    var start = new Int32Array(gw * gh + 1), n = cellOf.length, items = new Int32Array(n * 2), i2;
    for (i2 = 0; i2 < n; i2++) start[cellOf[i2] + 1]++;
    for (i2 = 0; i2 < gw * gh; i2++) start[i2 + 1] += start[i2];
    var fill = start.slice(0, gw * gh);
    for (i2 = 0; i2 < n; i2++) { var at = fill[cellOf[i2]]++; items[at * 2] = ent[i2 * 2]; items[at * 2 + 1] = ent[i2 * 2 + 1]; }
    st.proj = { meshes: meshes, start: start, items: items, gw: gw, gh: gh };
    return st.proj;
  }
  function around(x, y, r, fn) {
    var p = project(), x0 = Math.max(0, ((x - r) / CELL) | 0), x1 = Math.min(p.gw - 1, ((x + r) / CELL) | 0), y0 = Math.max(0, ((y - r) / CELL) | 0), y1 = Math.min(p.gh - 1, ((y + r) / CELL) | 0), r2 = r * r;
    for (var cy = y0; cy <= y1; cy++) for (var cx = x0; cx <= x1; cx++) {
      var c = cy * p.gw + cx;
      for (var k = p.start[c]; k < p.start[c + 1]; k++) {
        var li = p.items[k * 2], t = p.items[k * 2 + 1], m = p.meshes[li], dx = m.sx[t] - x, dy = m.sy[t] - y;
        if (dx * dx + dy * dy <= r2) fn(m, t);
      }
    }
  }

  function flags(mid, nt) { return st.sel[mid] || (st.sel[mid] = new Uint8Array(nt)); }
  function set(m, t, on) {
    var f = flags(m.mid, m.nt);
    if (!f[t] === !on) return;
    f[t] = on ? 1 : 0;
    st.cnt[m.mid] = (st.cnt[m.mid] || 0) + (on ? 1 : -1);
    if (st.stroke) st.stroke.push(m.mid, t, on ? 1 : 0);
    st.dirty = st.dirty || {};
    st.dirty[m.mid] = m;
  }
  function paintAt(x, y) { var on = st.mode !== 'erase'; around(x, y, st.size, function (m, t) { set(m, t, on); }); }
  function press(x, y) {
    if (!st) return;
    st.stroke = [];
    st.lastP = { x: x, y: y };
    paintAt(x, y);
    redraw();
  }
  function drag(x, y) {
    if (!st || !st.stroke) return;
    var a = st.lastP, d = Math.sqrt((x - a.x) * (x - a.x) + (y - a.y) * (y - a.y)), n = Math.max(1, Math.ceil(d / (st.size * 0.5)));
    for (var i = 1; i <= n; i++) paintAt(a.x + (x - a.x) * i / n, a.y + (y - a.y) * i / n);
    st.lastP = { x: x, y: y };
    redraw();
  }
  function release() {
    if (!st || !st.stroke) return;
    if (st.stroke.length) st.undo.push(st.stroke);
    if (st.undo.length > 40) st.undo.shift();
    st.stroke = null;
    redraw();
  }
  function tap(x, y) {
    if (!st) return false;
    var p = project(), c = st.ctx, best = null, bt = -1, rc = new T.Raycaster();
    rc.setFromCamera({ x: x / c.W() * 2 - 1, y: 1 - y / c.H() * 2 }, c.camera);
    var hit = rc.intersectObjects(p.meshes.map(function (m) { return m.m; }), false)[0];
    if (hit && hit.faceIndex !== undefined) p.meshes.forEach(function (m) { if (m.m === hit.object && hit.faceIndex < m.nt) { best = m; bt = hit.faceIndex; } });
    if (!best) around(x, y, 10, function (m, t) { if (!best || m.dz[t] < best.dz[bt]) { best = m; bt = t; } });
    if (!best) return false;
    var f = flags(best.mid, best.nt), on = !f[bt], region = grow(best, bt, on);
    st.stroke = [];
    region.forEach(function (t) { set(best, t, on); });
    st.undo.push(st.stroke);
    st.stroke = null;
    redraw();
    return true;
  }
  function grow(m, t0, on) {
    var a = adjacency(m.m), cos = Math.cos(st.angle * Math.PI / 180), f = flags(m.mid, m.nt), seen = new Uint8Array(m.nt), q = [t0], out = [];
    seen[t0] = 1;
    while (q.length) {
      var t = q.pop();
      out.push(t);
      for (var k = a.start[t]; k < a.start[t + 1]; k++) {
        var u = a.nb[k];
        if (seen[u] || (!on && !f[u])) continue;
        var d = a.n[t * 3] * a.n[u * 3] + a.n[t * 3 + 1] * a.n[u * 3 + 1] + a.n[t * 3 + 2] * a.n[u * 3 + 2];
        if (Math.abs(d) < cos) continue;
        seen[u] = 1; q.push(u);
      }
    }
    return out;
  }
  function adjacency(mesh) {
    var g = mesh.geometry;
    if (st.adj[g.uuid]) return st.adj[g.uuid];
    var pos = g.attributes.position, ix = g.index, nt = (ix ? ix.count : pos.count) / 3, nv = pos.count, i, t, j;
    var vid = new Int32Array(nv), map = {}, nw = 0, b = new T.Box3().setFromBufferAttribute(pos), q = 1e5 / (b.getSize(new T.Vector3()).length() || 1);
    for (i = 0; i < nv; i++) { var k = Math.round(pos.getX(i) * q) + ',' + Math.round(pos.getY(i) * q) + ',' + Math.round(pos.getZ(i) * q); vid[i] = map[k] === undefined ? (map[k] = nw++) : map[k]; }
    var n = new Float32Array(nt * 3), tv = new Int32Array(nt * 3), A = new T.Vector3(), B = new T.Vector3(), C = new T.Vector3();
    for (t = 0; t < nt; t++) {
      for (j = 0; j < 3; j++) tv[t * 3 + j] = vid[ix ? ix.getX(t * 3 + j) : t * 3 + j];
      A.fromBufferAttribute(pos, ix ? ix.getX(t * 3) : t * 3); B.fromBufferAttribute(pos, ix ? ix.getX(t * 3 + 1) : t * 3 + 1); C.fromBufferAttribute(pos, ix ? ix.getX(t * 3 + 2) : t * 3 + 2);
      B.sub(A); C.sub(A); B.cross(C).normalize();
      n[t * 3] = B.x; n[t * 3 + 1] = B.y; n[t * 3 + 2] = B.z;
    }
    var edge = {};
    for (t = 0; t < nt; t++) for (j = 0; j < 3; j++) {
      var a0 = tv[t * 3 + j], b0 = tv[t * 3 + (j + 1) % 3];
      if (a0 === b0) continue;
      var key2 = a0 < b0 ? a0 + '_' + b0 : b0 + '_' + a0;
      (edge[key2] = edge[key2] || []).push(t);
    }
    var lists = [];
    for (t = 0; t < nt; t++) lists.push([]);
    Object.keys(edge).forEach(function (k) { var l = edge[k]; for (var x = 0; x < l.length; x++) for (var y = 0; y < l.length; y++) if (x !== y) lists[l[x]].push(l[y]); });
    var start = new Int32Array(nt + 1);
    for (t = 0; t < nt; t++) start[t + 1] = start[t] + lists[t].length;
    var nb = new Int32Array(start[nt]);
    for (t = 0; t < nt; t++) nb.set(lists[t], start[t]);
    return (st.adj[g.uuid] = { start: start, nb: nb, n: n });
  }
  function undo() {
    if (!st || !st.undo.length) return;
    var s = st.undo.pop(), byMid = {};
    list().forEach(function (m, mid) { byMid[mid] = m; });
    for (var i = s.length - 3; i >= 0; i -= 3) {
      var mid = s[i], t = s[i + 1], was = s[i + 2] ? 0 : 1, f = st.sel[mid];
      if (!f) continue;
      f[t] = was;
      st.cnt[mid] += was ? 1 : -1;
      st.dirty = st.dirty || {};
      st.dirty[mid] = { m: byMid[mid], mid: mid };
    }
    redraw();
  }
  function cancel() {
    if (!st || !st.stroke) return;
    st.undo.push(st.stroke);
    st.stroke = null;
    undo();
  }
  function clear() {
    if (!st) return;
    Object.keys(st.sel).forEach(function (mid) { st.sel[mid].fill(0); st.cnt[mid] = 0; st.dirty = st.dirty || {}; st.dirty[mid] = { m: list()[mid], mid: +mid }; });
    st.undo = [];
    redraw();
  }

  function redraw() {
    if (!st || st.raf) return;
    st.raf = requestAnimationFrame(function () {
      if (!st) return;
      st.raf = 0;
      var d = st.dirty || {};
      st.dirty = null;
      Object.keys(d).forEach(function (mid) {
        var m = d[mid].m, f = st.sel[mid], n = st.cnt[mid] || 0, o = st.over[mid];
        if (!m) return;
        if (!n) { if (o) { drop(o); delete st.over[mid]; } return; }
        var g = m.geometry, ix = g.index, idx = new Uint32Array(n * 3), w = 0;
        for (var t = 0; t < f.length; t++) if (f[t]) for (var j = 0; j < 3; j++) idx[w++] = ix ? ix.getX(t * 3 + j) : t * 3 + j;
        if (!o) {
          var gg = new T.BufferGeometry();
          gg.setAttribute('position', g.attributes.position);
          o = st.over[mid] = new T.Mesh(gg, st.mat);
          o.raycast = function () {};
          o.renderOrder = 5;
          m.add(o);
        }
        o.geometry.setIndex(new T.BufferAttribute(idx, 1));
      });
      st.ctx.invalidate();
      if (st.ctx.changed) st.ctx.changed(count());
    });
  }
  function drop(o) { if (o.parent) o.parent.remove(o); o.geometry.deleteAttribute('position'); o.geometry.dispose(); }
  function count() { return st ? Object.keys(st.cnt).reduce(function (s, k) { return s + st.cnt[k]; }, 0) : 0; }
  function jobs() {
    if (!st) return [];
    return Object.keys(st.sel).filter(function (mid) { return st.cnt[mid] > 0; }).map(function (mid) {
      var f = st.sel[mid], l = [];
      for (var t = 0; t < f.length; t++) if (f[t]) l.push(t);
      return { mid: +mid, tris: l };
    });
  }

  window.D3Sel = {
    start: start, stop: stop, active: function () { return !!st; },
    mode: function (m) { if (st && m) st.mode = m; return st ? st.mode : ''; },
    size: function (px) { if (st && px) st.size = px; return st ? st.size : 0; },
    angle: function (d) { if (st && d) st.angle = d; return st ? st.angle : 0; },
    press: press, drag: drag, release: release, cancel: cancel, tap: tap, count: count, jobs: jobs, clear: clear, undo: undo,
    canUndo: function () { return !!(st && st.undo.length); },
    painting: function () { return !!(st && (st.mode === 'brush' || st.mode === 'erase')); }
  };
})();
