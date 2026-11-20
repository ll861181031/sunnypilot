(function () {
  'use strict';
  var PAD = 4, DILATE = 3, CELL_MAX = 256, ATLAS_MAX = 1024, DENS = 640, BEAM_PX = 256;

  var GLOW = { intensity: [0.3, 1.5, 1], core: [0, 1, 0.45], detail: [0, 1, 0.6], softness: [0, 1, 0.3] };
  var CORE = { tail: 0.12, brake: 0, fogR: 0.12, turnL: 0.2, turnR: 0.2, ambient: 0.15 };
  var INT = { brake: 1.5 };
  var BEAM_LIM = {
    length: [0.3, 5], width: [0.4, 3.5], intensity: [0.05, 1], spread: [0, 1],
    offset: [-0.3, 0.8], side: [-0.5, 0.5], height: [0, 0.1], lobeSpacing: [0.1, 1.2], lobeWidth: [0, 1], falloff: [0, 1], haze: [0, 1]
  };
  var BEAMS = {
    low: { role: 'head', key: 'beam', dir: 1, name: '近光', enabled: true, length: 2.2, width: 1.75, intensity: 0.6, spread: 0.45 },
    high: { role: 'head', key: 'beamHigh', dir: 1, name: '远光', enabled: true, length: 2.8, width: 1.9, intensity: 0.75, spread: 0.6 },
    fogF: { role: 'fogF', key: 'beam', dir: 1, name: '前雾灯', enabled: true, length: 1.2, width: 1.9, intensity: 0.4, spread: 0.85 },
    tail: { role: 'tail', key: 'beam', dir: -1, name: '尾灯', enabled: false, length: 0.7, width: 1.4, intensity: 0.45, spread: 0.7 },
    reverse: { role: 'reverse', key: 'beam', dir: -1, name: '倒车灯', enabled: false, length: 1, width: 1.3, intensity: 0.5, spread: 0.6 }
  };
  var SHAPES = ['cone', 'pool', 'bar'], LOBES = ['auto', 'single', 'double'];
  function hex(c) { return typeof c === 'string' && /^#[0-9a-f]{6}$/i.test(c); }
  function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
  function inLim(v, lim) { return typeof v === 'number' && isFinite(v) && v >= lim[0] && v <= lim[1]; }
  function smooth(a, b, x) { var t = clamp01((x - a) / Math.max(1e-9, b - a)); return t * t * (3 - 2 * t); }
  function glow(g, r) {
    g = g && typeof g === 'object' ? g : {};
    var o = {};
    for (var k in GLOW) o[k] = inLim(g[k], GLOW[k]) ? g[k] : k === 'core' && CORE[r] !== undefined ? CORE[r] : k === 'intensity' && INT[r] !== undefined ? INT[r] : GLOW[k][2];
    return o;
  }
  function beam(slot, b) {
    var d = BEAMS[slot], o = { dir: d.dir };
    b = b && typeof b === 'object' ? b : {};
    o.enabled = typeof b.enabled === 'boolean' ? b.enabled : d.enabled;
    o.shape = SHAPES.indexOf(b.shape) >= 0 ? b.shape : 'cone';
    o.lobeMode = LOBES.indexOf(b.lobeMode) >= 0 ? b.lobeMode : 'auto';
    var def = { lobeSpacing: 0.6, lobeWidth: 0.4, length: d.length, width: d.width, offset: 0.06, side: 0, height: 0.02, intensity: d.intensity, spread: d.spread, falloff: 0.45, haze: 0.35 };
    for (var k in def) o[k] = inLim(b[k], BEAM_LIM[k]) ? b[k] : def[k];
    o.color = hex(b.color) ? b.color.toLowerCase() : null;
    return o;
  }
  function cleanGlow(g) {
    if (!g || typeof g !== 'object') return null;
    var o = {}, n = 0;
    for (var k in GLOW) if (inLim(g[k], GLOW[k])) { o[k] = g[k]; n++; }
    return n ? o : null;
  }
  function cleanBeam(b) {
    if (!b || typeof b !== 'object' || Array.isArray(b)) return null;
    var o = {}, n = 0;
    if (typeof b.enabled === 'boolean') { o.enabled = b.enabled; n++; }
    if (SHAPES.indexOf(b.shape) >= 0) { o.shape = b.shape; n++; }
    if (LOBES.indexOf(b.lobeMode) >= 0) { o.lobeMode = b.lobeMode; n++; }
    for (var k in BEAM_LIM) if (inLim(b[k], BEAM_LIM[k])) { o[k] = b[k]; n++; }
    if (hex(b.color)) { o.color = b.color.toLowerCase(); n++; }
    return n ? o : null;
  }

  function bake(pieces) {
    var sizes = pieces.map(function (pc) {
      var b = bounds(pc.p), ex = Math.max(b.w, b.h, 1e-6), s = Math.min(DENS, (CELL_MAX - 2 * PAD) / ex);
      return { w: Math.max(16, Math.ceil(b.w * s) + 2 * PAD), h: Math.max(16, Math.ceil(b.h * s) + 2 * PAD), b: b };
    });
    var lay = pack(sizes);
    var data = new Uint8Array(lay.w * lay.h * 4);
    var uvs = pieces.map(function (pc, i) { return paint(data, lay.w, lay.h, lay.cells[i], pc, sizes[i].b); });
    return { w: lay.w, h: lay.h, data: data, uvs: uvs };
  }
  function bounds(p) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (var i = 0; i < p.length; i += 2) {
      if (p[i] < x0) x0 = p[i]; if (p[i] > x1) x1 = p[i];
      if (p[i + 1] < y0) y0 = p[i + 1]; if (p[i + 1] > y1) y1 = p[i + 1];
    }
    if (!isFinite(x0)) x0 = y0 = x1 = y1 = 0;
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }
  function pow2(v) { return Math.pow(2, Math.ceil(Math.log(Math.max(1, v)) / Math.LN2)); }
  function pack(sizes) {
    var scale = 1;
    for (var t = 0; t < 8; t++) {
      var sc = sizes.map(function (s) { return { w: Math.max(16, Math.round(s.w * scale)), h: Math.max(16, Math.round(s.h * scale)) }; });
      var area = 0, big = 1;
      sc.forEach(function (s) { area += s.w * s.h; big = Math.max(big, s.w, s.h); });
      var width = Math.min(ATLAS_MAX, Math.max(pow2(Math.ceil(Math.sqrt(area * 1.15))), pow2(big)));
      for (; width <= ATLAS_MAX; width *= 2) {
        var order = sc.map(function (s, i) { return { w: s.w, h: s.h, i: i }; }).sort(function (a, b) { return b.h - a.h || b.w - a.w; });
        var cells = [], x = 0, y = 0, rowH = 0;
        order.forEach(function (it) {
          if (x + it.w > width) { x = 0; y += rowH; rowH = 0; }
          cells[it.i] = { x: x, y: y, w: it.w, h: it.h };
          x += it.w; rowH = Math.max(rowH, it.h);
        });
        var height = pow2(y + rowH);
        if (height <= ATLAS_MAX) return { w: width, h: height, cells: cells };
      }
      scale *= 0.7;
    }
    throw new Error('灯光贴图装不下');
  }
  function paint(data, W, H, cell, pc, b) {
    var iw = cell.w - 2 * PAD, ih = cell.h - 2 * PAD, s = Math.min(iw / Math.max(b.w, 1e-6), ih / Math.max(b.h, 1e-6));
    if (!isFinite(s) || s <= 0) s = 1;
    var ox = PAD + (iw - b.w * s) / 2, oy = PAD + (ih - b.h * s) / 2, n = pc.p.length / 2, pts = new Float32Array(n * 2), out = new Float32Array(n * 2);
    for (var v = 0; v < n; v++) {
      pts[v * 2] = ox + (pc.p[v * 2] - b.x) * s;
      pts[v * 2 + 1] = oy + (pc.p[v * 2 + 1] - b.y) * s;
      out[v * 2] = (cell.x + pts[v * 2]) / W;
      out[v * 2 + 1] = (cell.y + pts[v * 2 + 1]) / H;
    }
    var cw = cell.w, ch = cell.h, N = cw * ch, mask = new Uint8Array(N), su = pc.uv && pc.img ? new Float32Array(N * 2) : null, idx = pc.i;
    for (var t = 0; t + 2 < idx.length; t += 3) {
      var a = idx[t], c1 = idx[t + 1], c2 = idx[t + 2];
      var ax = pts[a * 2], ay = pts[a * 2 + 1], bx = pts[c1 * 2], by = pts[c1 * 2 + 1], cx = pts[c2 * 2], cy = pts[c2 * 2 + 1];
      var area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
      if (!isFinite(area)) continue;
      var sg = area >= 0 ? 1 : -1, la = Math.hypot(bx - ax, by - ay) || 1, lb = Math.hypot(cx - bx, cy - by) || 1, lc = Math.hypot(ax - cx, ay - cy) || 1;
      var x0 = Math.max(0, Math.floor(Math.min(ax, bx, cx)) - 1), x1 = Math.min(cw - 1, Math.ceil(Math.max(ax, bx, cx)) + 1);
      var y0 = Math.max(0, Math.floor(Math.min(ay, by, cy)) - 1), y1 = Math.min(ch - 1, Math.ceil(Math.max(ay, by, cy)) + 1);
      for (var y = y0; y <= y1; y++) {
        var py = y + 0.5;
        for (var x = x0; x <= x1; x++) {
          var px = x + 0.5;
          var eAB = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) * sg, eBC = ((cx - bx) * (py - by) - (cy - by) * (px - bx)) * sg, eCA = ((ax - cx) * (py - cy) - (ay - cy) * (px - cx)) * sg;
          if (eAB / la < -0.7 || eBC / lb < -0.7 || eCA / lc < -0.7) continue;
          var at = y * cw + x;
          mask[at] = 1;
          if (su && Math.abs(area) > 1e-9) {
            var wA = Math.max(0, eBC) / Math.abs(area), wB = Math.max(0, eCA) / Math.abs(area), wC = Math.max(0, eAB) / Math.abs(area), sum = wA + wB + wC || 1;
            su[at * 2] = (pc.uv[a * 2] * wA + pc.uv[c1 * 2] * wB + pc.uv[c2 * 2] * wC) / sum;
            su[at * 2 + 1] = (pc.uv[a * 2 + 1] * wA + pc.uv[c1 * 2 + 1] * wB + pc.uv[c2 * 2 + 1] * wC) / sum;
          }
        }
      }
    }
    var inside = chamfer(mask, cw, ch, 1), outside = chamfer(mask, cw, ch, 0);
    var lum = new Float32Array(N), samples = [], dMax = 1, i;
    for (i = 0; i < N; i++) {
      if (!mask[i]) continue;
      var rgb = su ? sample(pc.img, su[i * 2], su[i * 2 + 1]) : pc.base;
      lum[i] = rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
      samples.push(lum[i]);
      if (inside[i] > dMax) dMax = inside[i];
    }
    if (!samples.length) return out;
    samples.sort(function (p, q) { return p - q; });
    var lo = samples[Math.floor(samples.length * 0.1)], hi = samples[Math.min(samples.length - 1, Math.floor(samples.length * 0.9))], flat = hi - lo < 0.04;
    var one = Math.min(255, Math.round(255 / dMax));
    for (var yy = 0; yy < ch; yy++) {
      for (var xx = 0; xx < cw; xx++) {
        i = yy * cw + xx;
        var cov = mask[i] === 1;
        if (!cov && outside[i] > DILATE) continue;
        var L = cov ? lum[i] : near(mask, lum, cw, ch, xx, yy);
        var o = ((cell.y + yy) * W + cell.x + xx) * 4;
        data[o] = flat ? 255 : Math.round(clamp01((L - lo) / (hi - lo)) * 255);
        data[o + 1] = cov ? Math.round(clamp01(inside[i] / dMax) * 255) : 0;
        data[o + 2] = one;
        data[o + 3] = 255;
      }
    }
    return out;
  }
  function near(mask, lum, w, h, x, y) {
    for (var r = 1; r <= DILATE + 1; r++) {
      for (var dy = -r; dy <= r; dy++) {
        for (var dx = -r; dx <= r; dx++) {
          var X = x + dx, Y = y + dy;
          if (X < 0 || Y < 0 || X >= w || Y >= h) continue;
          if (mask[Y * w + X]) return lum[Y * w + X];
        }
      }
    }
    return 0.5;
  }
  function chamfer(mask, w, h, target) {
    var d = new Float32Array(w * h), INF = 1e9, x, y, at, v;
    for (var i = 0; i < d.length; i++) d[i] = mask[i] === target ? INF : 0;
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) {
        at = y * w + x; v = d[at];
        if (!v) continue;
        if (x > 0) v = Math.min(v, d[at - 1] + 3);
        if (y > 0) {
          v = Math.min(v, d[at - w] + 3);
          if (x > 0) v = Math.min(v, d[at - w - 1] + 4);
          if (x < w - 1) v = Math.min(v, d[at - w + 1] + 4);
        }
        d[at] = v;
      }
    }
    for (y = h - 1; y >= 0; y--) {
      for (x = w - 1; x >= 0; x--) {
        at = y * w + x; v = d[at];
        if (!v) continue;
        if (x < w - 1) v = Math.min(v, d[at + 1] + 3);
        if (y < h - 1) {
          v = Math.min(v, d[at + w] + 3);
          if (x < w - 1) v = Math.min(v, d[at + w + 1] + 4);
          if (x > 0) v = Math.min(v, d[at + w - 1] + 4);
        }
        d[at] = v;
      }
    }
    for (i = 0; i < d.length; i++) d[i] = d[i] >= INF ? 0 : d[i] / 3;
    return d;
  }
  function sample(img, u, v) {
    var fx = (u - Math.floor(u)) * img.w - 0.5, fy = (v - Math.floor(v)) * img.h - 0.5, x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0;
    var xa = ((x0 % img.w) + img.w) % img.w, xb = (xa + 1) % img.w, ya = ((y0 % img.h) + img.h) % img.h, yb = (ya + 1) % img.h, d = img.d, o = [0, 0, 0];
    for (var c = 0; c < 3; c++) {
      var top = d[(ya * img.w + xa) * 4 + c] * (1 - tx) + d[(ya * img.w + xb) * 4 + c] * tx;
      var bot = d[(yb * img.w + xa) * 4 + c] * (1 - tx) + d[(yb * img.w + xb) * 4 + c] * tx;
      o[c] = (top * (1 - ty) + bot * ty) / 255;
    }
    return o;
  }

  function lobes(xs, c, hw, cw, bm) {
    var uOf = function (x) { return clamp01((c + hw - x) / Math.max(1e-6, 2 * hw)); };
    if (bm.lobeMode === 'single') return [0.5];
    if (bm.lobeMode === 'double') { var half = clamp01(bm.lobeSpacing * cw / 2 / Math.max(1e-6, 2 * hw)); return [clamp01(0.5 - half), clamp01(0.5 + half)]; }
    if (!xs.length) return [uOf(c)];
    var mn = Infinity, mx = -Infinity, i;
    for (i = 0; i < xs.length; i++) { if (xs[i] < mn) mn = xs[i]; if (xs[i] > mx) mx = xs[i]; }
    if (mx - mn > cw * 0.3) {
      var mid = (mn + mx) / 2, ls = 0, ln = 0, rs = 0, rn = 0;
      for (i = 0; i < xs.length; i++) { if (xs[i] > mid) { ls += xs[i]; ln++; } else { rs += xs[i]; rn++; } }
      return [uOf(ls / Math.max(1, ln)), uOf(rs / Math.max(1, rn))];
    }
    var sum = 0;
    for (i = 0; i < xs.length; i++) sum += xs[i];
    return [uOf(sum / xs.length)];
  }
  function beamTex(bm, lb) {
    var N = BEAM_PX, d = new Uint8Array(N * N * 4), sp = clamp01(bm.spread), fall = clamp01(bm.falloff), hazeW = 0.5 * clamp01(bm.haze), lw = clamp01(bm.lobeWidth), shape = bm.shape;
    var sigma0 = 0.04 + 0.14 * lw, growth = shape === 'bar' ? 0.3 * sp : 0.6 + 3.4 * sp, nearFall = shape === 'bar' ? 0.3 + 0.9 * fall : 0.08 + 0.32 * fall, farFall = 0.3 + 0.5 * fall;
    var hazeS0 = 0.1 + 0.15 * sp, poolU = 0.08 + 0.3 * lw, poolT = 0.2 + 0.45 * fall, poolC = 0.22;
    lb = lb && lb.length ? lb : [0.5];
    for (var y = 0; y < N; y++) {
      var t = 1 - y / (N - 1), sigma = sigma0 * (1 + growth * t), hs = hazeS0 * (1 + growth * t);
      var along = Math.min(1, 0.85 * Math.exp(-t / nearFall) + 0.25 * Math.exp(-t / farFall));
      var env = (shape === 'pool' ? 1 : along) * (1 - smooth(0.7, 1, t)) * smooth(0, 0.1, t);
      for (var x = 0; x < N; x++) {
        var u = x / (N - 1), lat = 1 - smooth(0.5, 1, Math.abs(u * 2 - 1)), lobe = 0;
        for (var k = 0; k < lb.length; k++) {
          if (shape === 'pool') { var dt = (t - poolC) / poolT, du = (u - lb[k]) / poolU; lobe += Math.exp(-0.5 * (du * du + dt * dt)); }
          else { var dd = (u - lb[k]) / sigma; lobe += Math.exp(-0.5 * dd * dd); }
        }
        lobe = Math.min(1, lobe);
        var hd = (u - 0.5) / hs, haze = Math.exp(-0.5 * hd * hd) * hazeW * (shape === 'pool' ? along : 1);
        var a = env * lat * Math.min(1, lobe + haze * (1 - lobe)), o = (y * N + x) * 4;
        d[o] = d[o + 1] = d[o + 2] = 255;
        d[o + 3] = Math.round(clamp01(a) * 255);
      }
    }
    return { w: N, h: N, data: d };
  }

  window.D3Lamp = {
    GLOW: GLOW, BEAM_LIM: BEAM_LIM, BEAMS: BEAMS, SHAPES: SHAPES, LOBES: LOBES,
    glow: glow, beam: beam, cleanGlow: cleanGlow, cleanBeam: cleanBeam, bake: bake, lobes: lobes, beamTex: beamTex
  };
})();
