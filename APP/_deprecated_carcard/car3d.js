(function () {
  'use strict';
  var T = window.THREE, D = window.D3Data;
  var el = document.getElementById('pgCar'), appEl = document.getElementById('app');
  var page = { id: 'car', el: el, needs: ['car'] };
  var $ = function (id) { return document.getElementById(id); };
  var app, gen = 0, offCar = null;
  var scene, camera, envRT, keyLight, root, M, parts, wheels, hitBox, ray;
  var std = false, envRoom = null;
  var REFL = 0.7;
  if (T.ShaderChunk.tonemapping_pars_fragment.indexOf('vec3 CustomToneMapping( vec3 color ) { return color; }') >= 0) {
    T.ShaderChunk.tonemapping_pars_fragment = T.ShaderChunk.tonemapping_pars_fragment.replace('vec3 CustomToneMapping( vec3 color ) { return color; }',
      'vec3 CustomToneMapping( vec3 color ) {\n' +
      '  const float StartCompression = 0.8 - 0.04; const float Desaturation = 0.15;\n' +
      '  color *= toneMappingExposure;\n' +
      '  float x = min( color.r, min( color.g, color.b ) );\n' +
      '  float offset = x < 0.08 ? x - 6.25 * x * x : 0.04;\n' +
      '  color -= offset;\n' +
      '  float peak = max( color.r, max( color.g, color.b ) );\n' +
      '  if ( peak < StartCompression ) return color;\n' +
      '  float d = 1. - StartCompression;\n' +
      '  float newPeak = 1. - d * d / ( peak + d - StartCompression );\n' +
      '  color *= newPeak / peak;\n' +
      '  float g = 1. - 1. / ( Desaturation * ( peak - newPeak ) + 1. );\n' +
      '  return mix( color, vec3( newPeak ), g );\n' +
      '}');
  }
  var cur = {}, want = {}, blinking = false, labelsOn = true;
  var HOME0 = { az: -0.72, el: 0.2 }, HOME = { az: HOME0.az, el: HOME0.el }, view = page.view = { az: HOME.az, el: HOME.el, vaz: 0, dist: 11, homing: false };
  var W = 1, H = 1, portrait = false, lastTap = 0;
  var KEY_DARK = 'd3-car-dark', dark = el.classList.contains('dark'), themeK = dark ? 1 : 0, tyreBad = {};
  var tyreSeen = {}, tyreNew = [], tyreOff = false, tyreAt = 0, tour = null, pin = null, focusK = 0, focusAt = null, focusZ = 0, focusL = 0.85, offSys = null, TOUR_HOLD = 3200, TYRE_ZOOM = 0.55, tourTimer = 0;
  var doorSeen = {}, doorOpen = [], doorV = null, doorDue = 0, doorEnd = 0, doorAt = 0, doorBoot = true, doorTimer = 0, fitA = null;
  var INTRO = { az: 1.15, el: 0.16, dist: 0.3, sec: 1.8 }, intro = { t: 1, rot: 1, wait: 0 };
  var lightOn = {}, forceLight = {}, ambientHex = null, LS = null, nightW = false, nightK = 0;
  var ambArea = 'all', musicOn = false, playing = false, colorful = false, hueCol = new T.Color(), offMedia = null, welT = 1, lastPw;
  var KEY = new T.Vector3(-2.2, 8, 3), shadowDirty = false, refl = null, fid = 0;
  var turnU = { value: new T.Vector4(0, 0, 0, 0) }, TURN_FB = ['head', 'led', 'side', 'tail'], QUADS = ['LF', 'RF', 'LB', 'RB'];
  var S = window.D3Set, nightData = null;
  var finishK = 1, finishFrom = null;
  var SHOW = { az: -1.38, el: 0.1 }, showOn = false, showK = 0, angK = 0, offY0 = 0, preview = false, remPx = 50, dbgOn = false;
  var TOP = { az: Math.PI, el: 1.08 }, top = { on: false, k: 0, dist: 11, last: 0, back: 0 }, wheelN = 0;
  var CHASE = { az: Math.PI, el: 0.38 }, drv = { on: false, k: 0, spd: 0, target: 0, dist: 0, last: 0 }, previewDrive = false;
  var track = null, road = null, spinners = [], spinA = 0, edgeMat = null;
  var FOG_DAY = new T.Color('#e2e5ea'), FOG_NIGHT = new T.Color('#14171c');
  var ROAD_LEN = 152, ROAD_X = 0, X_AXIS = new T.Vector3(1, 0, 0);
  var SIDES = ['L', 'R'], DEG = Math.PI / 180;
  var DOORS = { doorFL: 'body.doors.frontLeft.state', doorFR: 'body.doors.frontRight.state', doorRL: 'body.doors.rearLeft.state', doorRR: 'body.doors.rearRight.state' };
  var WINS = { winFL: 'body.windows.frontLeft.position', winFR: 'body.windows.frontRight.position', winRL: 'body.windows.rearLeft.position', winRR: 'body.windows.rearRight.position' };
  var LIDS = { hood: 'body.hood.state', trunk: 'body.tailgate.state' };
  var SLIDES = { sunroof: 'body.sunroof.position', sunshade: 'body.sunshade.position' };
  var FLAPS = { chargePort: 'energy.connectorState' };
  var LIGHTS = ['head', 'led', 'side', 'tail', 'brake', 'reverse', 'turnL', 'turnR', 'fogF', 'fogR', 'ambient'];
  var LAMP_PRI = ['turnL', 'turnR', 'brake', 'reverse', 'fogR', 'fogF', 'head', 'led', 'side', 'tail', 'ambient'];
  var LIGHT_COLOR = { head: '#fff4e2', led: '#ffffff', side: '#ffe6b8', tail: '#ff2018', brake: '#ff0800', reverse: '#ffffff', turnL: '#ffa31a', turnR: '#ffa31a', fogF: '#fff3d0', fogR: '#ff2018', ambient: '#7fd6ff' };
  var AMBIENT = { iceBlue: '#7fd6ff', coldWhite: '#e8f2ff', warmWhite: '#ffd9a0', redOrange: '#ff6a3d', red: '#ff2a2a', redPurple: '#d63cff', darkBlue: '#2a5cff' };
  var rec = null, cfg = null, modelObj = null, dims = { hw: 1.02, h: 1.26, hl: 2.3 }, wheelR = 0.345, envK = 1, expK = 1;
  var later = null;
  var compact = false, lookTo = null, editing = false, tuneK = 0, zoom = 1, pan = new T.Vector3(), aim = new T.Vector3(), force = {}, marks = [], hiMat = null, flashes = [], flashMat = null, flashT = 0;
  var DOOR_NAME = { 'body.doors.frontLeft.state': '左前门', 'body.doors.frontRight.state': '右前门', 'body.doors.rearLeft.state': '左后门', 'body.doors.rearRight.state': '右后门' };
  var TYRES = ['frontLeft', 'frontRight', 'rearLeft', 'rearRight'], TYRE_NAME = { frontLeft: '左前', frontRight: '右前', rearLeft: '左后', rearRight: '右后' };
  var POWER = { off: '熄火', acc: 'ACC', on: '已上电', ready: 'READY', fakeReady: 'READY' };
  var V = function (x, y, z) { return new T.Vector3(x, y, z); };

  page.load = function (a) {
    app = a;
    var g = ++gen;
    $('cLoad').textContent = '正在载入 3D 车模型';
    $('cLoad').className = 'loading on';
    scene = new T.Scene();
    scene.fog = new T.Fog(0xe2e5ea, 900, 1000);
    camera = new T.PerspectiveCamera(27, 1, 0.1, 80);
    camera.layers.enable(1);
    envRT = app.env('studio');
    scene.environment = envRT.texture;
    keyLight = new T.DirectionalLight(0xffffff, 1.3);
    keyLight.position.copy(KEY).normalize().multiplyScalar(12);
    var fx = S.fx();
    if (fx.shadow) {
      keyLight.castShadow = true;
      keyLight.shadow.mapSize.set(fx.shadow, fx.shadow);
      keyLight.shadow.bias = -0.0004; keyLight.shadow.normalBias = 0.025;
    }
    scene.add(keyLight);
    page.scene = scene; page.camera = camera;
    ray = new T.Raycaster();
    hitBox = new T.Box3(V(-1.1, 0, -2.4), V(1.1, 1.3, 2.4));
    cur = {}; want = {}; view.az = HOME.az; view.el = HOME.el; view.vaz = 0; view.homing = false;
    doorSeen = {}; doorOpen = []; doorV = null; doorDue = doorEnd = 0; doorBoot = true; pin = null; tyreOff = false;
    asides = {};
    if (window.D3DebugPanel && D3DebugPanel.isOpen()) asides.dbg = 1;
    if (window.D3CarInfo && D3CarInfo.isOpen()) asides.info = 1;
    dbgOn = Object.keys(asides).length > 0;
    stOpen = false;
    offCar = D.onCar(apply);
    offMedia = D.onMedia(onMedia);
    offSys = D.onSys(sysIcons); sysIcons(D.sys());
    bindMedia(); npShow(); npPaint(D.media());
    dtPaint(); setInterval(dtPaint, 1000);
    D.limWatch(S.get('showRoad') || S.get('showCam'));
    D.netOn(S.get('showNet'));
    lastPw = undefined; welT = 1;
    apply(D.car());
    return (fx.reflect ? app.loadPost().then(null, function () {}) : Promise.resolve()).then(function () { return D3Model.current(); }).then(function (r) {
      if (g !== gen) return;
      rec = r;
      return parse(g, null).then(null, function (e) {
        if (rec.builtin || g !== gen) throw e;
        console.error('导入的车模加载失败，换回默认车模', e);
        rec = D3Model.def(); rec.broken = true;
        return parse(g, null);
      });
    });
  };
  function parse(g, c, old) {
    return D3Model.inflate(rec.buf).then(function (b) { if (g === gen) rec.buf = b; return parseNow(g, c, old); });
  }
  function parseNow(g, c, old) {
    return new Promise(function (res, rej) {
      setTimeout(function () {
        if (g !== gen) return res();
        new T.GLTFLoader().parse(rec.buf, '', function (gltf) {
          if (g !== gen) { app.dispose(gltf.scene); return res(); }
          var model = gltf.scene;
          D3Model.ids(model, gltf.parser);
          cfg = c || rec.cfg || (D3Model.isW70(model) ? D3Model.W70 : D3Model.auto(model, app.renderer()));
          if (cfg.carve) {
            var jobs = cfg.carve, cut = null;
            delete cfg.carve;
            try { cut = D3Model.carve(rec.buf, model.userData.d3, jobs); } catch (er) { console.error('切车窗失败，照原样建', er); }
            if (cut) {
              cut.mids.forEach(function (mid, i) {
                if (mid < 0) return;
                var to = jobs[i].to;
                if (LIGHTS.indexOf(to) >= 0 || D3Model.BODY_ROLES.indexOf(to) >= 0) { cfg.pick = cfg.pick || {}; (cfg.pick[to] = cfg.pick[to] || []).push(mid); return; }
                var p = cfg.parts[to] = cfg.parts[to] || { meshes: [] };
                if (p.meshes.indexOf(mid) < 0) p.meshes.push(mid);
              });
              rec.buf = cut.buf;
              if (!rec.builtin) D3Model.save({ buf: rec.buf, cfg: rec.cfg || null, name: rec.name, credit: rec.credit });
              app.dispose(model);
              return parse(g, cfg).then(res, rej);
            }
          }
          var drop = function () { if (old) { freeRoot(old); old = null; marks = []; flashes = []; } };
          try { build(model, cfg); } catch (e) { drop(); if (modelObj === model) dropModel(); else app.dispose(model); return rej(e); }
          var R1 = root, wr = cfg.wraps && cfg.wraps.length;
          if (!wr) return after();
          R1.visible = false;
          page.wraps().then(after, function (e) { console.error('贴纸没贴上', e); after(); });
          function after() {
            if (!scene) return res();
            if (g !== gen) { drop(); if (R1.parent) R1.visible = true; return res(); }
            R1.visible = true;
            finishParse(g, c, drop);
            res();
          }
        }, function (e) { $('cLoad').textContent = '3D 车模型加载失败'; rej(e); });
      }, 30);
    });
  }
  function finishParse(g, c, drop) {
    drop();
    if (S.fx().lowp && std) lowp(root);
    $('cLoad').className = 'loading';
    themeK = dark ? 1 : 0;
    look();
    if (!c) { intro.t = S.get('intro') && !editing ? 0 : 1; intro.rot = 1; intro.wait = 2; }
    warmLamps(g);
    apply(D.car());
    modelUI();
    if (W > 1) page.resize(W, H, portrait);
  }
  page.reload = function () {
    if (!scene) return Promise.resolve();
    drop();
    var g = ++gen;
    dropModel();
    cur = {};
    $('cLoad').textContent = '正在载入 3D 车模型';
    $('cLoad').className = 'loading on';
    return D3Model.current().then(function (r) {
      if (g !== gen) return;
      rec = r;
      return parse(g, null);
    });
  };
  page.rebuild = function (c, wait) {
    if (!scene || !rec) return Promise.resolve();
    if (wait) return settle(function () { return parse(++gen, c, root); });
    drop();
    var g = ++gen;
    return parse(g, c, root);
  };
  function settle(run) {
    drop();
    gen++;
    return new Promise(function (res) { later = { run: run, res: res }; if (root) app.invalidate(); });
  }
  function drop() { if (later) { later.res(); later = null; } }
  page.settled = function () {
    if (!later) return Promise.resolve();
    var L = later;
    later = null;
    return L.run().then(L.res, L.res);
  };
  function freeRoot(r) {
    var u = r.userData;
    if (u.wraps) { u.wraps.dispose(); u.wraps = null; }
    scene.remove(r);
    if (u.lamps) u.lamps.dispose();
    if (u.refl) u.refl.dispose();
    if (u.origMap) u.origMap.dispose();
    app.dispose(r);
  }
  function dropModel() {
    if (root) freeRoot(root);
    wmark = null;
    if (plates) { plates.front && plateFree(plates.front); plates.rear && plateFree(plates.rear); }
    root = M = parts = wheels = modelObj = LS = plates = refl = null;
    track = road = null; spinners = [];
    wheelN = 0; marks = []; flashes = [];
  }
  page.unload = function () {
    gen++;
    if (offCar) offCar(); offCar = null;
    if (offMedia) offMedia(); offMedia = null;
    if (offSys) offSys(); offSys = null;
    tour = null; pin = null; clearTimeout(tourTimer); clearTimeout(warmT);
    doorV = null; doorDue = doorEnd = 0; clearTimeout(doorTimer);
    musicOn = playing = false; page.needs = ['car'];
    if (scene) scene.children.forEach(function (o) { if (o.userData.wraps) { o.userData.wraps.dispose(); o.userData.wraps = null; } });
    wmark = null; wkey = null; wdrag = null; wline = null; wdraw = false;
    if (scene) app.dispose(scene);
    if (LS) LS.dispose();
    if (refl) { refl.dispose(); refl = null; }
    if (M && M.orig && M.orig.map) M.orig.map.dispose();
    if (envRT) envRT.dispose();
    if (envRoom) envRoom.dispose();
    if (keyLight) keyLight.shadow.dispose();
    envRoom = null; std = false;
    app.renderer().toneMapping = T.ACESFilmicToneMapping;
    scene = camera = envRT = root = M = parts = wheels = modelObj = LS = page.scene = page.camera = null;
    nightW = false; nightK = 0; el.classList.remove('night');
    wheelN = 0; marks = []; flashes = []; force = {}; rec = null;
    track = road = null; spinners = [];
    drop();
    clearTimeout(top.back); top.on = false; top.k = 0; showK = angK = 0; finishK = 1; preview = false;
    drv.on = false; drv.k = 0; drv.spd = 0; drv.dist = 0; previewDrive = false; el.classList.remove('driving');
    $('cLoad').className = 'loading';
    TYRES.forEach(function (w) { $('tp-' + w).className = 'tp off'; });
  };

  function visBox(model) {
    var b = new T.Box3();
    model.traverse(function (o) { if (o.isMesh && o.visible) b.union(D3Model.box(o)); });
    return b;
  }
  function build(model, c) {
    root = new T.Group();
    scene.add(root);
    modelObj = model;
    var d = model.userData.d3, pc = c.parts || {};
    var mrefs = function (r) { return D3Model.meshRefs(d, r); }, mats = function (k) { return D3Model.matRefs(d, c.mats && c.mats[k]); };
    var hidden = mrefs(c.hide);
    hidden.forEach(function (m) { m.visible = false; });
    model.rotation.set((c.rotX || 0) * DEG, (c.rotY || 0) * DEG, (c.rotZ || 0) * DEG, 'YXZ');
    root.add(model);
    model.updateMatrixWorld(true);
    var bb = visBox(model);
    if (bb.isEmpty()) { hidden.forEach(function (m) { m.visible = true; }); bb = visBox(model); }
    if (bb.isEmpty()) throw new Error('车模里没有能显示的网格');
    model.scale.setScalar(c.scale || (c.length ? c.length / Math.max(1e-6, bb.max.z - bb.min.z) : 1));
    model.updateMatrixWorld(true);
    bb = visBox(model);
    model.position.set(-(bb.min.x + bb.max.x) / 2, -bb.min.y, -(bb.min.z + bb.max.z) / 2);
    var off = model.position.clone();
    root.updateMatrixWorld(true);
    bb = visBox(model);
    dims = c.box ? { hw: c.box[0], h: c.box[1], hl: c.box[2] } : { hw: Math.max(-bb.min.x, bb.max.x), h: bb.max.y, hl: Math.max(-bb.min.z, bb.max.z) };
    hitBox.set(V(-dims.hw - 0.08, 0, -dims.hl - 0.1), V(dims.hw + 0.08, dims.h + 0.04, dims.hl + 0.1));
    var P = c.coords === 'model' ? function (a) { return V(a[0], a[1], a[2]).add(off); } : function (a) { return V(a[0], a[1], a[2]); };
    envK = (c.env || 1) * REFL; expK = c.exposure || 1;
    std = !D3Model.isW70(model);
    if (std && !envRoom) envRoom = app.env('neutral');
    scene.environment = std ? envRoom.texture : envRT.texture;
    app.renderer().toneMapping = std ? T.CustomToneMapping : T.ACESFilmicToneMapping;

    M = {
      paint: new T.MeshPhysicalMaterial({ color: 0x9a0c13, metalness: 0.3, roughness: 0.34, clearcoat: 1, clearcoatRoughness: 0.03, aoMapIntensity: c.paintAo !== undefined ? c.paintAo : 0.6,
        iridescenceIOR: 1.8, iridescenceThicknessRange: [250, 900] }),
      glass: new T.MeshStandardMaterial({ color: 0x0a0e13, metalness: 0, roughness: 0.03, transparent: true, opacity: 0.78, envMapIntensity: 2 * REFL }),
      head: new T.MeshStandardMaterial({ color: 0xffffff, metalness: 0, roughness: 0.02, transparent: true, opacity: 0.22 }),
      tail: new T.MeshStandardMaterial({ color: 0x4a0606, metalness: 0, roughness: 0.08, transparent: true, opacity: 0.7 }),
      rims: mats('rim'), cals: mats('caliper'), lens: !!c.lens, fx: {}
    };
    paintFx(M.paint);
    var paintSet = mats('paint'), glassSet = c.keepGlass ? [] : mats('glass'), lampSets = {}, picked = {}, copies = {}, ents = {};
    M.glassSrc = mats('glass');
    LIGHTS.forEach(function (r) { lampSets[r] = mats(r); ents[r] = []; });
    lightFx();
    var pickedL = {};
    D3Model.BODY_ROLES.forEach(function (r) { mrefs(c.pick && c.pick[r]).forEach(function (m) { if (!picked[m.uuid]) picked[m.uuid] = r; }); });
    LIGHTS.forEach(function (r) { mrefs(c.pick && c.pick[r]).forEach(function (m) { if (!picked[m.uuid]) (pickedL[m.uuid] = pickedL[m.uuid] || []).push(r); }); });
    var pickedAs = function (r) { var l = []; model.traverse(function (o) { if (o.isMesh && picked[o.uuid] === r) l = l.concat(D3Model.mats(o)); }); return l; };
    M.glassSrc = M.glassSrc.concat(pickedAs('glass'));
    M.has = { paint: paintSet.length > 0 || pickedAs('paint').length > 0 };
    var srcs = paintSet.concat(pickedAs('paint')), src = srcs[0];
    D3Model.metalFix(d, M.rims.concat(pickedAs('rim')));
    var paintMeshes = [];
    model.traverse(function (o) { if (o.isMesh && (picked[o.uuid] === 'paint' || (!picked[o.uuid] && D3Model.mats(o).some(function (m) { return paintSet.indexOf(m) >= 0; })))) paintMeshes.push(o); });
    var uvAll = paintMeshes.every(function (o) { return !!o.geometry.attributes.uv; });
    var shared = function (k) { return !!src[k] && uvAll && srcs.every(function (m) { return m[k] === src[k]; }); };
    if (paintSet.length) {
      (c.paintKeep || ['aoMap', 'normalMap'].concat(c.keepPaintMap ? ['map'] : [])).forEach(function (k) { if (shared(k)) M.paint[k] = src[k]; });
      if (M.paint.normalMap && src.normalScale) M.paint.normalScale.copy(src.normalScale);
      if (M.paint.aoMap) paintMeshes.forEach(function (o) { var g = o.geometry; if (!g.attributes.uv2) g.setAttribute('uv2', g.attributes.uv); });
    }
    var oc = src && src.color ? src.color.clone() : null, omap = src && shared('map') ? src.map : null, oavg = src && src.map && !omap && texAvg(src.map);
    if (oavg) oc.multiply(oavg);
    M.orig = oc ? { color: oc, map: omap, metal: src.metalness !== undefined ? src.metalness : 0.3, rough: src.roughness !== undefined ? src.roughness : 0.4, coat: src.clearcoat || 0 } : null;
    M.keepMap = !!(c.keepPaintMap || (c.paintKeep && c.paintKeep.indexOf('map') >= 0));
    if (M.orig) {
      var shown = M.orig.color.clone(), avg = M.orig.map && texAvg(M.orig.map);
      if (avg) shown.multiply(avg);
      S.modelPaint({ color: '#' + M.orig.color.getHexString(), css: '#' + shown.getHexString(), metal: M.orig.metal, rough: M.orig.rough, coat: M.orig.coat });
    } else S.modelPaint(null);
    var two = function (set) { return set.some(function (m) { return m.side === T.DoubleSide || m.flatShading; }); };
    if (two(paintSet.concat(pickedAs('paint')))) M.paint.side = T.DoubleSide;
    if (two(glassSet.concat(pickedAs('glass')))) M.glass.side = T.DoubleSide;
    var zf = new T.Box3(), zr = new T.Box3(), zSplit = null;
    ['doorFL', 'doorFR'].forEach(function (k) { mrefs(pc[k] && pc[k].meshes).forEach(function (m) { zf.union(D3Model.box(m)); }); });
    ['doorRL', 'doorRR'].forEach(function (k) { mrefs(pc[k] && pc[k].meshes).forEach(function (m) { zr.union(D3Model.box(m)); }); });
    if (!zf.isEmpty() && !zr.isEmpty()) zSplit = (zf.min.z + zr.max.z) / 2;
    function zoneOf(o) {
      if (zSplit === null || !o) return '';
      var b = D3Model.box(o);
      return b.min.z > zSplit - 0.05 ? 'F' : b.max.z < zSplit + 0.05 ? 'R' : '';
    }
    function lampOf(o, r, gi) { if (o.visible && !o.isSkinnedMesh) ents[r].push({ o: o, i: gi, z: r === 'ambient' ? zoneOf(o) : '' }); }
    function asRole(r, m, o, gi) {
      if (r === 'paint') return M.paint;
      if (r === 'glass') return c.keepGlass || cover(o) ? m : M.glass;
      if (r === 'rim' || r === 'caliper') {
        var key = r + m.uuid;
        if (!copies[key]) { copies[key] = m.clone(); (r === 'rim' ? M.rims : M.cals).push(copies[key]); }
        return copies[key];
      }
      lampOf(o, r, gi);
      return m;
    }
    function cover(o) { if (!o) return false; var b = D3Model.box(o); return Math.abs(b.min.z + b.max.z) / 2 > 0.6 * dims.hl && b.max.y < 0.75 * dims.h; }
    function byMat(m, o, gi, have) {
      if (paintSet.indexOf(m) >= 0) return M.paint;
      if (glassSet.indexOf(m) >= 0) return cover(o) ? m : M.glass;
      var out = m;
      LIGHTS.forEach(function (r) {
        if (lampSets[r].indexOf(m) < 0) return;
        if (!have || have.indexOf(r) < 0) lampOf(o, r, gi);
        if (out === m && M.lens && r === 'head') out = M.head;
        else if (out === m && M.lens && r === 'tail') out = M.tail;
      });
      return out;
    }
    var swap = [], normalsDone = [];
    function fix(m) {
      if (m.userData.fixed) return;
      m.userData.fixed = 1;
      if (envK !== 1 && m.envMapIntensity !== undefined) m.envMapIntensity *= envK;
      if (m.userData.gltfDefault && m.metalness !== undefined) { m.metalness = 0.05; m.roughness = 0.6; }
      if (m.transparent && m.opacity >= 0.99 && !(m.transmission > 0) && !m.alphaMap && !m.vertexColors && (!m.map || /jpe?g/.test(m.map.userData && m.map.userData.mimeType || ''))) { m.transparent = false; m.depthWrite = true; }
      if (m.transmission > 0) { m.transmission = 0; m.transparent = true; m.opacity = Math.min(m.opacity, 0.45); m.depthWrite = false; }
    }
    model.traverse(function (o) {
      if (!o.isMesh) return;
      o.userData.car = 1;
      var g = o.geometry;
      if (g && g.attributes.position && !g.attributes.normal && normalsDone.indexOf(g) < 0) { g.computeVertexNormals(); normalsDone.push(g); }
      if (g && (Array.isArray(o.material) ? o.material : [o.material]).some(function (m) { return m.side === T.DoubleSide; })) D3Model.wind(g);
      var arr = Array.isArray(o.material), pr = picked[o.uuid], list = (arr ? o.material : [o.material]).map(function (m, mi) {
        if (m.flatShading) m.side = T.DoubleSide;
        var pl = pr ? null : pickedL[o.uuid];
        if (pl) pl.forEach(function (lr) { lampOf(o, lr, arr ? mi : -1); });
        var r = pr ? asRole(pr, m, o, arr ? mi : -1) : byMat(m, o, arr ? mi : -1, pl);
        if (r === m) fix(m);
        if (r !== m && swap.indexOf(m) < 0) swap.push(m);
        return r;
      });
      o.material = arr ? list : list[0];
    });
    var inUse = {};
    model.traverse(function (o) { if (o.isMesh) D3Model.mats(o).forEach(function (m) { inUse[m.uuid] = 1; }); });
    swap.forEach(function (m) { if (!inUse[m.uuid]) m.dispose(); });
    M.rim0 = M.rims.map(function (m) { return { c: m.color.clone(), m: m.metalness, r: m.roughness }; });
    M.cal0 = M.cals.map(function (m) { return m.color.clone(); });
    var shownOf = function (m) { var c = m.color.clone(), avg = m.map && texAvg(m.map); if (avg) c.multiply(avg); return '#' + c.getHexString(); };
    S.modelPart('rim', M.rims.length ? shownOf(M.rims[0]) : null);
    S.modelPart('caliper', M.cals.length ? shownOf(M.cals[0]) : null);
    finish(false);

    parts = {};
    var wins = {};
    if (c.winSplit) { var wg = mrefs([c.winSplit])[0]; if (wg) { var sp = splitX(wg); wins.winFL = [sp.L]; wins.winFR = [sp.R]; } }
    Object.keys(WINS).forEach(function (k) { var l = mrefs(pc[k] && pc[k].meshes).filter(seen); if (l.length) wins[k] = (wins[k] || []).concat(l); });
    var inWin = [].concat.apply([], Object.keys(wins).map(function (k) { return wins[k]; }));
    Object.keys(DOORS).concat(Object.keys(LIDS), Object.keys(FLAPS)).forEach(function (k) {
      var p = pc[k], ms = p ? mrefs(p.meshes).filter(function (m) { return seen(m) && inWin.indexOf(m) < 0; }) : [];
      if (ms.length) parts[k] = door(k, ms, p, P);
    });
    Object.keys(SLIDES).forEach(function (k) {
      var p = pc[k], ms = p ? mrefs(p.meshes).filter(seen) : [];
      if (ms.length) parts[k] = slider(k, ms, p);
    });
    Object.keys(wins).forEach(function (k) {
      var dp = parts['door' + k.slice(3)], list = wins[k], b = new T.Box3();
      list.forEach(function (m) { b.expandByObject(m); });
      var host = dp ? dp.pivot : new T.Group();
      if (!dp) root.add(host);
      list.forEach(function (m) { host.attach(m); m.userData.d3move = 1; });
      parts[k] = { meshes: list, y0: list.map(function (m) { return m.position.y; }), drop: c.winDrop || Math.min(0.3, (b.max.y - b.min.y) * 0.85) };
    });

    var groups = {}, wp = {}, out = {}, rs = [], Q = { LF: 'frontLeft', RF: 'frontRight', LB: 'rearLeft', RB: 'rearRight' };
    M.notWrap = {};
    mrefs(pc.wheels).forEach(function (m) { M.notWrap[m.uuid] = 1; });
    LIGHTS.forEach(function (r) { ents[r].forEach(function (e) { M.notWrap[e.o.uuid] = 1; }); });
    mrefs(pc.wheels).filter(seen).forEach(function (m) {
      if (D3Model.mats(m).some(function (x) { return M.cals.indexOf(x) >= 0; })) return;
      var wb = new T.Box3().expandByObject(m), cc = wb.getCenter(V(0, 0, 0)), q = (cc.x > 0 ? 'L' : 'R') + (cc.z > 0 ? 'F' : 'B');
      if ((wb.min.x < -0.1 * dims.hw && wb.max.x > 0.1 * dims.hw) || (wb.min.z < -0.1 * dims.hl && wb.max.z > 0.1 * dims.hl)) return;
      (groups[q] = groups[q] || []).push(m);
    });
    Object.keys(groups).forEach(function (q) {
      var b = new T.Box3();
      groups[q].forEach(function (m) { b.expandByObject(m); });
      var cc = b.getCenter(V(0, 0, 0));
      wheelN++;
      wp[Q[q]] = V(cc.x, 0, cc.z); out[Q[q]] = (b.max.x - b.min.x) / 2; rs.push((b.max.y - b.min.y) / 2);
      spinners.push(hinge(groups[q], cc, X_AXIS, 1).pivot);
    });
    wheelR = c.wheelR || (rs.length ? rs.reduce(function (a, x) { return a + x; }, 0) / rs.length : dims.h * 0.27);
    var WP = {};
    TYRES.forEach(function (w) {
      var f = w.indexOf('front') === 0 ? 1 : -1, l = /Left/.test(w) ? 1 : -1;
      WP[w] = c.wheelPos && c.wheelPos[w] ? P(c.wheelPos[w]) : wp[w] || V(l * dims.hw * 0.8, 0, f * dims.hl * 0.55);
    });

    wheels = {};
    var ringTex = app.canvasTex(128, 128, function (cx, w) {
      var gr = cx.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
      gr.addColorStop(0.5, 'rgba(255,255,255,0)'); gr.addColorStop(0.72, 'rgba(255,255,255,.9)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
      cx.fillStyle = gr; cx.fillRect(0, 0, w, w);
    });
    var ringS = 1.3 * wheelR / 0.345;
    TYRES.forEach(function (w) {
      var ring = new T.Mesh(new T.PlaneGeometry(ringS, ringS), new T.MeshBasicMaterial({ map: ringTex, color: 0xff8a1f, transparent: true, depthWrite: false, opacity: 0 }));
      ring.rotation.x = -Math.PI / 2; ring.position.copy(WP[w]).setY(0.004); ring.renderOrder = 1; ring.visible = false;
      root.add(ring);
      var side = WP[w].x > 0 ? 1 : -1, lo = c.labelOut || (out[w] ? out[w] + 0.3 : 0.42);
      wheels[w] = { ring: ring, side: side, c: V(WP[w].x, wheelR, WP[w].z), at: WP[w].clone().add(V(side * lo, 0, 0)), label: $('tp-' + w) };
    });

    root.add(shadow(WP));
    var fx = S.fx();
    model.traverse(function (o) {
      if (!o.isMesh) return;
      o.castShadow = !!fx.shadow && !D3Model.mats(o).every(function (m) { return m.transparent && m.opacity < 0.9; });
      o.receiveShadow = !!fx.shadow;
    });
    if (fx.shadow) {
      var L = Math.max(dims.hl, dims.hw) + 0.8, sc = keyLight.shadow.camera, SR = L * 1.6;
      sc.left = sc.bottom = -SR; sc.right = sc.top = SR; sc.near = 2; sc.far = 24; sc.updateProjectionMatrix();
      var catcher = new T.Mesh(new T.PlaneGeometry(L * 2, L * 2), new T.ShadowMaterial({ opacity: 0.32, depthWrite: false }));
      catcher.rotation.x = -Math.PI / 2; catcher.position.y = 0.001; catcher.receiveShadow = true; catcher.renderOrder = -2;
      root.add(catcher);
      shadowDirty = true;
    }
    if (fx.reflect && T.Reflector) root.add(root.userData.refl = mirror());
    root.userData.origMap = M.orig && M.orig.map;
    LS = root.userData.lamps = makeLamps(ents);
    hmark = null; showHinge();
    hiMat = new T.MeshBasicMaterial({ color: 0x3fb4ff, transparent: true, opacity: 0.45, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
    flashMat = new T.MeshBasicMaterial({ color: 0xffd23f, transparent: true, opacity: 0, depthTest: false, depthWrite: false, side: T.DoubleSide, toneMapped: false });
    plates = null;
    if (cfg.plates) page.plates(cfg.plates);
    buildTrack();
  }
  function buildTrack() {
    track = new T.Group();
    track.visible = false;
    root.add(track);
    var fade = function (g, far, near) {
      var pos = g.attributes.position, c = [];
      for (var i = 0; i < pos.count; i++) {
        var z = Math.abs(pos.getZ(i)), a = z <= near ? 1 : z >= far ? 0 : 1 - (z - near) / (far - near);
        c.push(1, 1, 1, a * a * (3 - 2 * a));
      }
      g.setAttribute('color', new T.Float32BufferAttribute(c, 4));
      return g;
    };
    var rg = new T.PlaneGeometry(10.4, ROAD_LEN, 1, 30);
    rg.rotateX(-Math.PI / 2);
    var tex = roadTex();
    tex.wrapT = T.RepeatWrapping; tex.repeat.set(1, ROAD_LEN / 12); tex.anisotropy = 4;
    road = new T.Mesh(fade(rg, ROAD_LEN / 2, 22), new T.MeshStandardMaterial({ map: tex, vertexColors: true, transparent: true, depthWrite: false, roughness: 0.82, metalness: 0, envMapIntensity: 0.6 }));
    road.position.set(ROAD_X, 0.001, 0); road.renderOrder = -2;
    track.add(road);
    edgeMat = new T.MeshBasicMaterial({ color: 0x8fdcff, vertexColors: true, transparent: true, depthWrite: false });
    [-3.95, 3.95].forEach(function (x) {
      var g = new T.PlaneGeometry(0.07, ROAD_LEN, 1, 30);
      g.rotateX(-Math.PI / 2);
      var e = new T.Mesh(fade(g, ROAD_LEN / 2, 22), edgeMat);
      e.position.set(ROAD_X + x, 0.003, 0); e.renderOrder = -1;
      track.add(e);
    });
  }
  function roadTex() {
    return app.canvasTex(256, 512, function (c, w, h) {
      var X = function (m) { return (m + 5.2) / 10.4 * w; };
      var g = c.createLinearGradient(0, 0, w, 0);
      g.addColorStop(0, 'rgba(58,62,69,0)'); g.addColorStop(X(-4.3) / w, 'rgba(58,62,69,1)'); g.addColorStop(X(4.3) / w, 'rgba(58,62,69,1)'); g.addColorStop(1, 'rgba(58,62,69,0)');
      c.fillStyle = g; c.fillRect(0, 0, w, h);
      for (var i = 0; i < 2600; i++) {
        var v = 40 + ((i * 7919) % 40), x = (i * 2654435761 % 9973) / 9973 * w, y = (i * 40503 % 9967) / 9967 * h;
        c.fillStyle = 'rgba(' + v + ',' + v + ',' + (v + 4) + ',.5)'; c.fillRect(x, y, 1.5, 1.5);
      }
      c.fillStyle = 'rgba(240,242,245,.92)';
      [-3.6, 3.6].forEach(function (m) {
        for (var dy = 0; dy < h; dy += h * 8 / 12) {
          c.fillRect(X(m) - 2, dy, 4, h * 4 / 12);
        }
      });
    });
  }
  function drive(on) {
    if (on === drv.on) return;
    drv.on = on;
    el.classList.toggle('driving', on);
    drv.last = 0;
    view.homing = true; view.vaz = 0;
    if (app && root) app.invalidate();
  }
  function driving() { return drv.on || previewDrive; }
  function chaseDist(fov) {
    var tv = Math.tan(fov * Math.PI / 360), th = tv * (W / H);
    return Math.max(2 * dims.hw / (2 * 0.3 * th), dims.h / (2 * 0.25 * tv)) + dims.hl + S.get('camAhead');
  }
  function driveStep(dt) {
    var moving = false, tk = driving() ? 1 : 0;
    if (Math.abs(drv.k - tk) > 0.002) { drv.k += (tk - drv.k) * (1 - Math.exp(-dt * 1.8)); moving = true; } else drv.k = tk;
    var target = drv.on ? drv.target : previewDrive ? 40 : 0;
    if (Math.abs(drv.spd - target) > 0.05) { drv.spd += (target - drv.spd) * (1 - Math.exp(-dt * 1.6)); moving = true; } else drv.spd = target;
    if (drv.spd !== 0) {
      var ms = drv.spd / 3.6;
      drv.dist += ms * dt;
      spinA = (spinA + ms / wheelR * dt) % (Math.PI * 2);
      spinners.forEach(function (p) { p.quaternion.setFromAxisAngle(X_AXIS, spinA); });
      moving = true;
    }
    track.visible = drv.k > 0.002;
    if (!track.visible) return moving;
    var k = drv.k, dk = ease(k);
    road.scale.z = 0.04 + 0.96 * dk;
    road.material.opacity = Math.min(0.06, k * 0.096);
    road.material.map.offset.y = -(drv.dist / 12) % 1;
    edgeMat.opacity = dk * (0.45 + 0.5 * themeK);
    return moving;
  }
  function seen(m) { return m.visible && !m.isSkinnedMesh; }
  var hkey = null, hmark = null, hdrag = null, hTapAt = 0;
  var plates = null, pkey = null, pdrag = null, pTapAt = 0;
  page.hinge = function (k) { hkey = k || null; showHinge(); if (app) app.invalidate(); };
  function showHinge() {
    if (hmark) { hmark.parent && hmark.parent.remove(hmark); hmark.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); }); hmark = null; }
    var h = hkey && parts && parts[hkey];
    if (!h || !h.pivot || !h.axis || !root) return;
    var kw = dims.hw / 1.02, L = Math.max(dims.hl, dims.h) * 0.8, m = new T.MeshBasicMaterial({ color: 0xff7a00, depthTest: false, transparent: true, toneMapped: false });
    hmark = new T.Group();
    var ball = new T.Mesh(new T.SphereGeometry(0.055 * kw, 18, 12), m);
    var line = new T.Line(new T.BufferGeometry().setFromPoints([h.axis.clone().multiplyScalar(-L), h.axis.clone().multiplyScalar(L)]),
      new T.LineDashedMaterial({ color: 0xff7a00, dashSize: 0.1 * kw, gapSize: 0.07 * kw, depthTest: false, transparent: true, toneMapped: false }));
    line.computeLineDistances();
    ball.renderOrder = line.renderOrder = 1000;
    hmark.add(ball); hmark.add(line);
    hmark.position.copy(h.pivot.position);
    root.add(hmark);
  }
  page.atRest = function (fn) {
    if (!parts || !root) return fn();
    var keep = [];
    Object.keys(parts).forEach(function (k) {
      var p = parts[k];
      if (p.pivot) { keep.push([p.pivot, p.pivot.position.clone(), p.pivot.quaternion.clone()]); if (p.slide) p.pivot.position.set(0, 0, 0); else p.pivot.quaternion.identity(); }
      else if (p.meshes && p.y0) p.meshes.forEach(function (m, i) { keep.push([m, m.position.clone(), m.quaternion.clone()]); m.position.y = p.y0[i]; });
    });
    root.updateMatrixWorld(true);
    try { return fn(); } finally {
      keep.forEach(function (x) { x[0].position.copy(x[1]); x[0].quaternion.copy(x[2]); });
      root.updateMatrixWorld(true);
    }
  };
  page.hingeAt = function () {
    if (!hmark || !camera) return null;
    var q = hmark.position.clone().applyMatrix4(root.matrixWorld).project(camera);
    return { x: Math.round((q.x + 1) / 2 * W), y: Math.round((1 - q.y) / 2 * H) };
  };
  function hingeHit(x, y) {
    if (!hmark) return false;
    var q = hmark.position.clone().applyMatrix4(root.matrixWorld).project(camera);
    return Math.hypot((q.x + 1) / 2 * W - x, (1 - q.y) / 2 * H - y) < 30;
  }
  function hingeMove(x, y) {
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    var n = camera.getWorldDirection(V(0, 0, 0)), pl = new T.Plane().setFromNormalAndCoplanarPoint(n, hdrag.p), out = V(0, 0, 0);
    if (!ray.ray.intersectPlane(pl, out)) return;
    hmark.position.copy(out); app.invalidate();
  }
  function hingeDone() {
    var h = parts && parts[hkey], pos = hmark && hmark.position;
    if (!h || !pos || !h.base) return;
    var c = function (v) { return Math.round(Math.max(-1.5, Math.min(1.5, v)) * 1000) / 1000; };
    var piv = [c((pos.x - h.base.x) / Math.max(1e-3, h.size.x) * h.side), c((pos.y - h.base.y) / Math.max(1e-3, h.size.y)), c((pos.z - h.base.z) / Math.max(1e-3, h.size.z))];
    if (window.D3Tune) D3Tune.hinged(hkey, piv);
  }
  function inScene(o) { for (var p = o; p; p = p.parent) if (!p.visible) return false; return true; }
  var YEL = '#ffc20d', INK = '#231f20', RED = '#e2232a', NIGHT = '#17181a';
  var C440 = [[15, 45], [72, 45], [151, 45], [208, 45], [265, 45], [322, 45], [379, 45]];
  var C480 = [[15.5, 45], [69.5, 43], [161.5, 43], [213.5, 43], [265.5, 43], [317.5, 43], [369.5, 43], [421.5, 43]];
  var CPOL = [[15, 45], [94, 45], [151, 45], [208, 45], [265, 45], [322, 45], [379, 45]];
  var S440 = [[100, 12.5], [325, 12.5], [100, 127.5], [325, 127.5]], S480 = [[120, 12.5], [345, 12.5], [120, 127.5], [345, 127.5]];
  var LAYOUT = {
    l440: { W: 440, H: 140, cells: C440, sep: { after: 2, x: 134, y: 70, r: 5 }, slots: S440 },
    l480: { W: 480, H: 140, cells: C480, slots: S480, logo: true },
    l480b: { W: 480, H: 140, cells: C480, slots: [[105, 12.5], [360, 12.5], [105, 127.5], [360, 127.5]], logo: true },
    row2: { W: 440, H: 220, slots: [[85, 12.5], [340, 12.5], [100, 207.5], [325, 207.5]], sep: { after: 1, x: 220, y: 57, r: 5 }, fonts: 'ttbbbbb',
      cells: [[110, 80, 15, 60], [250, 80, 15, 60], [27.5, 65, 90, 110], [107.5, 65, 90, 110], [187.5, 65, 90, 110], [267.5, 65, 90, 110], [347.5, 65, 90, 110]] },
    low: { W: 300, H: 165, slots: [[15, 12.5], [270, 12.5]], sep: { after: 1, x: 150, y: 32.5, r: 5 }, fonts: 'ttnnnnn',
      cells: [[70, 55, 10, 45], [175, 55, 10, 45], [13.5, 45, 65, 90], [70.5, 45, 65, 90], [127.5, 45, 65, 90], [184.5, 45, 65, 90], [241.5, 45, 65, 90]] },
    police: { W: 440, H: 140, cells: CPOL, sep: { after: 1, x: 77, y: 70, r: 7 }, slots: S440 },
    policeR: { W: 440, H: 140, cells: CPOL, sep: { after: 1, x: 77, y: 70, w: 16, h: 10 }, slots: S440 },
    embassy: { W: 440, H: 140, cells: [[15.5, 45], [72.5, 45], [129.5, 45], [208.5, 45], [265.5, 45], [322.5, 45], [379.5, 45]], sep: { after: 3, x: 191.5, y: 70, r: 5 }, slots: S440 },
    consulate: { W: 440, H: 140, cells: [[15.5, 45], [72.5, 45], [129.5, 45], [186.5, 45], [265.5, 45], [322.5, 45], [379.5, 45]], sep: { after: 4, x: 248.5, y: 70, r: 5 }, slots: S440 },
    emergency: { W: 480, H: 140, cells: [[15.5, 45], [91, 43], [146.2, 43], [201.4, 43], [256.6, 43], [311.8, 43], [366.5, 43.5], [421, 43.5]], sep: { after: 1, x: 76, y: 70, r: 5.5 }, slots: S480 }
  };
  var PLATE = {
    blue: { lay: 'l440', bg: '#213f99', fg: '#ffffff', frame: [1.5, 3], slot: '#e6ebf5' },
    green: { lay: 'l480', bg: ['#ffffff', '#7dc685'], fg: INK, frame: [2, 4], slot: '#f7faf7', slotLine: '#8a948c' },
    yellow: { lay: 'l440', layR: 'row2', bg: YEL, fg: INK, frame: [1.5, 3], slot: '#fff3c4' },
    bignev: { lay: 'l480', layR: 'l480b', bg: '#eba749', panel: '#7dc685', fg: INK, frame: [2, 4], slot: '#ffffff' },
    police: { lay: 'police', layR: 'policeR', bg: '#ffffff', fg: INK, frame: [1.5, 3], slot: '#ffffff', slotLine: '#6b6b6b', red: [6], fix: { 6: '警' } },
    emergency: { lay: 'emergency', bg: '#ffffff', fg: INK, frame: [2, 4], slot: '#ffffff', slotLine: '#6b6b6b', red: [6, 7], redF: [1], fix: { 6: '应', 7: '急' } },
    coach: { lay: 'l440', bg: YEL, fg: INK, frame: [1.5, 3], slot: '#fff3c4', fix: { 6: '学' } },
    trailer: { lay: 'row2', bg: YEL, fg: INK, frame: [1.5, 3], slot: '#fff3c4', fix: { 6: '挂' } },
    lowspeed: { lay: 'low', bg: YEL, fg: INK, frame: [1.5, 3], slot: '#fff3c4' },
    embassy: { lay: 'embassy', bg: NIGHT, fg: '#ffffff', frame: [1.5, 3], slot: '#dcdcdc', fix: { 6: '使' } },
    consulate: { lay: 'consulate', bg: NIGHT, fg: '#ffffff', frame: [1.5, 3], slot: '#dcdcdc', fix: { 6: '领' } },
    hkmo: { lay: 'l440', bg: NIGHT, fg: '#ffffff', frame: [1.5, 3], slot: '#dcdcdc', fix: { 0: '粤', 1: 'Z' } }
  };
  function plateStyle(st, which) {
    var s = PLATE[st.style] || { lay: 'l440', bg: st.bg || '#e9e9e9', fg: st.fg || '#15171a', frame: [1.5, 3], slot: '#f2f2f2' };
    var L = LAYOUT[(which === 'rear' && s.layR) || s.lay], o = {}, k;
    for (k in L) o[k] = L[k];
    for (k in s) o[k] = s[k];
    o.bg = [].concat(o.bg);
    return o;
  }
  var PK = 2.6;
  var pfont = 0, pPaths = {};
  function plateFont() {
    if (window.D3PlateFont || pfont === 2) return true;
    if (!pfont) {
      pfont = 1;
      var sc = document.createElement('script');
      sc.src = 'plate-font.js';
      sc.onload = sc.onerror = function () { pfont = 2; if (cfg && cfg.plates) page.plates(); };
      document.head.appendChild(sc);
    }
    return false;
  }
  var gly = null;
  function glyph(ch, font, c) {
    if (!gly) { gly = document.createElement('canvas'); gly.width = gly.height = 360; }
    var x = gly.getContext('2d'), N = 360;
    x.clearRect(0, 0, N, N);
    x.font = font; x.fillStyle = c; x.textBaseline = 'alphabetic';
    x.fillText(ch, 30, 280);
    var d = x.getImageData(0, 0, N, N).data, l = N, r = -1, t = N, b = -1;
    for (var y = 0; y < N; y++) for (var i = 0; i < N; i++) if (d[(y * N + i) * 4 + 3] > 60) { if (i < l) l = i; if (i > r) r = i; if (y < t) t = y; if (y > b) b = y; }
    return r < 0 ? null : { l: l, r: r + 1, t: t, b: b + 1 };
  }
  function rrect(x, X, Y, W2, H2, R) {
    x.beginPath();
    x.moveTo(X + R, Y); x.lineTo(X + W2 - R, Y); x.arc(X + W2 - R, Y + R, R, -Math.PI / 2, 0);
    x.lineTo(X + W2, Y + H2 - R); x.arc(X + W2 - R, Y + H2 - R, R, 0, Math.PI / 2);
    x.lineTo(X + R, Y + H2); x.arc(X + R, Y + H2 - R, R, Math.PI / 2, Math.PI);
    x.lineTo(X, Y + R); x.arc(X + R, Y + R, R, Math.PI, Math.PI * 1.5);
    x.closePath();
  }
  function nevLogo(x, cx, cy, R) {
    x.save();
    x.beginPath(); x.arc(cx, cy, R, 0, Math.PI * 2); x.fillStyle = '#84d2b4'; x.fill();
    var r = R * 0.86;
    x.beginPath(); x.arc(cx, cy, r, 0, Math.PI * 2); x.clip();
    [['#40acd4', -1, -0.42], ['#da3477', -0.42, -0.16], ['#ec7a2a', -0.16, 0.2], ['#c1ce39', 0.2, 0.46], ['#4faa47', 0.46, 1]].forEach(function (b) {
      x.fillStyle = b[0]; x.fillRect(cx - r, cy + b[1] * r, 2 * r, (b[2] - b[1]) * r);
    });
    x.fillStyle = '#ffffff';
    [-0.09, 0.03, 0.14].forEach(function (y0) { x.fillRect(cx - r, cy + y0 * r, 1.4 * r, 0.045 * r); });
    var px = cx + 0.18 * r;
    x.beginPath();
    x.moveTo(px, cy - 0.66 * r); x.arc(px, cy, 0.66 * r, -Math.PI / 2, Math.PI / 2); x.lineTo(px, cy + 0.36 * r);
    x.lineTo(cx - 0.52 * r, cy + 0.36 * r); x.lineTo(cx - 0.52 * r, cy - 0.36 * r); x.lineTo(px, cy - 0.36 * r); x.closePath();
    x.fillStyle = '#ffffff'; x.fill();
    x.lineWidth = 0.09 * r; x.strokeStyle = '#3f6b5c'; x.stroke();
    x.fillStyle = '#ec7a2a';
    [-0.16, -0.03, 0.09].forEach(function (y0) { x.fillRect(cx - r, cy + y0 * r, 0.7 * r + 0.18 * r, 0.075 * r); });
    x.restore();
  }
  function plateCanvas(st, which) {
    var s = plateStyle(st, which), k = PK, c = document.createElement('canvas');
    c.width = Math.round(s.W * k); c.height = Math.round(s.H * k);
    var x = c.getContext('2d');
    if (s.bg.length > 1) {
      var g = x.createLinearGradient(0, 0, 0, c.height);
      g.addColorStop(0, s.bg[0]); g.addColorStop(0.85, s.bg[1]); g.addColorStop(1, s.bg[1]);
      x.fillStyle = g;
    } else x.fillStyle = s.bg[0];
    x.fillRect(0, 0, c.width, c.height);
    if (s.panel) { rrect(x, 136.5 * k, 10 * k, 336 * k, 120 * k, 4 * k); x.fillStyle = s.panel; x.fill(); }
    s.slots.forEach(function (p) {
      rrect(x, p[0] * k, (p[1] - 4) * k, 15 * k, 8 * k, 4 * k);
      x.fillStyle = s.slot; x.fill();
      if (s.slotLine) { x.lineWidth = 0.6 * k; x.strokeStyle = s.slotLine; x.stroke(); }
    });
    var f0 = s.frame[0], f1 = s.frame[1], fm = (f0 + f1) / 2;
    rrect(x, fm * k, fm * k, (s.W - 2 * fm) * k, (s.H - 2 * fm) * k, (10 - fm) * k);
    x.lineWidth = (f1 - f0) * k; x.strokeStyle = s.fg; x.stroke();
    var cells = s.cells, fix = s.fix || {}, red = (s.red || []).concat(which === 'front' && s.redF || []);
    var chars = String(st.text || '').toUpperCase().replace(/[^A-Z0-9\u4e00-\u9fa5]/g, '').slice(0, cells.length);
    var CN = '700 240px "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", MiSans, sans-serif';
    var AN = '700 240px "sans-serif-condensed", "Arial Narrow", Arial, Helvetica, Roboto, sans-serif';
    var ref = null, zero = null, F = window.D3PlateFont;
    for (var i = 0; i < cells.length; i++) {
      var ch = chars[i] || fix[i];
      if (!ch) continue;
      var cell = cells[i], cw = cell[1] * k, cx = (cell[0] + cell[1] / 2) * k, top = (cell[2] === undefined ? 25 : cell[2]) * k, chh = (cell[3] || 90) * k;
      var cn = /[\u4e00-\u9fa5]/.test(ch), col = red.indexOf(i) >= 0 ? RED : s.fg, fs = (s.fonts && s.fonts[i]) || 'n';
      var e = F && (F[fs][ch] || (fs === 'b' && F.t[ch]));
      if (e) {
        var gw = cn ? cw : Math.min(e[0] * chh, cw), key = (F[fs][ch] ? fs : 't') + ch;
        if (!pPaths[key]) pPaths[key] = new Path2D(e[1]);
        x.save(); x.translate(cx - gw / 2, top); x.scale(gw / 400, chh / 400); x.fillStyle = col; x.fill(pPaths[key]); x.restore();
        continue;
      }
      if (!cn && !ref) { ref = glyph('H', AN, col); zero = glyph('0', AN, col); }
      var b = glyph(ch, cn ? CN : AN, col);
      if (!b) continue;
      var bw = b.r - b.l, bh = b.b - b.t;
      if (cn) { x.drawImage(gly, b.l, b.t, bw, bh, cell[0] * k, top, cw, chh); continue; }
      var sy = chh / (ref.b - ref.t), sxr = cw / (zero.r - zero.l), dw = Math.min(bw * sxr, cw);
      x.drawImage(gly, b.l, b.t, bw, bh, cx - dw / 2, top + (b.t - ref.t) * sy, dw, bh * sy);
    }
    var sp = s.sep;
    if (sp && chars.length > sp.after) {
      x.fillStyle = s.fg;
      if (sp.r) { x.beginPath(); x.arc(sp.x * k, sp.y * k, sp.r * k, 0, Math.PI * 2); x.fill(); }
      else x.fillRect((sp.x - sp.w / 2) * k, (sp.y - sp.h / 2) * k, sp.w * k, sp.h * k);
    }
    if (s.logo) nevLogo(x, 137 * k, 70 * k, 13.5 * k);
    return c;
  }
  function plateShape(w, h, r) {
    var sh = new T.Shape(), x0 = -w / 2, y0 = -h / 2;
    sh.moveTo(x0 + r, y0); sh.lineTo(x0 + w - r, y0); sh.absarc(x0 + w - r, y0 + r, r, -Math.PI / 2, 0, false);
    sh.lineTo(x0 + w, y0 + h - r); sh.absarc(x0 + w - r, y0 + h - r, r, 0, Math.PI / 2, false);
    sh.lineTo(x0 + r, y0 + h); sh.absarc(x0 + r, y0 + h - r, r, Math.PI / 2, Math.PI, false);
    sh.lineTo(x0, y0 + r); sh.absarc(x0 + r, y0 + r, r, Math.PI, Math.PI * 1.5, false);
    return sh;
  }
  function mkPlate(which) {
    var st = cfg.plates, s = plateStyle(st, which), p = st[which] || {}, w = p.w || s.W / 1000, h = w * s.H / s.W, r = w * 10 / s.W, D = 0.003;
    var shape = plateShape(w, h, r), grp = new T.Group();
    var bodyG = new T.ExtrudeGeometry(shape, { depth: D, bevelEnabled: false, curveSegments: 5 });
    bodyG.translate(0, 0, -D);
    var edge = new T.Color(s.bg[s.bg.length - 1]).multiplyScalar(0.75);
    grp.add(new T.Mesh(bodyG, new T.MeshStandardMaterial({ color: edge, roughness: 0.55, metalness: 0.3 })));
    var tex = new T.CanvasTexture(plateCanvas(st, which));
    tex.encoding = T.sRGBEncoding;
    tex.repeat.set(1 / w, 1 / h); tex.offset.set(0.5, 0.5);
    tex.anisotropy = Math.min(4, app.renderer().capabilities.getMaxAnisotropy());
    grp.add(new T.Mesh(new T.ShapeGeometry(shape, 5), new T.MeshStandardMaterial({ map: tex, roughness: 0.45, metalness: 0, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 })));
    grp.rotation.order = 'YXZ';
    grp.visible = !!p.on;
    grp.userData.w = w;
    return grp;
  }
  function plateAuto(which) {
    var front = which === 'front', want = front ? 1 : -1, nm = new T.Matrix3(), best = null;
    var list = modelObj.userData.d3.meshes.filter(function (m) { return m.parent && inScene(m); });
    for (var k = 0; k < 8 && !best; k++) {
      var y = dims.h * (0.12 + k * 0.05), hit = null, hn = null;
      for (var i = 0; i < 5; i++) {
        ray.set(V((i - 2) * 0.08, y, want * (dims.hl * 1.5 + 1)), V(0, 0, -want));
        var hs = ray.intersectObjects(list, false).filter(function (h) { return !!h.face; });
        if (!hs.length || (hit && Math.abs(hs[0].point.z) <= Math.abs(hit.z))) continue;
        hit = hs[0].point;
        hn = hs[0].face.normal.clone().applyMatrix3(nm.getNormalMatrix(hs[0].object.matrixWorld)).normalize();
      }
      if (hit && hn && Math.abs(hn.z) > 0.6 && Math.abs(hit.z) >= 0.8 * dims.hl) best = [0, Math.round(y * 1000) / 1000, hit.z + want * 0.012];
    }
    return best || [0, dims.h * 0.3, want * dims.hl * 0.98];
  }
  function plateXform(m) {
    var st = cfg.plates, p = st[m.userData.which] || (st[m.userData.which] = {});
    if (!p.at) p.at = plateAuto(m.userData.which);
    m.position.fromArray(p.at);
    m.rotation.set(-(p.pitch || 0) * DEG, (m.userData.which === 'front' ? 0 : Math.PI) + (p.yaw || 0) * DEG, 0);
  }
  function plateAttach(m) {
    if (!m || !root) return;
    root.attach(m);
    if (!parts) return;
    page.atRest(function () {
      Object.keys(parts).forEach(function (k) {
        var p = parts[k];
        if (!p.pivot || !p.meshes) return;
        var b = new T.Box3();
        p.meshes.forEach(function (mm) { b.expandByObject(mm); });
        if (b.containsPoint(m.position)) p.pivot.attach(m);
      });
    });
  }
  function plateFree(m) {
    if (!m) return;
    if (m.parent) m.parent.remove(m);
    m.traverse(function (o) {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
  }
  page.plates = function (st) {
    if (st) cfg.plates = st;
    if (!cfg.plates || !root || !modelObj) return;
    var P = cfg.plates;
    if ((P.front && P.front.on || P.rear && P.rear.on) && !plateFont()) return;
    if (!plates) { plates = {}; ['front', 'rear'].forEach(function (k) { var m = mkPlate(k); m.userData.which = k; plates[k] = m; plateXform(m); root.add(m); plateAttach(m); }); }
    else ['front', 'rear'].forEach(function (k) {
      plateFree(plates[k]);
      var m = mkPlate(k);
      m.userData.which = k;
      plates[k] = m;
      plateXform(m);
      root.add(m);
      plateAttach(m);
    });
    app.invalidate();
  };
  page.plateAuto = function (which) { return plateAuto(which); };
  page.plateMM = function (style, which) { var s = plateStyle({ style: style }, which); return [s.W, s.H]; };
  page.platesAt = function () { return plates; };
  page.partsAt = function () { return parts; };
  page.plateEdit = function (which) { pkey = which || null; };
  page.plateAt = function () {
    var m = pkey && plates && plates[pkey];
    if (!m || !m.visible || !camera) return null;
    var q = m.getWorldPosition(V(0, 0, 0)).project(camera);
    return { x: Math.round((q.x + 1) / 2 * W), y: Math.round((1 - q.y) / 2 * H) };
  };
  function plateHit(x, y) {
    var a = page.plateAt();
    return !!a && Math.hypot(a.x - x, a.y - y) < 46;
  }
  function plateMove(x, y) {
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    var n = camera.getWorldDirection(V(0, 0, 0)), pl = new T.Plane().setFromNormalAndCoplanarPoint(n, pdrag.p), out = V(0, 0, 0);
    if (!ray.ray.intersectPlane(pl, out)) return;
    var m = plates[pkey];
    if (m.parent !== root) root.attach(m);
    m.position.copy(out);
    app.invalidate();
  }
  function plateDone() {
    var w = plates[pkey].getWorldPosition(V(0, 0, 0));
    if (window.D3Tune) D3Tune.plated(pkey, [Math.round(w.x * 1000) / 1000, Math.round(w.y * 1000) / 1000, Math.round(w.z * 1000) / 1000]);
  }
  var wrapP = null, wkey = null, wmark = null, wdrag = null, wTapAt = 0, wRaf = 0, bufIds = typeof WeakMap === 'function' ? new WeakMap() : null, bufN = 0;
  function wrapLib() {
    if (window.D3Wrap) return Promise.resolve();
    if (!wrapP) wrapP = new Promise(function (ok, no) {
      var s = document.createElement('script');
      s.src = 'carwrap.js';
      s.onload = function () { if (window.D3Wrap) ok(); else { wrapP = null; no(new Error('贴纸脚本加载失败')); } };
      s.onerror = function () { wrapP = null; no(new Error('贴纸脚本加载失败')); };
      document.head.appendChild(s);
    });
    return wrapP;
  }
  function wrapCtx() {
    var R0 = root, M0 = M, d0 = { hw: dims.hw, h: dims.h, hl: dims.hl }, b0 = rec.buf, envK0 = envK, lp = !!(S.fx().lowp && std), sh = !!S.fx().shadow;
    if (bufIds && !bufIds.has(b0)) bufIds.set(b0, ++bufN);
    var shown = function (o) { for (var p = o; p && p !== R0; p = p.parent) if (!p.visible) return false; return true; };
    var carMeshes = function () { var l = []; R0.traverse(function (o) { if (o.isMesh && o.userData.car && !o.isSkinnedMesh && shown(o)) l.push(o); }); return l; };
    var glassy = function (m) { return m === M0.glass || M0.glassSrc.indexOf(m) >= 0; };
    return {
      renderer: app.renderer(), root: R0, dims: d0, buf: b0, bufId: bufIds ? bufIds.get(b0) : 0, shadow: sh,
      invalidate: function () { if (app && root === R0) app.invalidate(); },
      atRest: function (fn) { return root === R0 ? page.atRest(fn) : fn(); },
      occluders: carMeshes,
      targets: function (on) {
        var body = on !== 'glass', glass = on !== 'body', paint = M0.has.paint, out = [];
        carMeshes().forEach(function (o) {
          if (M0.notWrap[o.uuid]) return;
          var ok = D3Model.mats(o).map(function (m) {
            if (glassy(m)) return glass;
            if (paint) return body && m === M0.paint;
            return body && !(m.transparent && m.opacity < 0.9) && M0.rims.indexOf(m) < 0 && M0.cals.indexOf(m) < 0;
          });
          var g = o.geometry;
          if (Array.isArray(o.material) && g.groups.length) {
            var rg = g.groups.filter(function (q) { return ok[q.materialIndex]; }).map(function (q) { return [q.start, q.count]; });
            if (rg.length) out.push({ m: o, ranges: rg });
          } else if (ok[0]) out.push({ m: o, ranges: null });
        });
        return out;
      },
      owner: function (m) { for (var o = m; o && o !== R0; o = o.parent) if (o.userData.d3move) return o; return R0; },
      mat: function (p) { p.envMapIntensity = envK0; if (lp) p.defines = { D3_LOWP: '' }; return p; }
    };
  }
  page.wraps = function (list) {
    if (list) cfg.wraps = list;
    var r = root;
    if (!r || !modelObj || !cfg) return Promise.resolve();
    if (!(cfg.wraps && cfg.wraps.length) && !r.userData.wraps) { showWrapMark(); return Promise.resolve(); }
    return wrapLib().then(function () {
      if (root !== r || !scene) return;
      var s = r.userData.wraps || (r.userData.wraps = D3Wrap.session(wrapCtx()));
      return s.apply(cfg.wraps || []).then(function () { if (root === r && wkey === null) s.forget(); showWrapMark(); });
    });
  };
  page.wrapLib = wrapLib;
  page.wrapThumb = function (w, size) { var s = root && root.userData.wraps; return s && s.ctx && window.D3Wrap ? D3Wrap.thumb(w, s.ctx, size || 120) : null; };
  page.wrapStats = function () { var s = root && root.userData.wraps; return { meshes: s ? s.count() : 0, n: cfg && cfg.wraps ? cfg.wraps.length : 0 }; };
  page.wrapEdit = function (id) {
    wkey = id === undefined || id === null ? null : id;
    if (wkey === null && root && root.userData.wraps) root.userData.wraps.forget();
    showWrapMark();
  };
  function showWrapMark() {
    if (wmark) { if (wmark.parent) wmark.parent.remove(wmark); wmark.geometry.dispose(); wmark.material.dispose(); wmark = null; }
    var s = root && root.userData.wraps, c = wkey && s && s.center(wkey);
    if (c) {
      var kw = dims.hw / 1.02;
      wmark = new T.Mesh(new T.SphereGeometry(0.05 * kw, 18, 12), new T.MeshBasicMaterial({ color: 0xff7a00, depthTest: false, transparent: true, toneMapped: false }));
      wmark.renderOrder = 1000; wmark.raycast = function () {};
      wmark.position.copy(c);
      root.add(wmark);
    }
    if (app && root) app.invalidate();
  }
  page.wrapAt = function () {
    if (!wmark || !camera) return null;
    var q = wmark.position.clone().applyMatrix4(root.matrixWorld).project(camera);
    return { x: Math.round((q.x + 1) / 2 * W), y: Math.round((1 - q.y) / 2 * H) };
  };
  function wrapHit(x, y) { var a = page.wrapAt(); return !!a && Math.hypot(a.x - x, a.y - y) < 36; }
  function snapDir(n) {
    var best = null;
    [V(1, 0, 0), V(-1, 0, 0), V(0, 1, 0), V(0, 0, 1), V(0, 0, -1)].forEach(function (a) { if (n.dot(a) > Math.cos((a.y ? 25 : 35) * DEG) && (!best || n.dot(a) > n.dot(best))) best = a; });
    return best || n;
  }
  function r3(v) { return [Math.round(v.x * 1000) / 1000, Math.round(v.y * 1000) / 1000, Math.round(v.z * 1000) / 1000]; }
  function surfaceRay(o, d) {
    ray.set(o, d);
    var list = [];
    root.traverse(function (m) { if (m.isMesh && m.userData.car && inScene(m)) list.push(m); });
    var h = ray.intersectObjects(list, false).filter(function (x) { return !!x.face; })[0];
    if (!h) return null;
    var inv = new T.Matrix4().copy(root.matrixWorld).invert(), n = h.face.normal.clone().transformDirection(h.object.matrixWorld).transformDirection(inv);
    if (n.dot(d) > 0) n.negate();
    return { at: r3(h.point.clone().applyMatrix4(inv)), dir: r3(snapDir(n.normalize())) };
  }
  function wrapPick(x, y) {
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    return surfaceRay(ray.ray.origin.clone(), ray.ray.direction.clone());
  }
  page.wrapPick = function (x, y) { return root && camera ? wrapPick(x, y) : null; };
  page.wrapPlace = function () {
    if (!root) return null;
    var az = view.az, sx = Math.sin(az), cz = Math.cos(az), d, o;
    if (Math.abs(sx) >= Math.abs(cz)) { d = V(sx > 0 ? -1 : 1, 0, 0); o = V(sx > 0 ? dims.hw + 2 : -dims.hw - 2, dims.h * 0.45, 0); }
    else { d = V(0, 0, cz > 0 ? -1 : 1); o = V(0, dims.h * 0.42, cz > 0 ? dims.hl + 2 : -dims.hl - 2); }
    var h = null;
    page.atRest(function () { h = surfaceRay(o.applyMatrix4(root.matrixWorld), d.clone().transformDirection(root.matrixWorld)); });
    return h || { at: r3(o.clone().addScaledVector(d, 2)), dir: r3(d.clone().negate()) };
  };
  page.wrapPlaceAt = function (where) {
    if (!root) return null;
    var d = dims, o, dv;
    if (where === 'left') { o = V(d.hw + 2, d.h * 0.45, 0); dv = V(-1, 0, 0); }
    else if (where === 'right') { o = V(-d.hw - 2, d.h * 0.45, 0); dv = V(1, 0, 0); }
    else if (where === 'hood') { o = V(0, d.h + 2, d.hl * 0.62); dv = V(0, -1, 0); }
    else if (where === 'roof') { o = V(0, d.h + 2, -d.hl * 0.08); dv = V(0, -1, 0); }
    else if (where === 'trunk') { o = V(0, d.h + 2, -d.hl * 0.82); dv = V(0, -1, 0); }
    else if (where === 'front') { o = V(0, d.h * 0.4, d.hl + 2); dv = V(0, 0, -1); }
    else { o = V(0, d.h * 0.45, -d.hl - 2); dv = V(0, 0, 1); }
    var h = null;
    page.atRest(function () { h = surfaceRay(o.clone().applyMatrix4(root.matrixWorld), dv.clone().transformDirection(root.matrixWorld)); });
    return h;
  };
  var wdraw = false, wline = null;
  page.wrapDraw = function (on) { wdraw = !!on; if (!on) dropLine(); };
  function dropLine() {
    if (wline && wline.mesh) { if (wline.mesh.parent) wline.mesh.parent.remove(wline.mesh); wline.mesh.geometry.dispose(); wline.mesh.material.dispose(); }
    wline = null;
    if (app && root) app.invalidate();
  }
  function lineAt(x, y) {
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    var out = V(0, 0, 0);
    if (!ray.ray.intersectPlane(wline.plane, out)) return null;
    return out.applyMatrix4(new T.Matrix4().copy(root.matrixWorld).invert());
  }
  function lineStart(x, y) {
    var h = wrapPick(x, y);
    if (!h) return false;
    var top = h.dir[1] > 0.6, n = top ? V(0, 1, 0) : V(h.at[0] >= 0 ? 1 : -1, 0, 0), p0 = V(h.at[0], h.at[1], h.at[2]);
    wline = { top: top, side: h.at[0] >= 0 ? 'L' : 'R', plane: new T.Plane().setFromNormalAndCoplanarPoint(n.clone().transformDirection(root.matrixWorld), p0.clone().applyMatrix4(root.matrixWorld)), pts: [], sx: x, sy: y, lx: -1e9, ly: -1e9 };
    wline.mesh = new T.Line(new T.BufferGeometry(), new T.LineBasicMaterial({ color: 0xff7a00, depthTest: false, transparent: true, toneMapped: false }));
    wline.mesh.renderOrder = 1000; wline.mesh.raycast = function () {};
    root.add(wline.mesh);
    lineMove(x, y);
    return true;
  }
  function lineMove(x, y) {
    if (Math.hypot(x - wline.lx, y - wline.ly) < 4 || wline.pts.length >= 400) return;
    var p = lineAt(x, y);
    if (!p) return;
    wline.lx = x; wline.ly = y;
    wline.pts.push(p);
    wline.mesh.geometry.dispose();
    wline.mesh.geometry = new T.BufferGeometry().setFromPoints(wline.pts.map(function (q) { return q.clone().add(wline.top ? V(0, 0.01, 0) : V(wline.side === 'L' ? 0.01 : -0.01, 0, 0)); }));
    app.invalidate();
  }
  function lineDone() {
    var l = wline, d = dims, r4 = function (v) { return Math.round(v * 1e4) / 1e4; }, pts = l.pts.map(function (q) { return l.top ? [r4((d.hw - q.x) / (2 * d.hw)), r4((d.hl - q.z) / (2 * d.hl))] : [r4((d.hl - q.z) / (2 * d.hl)), r4((d.h - q.y) / d.h)]; });
    dropLine();
    if (pts.length < 2) return;
    var keep = [pts[0]];
    pts.forEach(function (q) { var a = keep[keep.length - 1]; if (Math.hypot(q[0] - a[0], q[1] - a[1]) > 0.004) keep.push(q); });
    if (keep.length < 2) keep.push(pts[pts.length - 1]);
    if (window.D3Tune) D3Tune.wrapDrawn({ path: keep.slice(0, 200), pf: l.top ? 'top' : 'side', side: l.side });
  }
  function wrapMove(x, y) {
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    var n = V(wdrag.w.dir[0], wdrag.w.dir[1], wdrag.w.dir[2]).transformDirection(root.matrixWorld), p = wdrag.p.clone().applyMatrix4(root.matrixWorld), out = V(0, 0, 0);
    if (Math.abs(n.dot(ray.ray.direction)) < 0.25) n = camera.getWorldDirection(V(0, 0, 0));
    if (!ray.ray.intersectPlane(new T.Plane().setFromNormalAndCoplanarPoint(n, p), out)) return;
    out.applyMatrix4(new T.Matrix4().copy(root.matrixWorld).invert());
    wdrag.w.at = r3(out);
    if (wRaf) return;
    wRaf = requestAnimationFrame(function () {
      wRaf = 0;
      var s = root && root.userData.wraps;
      if (!wdrag || !s) return;
      s.one(wdrag.w, wdrag.i);
      showWrapMark();
    });
  }
  function wrapDone() {
    var w = wdrag.w, n = V(w.dir[0], w.dir[1], w.dir[2]), c = V(w.at[0], w.at[1], w.at[2]), h = null;
    page.atRest(function () { h = surfaceRay(c.clone().addScaledVector(n, 1.5).applyMatrix4(root.matrixWorld), n.clone().negate().transformDirection(root.matrixWorld)); });
    if (h && c.distanceTo(V(h.at[0], h.at[1], h.at[2])) < 0.5) w.at = h.at;
    if (window.D3Tune) D3Tune.wrapped(w.id, w.at);
  }
  function lampRgb(r, hx) {
    var h = hx || (cfg && cfg.lights && cfg.lights[r] && cfg.lights[r].color) || LIGHT_COLOR[r];
    return [parseInt(h.slice(1, 3), 16) / 255, parseInt(h.slice(3, 5), 16) / 255, parseInt(h.slice(5, 7), 16) / 255];
  }
  function lightFx() {
    LIGHTS.forEach(function (r) { var l = cfg && cfg.lights && cfg.lights[r]; M.fx[r] = (l && l.effect) || (r === 'turnL' || r === 'turnR' ? 'blink' : 'steady'); });
  }
  var LAMP_VS = 'varying vec2 vUv;\nvarying float vX;\nvarying float vZ;\nvoid main() {\n  vUv = uv;\n  vec4 w = modelMatrix * vec4(position, 1.0);\n  vX = w.x; vZ = w.z;\n  gl_Position = projectionMatrix * viewMatrix * w;\n}';
  var LAMP_FS = [
    'uniform sampler2D tMap;', 'uniform vec3 uTint;', 'uniform vec3 uTurnCol;', 'uniform float uOn;', 'uniform vec4 uGlow;', 'uniform float uFlow;', 'uniform vec2 uAx;', 'uniform vec4 uTurn;', 'uniform float uBorrow;', 'uniform vec2 uMute;',
    'varying vec2 vUv;', 'varying float vX;', 'varying float vZ;',
    'void main() {',
    '  vec4 t = texture2D(tMap, vUv);',
    '  vec3 base = uTint * (1.0 - uGlow.z + uGlow.z * (0.3 + 0.7 * t.r));',
    '  vec3 lit = base + (1.0 - base) * uGlow.y * pow(clamp(t.g / 0.9, 0.0, 1.0), 1.4);',
    '  vec3 col = lit + (1.0 - lit) * max(0.0, uGlow.x - 1.0) * 0.6;',
    '  float shape = clamp(uGlow.x, 0.0, 1.0) * t.a;',
    '  if (uGlow.w > 0.0) shape *= smoothstep(0.0, uGlow.w * 0.4 + t.b, t.g);',
    '  float m = step(clamp((vX - uAx.x) / (uAx.y - uAx.x + 1e-5), 0.0, 1.0), uFlow);',
    '  float tq = uBorrow * (vZ > 0.0 ? (vX > 0.0 ? uTurn.x : uTurn.y) : (vX > 0.0 ? uTurn.z : uTurn.w));',
    '  float a = mix(shape * uOn * m, shape, tq) * (1.0 - (vX > 0.0 ? uMute.x : uMute.y));',
    '  if (a < 0.004) discard;',
    '  gl_FragColor = vec4(pow(mix(col, uTurnCol, tq), vec3(2.2)), a);',
    '  #include <encodings_fragment>',
    '}'].join('\n');
  function lampMat(u) {
    return new T.ShaderMaterial({ uniforms: u, vertexShader: LAMP_VS, fragmentShader: LAMP_FS, transparent: true, depthWrite: false, side: T.DoubleSide, toneMapped: false,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -4 });
  }
  function dataTex(d, w, h) {
    var t = new T.DataTexture(d, w, h, T.RGBAFormat);
    t.magFilter = t.minFilter = T.LinearFilter; t.generateMipmaps = false; t.needsUpdate = true;
    return t;
  }
  function makeLamps(ents) {
    var ov = {}, B = {}, bc = {}, own = [], imgs = {}, xsC = {}, R0 = root, carL = 2 * dims.hl;
    var off = Math.min(0.004, Math.max(0.001, carL * 0.0004));
    var quadsOf = function (o) {
      var b = D3Model.box(o), cz = (b.min.z + b.max.z) / 2, e = cz > 0.5 * dims.hl ? 'F' : cz < -0.5 * dims.hl ? 'B' : '', q = [];
      if (!e) return q;
      if (b.max.x > 0.02 * dims.hw) q.push('L' + e);
      if (b.min.x < -0.02 * dims.hw) q.push('R' + e);
      return q;
    };
    var hasTurn = {}, hasFb = {}, fbQ = {}, ax = {};
    ents.turnL.concat(ents.turnR).forEach(function (e) { quadsOf(e.o).forEach(function (q) { hasTurn[q] = 1; }); });
    TURN_FB.forEach(function (r) { ents[r].forEach(function (e) { quadsOf(e.o).forEach(function (q) { hasFb[q] = 1; }); }); });
    QUADS.forEach(function (q) { if (!hasTurn[q] && hasFb[q]) fbQ[q] = 1; });
    SIDES.forEach(function (sd) {
      var b = new T.Box3();
      ents['turn' + sd].forEach(function (e) { b.union(D3Model.box(e.o)); });
      if (!b.isEmpty()) ax['turn' + sd] = sd === 'L' ? [b.min.x, b.max.x] : [b.max.x, b.min.x];
    });
    function imgOf(t) {
      if (!t || !t.image) return null;
      if (imgs[t.uuid] !== undefined) return imgs[t.uuid];
      var im = t.image, w = im.width, h = im.height, r = null;
      try {
        if (w && h && !im.data) {
          var k = Math.min(1, 256 / Math.max(w, h)), cw = Math.max(1, Math.round(w * k)), ch = Math.max(1, Math.round(h * k)), c = document.createElement('canvas');
          c.width = cw; c.height = ch;
          var cx = c.getContext('2d');
          cx.drawImage(im, 0, 0, cw, ch);
          r = { w: cw, h: ch, d: cx.getImageData(0, 0, cw, ch).data };
        }
      } catch (er) { r = null; }
      return (imgs[t.uuid] = r);
    }
    function pieceOf(e) {
      var o = e.o, g = o.geometry, pos = g && g.attributes.position;
      if (!pos) return null;
      o.updateWorldMatrix(true, false);
      var ix = g.index, total = ix ? ix.count : pos.count, tri = [];
      var add = function (s0, n) { for (var k = s0; k < Math.min(total, s0 + n); k++) tri.push(ix ? ix.getX(k) : k); };
      if (e.i >= 0 && g.groups.length) g.groups.forEach(function (q) { if (q.materialIndex === e.i) add(q.start, q.count); });
      else add(0, total);
      tri.length -= tri.length % 3;
      if (!tri.length) return null;
      var map = new Int32Array(pos.count), used = [], idx = new Uint32Array(tri.length), k, i;
      for (k = 0; k < map.length; k++) map[k] = -1;
      for (k = 0; k < tri.length; k++) { var v = tri[k]; if (map[v] < 0) { map[v] = used.length; used.push(v); } idx[k] = map[v]; }
      var n = used.length, mw = o.matrixWorld, wp = new Float32Array(n * 3), lp = new Float32Array(n * 3), v3 = new T.Vector3();
      for (i = 0; i < n; i++) {
        v3.fromBufferAttribute(pos, used[i]);
        lp[i * 3] = v3.x; lp[i * 3 + 1] = v3.y; lp[i * 3 + 2] = v3.z;
        v3.applyMatrix4(mw);
        wp[i * 3] = v3.x; wp[i * 3 + 1] = v3.y; wp[i * 3 + 2] = v3.z;
      }
      var nl = new Float32Array(n * 3), na = g.attributes.normal, C = [0, 0, 0, 0, 0, 0];
      if (na) for (i = 0; i < n; i++) { nl[i * 3] = na.getX(used[i]); nl[i * 3 + 1] = na.getY(used[i]); nl[i * 3 + 2] = na.getZ(used[i]); }
      for (k = 0; k < idx.length; k += 3) {
        var a = idx[k] * 3, b = idx[k + 1] * 3, c = idx[k + 2] * 3;
        var ux = wp[b] - wp[a], uy = wp[b + 1] - wp[a + 1], uz = wp[b + 2] - wp[a + 2], vx = wp[c] - wp[a], vy = wp[c + 1] - wp[a + 1], vz = wp[c + 2] - wp[a + 2];
        var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, len = Math.sqrt(nx * nx + ny * ny + nz * nz);
        if (len < 1e-12) continue;
        var w = len / 2; nx /= len; ny /= len; nz /= len;
        C[0] += w * nx * nx; C[1] += w * nx * ny; C[2] += w * nx * nz; C[3] += w * ny * ny; C[4] += w * ny * nz; C[5] += w * nz * nz;
        if (!na) {
          var lx = lp[idx[k + 1] * 3] - lp[a], ly = lp[idx[k + 1] * 3 + 1] - lp[a + 1], lz = lp[idx[k + 1] * 3 + 2] - lp[a + 2];
          var mx = lp[c] - lp[a], my = lp[c + 1] - lp[a + 1], mz = lp[c + 2] - lp[a + 2], fx = ly * mz - lz * my, fy = lz * mx - lx * mz, fz = lx * my - ly * mx;
          [a, b, c].forEach(function (q) { nl[q] += fx; nl[q + 1] += fy; nl[q + 2] += fz; });
        }
      }
      var d = [C[0], C[3], C[5]], ex = new T.Vector3(d[0] >= d[1] && d[0] >= d[2] ? 1 : 0, d[1] > d[0] && d[1] >= d[2] ? 1 : 0, d[2] > d[0] && d[2] > d[1] ? 1 : 0);
      for (k = 0; k < 24; k++) {
        var x = C[0] * ex.x + C[1] * ex.y + C[2] * ex.z, y = C[1] * ex.x + C[3] * ex.y + C[4] * ex.z, z = C[2] * ex.x + C[4] * ex.y + C[5] * ex.z;
        if (x * x + y * y + z * z < 1e-24) break;
        ex.set(x, y, z).normalize();
      }
      if (ex.lengthSq() < 0.5) ex.set(0, 0, 1);
      var e1 = new T.Vector3().crossVectors(Math.abs(ex.y) < 0.9 ? new T.Vector3(0, 1, 0) : new T.Vector3(1, 0, 0), ex).normalize(), e2 = new T.Vector3().crossVectors(ex, e1);
      var p2 = new Float32Array(n * 2), offL = off / Math.max(1e-6, mw.getMaxScaleOnAxis()), op = new Float32Array(n * 3);
      for (i = 0; i < n; i++) {
        var X = wp[i * 3], Y = wp[i * 3 + 1], Z = wp[i * 3 + 2];
        p2[i * 2] = X * e1.x + Y * e1.y + Z * e1.z; p2[i * 2 + 1] = X * e2.x + Y * e2.y + Z * e2.z;
        var qx = nl[i * 3], qy = nl[i * 3 + 1], qz = nl[i * 3 + 2], ql = Math.sqrt(qx * qx + qy * qy + qz * qz) || 1;
        op[i * 3] = lp[i * 3] + qx / ql * offL; op[i * 3 + 1] = lp[i * 3 + 1] + qy / ql * offL; op[i * 3 + 2] = lp[i * 3 + 2] + qz / ql * offL;
      }
      var ua = g.attributes.uv, suv = null;
      if (ua) { suv = new Float32Array(n * 2); for (i = 0; i < n; i++) { suv[i * 2] = ua.getX(used[i]); suv[i * 2 + 1] = ua.getY(used[i]); } }
      var mat = Array.isArray(o.material) ? o.material[e.i >= 0 ? e.i : 0] : o.material, img = imgOf(mat && (mat.emissiveMap || mat.map));
      var base = mat && mat.color ? mat.color.clone().convertLinearToSRGB().toArray() : [1, 1, 1];
      var geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(op, 3));
      geo.setIndex(new T.BufferAttribute(n < 65536 ? new Uint16Array(idx) : idx, 1));
      return { o: o, z: e.z || '', geo: geo, bake: { p: p2, i: idx, uv: suv, img: img, base: base } };
    }
    function build(r) {
      var pcs = [];
      ents[r].forEach(function (e) { try { var pc = pieceOf(e); if (pc) pcs.push(pc); } catch (er) { console.error('灯罩的形状没算出来', er); } });
      if (!pcs.length) return null;
      var art;
      try { art = D3Lamp.bake(pcs.map(function (pc) { return pc.bake; })); } catch (er) { console.error('灯的点亮层没做成', er); pcs.forEach(function (pc) { pc.geo.dispose(); }); return null; }
      var tc = lampRgb(r === 'turnR' ? 'turnR' : 'turnL', LIGHT_COLOR.turnL), u = {
        tMap: { value: dataTex(art.data, art.w, art.h) }, uTint: { value: new T.Vector3(1, 1, 1) }, uTurnCol: { value: new T.Vector3(tc[0], tc[1], tc[2]) }, uOn: { value: 0 },
        uGlow: { value: new T.Vector4(1, 0.45, 0.6, 0.3) }, uFlow: { value: 1 }, uAx: { value: new T.Vector2(0, 1) }, uTurn: turnU, uBorrow: { value: 0 }, uMute: { value: new T.Vector2(0, 0) }
      };
      own.push(u.tMap.value);
      if (ax[r]) u.uAx.value.set(ax[r][0], ax[r][1]);
      var mats = {}, list = [];
      pcs.forEach(function (pc, i) {
        if (!mats[pc.z]) { var zu = {}; for (var k in u) zu[k] = u[k]; zu.uOn = { value: 0 }; mats[pc.z] = lampMat(zu); own.push(mats[pc.z]); }
        pc.geo.setAttribute('uv', new T.BufferAttribute(art.uvs[i], 2));
        var m = new T.Mesh(pc.geo, mats[pc.z]);
        m.raycast = function () {}; m.renderOrder = 2; m.visible = false; m.userData.d3ov = 1; m.userData.z = pc.z; m.userData.src = pc.o.uuid; m.layers.set(1);
        pc.o.add(m);
        own.push(pc.geo);
        list.push(m);
      });
      return { u: u, mats: mats, meshes: list };
    }
    function xsOf(r) {
      if (xsC[r]) return xsC[r];
      var xs = [], v = new T.Vector3();
      ents[r].forEach(function (e) {
        var pos = e.o.geometry && e.o.geometry.attributes.position;
        if (!pos) return;
        e.o.updateWorldMatrix(true, false);
        for (var i = 0, st = Math.max(1, Math.floor(pos.count / 400)); i < pos.count; i += st) { v.fromBufferAttribute(pos, i).applyMatrix4(e.o.matrixWorld); xs.push(v.x); }
      });
      return (xsC[r] = xs);
    }
    function beam(slot, a) {
      var d = D3Lamp.BEAMS[slot], r = d.role, x = B[slot];
      if (a <= 0.01 || !ents[r].length) { if (x) x.mesh.visible = false; return; }
      var L = (cfg && cfg.lights && cfg.lights[r]) || {}, b = D3Lamp.beam(slot, L[d.key]);
      if (!b.enabled) { if (x) x.mesh.visible = false; return; }
      var wid = 2 * dims.hw, len = 2 * dims.hl, hwB = b.width * wid / 2, cx = b.side * wid, lb = D3Lamp.lobes(xsOf(r), cx, hwB, wid, b);
      var key = [b.shape, b.lobeMode, b.lobeSpacing, b.lobeWidth, b.length, b.width, b.offset, b.side, b.height, b.spread, b.falloff, b.haze, lb.join('/')].join();
      if (!x || x.key !== key) {
        if (x) { R0.remove(x.mesh); x.mesh.geometry.dispose(); if (x.mesh.material.map) x.mesh.material.map.dispose(); x.mesh.material.dispose(); }
        var near = b.dir * (dims.hl + b.offset * len), far = near + b.dir * b.length * len, y = Math.max(0.004, b.height), g = new T.BufferGeometry();
        g.setAttribute('position', new T.BufferAttribute(new Float32Array([cx + hwB, y, near, cx - hwB, y, near, cx - hwB, y, far, cx + hwB, y, far]), 3));
        g.setAttribute('uv', new T.BufferAttribute(new Float32Array([0, 1, 1, 1, 1, 0, 0, 0]), 2));
        g.setIndex([0, 1, 2, 0, 2, 3]);
        var tx = D3Lamp.beamTex(b, lb), mesh = new T.Mesh(g, new T.MeshBasicMaterial({ map: dataTex(tx.data, tx.w, tx.h), transparent: true, depthWrite: false, toneMapped: false, side: T.DoubleSide }));
        mesh.renderOrder = 1; mesh.raycast = function () {}; mesh.layers.set(1);
        R0.add(mesh);
        x = B[slot] = { mesh: mesh, key: key };
      }
      var rgb = b.color ? lampRgb(r, b.color) : lampRgb(r);
      x.mesh.material.color.setRGB(1 - 0.4 * (1 - rgb[0]), 1 - 0.4 * (1 - rgb[1]), 1 - 0.4 * (1 - rgb[2]), T.SRGBColorSpace || 'srgb');
      x.mesh.material.opacity = a * b.intensity;
      x.mesh.visible = true;
    }
    return {
      fbQ: fbQ, bc: bc,
      has: function (r) { return ents[r].length > 0; },
      srcs: function (r) { return ents[r].map(function (e) { return e.o.uuid; }); },
      built: function (r) { return ov[r] !== undefined; },
      get: function (r) { if (ov[r] === undefined) ov[r] = ents[r].length ? build(r) : null; return ov[r]; },
      beam: beam,
      lit: function () { return LIGHTS.filter(function (r) { return ov[r] && ov[r].meshes.some(function (m) { return m.visible; }); }).join(','); },
      amb: function () {
        var o = ov.ambient;
        if (!o) return '';
        var t = o.u.uTint.value, hx = [t.x, t.y, t.z].map(function (v) { return ('0' + Math.round(v * 255).toString(16)).slice(-2); }).join('');
        return Object.keys(o.mats).sort().map(function (z) { return (z || '-') + ':' + Math.round(o.mats[z].uniforms.uOn.value * 100) / 100 + ':' + hx; }).join(' ');
      },
      beams: function () { return Object.keys(B).filter(function (k) { return B[k].mesh.visible; }).join(','); },
      warmBeam: function (slot) { var x = B[slot]; beam(slot, 1); if (B[slot]) B[slot].mesh.visible = !!(x && x.mesh.visible); return true; },
      mesh: function (slot) { return B[slot] && B[slot].mesh.visible ? B[slot].mesh : null; },
      dispose: function () {
        LIGHTS.forEach(function (r) { if (ov[r]) ov[r].meshes.forEach(function (m) { if (m.parent) m.parent.remove(m); }); });
        Object.keys(B).forEach(function (k) { var m = B[k].mesh; if (m.parent) m.parent.remove(m); m.geometry.dispose(); if (m.material.map) m.material.map.dispose(); m.material.dispose(); });
        own.forEach(function (x) { x.dispose(); });
        ov = {}; B = {}; own = [];
      }
    };
  }
  function door(k, ms, p, P) {
    var sign = p.flip ? -1 : 1, b = new T.Box3();
    ms.forEach(function (m) { b.expandByObject(m); });
    var side = b.min.x + b.max.x > 0 ? 1 : -1, outer = side > 0 ? b.max.x : b.min.x, inner = side > 0 ? b.min.x : b.max.x;
    if (p.at && p.axis) return moved(hinge(ms, moveAt(P(p.at), p, b, k === 'hood' || k === 'trunk' ? 1 : side), V(p.axis[0], p.axis[1], p.axis[2]).normalize(), (p.angle || 60) * DEG * sign), P(p.at), b, side);
    var midY = (b.min.y + b.max.y) / 2, midZ = (b.min.z + b.max.z) / 2, st = p.style || 'normal', at, axis, ang, dir = 'up';
    if (k === 'hood' || k === 'trunk') {
      var z = (k === 'hood') !== !!p.other ? b.min.z : b.max.z;
      at = V(0, b.max.y, z); axis = V(1, 0, 0); ang = k === 'hood' ? 55 : 50;
    } else if (st === 'butterfly') { at = V(inner + (outer - inner) * 0.15, b.min.y + (b.max.y - b.min.y) * 0.85, b.max.z - (b.max.z - b.min.z) * 0.12); axis = V(-0.32 * side, 0.5, -0.8); ang = 66; }
    else if (st === 'scissor') { at = V(outer, b.min.y + (b.max.y - b.min.y) * 0.4, b.max.z); axis = V(1, 0, 0); ang = 75; }
    else if (st === 'gull') { at = V(inner, b.max.y, midZ); axis = V(0, 0, 1); ang = 75; }
    else { at = V(outer, midY, st === 'rear' ? b.min.z : b.max.z); axis = V(0, 1, 0); ang = k === 'chargePort' ? 95 : 60; dir = 'out'; }
    var sd = k === 'hood' || k === 'trunk' ? 1 : side;
    return moved(hinge(ms, moveAt(at.clone(), p, b, sd), axis.normalize(), (p.angle || ang) * DEG * sign, dir), at, b, sd);
  }
  function moveAt(at, p, b, side) {
    if (!p.piv) return at;
    var sz = b.getSize(V(0, 0, 0));
    return at.add(V(p.piv[0] * sz.x * side, p.piv[1] * sz.y, p.piv[2] * sz.z));
  }
  function moved(h, base, b, side) { h.base = base; h.size = b.getSize(V(0, 0, 0)); h.side = side; return h; }

  function slider(k, ms, p) {
    var b = new T.Box3();
    ms.forEach(function (m) { b.expandByObject(m); });
    if (p.mode === 'tilt') return moved(hinge(ms, moveAt(V(0, b.max.y, b.min.z), p, b, 1), V(1, 0, 0), (p.angle || 7) * DEG * (p.flip ? -1 : 1), 'up'), V(0, b.max.y, b.min.z), b, 1);
    var pivot = new T.Group();
    pivot.userData.d3move = 1;
    root.add(pivot);
    root.updateMatrixWorld(true);
    ms.forEach(function (m) { pivot.attach(m); });
    return { pivot: pivot, slide:V(0, (b.max.y - b.min.y) * 0.15 + 0.004, -(b.max.z - b.min.z) * (p.travel || 0.85)), meshes: ms };
  }
  function hinge(meshes, at, axis, angle, dir) {
    var pivot = new T.Group();
    pivot.userData.d3move = 1;
    pivot.position.copy(at);
    root.add(pivot);
    root.updateMatrixWorld(true);
    meshes.forEach(function (m) { pivot.attach(m); });
    var bb = new T.Box3();
    meshes.forEach(function (m) { bb.expandByObject(m); });
    var c = bb.getCenter(V(0, 0, 0)).sub(at), a = c.clone().applyAxisAngle(axis, 0.5), b = c.clone().applyAxisAngle(axis, -0.5);
    var up = dir === 'out' ? (Math.abs(a.x + at.x) > Math.abs(b.x + at.x) ? 1 : -1) : a.y > b.y ? 1 : -1;
    return { pivot: pivot, axis: axis, angle: angle * up, meshes: meshes };
  }
  function splitX(mesh) {
    var g = mesh.geometry, pos = g.attributes.position, idx = g.index.array, mw = mesh.matrixWorld, out = {}, v = V(0, 0, 0);
    var sides = { L: [], R: [] };
    for (var i = 0; i < idx.length; i += 3) {
      v.set(0, 0, 0);
      for (var j = 0; j < 3; j++) v.x += pos.getX(idx[i + j]) / 3, v.y += pos.getY(idx[i + j]) / 3, v.z += pos.getZ(idx[i + j]) / 3;
      v.applyMatrix4(mw);
      sides[v.x > 0 ? 'L' : 'R'].push(idx[i], idx[i + 1], idx[i + 2]);
    }
    SIDES.forEach(function (s) {
      var gg = g.clone();
      gg.setIndex(sides[s]);
      var m = new T.Mesh(gg, mesh.material);
      m.userData.car = 1;
      mesh.parent.add(m);
      m.position.copy(mesh.position); m.quaternion.copy(mesh.quaternion); m.scale.copy(mesh.scale);
      m.updateMatrixWorld(true);
      out[s] = m;
    });
    mesh.parent.remove(mesh);
    g.dispose();
    return out;
  }
  function shadow(WP) {
    var kx = dims.hw / 1.02, kz = dims.hl / 2.3, kr = wheelR / 0.345;
    var sw = 4.6 * kx, sl = 7.6 * kz;
    var tex = app.canvasTex(184, Math.round(184 * sl / sw), function (c, w, h) {
      var sx = w / sw, sz = h / sl;
      c.filter = 'blur(' + Math.round(w * 0.09) + 'px)';
      c.fillStyle = 'rgba(0,0,0,.18)';
      ell(c, w / 2, h / 2, 1.5 * kx * sx, 2.75 * kz * sz);
      c.filter = 'blur(' + Math.round(w * 0.045) + 'px)';
      c.fillStyle = 'rgba(0,0,0,.42)';
      rr(c, w / 2 - 0.95 * kx * sx, h / 2 - 2.2 * kz * sz, 1.9 * kx * sx, 4.4 * kz * sz, 0.45 * kx * sx);
      c.filter = 'blur(' + Math.round(w * 0.02) + 'px)';
      c.fillStyle = 'rgba(0,0,0,.55)';
      TYRES.forEach(function (t) { ell(c, w / 2 + WP[t].x * sx, h / 2 - WP[t].z * sz, 0.16 * kr * sx, 0.36 * kr * sz); });
    }, false);
    var m = new T.Mesh(new T.PlaneGeometry(sw, sl), new T.MeshBasicMaterial({ map: tex, color: 0x000000, transparent: true, depthWrite: false }));
    m.rotation.x = -Math.PI / 2; m.position.y = 0.002;
    return m;
  }
  function ell(c, x, y, rx, ry) { c.beginPath(); c.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2); c.fill(); }
  function rr(c, x, y, w, h, r) {
    c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); c.fill();
  }
  var NOISE = 'vec3 d3h3(vec3 p) { p = fract(p * vec3(0.1031, 0.1030, 0.0973)); p += dot(p, p.yxz + 33.33); return fract((p.xxy + p.yxx) * p.zyx) - 0.5; }\n' +
    'float d3h(vec3 p) { p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }\n' +
    'float d3n(vec3 p) { vec3 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);\n' +
    '  return mix(mix(mix(d3h(i), d3h(i + vec3(1.0, 0.0, 0.0)), f.x), mix(d3h(i + vec3(0.0, 1.0, 0.0)), d3h(i + vec3(1.0, 1.0, 0.0)), f.x), f.y),\n' +
    '    mix(mix(d3h(i + vec3(0.0, 0.0, 1.0)), d3h(i + vec3(1.0, 0.0, 1.0)), f.x), mix(d3h(i + vec3(0.0, 1.0, 1.0)), d3h(i + vec3(1.0, 1.0, 1.0)), f.x), f.y), f.z); }\n';
  function paintFx(m) {
    M.fx.flake = { value: 0 }; M.fx.peel = { value: 0 }; M.fx.fb = 0;
    m.onBeforeCompile = function (sh) {
      sh.uniforms.uD3Flake = M.fx.flake; sh.uniforms.uD3Peel = M.fx.peel;
      sh.vertexShader = 'varying vec3 vD3P;\n' + sh.vertexShader.replace('#include <project_vertex>', '#include <project_vertex>\n  vD3P = (modelMatrix * vec4(transformed, 1.0)).xyz;');
      sh.fragmentShader = 'uniform float uD3Flake;\nuniform float uD3Peel;\nvarying vec3 vD3P;\n' + NOISE + sh.fragmentShader
        .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\n  if (uD3Flake > 0.0) roughnessFactor = clamp(roughnessFactor * (1.0 + uD3Flake * 9.0 * d3h3(floor(vD3P * 160.0)).x), 0.04, 1.0);')
        .replace('#include <clearcoat_normal_fragment_maps>', '#include <clearcoat_normal_fragment_maps>\n#ifdef USE_CLEARCOAT\n  if (uD3Peel > 0.0) clearcoatNormal = normalize(clearcoatNormal + uD3Peel * vec3(d3n(vD3P * 70.0) - 0.5, d3n(vD3P * 70.0 + 13.1) - 0.5, 0.0));\n#endif');
    };
    m.customProgramCacheKey = function () { return 'd3paint'; };
  }
  function lowp(r) {
    r.traverse(function (o) {
      if (o.isMesh) [].concat(o.material).forEach(function (m) {
        if (!m || !m.isMeshStandardMaterial || (m.defines && 'D3_LOWP' in m.defines)) return;
        m.defines = m.defines || {};
        m.defines.D3_LOWP = '';
        m.needsUpdate = true;
      });
    });
  }
  function mirror() {
    var R = Math.max(dims.hl, dims.hw) + 1.2, sz = reflSize(), sp = app.rtSpec();
    refl = new T.Reflector(new T.PlaneGeometry(R * 2, R * 2), {
      textureWidth: sz.x, textureHeight: sz.y, clipBias: 0.003, multisample: sp.samples,
      type: sp.type,
      shader: {
        uniforms: { color: { value: null }, tDiffuse: { value: null }, textureMatrix: { value: null }, uK: { value: 0.2 }, uR: { value: R }, uPx: { value: new T.Vector2(0.5 / sz.x, 0.5 / sz.y) } },
        vertexShader: 'uniform mat4 textureMatrix;\nvarying vec4 vUv;\nvarying vec2 vP;\nvoid main() { vUv = textureMatrix * vec4(position, 1.0); vP = position.xy; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
        fragmentShader: 'uniform sampler2D tDiffuse;\nuniform float uK;\nuniform float uR;\nuniform vec2 uPx;\nvarying vec4 vUv;\nvarying vec2 vP;\nvoid main() {\n' +
          '  vec2 uv = vUv.xy / vUv.w, o = uPx;\n' +
          '  vec4 c = (texture2D(tDiffuse, uv + o) + texture2D(tDiffuse, uv - o) + texture2D(tDiffuse, uv + vec2(o.x, -o.y)) + texture2D(tDiffuse, uv + vec2(-o.x, o.y))) * 0.25;\n' +
          '  gl_FragColor = vec4(c.rgb / max(c.a, 1e-4), c.a * uK * (1.0 - smoothstep(0.25, 0.8, length(vP) / uR)));\n  #include <tonemapping_fragment>\n  #include <encodings_fragment>\n}'
      }
    });
    refl.material.transparent = true; refl.material.depthWrite = false;
    refl.rotation.x = -Math.PI / 2; refl.renderOrder = -3;
    refl.camera.layers.enable(1);
    var ob = refl.onBeforeRender, last = -1;
    refl.onBeforeRender = function (a, b, c2) { if (last === fid) return; last = fid; ob.call(this, a, b, c2); };
    return refl;
  }
  function reflSize() {
    var pr = app.renderer().getPixelRatio(), w = W * pr, h = H * pr, k = Math.min(1, 2048 / Math.max(w, h, 1));
    return { x: Math.max(64, Math.round(w * k)), y: Math.max(64, Math.round(h * k)) };
  }
  function reflResize() {
    if (!refl) return;
    var sz = reflSize(), rt = refl.getRenderTarget();
    if (rt.width !== sz.x || rt.height !== sz.y) rt.setSize(sz.x, sz.y);
    refl.material.uniforms.uPx.value.set(0.5 / sz.x, 0.5 / sz.y);
  }
  function texAvg(tex) {
    try {
      var img = tex.image, cv = document.createElement('canvas'), x = cv.getContext('2d'), d, r = 0, g = 0, b = 0;
      if (!img || !img.width) return null;
      cv.width = cv.height = 4;
      x.drawImage(img, 0, 0, 4, 4);
      d = x.getImageData(0, 0, 4, 4).data;
      for (var i = 0; i < 64; i += 4) { r += d[i]; g += d[i + 1]; b += d[i + 2]; }
      return new T.Color(r / 16 / 255, g / 16 / 255, b / 16 / 255).convertSRGBToLinear();
    } catch (e) { return null; }
  }
  function finish(animate) {
    if (!M) return;
    finishFrom = { paint: M.paint.color.clone(), pm: M.paint.metalness, pr: M.paint.roughness, pc: M.paint.clearcoat, pcr: M.paint.clearcoatRoughness, pi: M.paint.iridescence, pf: M.fx.fb, pp: M.fx.peel.value,
      rim: M.rims.map(function (m) { return { c: m.color.clone(), m: m.metalness, r: m.roughness }; }), cal: M.cals.map(function (m) { return m.color.clone(); }) };
    finishK = animate ? 0 : 1;
    tint(finishK);
    if (app) app.invalidate();
  }
  function tint(k) {
    var p = S.pick('paint'), r = S.pick('rim'), c = S.pick('caliper'), e = k * k * (3 - 2 * k), f = finishFrom;
    M.paint.color.copy(f.paint).lerp(new T.Color(p.color), e);
    M.paint.metalness = f.pm + (p.metal - f.pm) * e;
    M.paint.roughness = f.pr + (p.rough - f.pr) * e;
    M.paint.clearcoat = f.pc + ((p.coat === undefined ? 1 : p.coat) - f.pc) * e;
    M.paint.clearcoatRoughness = f.pcr + (p.ccr - f.pcr) * e;
    M.paint.iridescence = f.pi + (p.iri - f.pi) * e;
    M.paint.iridescenceThicknessRange = p.thick;
    var on = S.fx().flake;
    M.fx.fb = f.pf + ((on ? p.flake : 0) - f.pf) * e;
    M.fx.peel.value = f.pp + ((on && p.coat > 0.3 ? 0.035 : 0) - f.pp) * e;
    var map = p.model && M.orig ? M.orig.map : M.keepMap && M.orig ? M.orig.map : null;
    if ((M.paint.map || null) !== map) { M.paint.map = map; M.paint.needsUpdate = true; }
    M.rims.forEach(function (m, i) {
      var o = f.rim[i], t = r.model && M.rim0[i] ? M.rim0[i] : { c: new T.Color(r.color), m: r.metal, r: r.rough };
      m.color.copy(o.c).lerp(t.c, e); m.metalness = o.m + (t.m - o.m) * e; m.roughness = o.r + (t.r - o.r) * e;
    });
    M.cals.forEach(function (m, i) { m.color.copy(f.cal[i]).lerp(c.model && M.cal0[i] ? M.cal0[i] : new T.Color(c.color), e); });
  }

  function apply(c) {
    var n = c['display.dayNightMode'];
    if (n) { try { localStorage.setItem(KEY_DARK, n === 'night' ? '1' : '0'); } catch (e) {} }
    nightData = n ? n === 'night' : null;
    theme(wantDark());
    var gear = c['driving.gear'];
    var still = editing;
    var sp = c['driving.speed'];
    drv.target = typeof sp === 'number' ? (gear === 'R' ? -sp : sp) : 0;
    drive(!still && S.get('drive') && (gear === 'D' || gear === 'R'));
    topView(!still && gear === 'D' && !drv.on);
    TYRES.forEach(function (w) {
      var st = c['tyre.' + w + '.state'], p = c['tyre.' + w + '.pressure'];
      tyreBad[w] = p === 'burst' || p === 'fault' || st === 'critical' ? 2 : st && st !== 'normal' ? 1 : 0;
      setText($('tp-' + w).querySelector('b'), pressure(p, c['tyre.displayUnit']));
      var tt = c['tyre.' + w + '.temperature'], ti = $('tp-' + w).querySelector('i'), tx = typeof tt === 'number' ? '胎温 ' + Math.round(tt) : '';
      if (ti.textContent !== tx) ti.textContent = tx;
    });
    tyreTour();
    text(c);
    if (!root) return;
    doorWatch(c);
    Object.keys(DOORS).forEach(function (k) { want[k] = (!still && c[DOORS[k]] === 'open') || force[k] ? 1 : 0; });
    Object.keys(LIDS).forEach(function (k) { want[k] = (!still && c[LIDS[k]] === 'open') || force[k] ? 1 : 0; });
    Object.keys(WINS).forEach(function (k) { var w = still ? 0 : c[WINS[k]]; want[k] = force[k] ? 1 : typeof w === 'number' ? Math.max(0, Math.min(1, w / 100)) : 0; });
    Object.keys(SLIDES).forEach(function (k) { var w = still ? 0 : c[SLIDES[k]]; want[k] = force[k] ? 1 : typeof w === 'number' ? Math.max(0, Math.min(1, w / 100)) : 0; });
    want.chargePort = force.chargePort || (!still && c[FLAPS.chargePort] && c[FLAPS.chargePort] !== 'none') ? 1 : 0;
    var low = !still && c['light.lowBeam'] === true, high = !still && c['light.highBeam'] === true, lit = low || high, side = !still && c['light.side'] === true;
    var pw = c['power.level'], on = pw === 'on' || pw === 'ready' || pw === 'fakeReady', powered = !pw || on, ab = c['light.ambient.brightness'], drl = c['light.daytimeRunning'];
    if (!still && on && lastPw !== undefined && lastPw !== pw && !(lastPw === 'on' || lastPw === 'ready' || lastPw === 'fakeReady')) welT = 0;
    lastPw = pw;
    lightOn = {
      head: lit ? 1 : 0, high: high ? 1 : 0, tail: lit || side ? 1 : 0, side: side ? 1 : 0, led: !still && (drl === undefined ? lit : drl === true && powered) ? 1 : 0,
      brake: !still && (c['light.brake'] === 'on' || c['light.brake'] === 'flash') ? 1 : 0,
      reverse: !still && c['driving.gear'] === 'R' ? 1 : 0, fogF: !still && c['light.frontFog'] === true ? 1 : 0, fogR: !still && c['light.rearFog'] === true ? 1 : 0,
      ambient: !still && powered ? (typeof ab === 'number' ? Math.max(0.25, Math.min(1, ab <= 5 ? ab / 5 : ab <= 10 ? ab / 10 : ab / 100)) : 0.8) : 0
    };
    brakeFlash = !still && c['light.brake'] === 'flash';
    ambientHex = AMBIENT[c['light.ambient.color']] || null;
    ambArea = c['light.ambient.area'] || 'all';
    colorful = c['light.dynamicColorful'] === true;
    var mm = c['light.ambient.musicMode'] === true;
    if (mm !== musicOn) {
      musicOn = mm;
      page.needs = mm ? ['car', 'media'] : ['car'];
      if (window.D3) D3.needs(page);
    }
    turnHz = !still && c['light.hazard'] === true;
    var now = Date.now();
    SIDES.forEach(function (s) {
      var lit = !still && c[s === 'L' ? 'light.turnSignal.left' : 'light.turnSignal.right'] === true;
      if (lit && !bulb[s] && now - offAt[s] < 1200) pulsed = true;
      if (!lit && bulb[s]) offAt[s] = now;
      if (still) blinkAt[s] = 0;
      else if (lit || bulb[s]) blinkAt[s] = now;
      bulb[s] = lit;
    });
    turnState();
    TYRES.forEach(function (w) {
      var bad = tyreBad[w], ring = wheels[w].ring;
      ring.visible = bad > 0;
      ring.material.color.set(bad === 2 ? 0xff3b30 : 0xff9a1f);
      ring.material.opacity = bad ? 0.9 : 0;
    });
    var k3 = JSON.stringify([want, lightOn, brakeFlash, ambientHex, ambArea, colorful, musicOn, turnHz, bulb, pulsed, tyreBad, drv.on, drv.target]);
    if (k3 !== applyKey || root !== applyRoot || welT === 0) { applyKey = k3; applyRoot = root; app.invalidate(); }
  }
  var applyKey = '', applyRoot = null;
  function wantDark() { var t = S.get('theme'); return t === 'dark' ? true : t === 'light' ? false : nightData !== null ? nightData : guessDark(); }
  function topView(on) {
    if (on === top.on) return;
    top.on = on;
    top.last = 0;
    view.homing = true; view.vaz = 0;
    if (app) app.invalidate();
  }

  function guessDark() {
    try { var v = localStorage.getItem(KEY_DARK); if (v === '1' || v === '0') return v === '1'; } catch (e) {}
    var h = new Date().getHours();
    return h >= 19 || h < 6;
  }
  var turnOn = { L: false, R: false }, bulb = { L: false, R: false }, blinkAt = { L: 0, R: 0 }, offAt = { L: 0, R: 0 }, turnHz = false, pulsed = false, brakeFlash = false;
  var WELCOME = { head: 1, tail: 1, led: 1, side: 1 };
  function lightsTick(wf) {
    if (!LS) return false;
    var now = Date.now(), moving = false, ph = now % 760, blink = ph < 420, tOn = blinking && blink, L = (cfg && cfg.lights) || {};
    var tl = { L: blink && ((blinking && turnOn.L) || !!forceLight.turnL), R: blink && ((blinking && turnOn.R) || !!forceLight.turnR) }, q = LS.fbQ, bw = !!(cfg && cfg.borrow);
    turnU.value.set(bw && q.LF && tl.L ? 1 : 0, bw && q.RF && tl.R ? 1 : 0, bw && q.LB && tl.L ? 1 : 0, bw && q.RB && tl.R ? 1 : 0);
    var borrowing = turnU.value.x + turnU.value.y + turnU.value.z + turnU.value.w > 0, tc = lampRgb('turnL');
    if (bw && (forceLight.turnL || forceLight.turnR)) moving = true;
    var claim = {}, mute = {};
    LAMP_PRI.forEach(function (r) {
      if (!LS.has(r)) return;
      var turn = r === 'turnL' || r === 'turnR';
      if (!(turn ? !!forceLight[r] || (blinking && turnOn[r.slice(4)]) : Math.max(lightOn[r] || 0, WELCOME[r] ? wf : 0, forceLight[r] ? 1 : 0) > 0.003)) return;
      if (!turn) { LS.srcs(r).forEach(function (u) { if (!claim[u]) claim[u] = r; }); return; }
      var src = {};
      LS.srcs(r).forEach(function (u) { src[u] = 1; });
      LIGHTS.forEach(function (q) {
        if (q === 'turnL' || q === 'turnR' || !LS.has(q) || !LS.srcs(q).some(function (u) { return src[u]; })) return;
        (mute[q] = mute[q] || [0, 0])[r === 'turnL' ? 0 : 1] = 1;
      });
    });
    LIGHTS.forEach(function (r) {
      if (!LS.has(r)) return;
      var fx = M.fx[r], k, flow = 1;
      if (r === 'turnL' || r === 'turnR') {
        var s = r.slice(4), on = forceLight[r] ? blink : tOn && turnOn[s];
        k = on ? 1 : 0;
        if (forceLight[r] || (fx === 'flow' && on)) moving = true;
        if (fx === 'flow') flow = Math.min(1, ph / 300);
      } else {
        k = Math.max(lightOn[r] || 0, WELCOME[r] ? wf : 0, forceLight[r] ? 1 : 0);
        if (k > 0 && fx === 'breath') { k *= 0.5 + 0.5 * Math.sin(now / 1000 * 2.4); moving = true; }
        if (k > 0 && fx === 'blink') { k = blink ? k : 0; moving = true; }
        if (r === 'brake' && brakeFlash && lightOn.brake && !forceLight.brake) { k = now % 250 < 125 ? k : 0; moving = true; }
      }
      var byCar = r === 'ambient' && lightOn.ambient > 0 && !forceLight.ambient;
      if (byCar && musicOn && playing) { k *= beat(now); moving = true; }
      var lend = borrowing && TURN_FB.indexOf(r) >= 0;
      if (k <= 0.003 && !lend && !LS.built(r)) return;
      var o = LS.get(r);
      if (!o) return;
      var col = null;
      if (r === 'ambient' && !(L.ambient && L.ambient.color)) {
        if (byCar && colorful) { col = '#' + hueCol.setHSL(now / 9000 % 1, 0.9, 0.6).getHexString(); moving = true; } else col = ambientHex;
      }
      var rgb = lampRgb(r, col), g = D3Lamp.glow(L[r] && L[r].glow, r);
      o.u.uTint.value.set(rgb[0], rgb[1], rgb[2]);
      o.u.uTurnCol.value.set(tc[0], tc[1], tc[2]);
      o.u.uGlow.value.set(g.intensity, g.core, g.detail, g.softness);
      o.u.uFlow.value = flow;
      o.u.uBorrow.value = lend ? 1 : 0;
      o.u.uMute.value.set(mute[r] ? mute[r][0] : 0, mute[r] ? mute[r][1] : 0);
      var zone = byCar && ambArea !== 'all' ? (ambArea === 'front' ? 'R' : 'F') : '';
      for (var z in o.mats) o.mats[z].uniforms.uOn.value = zone && z === zone ? 0 : k;
      var isTurn = r === 'turnL' || r === 'turnR';
      o.meshes.forEach(function (m) { var c = claim[m.userData.src]; m.visible = (isTurn || !c || c === r) && (lend || (k > 0.003 && !(zone && m.userData.z === zone))); });
    });
    return moving;
  }
  function beamsTick(dt) {
    if (!LS) return false;
    var moving = false, bc = LS.bc;
    Object.keys(D3Lamp.BEAMS).forEach(function (slot) {
      var on = slot === 'low' ? Math.max(lightOn.head || 0, forceLight.head ? 1 : 0) : Math.max(lightOn[slot] || 0, forceLight[slot] ? 1 : 0);
      var a = bc[slot] || 0;
      if (Math.abs(a - on) > 0.002) { a += (on - a) * (1 - Math.exp(-dt * 6)); moving = true; } else a = on;
      bc[slot] = a;
      LS.beam(slot, a);
    });
    return moving;
  }

  function beat(now) {
    var t = now % 2000 / 500, i = Math.floor(t), f = t - i;
    return 0.3 + (i === 0 ? 0.7 : 0.5) * (1 - f) * (1 - f);
  }
  function onMedia(m) {
    var p = !!(m && m.playing);
    if (p !== playing) {
      playing = p;
      if (app && musicOn) app.invalidate();
    }
    npShow(m); npPaint(m);
  }
  var npHideTimer = null;
  function npShow(m) {
    m = m || D.media();
    if (S.get('showMusic') && m && (m.title || m.trackId || m.lyric || m.coverUrl)) {
      $('np').style.display = '';
      if (npHideTimer) { clearTimeout(npHideTimer); npHideTimer = null; }
    } else if (!npHideTimer) npHideTimer = setTimeout(function () { $('np').style.display = 'none'; npHideTimer = null; }, 5000);
  }
  function setPlayIcon(m) {
    var btn = $('mcPlay');
    if (!btn) return;
    var playing = !!(m && m.playing);
    var ip = btn.querySelector('.ic-play'), ipa = btn.querySelector('.ic-pause');
    if (ip) ip.style.display = playing ? 'none' : '';
    if (ipa) ipa.style.display = playing ? '' : 'none';
    btn.setAttribute('aria-label', playing ? '暂停' : '播放');
  }
  var npLines = [];
  function drawLyric(m) {
    if (!m || !npLines.length) { $('npLyric').textContent = m ? (m.lyric || '') : ''; return; }
    var cur = 0, rawLyric = (m.lyric || '').trim();
    if (rawLyric) {
      for (var i = 0; i < npLines.length; i++) { if ((npLines[i].text || '').trim() === rawLyric) { cur = i; break; } }
    } else {
      var t = m.positionMs || 0;
      for (var i = 0; i < npLines.length; i++) if (npLines[i].timeMs <= t) cur = i;
    }
    var n = S.get('npLines') || 5;
    var html = '';
    for (var j = cur; j < Math.min(npLines.length, cur + n); j++) {
      if (j === cur) html += '<div class="now">' + npLines[j].text + '</div>';
      else { var op = Math.max(0.6, 1 - (j - cur) * 0.12); html += '<div style="opacity:' + op + '">' + npLines[j].text + '</div>'; }
    }
    var lyricEl = $('npLyric');
    lyricEl.style.opacity = '0';
    setTimeout(function () { lyricEl.innerHTML = html; lyricEl.style.opacity = '1'; }, 200);
  }
  function npPaint(m) {
    m = m || D.media();
    setText($('npTitle'), m ? (m.title || '—') : '—');
    setText($('npArtist'), m ? (m.artist || '') : '');
    if ($('npCover')) $('npCover').src = m && m.coverUrl ? m.coverUrl : '';
    if (m && m.trackId) D.lyrics().then(function (ls) { npLines = ls || []; drawLyric(m); });
    else npLines = [];
    drawLyric(m);
    setPlayIcon(m);
  }
  var mediaBound = false;
  function bindMedia() {
    if (mediaBound || !$('mcPlay')) return;
    mediaBound = true;
    $('mcPrev').addEventListener('click', function () { D.send('previous'); });
    $('mcNext').addEventListener('click', function () { D.send('next'); });
    $('mcPlay').addEventListener('click', function () {
      var mm = D.media();
      if (mm && mm.playing !== undefined) D.send(mm.playing ? 'pause' : 'play');
      else if (D.can('togglePlayPause')) D.send('togglePlayPause');
      else D.send('play');
    });
    setPlayIcon(D.media());
  }
  function turnState() {
    var now = Date.now();
    SIDES.forEach(function (s) { turnOn[s] = turnHz || bulb[s] || (pulsed && now - blinkAt[s] < 1600); });
    blinking = turnOn.L || turnOn.R;
  }
  function pressure(v, u) {
    if (v === 'burst') return '爆胎';
    if (v === 'fault') return '异常';
    if (typeof v !== 'number') return '—';
    return u === 'psi' ? Math.round(v / 6.894757) + ' psi' : u === 'kPa' ? Math.round(v) + ' kPa' : (v / 100).toFixed(1) + ' bar';
  }
  function lims(c) {
    var el = $('cRoadLim');
    if (!el) return;
    var r = c['limit.road'], m = c['limit.cam'];
    var showR = S.get('showRoad') !== false && !portrait && typeof r === 'number' && r > 0;
    el.classList.toggle('on', showR);
    if (showR) $('cRoadLimV').textContent = String(r);
    var el2 = $('cCamLim');
    var showC = S.get('showCam') !== false && !portrait && typeof m === 'number' && m > 0;
    el2.classList.toggle('on', showC);
    if (showC) $('cCamLimV').textContent = String(m);
  }
  function adas(c) {
    var adEl = $('cAdas');
    if (!adEl) return;
    var accS = c['adas.acc.state'], iccS = c['adas.icc.state'];
    var accOn = typeof accS === 'number' && accS > 0, iccOn = typeof iccS === 'number' && iccS > 0;
    setText($('adasAcc'), accOn ? String(accS) : '—');
    setText($('adasIcc'), iccOn ? String(iccS) : '—');
    $('adasA').style.display = accOn ? '' : 'none';
    $('adasI').style.display = iccOn ? '' : 'none';
    $('adasDot').style.display = (accOn && iccOn) ? '' : 'none';
    adEl.style.display = (S.get('showAdas') && (accOn || iccOn)) ? '' : 'none';
  }
  function acPaint(c) {
    var el = $('cAc');
    if (!el) return;
    var acPw = c['ac.power'], acTp = c['ac.driverTemperature'], acFn = c['ac.fanLevel'], acCir = c['ac.circulation'];
    setText($('acOnW'), acPw === undefined ? '—' : (acPw === true ? '开' : '关'));
    $('acOn').className = 'ac-tg' + (acPw === true ? ' on' : '');
    setText($('acTemp'), typeof acTp === 'number' ? Math.round(acTp) + '°' : '—');
    setText($('acFan'), typeof acFn === 'number' ? String(acFn) : '—');
    var cir = (acCir === 'inside' || acCir === true || acCir === 1) ? 'inside' : ((acCir === 'outside' || acCir === false || acCir === 0) ? 'outside' : null);
    setText($('acCircW'), cir === 'inside' ? '内' : (cir === 'outside' ? '外' : '—'));
    $('acCirc').className = 'ac-tg' + (cir === 'inside' ? ' on' : '');
  }
  function acBlockHtml() {
    return '<span>空调</span><div class="ac"><button type="button" class="ac-tg" id="acOn"><span id="acOnW">—</span></button>'
      + '<button type="button" class="ac-pick" id="acTempBtn"><i>温度</i><b id="acTemp">—</b></button>'
      + '<button type="button" class="ac-pick" id="acFanBtn"><b id="acFan">—</b><i>风量</i></button>'
      + '<button type="button" class="ac-tg" id="acCirc"><span id="acCircW">—</span></button></div>';
  }
  function mediaBlockHtml() {
    return '<span>音乐</span><div class="mc">'
      + '<button type="button" class="mc-btn" id="mcPrev" aria-label="上一首"><svg viewBox="0 0 24 24"><path d="M6.5 5.5h2.2v13H6.5z M18.5 6.2v11.6L9.8 12z"></path></svg></button>'
      + '<button type="button" class="mc-btn mc-main" id="mcPlay" aria-label="播放"><svg class="ic-play" viewBox="0 0 24 24"><path d="M8.5 5.8v12.4L18.6 12z"></path></svg><svg class="ic-pause" viewBox="0 0 24 24" style="display:none"><path d="M7.2 5.5h3.4v13H7.2z M13.4 5.5h3.4v13h-3.4z"></path></svg></button>'
      + '<button type="button" class="mc-btn" id="mcNext" aria-label="下一首"><svg viewBox="0 0 24 24"><path d="M15.3 5.5h2.2v13h-2.2z M5.5 6.2v11.6L14.2 12z"></path></svg></button></div>';
  }
  var LUNAR_INFO = [0x04bd8,0x04ae0,0x0a570,0x054d5,0x0d260,0x0d950,0x16554,0x056a0,0x09ad0,0x055d2,
    0x04ae0,0x0a5b6,0x0a5b0,0x14970,0x052b0,0x052b0,0x0a950,0x0e954,0x06aa0,0x0aea6,
    0x0ab00,0x09b20,0x0b2d0,0x0d550,0x0d6a0,0x0ada0,0x095b0,0x149b0,0x04970,0x064b0,
    0x064b0,0x086a0,0x06aa0,0x0ada0,0x095b0,0x149b0,0x04970,0x064b0,0x064b0,0x086a0,
    0x06aa0,0x0ada0,0x095b0,0x149b0,0x04970,0x064b0,0x064b0,0x086a0,0x06aa0,0x0ada0,
    0x095b0,0x149b0,0x04970,0x064b0,0x064b0,0x086a0,0x06aa0,0x0ada0,0x095b0,0x149b0];
  function yearDays(y) { var sum = 348; for (var i = 0x8000; i > 0x8; i >>= 1) { if ((LUNAR_INFO[y - 1900] & i) !== 0) sum++; } return sum + leapDays(y); }
  function leapDays(y) { if (leapMonth(y) !== 0) { return (LUNAR_INFO[y - 1900] & 0x10000) !== 0 ? 30 : 29; } return 0; }
  function leapMonth(y) { return LUNAR_INFO[y - 1900] & 0xf; }
  function monthDays(y, m) { return ((LUNAR_INFO[y - 1900] & (0x10000 >> m)) !== 0) ? 30 : 29; }
  function lunar(d) {
    try {
      var baseDate = new Date(1900, 0, 31);
      var offset = Math.floor((d - baseDate) / 86400000);
      var i, temp = 0;
      for (i = 1900; i < 2050 && offset > 0; i++) { temp = yearDays(i); offset -= temp; }
      if (offset < 0) { offset += temp; i--; }
      var year = i;
      var leap = leapMonth(i);
      var isLeap = false;
      for (i = 1; i < 13 && offset > 0; i++) {
        if (leap > 0 && i === (leap + 1) && !isLeap) { --i; isLeap = true; temp = leapDays(year); }
        else temp = monthDays(year, i);
        if (isLeap && i === (leap + 1)) isLeap = false;
        offset -= temp;
      }
      if (offset === 0 && leap > 0 && i === leap + 1) { if (isLeap) { isLeap = false; } else { isLeap = true; --i; } }
      if (offset < 0) { offset += temp; --i; }
      var month = i;
      var day = offset + 1;
      return (isLeap ? '闰' : '') + '农历' + month + '月' + day;
    } catch (e) { return ''; }
  }
  function dtPaint() {
    var now = new Date();
    var y = now.getFullYear(), m = now.getMonth() + 1, d = now.getDate();
    var h = now.getHours(), mi = now.getMinutes();
    setText($('dtDate'), y + '年' + m + '月' + d + '日');
    setText($('dtTime'), (h < 10 ? '0' + h : h) + ':' + (mi < 10 ? '0' + mi : mi));
    setText($('dtLunar'), lunar(now));
    $('dtDate').style.display = S.get('showDate') ? '' : 'none';
    $('dtLunar').style.display = S.get('showLunar') ? '' : 'none';
    $('dtTime').style.display = S.get('showTime') ? '' : 'none';
    var dtEl = $('cDatetime');
    if (dtEl) dtEl.style.fontSize = 'var(--dt-fs, ' + S.get('dtScale') + 'rem)';
  }
  function text(c) {
    var gear = c['driving.gear'], gi = $('cGears').children;
    for (var i = 0; i < gi.length; i++) setCls(gi[i], gi[i].textContent === gear ? 'on' : '');
    var sp = c['driving.speed'], moving = typeof sp === 'number' && (sp > 0 || gear === 'D' || gear === 'R');
    if ($('cSpeed').style.visibility !== (moving ? 'visible' : 'hidden')) $('cSpeed').style.visibility = moving ? 'visible' : 'hidden';
    setText($('cSpeed').firstChild, String(moving ? Math.round(sp) : 0));
    var pw = c['power.level'];
    setText($('cPower'), POWER[pw] || '—');
    setCls($('cPower'), 'chip' + (pw === 'on' || pw === 'ready' || pw === 'fakeReady' ? ' live' : ''));
    if ($('cDemo').style.display !== (D.demo ? '' : 'none')) $('cDemo').style.display = D.demo ? '' : 'none';
    var er = c['energy.electricRange'], fr = c['energy.fuelRange'], has = typeof er === 'number' || typeof fr === 'number';
    var num = function (v) { return typeof v === 'number' ? Math.round(v) : null; };
    stats();
    S.get('stats').forEach(function (id) {
      var e = $(STAT[id] ? STAT[id][0] : 'cS-' + id);
      if (!STAT[id]) { var sv = CS.stat(id, c, D.unit); stat(e, sv ? sv[0] : null, sv ? sv[1] : ''); return; }
      if (id === 'bat') energy(e, '电量', c['energy.batteryPercent'], c['energy.chargingState'] === 'charging' ? '电量 · 充电中' : '');
      else if (id === 'fuel') energy(e, '油量', c['energy.fuelPercent'], '');
      else if (id === 'range') stat(e, has ? Math.round((typeof er === 'number' ? er : 0) + (typeof fr === 'number' ? fr : 0)) : null, 'km');
      else if (id === 'eRange') stat(e, num(er), 'km');
      else if (id === 'fRange') stat(e, num(fr), 'km');
      else if (id === 'temp') stat(e, num(c['ac.outdoorTemperature']), '°C');
      else if (id === 'inTemp') stat(e, num(c['ac.indoorTemperature']), '°C');
      else if (id === 'odo') stat(e, num(c['driving.totalDistance']), 'km');
    });
    lims(c);
    adas(c);
    acPaint(c);
    var mc = $('cMediaCtl');
    if (mc) mc.style.display = S.get('showMediaCtl') ? '' : 'none';
    showStatus(statuses(c));
    telltales(c);
    roofState(c);
  }
  var ttKey = '';
  function setText(e, t) { if (e.textContent !== t) e.textContent = t; }
  function setCls(e, c) { if (e.className !== c) e.className = c; }
  function telltales(c) {
    var t = CS.telltales(c), l = portrait ? rank(t.L.concat(t.R)) : t.L, r = portrait ? [] : t.R, key = l.join('') + '|' + r.join('');
    if (key === ttKey) return;
    ttKey = key;
    $('ttL').innerHTML = l.join(''); $('ttR').innerHTML = r.join('');
    fit($('ttL')); fit($('ttR'));
  }
  function fit(box) {
    var kids = box.children, lim = portrait ? box.clientWidth : box.clientHeight;
    for (var i = 0; i < kids.length; i++) {
      var e = kids[i], end = portrait ? e.offsetLeft + e.offsetWidth : e.offsetTop + e.offsetHeight;
      if (end > lim + 1) e.style.display = 'none';
    }
  }
  function rank(l) {
    var k = function (h) { var m = /^<div class="tti([^"]*)"/.exec(h), c = m ? m[1] : ''; return /\bbad\b/.test(c) ? 0 : /\bwarn\b/.test(c) ? 1 : 2; };
    return l.map(function (h, i) { return [k(h), i, h]; }).sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; }).map(function (x) { return x[2]; });
  }
  var sysKey = null;
  function sysIcons(s) { var h = CS.sys(s || {}); if (h !== sysKey) { sysKey = h; $('cSys').innerHTML = h; } }
  var LOW_RANGE = 50, LOW_BAT = 20, LOW_FUEL = 10, stList = [], stOpen = false;
  var ABBR = { frontLeft: 'FL', frontRight: 'FR', rearLeft: 'RL', rearRight: 'RR' };
  function statuses(c) {
    var L = [], add = function (lv, t, go) { L.push({ lv: lv, t: t, go: go || null }); };
    var gp = function (keys) { var k = keys.filter(function (x) { return parts && parts[x]; }); return k.length ? { parts: k } : null; };
    var gt = function (ws) { return wheels && ws.length ? { tyres: ws.slice() } : null; };
    var gear = c['driving.gear'], sp = c['driving.speed'], moving = gear === 'D' || gear === 'R' || (typeof sp === 'number' && sp > 0);
    var tyre = { burst: [], fault: [], low: [], high: [], warn: [] }, tw = { burst: [], fault: [], low: [], high: [], warn: [] }, anyP = false;
    TYRES.forEach(function (w) {
      var p = c['tyre.' + w + '.pressure'], st = c['tyre.' + w + '.state'];
      var g = p === 'burst' ? 'burst' : p === 'fault' || st === 'critical' ? 'fault' : st === 'low' ? 'low' : st === 'high' ? 'high' : st && st !== 'normal' ? 'warn' : '';
      if (typeof p === 'number') anyP = true;
      if (g) { tyre[g].push(TYRE_NAME[w]); tw[g].push(w); }
    });
    if (tyre.burst.length) add(3, tyre.burst.join('、') + '爆胎', gt(tw.burst));
    if (tyre.fault.length) add(3, tyre.fault.join('、') + '胎压异常', gt(tw.fault));
    if (c['warning.lowVoltageBatteryFault'] === true) add(3, '低压供电异常');
    if (moving && c['seat.driver.beltFastened'] === false) add(3, '主驾没系安全带');
    if (tyre.low.length) add(2, tyre.low.join('、') + '胎压偏低', gt(tw.low));
    if (tyre.high.length) add(2, tyre.high.join('、') + '胎压偏高', gt(tw.high));
    if (tyre.warn.length) add(2, tyre.warn.join('、') + '胎压异常', gt(tw.warn));
    var open = [], ok = [], wins = [], wk = [];
    Object.keys(DOORS).forEach(function (k) { if (c[DOORS[k]] === 'open') { open.push(DOOR_NAME[DOORS[k]]); ok.push(k); } });
    if (c['body.hood.state'] === 'open') { open.push('前机舱盖'); ok.push('hood'); }
    if (c['body.tailgate.state'] === 'open') { open.push('尾门'); ok.push('trunk'); }
    if (open.length) add(2, open.join('、') + '开着', gp(ok));
    TYRES.forEach(function (w) { var v = c['body.windows.' + w + '.position']; if (typeof v === 'number' && v > 0) { wins.push(TYRE_NAME[w] + ' ' + Math.round(v) + '%'); wk.push(parts && parts['win' + ABBR[w]] ? 'win' + ABBR[w] : 'door' + ABBR[w]); } });
    if (wins.length) add(2, '车窗开着：' + wins.join('、'), gp(wk));
    else if (c['body.windows.anyOpen'] === true) add(2, '车窗开着');
    var sr = c['body.sunroof.position'];
    if (typeof sr === 'number' && sr > 0) add(2, '天窗开了 ' + Math.round(sr) + '%', gp(['sunroof', 'sunshade']));
    if (c['warning.keyBatteryLow'] === true) add(2, '钥匙电池电量低');
    var cs = c['energy.chargingState'], con = c['energy.connectorState'], bat = c['energy.batteryPercent'], fuel = c['energy.fuelPercent'];
    var er = c['energy.electricRange'], fr = c['energy.fuelRange'], range = typeof er === 'number' || typeof fr === 'number' ? (typeof er === 'number' ? er : 0) + (typeof fr === 'number' ? fr : 0) : null;
    var port = gp(['chargePort']);
    if (cs === 'terminated') add(2, '充电中断', port);
    if (cs !== 'charging' && range !== null && range < LOW_RANGE) add(2, '续航只剩 ' + Math.round(range) + ' km');
    else if (cs !== 'charging' && typeof bat === 'number' && bat < LOW_BAT) add(2, '电量低 ' + Math.round(bat) + '%');
    if (typeof fuel === 'number' && fuel < LOW_FUEL && !(range !== null && range < LOW_RANGE)) add(2, '油量低 ' + Math.round(fuel) + '%');
    if (cs === 'charging') {
      var rt = c['energy.charging.remainingTime'], left = '';
      if (Array.isArray(rt) && rt.length === 2 && typeof rt[0] === 'number' && typeof rt[1] === 'number' && rt[0] * 60 + rt[1] > 0) left = ' · 还要 ' + (rt[0] ? rt[0] + ' 小时 ' : '') + rt[1] + ' 分';
      add(1, '充电中' + (typeof bat === 'number' ? ' ' + Math.round(bat) + '%' : '') + left, port);
    } else if (cs === 'finished') add(1, '已充满', port);
    else if (con === 'vtol') add(1, '对外放电中', port);
    else if (con === 'ac' || con === 'dc' || con === 'acDc') add(1, '插着充电枪', port);
    if (Object.keys(c).length && !L.some(function (x) { return x.lv >= 2; })) add(0, '车门车窗都关好了' + (anyP ? ' · 胎压正常' : ''));
    L.sort(function (x, y) { return y.lv - x.lv; });
    return L;
  }
  var ST_CLS = ['', ' info', ' warn', ' bad'];
  function showStatus(L) {
    stList = L;
    var st = $('cStatus'), top = L.length ? L[0].lv : 0, shown = L.filter(function (x) { return x.lv === top; }).slice(0, 2), more = L.length - shown.length;
    var msg = shown.map(function (x) { return x.t; }).join(' · ');
    var sp = st.querySelector('.c-pill span'), mb = st.querySelector('.c-pill b');
    if (sp.textContent !== msg) sp.textContent = msg;
    setText(mb, more > 0 ? '+' + more : '');
    if (L.length < 2) stOpen = false;
    setCls(st, 'c-msg' + ST_CLS[top] + (msg ? '' : ' none') + (L.length > 1 ? ' more' : L.length && L[0].go ? ' go' : '') + (stOpen ? ' open' : ''));
    var list = st.querySelector('.c-list'), html = stOpen ? L.map(function (x, i) { return '<p class="lv' + x.lv + (x.go ? ' go" data-i="' + i : '') + '"><i></i><span>' + x.t + '</span></p>'; }).join('') : '';
    if (list.innerHTML !== html) list.innerHTML = html;
  }
  function stToggle(on) { stOpen = on && stList.length > 1; showStatus(stList); }
  $('cStatus').querySelector('.c-pill').addEventListener('click', function (e) {
    e.stopPropagation();
    if (stList.length > 1) stToggle(!stOpen); else if (stList.length && stList[0].go) lookAt(stList[0].go);
  });
  $('cStatus').querySelector('.c-list').addEventListener('click', function (e) {
    var r = e.target.closest && e.target.closest('p[data-i]'), x = r && stList[+r.getAttribute('data-i')];
    e.stopPropagation();
    if (x && x.go && lookAt(x.go)) stToggle(false);
  });
  $('cAll').addEventListener('click', function (e) {
    e.stopPropagation(); stToggle(false);
    (window.D3CarInfo ? Promise.resolve() : loadJs('carinfo.js')).then(function () { D3CarInfo.open(); }, function () { D3.toast('全部车况没加载出来'); });
  });
  var jsLoad = {};
  function loadJs(src) {
    if (!jsLoad[src]) jsLoad[src] = new Promise(function (ok, no) {
      var sc = document.createElement('script');
      sc.src = src; sc.onload = ok;
      sc.onerror = function () { delete jsLoad[src]; no(new Error(src + ' 加载失败')); };
      document.head.appendChild(sc);
    });
    return jsLoad[src];
  }
  document.addEventListener('pointerdown', function (e) { if (stOpen && !(e.target.closest && e.target.closest('#cStatus'))) stToggle(false); }, true);

  function stat(e, v, unit) {
    var b = e.querySelector('b'), h = v === null ? '—' : v + '<small>' + unit + '</small>';
    if (b.getAttribute('data-h') !== h) { b.setAttribute('data-h', h); b.innerHTML = h; }
  }
  function energy(e, name, v, label) {
    var ok = typeof v === 'number';
    setText(e.querySelector('span'), label || name);
    stat(e, ok ? Math.round(v) : null, '%');
    var f = e.querySelector('.bar i'), tf = 'scaleX(' + (ok ? Math.max(0, Math.min(100, v)) / 100 : 0) + ')';
    if (f.getAttribute('data-t') !== tf) { f.setAttribute('data-t', tf); f.style.transform = tf; }
    setCls(f, !ok ? '' : v < 20 ? 'low' : v < 50 ? 'mid' : '');
  }
  var CS = window.D3CarStat;
  var STAT = { bat: ['cBat', '电量'], fuel: ['cFuel', '油量'], range: ['cRange', '总续航'], eRange: ['cERange', '纯电续航'], fRange: ['cFRange', '燃油续航'],
    temp: ['cTemp', '车外'], inTemp: ['cInTemp', '车内'], odo: ['cOdo', '总里程'], ac: ['cAc', '空调'], media: ['cMediaCtl', '音乐'] };
  function stats() {
    var box = el.querySelector('.stats'), l = S.get('stats'), key = l.join();
    if (box.getAttribute('data-k') === key) return;
    box.setAttribute('data-k', key);
    box.className = 'stats n' + l.length;
    box.innerHTML = '';
    l.forEach(function (id) {
      var d = document.createElement('div');
      var o = CS.STATS.filter(function (x) { return x[0] === id; })[0];
      if (id === 'ac') { d.className = 'st st-ac'; d.id = 'cAc'; d.innerHTML = acBlockHtml(); }
      else if (id === 'media') { d.className = 'st st-media'; d.id = 'cMediaCtl'; d.innerHTML = mediaBlockHtml(); }
      else {
        d.className = 'st'; d.id = STAT[id] ? STAT[id][0] : 'cS-' + id;
        d.innerHTML = '<span>' + (STAT[id] ? STAT[id][1] : o[1]) + '</span><b>—</b>' + (id === 'bat' || id === 'fuel' ? '<i class="bar"><i></i></i>' : '');
      }
      box.appendChild(d);
    });
    if (l.indexOf('media') >= 0) bindMedia();
    D.extra(l.map(CS.field).filter(Boolean));
  }
  var bgUrl = '', hasBg = false;
  D3Model.bgGet().then(function (b) { hasBg = !!b; });
  function background() {
    var b = S.get('bg'), bgEl = el.querySelector('.bg');
    ['warm', 'cool', 'image'].forEach(function (x) { el.classList.toggle('bg-' + x, b === x); });
    el.style.setProperty('--dim', S.get('bgDim'));
    if (b === 'image' && !bgUrl) D3Model.bgGet().then(function (blob) {
      if (!blob || S.get('bg') !== 'image' || bgUrl) return;
      bgUrl = URL.createObjectURL(blob);
      bgEl.style.backgroundImage = 'url(' + bgUrl + ')';
    });
    if (b !== 'image' && bgUrl) { bgEl.style.backgroundImage = ''; URL.revokeObjectURL(bgUrl); bgUrl = ''; }
  }
  function pickBg() {
    var f = $('fileBg');
    f.value = '';
    f.onchange = function () {
      if (!f.files || !f.files[0]) return;
      D3Model.bgSet(f.files[0]).then(function () {
        hasBg = true;
        if (bgUrl) { URL.revokeObjectURL(bgUrl); bgUrl = ''; el.querySelector('.bg').style.backgroundImage = ''; }
        if (S.get('bg') === 'image') background(); else S.set('bg', 'image');
        D3.toast('背景换好了');
      }, function (e) { D3.toast(e && e.user ? e.message : '这张图片用不了'); });
    };
    f.click();
  }
  function modelUI() {
    if (!rec) return;
    $('setModelName').textContent = rec.name + (rec.custom && rec.builtin ? '（调整过）' : '');
    $('setModelCredit').textContent = rec.broken ? '导入的车模读不出来，先用默认车模' : rec.credit;
    $('setModelReset').disabled = rec.builtin && !rec.custom && !rec.broken;
    [['Paint', M && M.has.paint, '车漆', '车身'], ['Rim', M && M.rims.length, '轮毂', '轮毂'], ['Caliper', M && M.cals.length, '卡钳', '卡钳']].forEach(function (r) {
      var e = $('set' + r[0] + 'Tip');
      e.textContent = !M || r[1] ? '' : '这个车模还没标出' + r[2] + '，换色不生效（在"调整车模"里点一下' + r[3] + '标上）';
      e.style.display = e.textContent ? '' : 'none';
    });
  }
  function homeView() { HOME.az = S.get('viewAz'); HOME.el = S.get('viewEl'); }
  function theme(d) {
    if (d === dark) return;
    dark = d;
    el.classList.toggle('dark', d);
    appEl.classList.toggle('car-dark', d);
    if (root) app.invalidate();
  }
  function look() {
    var cd = camera.position.length(), dk = ease(drv.k);
    scene.fog.color.copy(FOG_DAY).lerp(FOG_NIGHT, themeK);
    scene.fog.near = 900 - (900 - cd - 6) * dk;
    scene.fog.far = 1000 - (1000 - cd - 58) * dk;
    app.renderer().toneMappingExposure = (1 - 0.15 * themeK) * expK * (1 - 0.82 * nightK);
    keyLight.intensity = std ? 0 : 1.3 - 0.6 * themeK;
    M.paint.envMapIntensity = (0.85 - 0.15 * themeK) * envK;
    if (refl) refl.material.uniforms.uK.value = 0.2 + 0.14 * themeK;
  }

  page.update = function (dt) {
    if (!root) return 0;
    var moving = false;
    fid++;
    for (var k in want) {
      var a = cur[k] === undefined ? want[k] : cur[k], b = want[k];
      if (Math.abs(a - b) > 0.002) { a += (b - a) * (1 - Math.exp(-dt * 5)); moving = true; shadowDirty = true; } else a = b;
      if (a !== cur[k]) shadowDirty = true;
      cur[k] = a;
    }
    if (shadowDirty && keyLight.castShadow) { app.renderer().shadowMap.needsUpdate = true; shadowDirty = false; }
    Object.keys(parts).forEach(function (k) {
      var p = parts[k], v = cur[k] || 0;
      if (p.slide) p.pivot.position.copy(p.slide).multiplyScalar(ease(v));
      else if (p.pivot) p.pivot.quaternion.setFromAxisAngle(p.axis, p.angle * ease(v));
      else p.meshes.forEach(function (m, i) { m.position.y = p.y0[i] - p.drop * v; });
    });
    var tk = dark ? 1 : 0, nk = nightW ? 1 : 0;
    if (Math.abs(themeK - tk) > 0.002) { themeK += (tk - themeK) * (1 - Math.exp(-dt * 7)); moving = true; } else themeK = tk;
    if (Math.abs(nightK - nk) > 0.002) { nightK += (nk - nightK) * (1 - Math.exp(-dt * 6)); moving = true; } else nightK = nk;
    look();
    if (intro.t < 1) {
      if (intro.wait > 0) intro.wait--;
      else intro.t = Math.min(1, intro.t + dt / INTRO.sec);
      moving = true;
    }
    var wf = intro.t > 0.35 && intro.t < 0.95 ? Math.sin((intro.t - 0.35) / 0.6 * Math.PI) : 0;
    if (welT < 1) { welT = Math.min(1, welT + dt / 1.1); wf = Math.max(wf, Math.sin(welT * Math.PI)); moving = true; }
    turnState();
    var lightAnim = lightsTick(wf);
    if (beamsTick(dt)) moving = true;
    if (finishK < 1) { finishK = Math.min(1, finishK + dt / 0.6); tint(finishK); moving = true; }
    var sw = (showOn || dbgOn) && !editing ? 1 : 0, aw = showOn && !editing && !preview ? 1 : 0, tw = editing ? 1 : 0;
    if (Math.abs(showK - sw) > 0.002) { showK += (sw - showK) * (1 - Math.exp(-dt * 4.5)); moving = true; } else showK = sw;
    if (Math.abs(angK - aw) > 0.002) { angK += (aw - angK) * (1 - Math.exp(-dt * 4.5)); moving = true; } else angK = aw;
    if (Math.abs(tuneK - tw) > 0.002) { tuneK += (tw - tuneK) * (1 - Math.exp(-dt * 4.5)); moving = true; } else tuneK = tw;
    if (!editing && tuneK === 0) { zoom = 1; pan.set(0, 0, 0); }
    if (flashes.length) {
      flashT += Math.min(dt, 0.05);
      if (flashT > 1.5) page.flash([]);
      else { flashMat.opacity = flashT % 0.5 < 0.3 ? 0.85 : 0; moving = true; }
    }
    lookTick();
    if (driveStep(dt)) moving = true;
    var qt = topNow() ? 1 : 0;
    var pv = pin && pin.v, ft = pv ? pv.at : tour ? tour.at : doorNow() ? doorV.at : null, fz = pv ? pv.zoom : tour ? tour.zoom : doorNow() ? doorV.zoom : focusZ, fw = ft ? 1 : 0;
    if (ft) {
      if (!focusAt || focusK < 0.002) { focusAt = ft.clone(); focusZ = fz; }
      else if (focusAt.distanceToSquared(ft) > 1e-6) { focusAt.lerp(ft, 1 - Math.exp(-dt * 2.4)); moving = true; } else focusAt.copy(ft);
    }
    if (Math.abs(focusZ - fz) > 0.001) { focusZ += (fz - focusZ) * (1 - Math.exp(-dt * 2.4)); moving = true; } else focusZ = fz;
    var fl = tour && !pv ? 1 : 0.85;
    if (Math.abs(focusL - fl) > 0.001) { focusL += (fl - focusL) * (1 - Math.exp(-dt * 2.4)); moving = true; } else focusL = fl;
    if (Math.abs(focusK - fw) > 0.002) { focusK += (fw - focusK) * (1 - Math.exp(-dt * 2.4)); moving = true; } else focusK = fw;
    if (Math.abs(top.k - qt) > 0.002) { top.k += (qt - top.k) * (1 - Math.exp(-dt * 2.4)); moving = true; } else top.k = qt;
    labelsOn = S.get('labels') && intro.t > 0.8 && drv.k < 0.05 && (showK < 0.05 || dbgOn) && tuneK < 0.05;
    var hm = home();
    if (driving() && !view.homing && drv.last && Date.now() - drv.last > 6000 && (Math.abs(wrap(hm.az - view.az)) > 0.01 || Math.abs(hm.el - view.el) > 0.01)) view.homing = true;
    if (topNow() && !view.homing && top.last && Date.now() - top.last > 6000 && (Math.abs(wrap(hm.az - view.az)) > 0.01 || Math.abs(hm.el - view.el) > 0.01)) view.homing = true;
    if (view.homing) {
      var da = wrap(hm.az - view.az), de = hm.el - view.el, k2 = 1 - Math.exp(-dt * (qt || top.k > 0.01 || driving() || drv.k > 0.01 || focusK > 0.01 || ft ? 2.4 : 6));
      view.az += da * k2; view.el += de * k2;
      if (Math.abs(da) < 0.002 && Math.abs(de) < 0.002) { view.az = hm.az; view.el = hm.el; view.homing = false; } else moving = true;
    } else if (Math.abs(view.vaz) > 0.0004) {
      view.az += view.vaz * dt * 60;
      view.vaz *= Math.exp(-dt * 3.2);
      moving = true;
    } else view.vaz = 0;
    place();
    if (later) {
      if (!editing && tuneK === 0 && !view.homing) { var L = later; later = null; L.run().then(L.res, L.res); } else moving = true;
    }
    if (moving || lightAnim) movedAt = Date.now();
    page.still = !moving && !editing && !refl;
    return moving ? 60 : lightAnim ? 30 : blinking ? 8 : 0;
  };
  var WELCOME = ['head', 'led', 'side', 'tail'], warmT = 0, touchAt = 0, movedAt = 0;
  function warmLamps(g) {
    clearTimeout(warmT);
    if (!LS || editing) return;
    var did = false;
    if (intro.t < 1) WELCOME.forEach(function (r) { if (LS.has(r) && !LS.built(r)) { LS.get(r); did = true; } });
    if (did) { try { app.renderer().compile(scene, camera); } catch (e) {} }
    var beams = Object.keys(D3Lamp.BEAMS);
    warmT = setTimeout(function step() {
      if (g !== gen || !LS || editing || !scene) return;
      var now = Date.now();
      if (intro.t < 1 || now - touchAt < 1500 || now - movedAt < 800 || document.hidden) { warmT = setTimeout(step, 500); return; }
      var r = LIGHTS.filter(function (x) { return LS.has(x) && !LS.built(x); })[0];
      if (r) LS.get(r);
      else if (beams.length) LS.warmBeam(beams.shift());
      else { try { app.renderer().compile(scene, camera); } catch (e) {} return; }
      warmT = setTimeout(step, 300);
    }, 1200);
  }
  function tyreTour() {
    var ch = false;
    TYRES.forEach(function (w) {
      if (tyreBad[w]) { if (!tyreSeen[w]) { tyreSeen[w] = 1; tyreNew.push(w); tyreOff = false; tyreAt = Date.now(); ch = true; } }
      else if (tyreSeen[w]) { delete tyreSeen[w]; ch = true; }
    });
    if (root && ch) app.invalidate();
  }
  function canLook() { return !!(root && intro.t >= 1 && !editing && !showOn && !document.hidden && !guideOn() && !driving()); }
  function guideOn() { var g = $('guide'); return !!(g && g.classList.contains('on')); }
  function canTour() { return !!(wheels && canLook()); }
  function lookTick() {
    var now = Date.now(), bad = TYRES.filter(function (w) { return tyreBad[w]; });
    if (guideOn() && (tour || doorV || pin)) {
      tour = null; pin = null; clearTimeout(tourTimer);
      if (doorV) { doorV = null; doorDue = now; }
      view.homing = true; view.vaz = 0;
    }
    if (pin) {
      if (pin.tyres) pin.tyres = pin.tyres.filter(function (w) { return tyreBad[w]; });
      if (pin.tyres ? !pin.tyres.length : !pin.parts.some(function (k) { return (want[k] || 0) > 0.02; })) { pin = null; view.homing = true; view.vaz = 0; }
    }
    if (((bad.length && !tour) || doorDue) && !canLook()) doorWake(1000);
    if (doorDue && now >= doorDue && canLook()) { doorDue = 0; doorFrame(); }
    if (doorEnd && now >= doorEnd) { doorEnd = 0; if (doorV) { doorV = null; view.homing = true; view.vaz = 0; } }
    var list = !canTour() ? [] : pin ? pin.tyres || [] : bad.length && !tyreOff && !top.on && !preview && !(doorAuto() && doorAt > tyreAt + 1500) ? bad : [];
    tyreNew = tyreNew.filter(function (w) { return tyreBad[w] && !(tour && tour.w === w); });
    if (!list.length) { if (tour) { tour = null; clearTimeout(tourTimer); view.homing = true; view.vaz = 0; } return; }
    var i = tour ? list.indexOf(tour.w) : -1, next = function () { var j = 0; while (j < tyreNew.length && list.indexOf(tyreNew[j]) < 0) j++; return j < tyreNew.length ? tyreNew.splice(j, 1)[0] : ''; };
    if (i < 0) setTour(next() || list[0], list.length);
    else if (list.length > 1 && now > tour.until) setTour(next() || list[(i + 1) % list.length], list.length);
  }
  function setTour(w, n) {
    var at = wheels[w].c.clone().applyMatrix4(root.matrixWorld);
    clearTimeout(tourTimer);
    tour = { w: w, at: at, zoom: TYRE_ZOOM, until: Date.now() + TOUR_HOLD + 1200, view: { az: Math.atan2(at.x, at.z), el: 0.3 } };
    view.homing = true; view.vaz = 0;
    if (n > 1) tourTimer = setTimeout(function () { if (root) app.invalidate(); }, TOUR_HOLD + 1250);
  }
  function stopLook() {
    if (tour || pin) { tour = null; pin = null; clearTimeout(tourTimer); }
    tyreOff = true;
    if (doorV || doorDue) { doorV = null; doorDue = doorEnd = 0; }
  }
  function lookAt(go) {
    if (!go || !root || !canLook()) return false;
    if (go.tyres) {
      var l = go.tyres.filter(function (w) { return wheels && wheels[w]; });
      if (!l.length) return false;
      pin = { tyres: l }; tour = null;
    } else {
      var f = frameOf(go.parts);
      if (!f) return false;
      pin = { parts: go.parts, v: f }; tour = null;
    }
    view.homing = true; view.vaz = 0; app.invalidate();
    return true;
  }
  var OPENS = { doorFL: DOORS.doorFL, doorFR: DOORS.doorFR, doorRL: DOORS.doorRL, doorRR: DOORS.doorRR, hood: LIDS.hood, trunk: LIDS.trunk };
  function doorWatch(c) {
    var open = Object.keys(OPENS).filter(function (k) { return parts[k] && c[OPENS[k]] === 'open'; });
    Object.keys(doorSeen).forEach(function (k) { if (open.indexOf(k) < 0) delete doorSeen[k]; });
    var fresh = open.filter(function (k) { return !doorSeen[k]; });
    fresh.forEach(function (k) { doorSeen[k] = 1; });
    doorOpen = open;
    if (fresh.length) { doorAt = doorBoot ? 0 : Date.now(); doorDue = Date.now() + 900; doorEnd = 0; doorWake(950); }
    else if (!open.length && doorV && !doorEnd) { doorEnd = Date.now() + 1500; doorWake(1550); }
    doorBoot = false;
  }
  function doorWake(ms) { clearTimeout(doorTimer); doorTimer = setTimeout(function () { if (root) app.invalidate(); }, ms); }
  function frameOf(keys) {
    if (!parts || !keys || !keys.length) return null;
    var sx = Math.sin(HOME.az) < 0 ? -1 : 1, sum = V(0, 0, 0), at = V(0, 0, 0), el = 0, n = 0;
    page.atRest(function () {
      keys.forEach(function (k) {
        var p = parts[k], b = new T.Box3();
        if (!p) return;
        (p.meshes || []).forEach(function (m) { b.expandByObject(m); });
        if (b.isEmpty()) return;
        var c = b.getCenter(V(0, 0, 0)), d, e;
        if (k === 'hood') { d = V(sx * 0.5, 0, 1); e = 0.5; }
        else if (k === 'trunk') { d = V(sx * 0.5, 0, -1); e = 0.45; }
        else if (k === 'sunroof' || k === 'sunshade') { d = V(sx * 0.6, 0, -1); e = 0.85; }
        else if (/^(door|win)/.test(k)) { d = V(p.side || (c.x > 0 ? 1 : -1), 0, -0.8); e = 0.34; }
        else { d = V(c.x, 0, c.z); if (d.lengthSq() < 1e-6) d.set(sx, 0, 0); e = 0.3; }
        sum.add(d.normalize()); at.add(c);
        el = Math.max(el, e);
        n++;
      });
    });
    if (!n) return null;
    var len = sum.length(), spread = 1 - len / n;
    var f = { keys: keys.slice(), at: at.multiplyScalar(1 / n), want: 0.22 * (1 - spread), view: { az: len > 0.15 ? Math.atan2(sum.x, sum.z) : Math.PI, el: Math.min(0.9, el + spread * 0.45) } };
    f.zoom = fitZoom(f.view, f.at, f.want, true);
    return f;
  }
  function doorFrame() {
    var f = frameOf(doorOpen);
    if (!f) return;
    doorV = f;
    view.homing = true; view.vaz = 0;
    app.invalidate();
  }
  function doorAuto() { return !!doorV && !top.on && !preview && !editing && !driving(); }
  function doorNow() { return !pin && !tour && doorAuto(); }
  function fitZoom(v, at, want, wide) {
    if (!fitA || !camera || !root) return want;
    var keep = [view.az, view.el, focusK, focusAt, focusZ, intro.t, showK, angK, tuneK, top.k, focusL], pts = [], hw = dims.hw + (wide ? 0.4 : 0.05), lo = -0.4, hi = want;
    for (var i = 0; i < 8; i++) pts.push(V(i & 1 ? hw : -hw, i & 2 ? dims.h : 0, i & 4 ? dims.hl : -dims.hl).applyMatrix4(root.matrixWorld));
    view.az = v.az; view.el = v.el; focusK = 1; focusAt = at; intro.t = 1; showK = angK = tuneK = 0; top.k = 0; focusL = 0.85;
    var ok = function (z) {
      focusZ = z; place();
      var mx = 0, mn = 1, my = -1;
      pts.forEach(function (c) { var q = c.clone().project(camera); mx = Math.max(mx, Math.abs(q.x)); my = Math.max(my, q.y); mn = Math.min(mn, q.y); });
      return mx <= fitA.aw && my <= fitA.yc + fitA.ah && mn >= fitA.yc - fitA.ah;
    };
    if (!ok(want)) { for (var it = 0; it < 14; it++) { var m = (lo + hi) / 2; if (ok(m)) lo = m; else hi = m; } want = lo; }
    view.az = keep[0]; view.el = keep[1]; focusK = keep[2]; focusAt = keep[3]; focusZ = keep[4]; intro.t = keep[5]; showK = keep[6]; angK = keep[7]; tuneK = keep[8]; top.k = keep[9]; focusL = keep[10];
    place();
    return want;
  }
  function topNow() { return top.on && !preview && !tour && !(pin && pin.v) && !driving(); }
  function home() {
    if (editing && lookTo) return lookTo;
    if (pin && pin.v) return pin.v.view;
    if (tour) return tour.view;
    if (driving()) { CHASE.el = S.get('camPitch') * Math.PI / 180; return CHASE; }
    if (topNow()) return TOP;
    return doorNow() ? doorV.view : HOME;
  }
  function ease(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }
  function wrap(a) { while (a > Math.PI) a -= Math.PI * 2; while (a < -Math.PI) a += Math.PI * 2; return a; }
  function place() {
    var r = 1 - easeOut(intro.t), qk = ease(top.k), dk = ease(drv.k), sk = showK * showK * (3 - 2 * showK), ak = angK * angK * (3 - 2 * angK), tk = tuneK * tuneK * (3 - 2 * tuneK), ok = Math.max(sk, tk);
    var fov = S.get('viewFov') + (S.get('camFov') - S.get('viewFov')) * dk;
    var az = view.az + r * intro.rot * INTRO.az, el = view.el + r * intro.rot * INTRO.el;
    var d = view.dist * (1 + r * INTRO.dist) * (1 - qk) * (1 - dk) + top.dist * qk + chaseDist(S.get('camFov')) * S.get('camDist') * dk;
    if (sk > 0) { az += wrap(SHOW.az - az) * ak; el += (SHOW.el - el) * ak; d *= 1 + (portrait ? 0.3 : 0.42) * sk; }
    if (tk > 0) d *= (1 + (portrait ? (compact ? 0.3 : 0.6) : 0.75) * tk) * (1 + (1 / zoom - 1) * tk);
    var ty = dims.h * (0.357 - 0.157 * qk + 0.0794 * dk), tz = S.get('camAhead') * dk, ce = Math.cos(el);
    aim.set(pan.x * tk, ty + pan.y * tk, tz + pan.z * tk);
    if (focusK > 0 && focusAt && !driving()) { var fk = ease(focusK); aim.lerp(focusAt, fk * focusL); d *= 1 - focusZ * fk; }
    if (camera.fov !== fov) camera.fov = fov;
    if (M && M.fx.fb) { var px = 0.00625 * H / (2 * d * Math.tan(fov * Math.PI / 360)); M.fx.flake.value = M.fx.fb * Math.max(0, Math.min(1, (px - 1.5) / 1.5)); }
    else if (M) M.fx.flake.value = 0;
    camera.position.set(aim.x + Math.sin(az) * ce * d, aim.y + Math.sin(el) * d, aim.z + Math.cos(az) * ce * d);
    camera.lookAt(aim);
    camera.updateMatrixWorld();
    var showY = portrait ? (H / 2 - offY0) - (remPx * 1.15 + H * (tk > 0 && compact ? 0.54 : 0.36) - remPx * 0.16) / 2 : 0;
    camera.setViewOffset(W, H, (sk * 0.2 + tk * 0.23) / Math.max(1, sk + tk) * (portrait ? 0 : W), offY0 + ok * showY, W, H);
  }
  var pv = V(0, 0, 0), tagMax = null;
  page.after = function () {
    roofPlace();
    if (!wheels) return;
    var cx = camera.position.x, rm = app.rem();
    if (tagMax === null) { var row = document.querySelector('#cStatus .c-row'), lp = $('tp-frontLeft').offsetParent; tagMax = row && lp ? row.getBoundingClientRect().top - lp.getBoundingClientRect().top - rm * 0.22 : 0; if (tagMax <= 0) tagMax = H; }
    TYRES.forEach(function (w) {
      var wh = wheels[w], lb = wh.label, far = cx * wh.side < -0.25 * view.dist;
      var bad = tyreBad[w], cls = 'tp' + (bad === 2 ? ' bad' : bad ? ' warn' : '') + (far || !labelsOn ? ' off' : '');
      if (lb.className !== cls) lb.className = cls;
      if (far) return;
      pv.copy(wh.at).applyMatrix4(root.matrixWorld).project(camera);
      lb.style.transform = 'translate3d(' + ((pv.x + 1) / 2 * W).toFixed(1) + 'px,' + Math.min((1 - pv.y) / 2 * H + rm * 0.38, tagMax).toFixed(1) + 'px,0)';
    });
  };

  var roofBtn = $('roofAc'), roofWant = null, roofTimer = 0;
  function acApi() { var v = window.uiplus && window.uiplus.vehicle; return v && v.ac && typeof v.ac.setPower === 'function' ? v.ac : null; }
  function roofState(c) {
    var on = c['ac.power'] === true;
    if (roofWant !== null && roofWant === on) { roofWant = null; clearTimeout(roofTimer); }
    var show = roofWant !== null ? roofWant : on, cls = 'roof-btn' + (show ? ' on' : '') + (roofWant !== null ? ' wait' : '') + (roofBtn.className.indexOf(' off') >= 0 ? ' off' : '');
    if (roofBtn.className !== cls) roofBtn.className = cls;
    if (roofBtn.getAttribute('aria-label') !== (show ? '关空调' : '开空调')) roofBtn.setAttribute('aria-label', show ? '关空调' : '开空调');
  }
  roofBtn.addEventListener('click', function (e) {
    var ac = acApi();
    e.stopPropagation();
    if (!ac || roofWant !== null) return;
    var to = D.car()['ac.power'] !== true;
    roofWant = to; roofState(D.car());
    clearTimeout(roofTimer);
    roofTimer = setTimeout(function () { roofWant = null; roofState(D.car()); }, 4000);
    Promise.resolve().then(function () { return ac.setPower(to); }).then(null, function (er) {
      var code = er && (er.code || er.error && er.error.code);
      roofWant = null; clearTimeout(roofTimer); roofState(D.car());
      D3.toast(code === 'PERMISSION_DENIED' ? '没开车控权限：在 Web 卡片管理页给这张卡片打开「车控」' : '空调没' + (to ? '开' : '关') + '成' + (er && er.message ? '：' + er.message : ''), 3500);
    });
  });
  function roofPlace() {
    var ok = !!(root && acApi() && intro.t > 0.8 && (showK < 0.05 || dbgOn) && tuneK < 0.05), x = 0, y = 0;
    if (ok) ok = (camera.position.y - aim.y) / (camera.position.distanceTo(aim) || 1) > 0.82;
    if (ok) {
      if (wheels) {
        TYRES.forEach(function (w) { pv.copy(wheels[w].c).applyMatrix4(root.matrixWorld).project(camera); x += (pv.x + 1) / 8 * W; y += (1 - pv.y) / 8 * H; });
      } else { pv.set(0, dims.h * 0.5, 0).applyMatrix4(root.matrixWorld).project(camera); x = (pv.x + 1) / 2 * W; y = (1 - pv.y) / 2 * H; }
      ok = pv.z < 1 && x > remPx * 0.4 && x < W - remPx * 0.4 && y > remPx * 1.1 && y < (tagMax || H) - remPx * 0.2;
    }
    if (roofBtn.hidden !== !acApi()) roofBtn.hidden = !acApi();
    var cls = roofBtn.className.replace(/ off/g, '') + (ok ? '' : ' off');
    if (roofBtn.className !== cls) roofBtn.className = cls;
    if (ok) roofBtn.style.transform = 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0)';
  }

  page.resize = function (w, h, p) {
    if (portrait !== p || W !== w || H !== h) { portrait = p; ttKey = ''; setTimeout(function () { telltales(D.car()); }, 0); }
    W = w; H = h; portrait = p; tagMax = null;
    if (!camera) return;
    reflResize();
    var r = remPx = app.rem(), padT = (p ? 2.3 : 1.05) * r, padB = (p ? 2.75 : 2.0) * r, cy = (padT + h - padB) / 2;
    camera.aspect = w / h;
    offY0 = h / 2 - cy;
    camera.setViewOffset(w, h, 0, offY0, w, h);
    var ah = (h - padT - padB) / h * 0.92, corners = [], tags = [], it0 = intro.t, sk0 = showK, ak0 = angK, qk0 = top.k, dk0 = drv.k, tk0 = tuneK, az = view.az, el0 = view.el;
    intro.t = 1; showK = angK = 0; tuneK = 0; drv.k = 0;
    for (var i = 0; i < 8; i++) corners.push(V(i & 1 ? dims.hw : -dims.hw, i & 2 ? dims.h : 0, i & 4 ? dims.hl : -dims.hl));
    if (wheels) TYRES.forEach(function (w) { var a = wheels[w].at; tags.push(a, V(a.x + wheels[w].side * 0.55, a.y, a.z)); });
    function fit(v, k, pts, aw) {
      var lo = 2, hi = 60;
      top.k = k; view.az = v.az; view.el = v.el;
      for (var it = 0; it < 22; it++) {
        view.dist = top.dist = (lo + hi) / 2;
        place();
        camera.updateProjectionMatrix();
        var mx = 0, mn = 1, my = -1;
        pts.forEach(function (c) { var q = c.clone().project(camera); mx = Math.max(mx, Math.abs(q.x)); my = Math.max(my, q.y); mn = Math.min(mn, q.y); });
        if (mx > aw || (my - mn) / 2 > ah) lo = view.dist; else hi = view.dist;
      }
      return hi;
    }
    var dTop = fit(TOP, 1, corners.concat(tags), p ? 0.9 : 0.8);
    view.dist = fit(HOME, 0, corners, p ? 0.94 : 0.86) / S.get('viewZoom');
    top.dist = dTop;
    intro.t = it0; showK = sk0; angK = ak0; top.k = qk0; drv.k = dk0; tuneK = tk0; view.az = az; view.el = el0;
    fitA = { ah: ah, aw: p ? 0.94 : 0.86, yc: 1 - 2 * cy / h };
    if (doorV) doorV.zoom = fitZoom(doorV.view, doorV.at, doorV.want, true);
    if (pin && pin.v) pin.v.zoom = fitZoom(pin.v.view, pin.v.at, pin.v.want, true);
    place();
    camera.updateProjectionMatrix();
  };

  page.grab = function (x, y) {
    touchAt = Date.now();
    if (!root) return false;
    if (editing) return true;
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    return ray.ray.intersectsBox(hitBox);
  };
  var stroke = false;
  page.press = function (x, y) {
    if (editing && hingeHit(x, y)) { hdrag = { p: hmark.position.clone() }; return; }
    if (editing && plateHit(x, y)) { pdrag = { p: plates[pkey].getWorldPosition(V(0, 0, 0)) }; return; }
    if (editing && wrapHit(x, y) && window.D3Tune) { var gw = D3Tune.wrapGrab(wkey); if (gw) wdrag = { w: gw.w, i: gw.i, p: wmark.position.clone(), at0: gw.w.at.slice() }; return; }
    if (editing && wdraw && lineStart(x, y)) return;
    if (editing && window.D3Sel && D3Sel.active() && D3Sel.painting() && page.pickAt(x, y)) { stroke = true; D3Sel.press(x, y); }
  };
  page.drag = function (dx, dy, x, y) {
    if (hdrag) { hingeMove(x, y); return; }
    if (pdrag) { plateMove(x, y); return; }
    if (wdrag) { wrapMove(x, y); return; }
    if (wline) { lineMove(x, y); return; }
    if (stroke) { D3Sel.drag(x, y); return; }
    view.homing = false; view.vaz = 0; top.last = Date.now(); drv.last = Date.now();
    stopLook();
    if (intro.t < 1 && intro.rot) { var r = 1 - easeOut(intro.t); view.az += r * INTRO.az; view.el += r * INTRO.el; intro.rot = 0; }
    view.az -= dx * 0.009;
    view.el = Math.max(0.04, Math.min(Math.max(top.k > 0.5 ? 1.3 : 0.62, view.el), view.el + dy * 0.004));
    hint();
  };
  page.release = function (vx, x, y, cancel) {
    if (hdrag) { hdrag = null; hTapAt = Date.now(); if (cancel) showHinge(); else hingeDone(); return; }
    if (pdrag) { pdrag = null; pTapAt = Date.now(); if (cancel) page.plates(cfg.plates); else plateDone(); return; }
    if (wdrag) {
      wTapAt = Date.now();
      cancelAnimationFrame(wRaf); wRaf = 0;
      if (cancel) { wdrag.w.at = wdrag.at0; wdrag = null; page.wraps(); } else { wrapDone(); wdrag = null; }
      return;
    }
    if (wline) { wTapAt = Date.now(); if (cancel) dropLine(); else lineDone(); return; }
    if (stroke) { stroke = false; if (cancel) D3Sel.cancel(); else D3Sel.release(); return; }
    view.vaz = -vx * 0.13; top.last = Date.now(); drv.last = Date.now(); app.invalidate();
    clearTimeout(top.back);
    if (top.on) top.back = setTimeout(function () { if (app && root) app.invalidate(); }, 6100);
  };
  page.tap = function (x, y) {
    if (Date.now() - hTapAt < 350 || Date.now() - pTapAt < 350) return;
    if (editing && pkey && plateHit(x, y)) return;
    if (editing && wkey) {
      if (Date.now() - wTapAt < 350 || wrapHit(x, y)) return;
      var hp = wrapPick(x, y);
      if (hp) { if (window.D3Tune) D3Tune.wrapTap(hp); return; }
    }
    if (editing && !page.pickAt(x, y)) {
      var t0 = Date.now();
      if (t0 - lastTap < 450) { lastTap = 0; page.viewAll(); view.homing = true; view.vaz = 0; if (window.D3Tune) D3Tune.zoomed(1); return; }
      lastTap = t0;
    }
    if (editing && window.D3Sel && D3Sel.active()) {
      var md = D3Sel.mode();
      if (md === 'smart') D3Sel.tap(x, y);
      else if (md !== 'rotate') { D3Sel.press(x, y); D3Sel.release(); }
      return;
    }
    if (editing) { if (window.D3Tune) D3Tune.picked(page.pickAt(x, y)); return; }
    var t = Date.now();
    if (t - lastTap < 450) { stopLook(); view.homing = true; view.vaz = 0; app.invalidate(); lastTap = 0; } else lastTap = t;
  };
  page.wake = function (on) { if (on) app.invalidate(); };
  var asides = {};
  page.aside = function (k, on) { if (on) asides[k] = 1; else delete asides[k]; dbgOn = Object.keys(asides).length > 0; if (root) app.invalidate(); };
  page.debug = function (on) { page.aside('dbg', on); };
  page.focus = function (on) {
    showOn = on;
    if (!on && preview) { preview = false; previewDrive = false; el.classList.toggle('driving', drv.on); view.homing = true; }
    if (root) app.invalidate();
  };
  page.focusDist = function () { return camera ? camera.position.distanceTo(aim) : 10; };
  page.focusRect = function () {
    if (!root) return null;
    var q = V(0, 0, 0), x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (var i = 0; i < 8; i++) {
      q.set(i & 1 ? dims.hw : -dims.hw, i & 2 ? dims.h : 0, i & 4 ? dims.hl : -dims.hl).project(camera);
      var x = (q.x + 1) / 2 * W, y = (1 - q.y) / 2 * H;
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
    return { left: x0, top: y0, width: x1 - x0, height: y1 - y0 };
  };
  page.rectOf = function (mids) {
    if (!modelObj || !camera || !mids || !mids.length) return null;
    var all = modelObj.userData.d3.meshes, b = new T.Box3();
    mids.forEach(function (id) { var m = all[id]; if (m && m.parent && m.visible) b.expandByObject(m); });
    return rectOfBox(b);
  };
  page.beamRect = function (slot) { var m = LS && LS.mesh(slot); return m ? rectOfBox(new T.Box3().setFromObject(m)) : null; };
  page.frame = function (r, to, fill) {
    if (!editing || !r || !r.width || !r.height) return;
    var z1 = Math.max(0.6, Math.min(6, zoom * Math.min(to.width * fill / r.width, to.height * fill / r.height))), n = 12, i = 0;
    var ks = Math.pow(z1 / zoom, 1 / n), p = { x: r.left + r.width / 2, y: r.top + r.height / 2 }, dx = (to.left + to.width / 2 - p.x) / n, dy = (to.top + to.height / 2 - p.y) / n;
    (function step() {
      if (!editing || i++ >= n) return;
      page.pinch(ks, p.x, p.y, dx, dy);
      p.x += dx; p.y += dy;
      requestAnimationFrame(step);
    })();
  };
  page.rectOfCar = function (a, b) { return rectOfBox(new T.Box3(V(a[0] * dims.hw, a[1] * dims.h, a[2] * dims.hl), V(b[0] * dims.hw, b[1] * dims.h, b[2] * dims.hl))); };
  function rectOfBox(b) {
    if (!camera || b.isEmpty()) return null;
    var q = V(0, 0, 0), x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (var i = 0; i < 8; i++) {
      q.set(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, i & 4 ? b.max.z : b.min.z).project(camera);
      var x = (q.x + 1) / 2 * W, y = (1 - q.y) / 2 * H;
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
    }
    return { left: x0, top: y0, width: x1 - x0, height: y1 - y0 };
  }
  page.pointOn = function (mids, avoid) {
    var r = page.rectOf(mids), pts = [];
    if (!r) return null;
    for (var i = 1; i < 6; i++) for (var j = 1; j < 6; j++) pts.push({ x: r.left + r.width * i / 6, y: r.top + r.height * j / 6 });
    pts.sort(function (a, b) { return Math.abs(a.x - r.left - r.width / 2) + Math.abs(a.y - r.top - r.height / 2) - Math.abs(b.x - r.left - r.width / 2) - Math.abs(b.y - r.top - r.height / 2); });
    for (var k = 0; k < pts.length; k++) {
      var p = pts[k];
      if (p.x < 8 || p.y < 8 || p.x > W - 8 || p.y > H - 8) continue;
      if (avoid && p.x > avoid.left && p.x < avoid.right && p.y > avoid.top && p.y < avoid.bottom) continue;
      var h = page.pickAt(p.x, p.y);
      if (h && mids.indexOf(h.mid) >= 0) return p;
    }
    return null;
  };
  page.demo = function (on) {
    if (!scene) return Promise.resolve();
    var swap = function () { return D3Model.current().then(function (r) { rec = r; cur = {}; return parse(++gen, null, root); }).then(function () { intro.t = 1; }); };
    if (!on) return settle(swap);
    drop();
    var g = ++gen;
    rec = D3Model.def(); cur = {};
    return parse(g, null, root).then(function () { intro.t = 1; });
  };
  page.edit = function (on) {
    editing = on;
    if (!on) { force = {}; forceLight = {}; lookTo = null; page.mark([]); page.flash([]); if (window.D3Sel) D3Sel.stop(); apply(D.car()); view.homing = true; }
    if (on) { intro.t = 1; view.vaz = 0; zoom = 1; pan.set(0, 0, 0); apply(D.car()); }
    if (root) app.invalidate();
  };
  page.modelUI = modelUI;
  page.zoom = function (z) { zoom = z; if (root) app.invalidate(); };
  page.pinch = function (k, x, y, dx, dy) {
    if (!editing || !root || !camera) return zoom;
    var z1 = Math.max(0.6, Math.min(6, zoom * k)), q = V(0, 0, 0), n = camera.getWorldDirection(V(0, 0, 0));
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    if (z1 !== zoom && ray.ray.intersectPlane(new T.Plane().setFromNormalAndCoplanarPoint(n, aim), q)) {
      pan.add(q.clone().add(aim.clone().sub(q).multiplyScalar(zoom / z1)).sub(aim));
    }
    zoom = z1;
    if (dx || dy) {
      var wpp = 2 * camera.position.distanceTo(aim) * Math.tan(camera.fov * Math.PI / 360) / H, e = camera.matrixWorld.elements;
      pan.x += (-e[0] * dx + e[4] * dy) * wpp; pan.y += (-e[1] * dx + e[5] * dy) * wpp; pan.z += (-e[2] * dx + e[6] * dy) * wpp;
    }
    var lim = Math.max(dims.hl, dims.hw, dims.h) * 1.5;
    if (pan.length() > lim) pan.setLength(lim);
    place();
    app.invalidate();
    if (window.D3Tune) D3Tune.zoomed(zoom);
    return zoom;
  };
  page.look = function (az, el) {
    lookTo = typeof az === 'number' ? { az: az, el: el } : null;
    view.homing = true; view.vaz = 0;
    if (root) app.invalidate();
  };
  page.compact = function (on) { compact = !!on; if (root) app.invalidate(); };
  page.selCtx = function () {
    if (!modelObj) return null;
    return { renderer: app.renderer(), camera: camera, root: root, meshes: modelObj.userData.d3.meshes, W: function () { return W; }, H: function () { return H; },
      len: function () { return dims.hl * 2; }, invalidate: function () { app.invalidate(); }, changed: function (n) { if (window.D3Tune && D3Tune.selChanged) D3Tune.selChanged(n); } };
  };
  page.viewAll = function () { zoom = 1; pan.set(0, 0, 0); if (root) app.invalidate(); };
  page.flash = function (mids, at) {
    flashes.forEach(function (o) { if (o.parent) o.parent.remove(o); });
    flashes = [];
    if (!modelObj || !mids || !mids.length) return;
    var hsl = M.paint.color.getHSL({});
    flashMat.color.setHSL(hsl.s < 0.2 ? 0.52 : (hsl.h + 0.5) % 1, 1, 0.55);
    var all = modelObj.userData.d3.meshes;
    mids.forEach(function (id) {
      var m = all[id];
      if (!m || m.isSkinnedMesh) return;
      var o = m.isInstancedMesh ? new T.InstancedMesh(m.geometry, flashMat, m.count) : new T.Mesh(m.geometry, flashMat);
      if (m.isInstancedMesh) o.instanceMatrix = m.instanceMatrix;
      o.raycast = function () {};
      o.renderOrder = 10;
      m.add(o); flashes.push(o);
    });
    flashT = at ? Math.max(0, (Date.now() - at) / 1000) : 0;
    app.invalidate();
  };
  page.model = function () { return rec ? { rec: rec, cfg: cfg, d3: modelObj && modelObj.userData.d3 } : null; };
  page.pickAt = function (x, y) {
    if (!modelObj) return null;
    ray.setFromCamera({ x: x / W * 2 - 1, y: 1 - y / H * 2 }, camera);
    var list = modelObj.userData.d3.meshes.filter(function (m) { return m.visible && m.parent; }), hit = ray.intersectObjects(list, false)[0];
    if (!hit) return null;
    var o = hit.object, mi = hit.face && Array.isArray(o.material) ? hit.face.materialIndex : 0;
    return { mid: o.userData.mid, mat: (o.userData.mats || [])[mi], name: o.name };
  };
  page.mark = function (mids, color) {
    marks.forEach(function (o) { if (o.parent) o.parent.remove(o); });
    marks = [];
    if (!modelObj || !mids.length) { if (root) app.invalidate(); return; }
    if (color) hiMat.color.set(color);
    var all = modelObj.userData.d3.meshes;
    mids.forEach(function (id) {
      var m = all[id];
      if (!m || !m.visible || m.isSkinnedMesh) return;
      var o = new T.Mesh(m.geometry, hiMat);
      o.raycast = function () {};
      m.add(o); marks.push(o);
    });
    app.invalidate();
  };
  page.force = function (k, on) {
    var bag = LIGHTS.indexOf(k) >= 0 || k === 'high' ? forceLight : force;
    if (on) bag[k] = 1; else delete bag[k];
    apply(D.car());
  };
  page.lights = function (l, borrow) {
    if (!cfg) return;
    cfg.lights = JSON.parse(JSON.stringify(l || {}));
    if (borrow) cfg.borrow = true; else delete cfg.borrow;
    if (M) lightFx();
    if (app && root) app.invalidate();
  };
  page.night = function (on) {
    nightW = !!on;
    el.classList.toggle('night', nightW);
    if (app && root) app.invalidate();
  };
  page.info = function () { return { tour: tour ? tour.w : '', door: doorV ? doorV.keys.join(',') : '', look: pin ? 'pin' : tour ? 'tyre' : doorNow() ? 'door' : '', drive: drv.on, k: Math.round(drv.k * 100) / 100, spd: Math.round(drv.spd), dist: Math.round(drv.dist), top: Math.round(top.k * 100) / 100, shadow: !!(keyLight && keyLight.castShadow), refl: !!refl, flake: M ? Math.round(M.fx.fb * 100) / 100 : 0, env: !!(scene && scene.environment && scene.environment.image), paint: M ? '#' + M.paint.color.getHexString() : '', show: Math.round(showK * 100) / 100, turn: (turnOn.L ? 'L' : '') + (turnOn.R ? 'R' : ''),
    parts: parts ? Object.keys(parts).sort().join(',') : '', wheels: wheelN, model: rec ? rec.name : '', builtin: rec ? rec.builtin : null,
    dims: [dims.hw, dims.h, dims.hl].map(function (v) { return Math.round(v * 100) / 100; }).join(','), editing: editing,
    open: Object.keys(cur).filter(function (k) { return cur[k] > 0.5 && (DOORS[k] || LIDS[k] || WINS[k] || SLIDES[k] || FLAPS[k]); }).sort().join(','),
    plates: plates ? ['front', 'rear'].filter(function (k) { return plates[k].visible; }).join(',') : '',
    plateText: cfg && cfg.plates ? (cfg.plates.text || '') : '', plateStyle: cfg && cfg.plates ? (cfg.plates.style || '') : '',
    wraps: root && root.userData.wraps ? root.userData.wraps.count() : 0, wrapN: cfg && cfg.wraps ? cfg.wraps.length : 0,
    lamps: LS ? LIGHTS.filter(LS.has).join(',') : '', lights: LS ? LS.lit() : '', amb: LS ? LS.amb() : '', beams: LS ? LS.beams() : '', night: Math.round(nightK * 100) / 100, needs: page.needs.join(), welcome: welT < 1, coat: M ? Math.round(M.paint.clearcoat * 100) / 100 : null,
    zoom: Math.round(zoom * 100) / 100, pan: Math.round(pan.length() * 100) / 100, flash: flashes.length, az: Math.round(wrap(view.az) * 100) / 100 }; };
  S.on(function (k) {
    if (k === 'theme') theme(wantDark());
    if (k === 'bg' || k === 'bgDim') background();
    if (k === 'act:bgImage') pickBg();
    if (k === 'act:bgUse') { if (hasBg) S.set('bg', 'image'); else pickBg(); }
    if (k === 'stats') apply(D.car());
    if (k === 'showRoad' || k === 'showCam') { D.limWatch(S.get('showRoad') || S.get('showCam')); apply(D.car()); }
    if (k === 'showNet') D.netOn(S.get('showNet'));
    if (k === 'showAdas' || k === 'showMediaCtl') apply(D.car());
    if (k === 'showDate' || k === 'showLunar' || k === 'showTime' || k === 'dtScale') dtPaint();
    if (k === 'showMusic' || /^np/.test(k)) { npShow(); npPaint(D.media()); }
    if (k === 'act:viewSave') { S.set('viewAz', Math.round(wrap(view.az) * 1000) / 1000); S.set('viewEl', Math.round(Math.min(0.62, view.el) * 1000) / 1000); D3.toast('以后打开就是这个角度'); }
    if (k === 'viewAz' || k === 'viewEl') homeView();
    if (/^view(Az|El|Zoom|Fov)$/.test(k) && root) {
      if (S.isOpen()) preview = true;
      if (k === 'viewZoom' || k === 'viewFov') page.resize(W, H, portrait);
      view.homing = true; view.vaz = 0;
    }
    if (!root) return;
    if (k === 'drive') apply(D.car());
    else if (/^cam/.test(k)) {
      if (S.isOpen() && !previewDrive) { previewDrive = true; el.classList.add('driving'); }
      if (driving()) { view.homing = true; view.vaz = 0; }
      app.invalidate();
    } else {
      if (/^(paint|rim|caliper|finish|paintHex|rimHex|calHex)$/.test(k)) finish(true);
      app.invalidate();
    }
  });
  function hint() {
    var e = $('cHint');
    if (e.className === 'c-hint gone') return;
    e.className = 'c-hint gone';
    try { localStorage.setItem('d3-car-hint', '1'); } catch (er) {}
  }
  try { if (localStorage.getItem('d3-car-hint')) $('cHint').className = 'c-hint gone'; } catch (e) {}
  homeView(); view.az = HOME.az; view.el = HOME.el;
  stats(); background();

  page.fast = true;
  window.D3Car = page;
  D3.add(page);
})();
