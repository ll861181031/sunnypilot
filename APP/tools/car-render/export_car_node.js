const fs = require('fs');
const vm = require('vm');
const path = require('path');
const v6 = require('./car_format_v6.cjs');
const USE_V6 = process.argv.includes('--v6');

const DIR = 'D:\\Desktop\\openpilot\\_byd_card2';
const GLB = process.argv[2] || path.join(DIR, 'car_geo.glb');
const OUT = process.argv[3] || 'D:\\Desktop\\openpilot\\_navipilot_full\\app\\src\\main\\assets\\car.bin';

const POS_SCALE = 1000.0;
const NRM_SCALE = 127.0;
const MAX_ABS = 12.0;

const KIND = { body: 0, doorFL: 1, doorFR: 2, doorRL: 3, doorRR: 4, hood: 5, trunk: 6 };
const ROLE = { none: 0, turnL: 1, turnR: 2, brake: 3, reverse: 4, drl: 5, pos: 6, tail: 7 };
const ROLE_NAME = ['Lamp', 'LampTurnL', 'LampTurnR', 'LampBrake', 'LampReverse', 'LampDrl', 'LampPos', 'LampTail'];

function classify(name) {
  if (name.includes('藏')) return { rig: 'skip' };
  let rig = 'body';
  if (name.startsWith('Door_FL')) rig = 'doorFL';
  else if (name.startsWith('Door_FR')) rig = 'doorFR';
  else if (name.startsWith('Door_RL')) rig = 'doorRL';
  else if (name.startsWith('Door_RR')) rig = 'doorRR';
  else if (name.startsWith('Trunk')) rig = 'trunk';
  else if (name.startsWith('Hood')) rig = 'hood';
  let role = 'none';
  if (name.includes('左转向灯')) role = 'turnL';
  else if (name.includes('右转向灯')) role = 'turnR';
  else if (name.includes('刹车灯')) role = 'brake';
  else if (name.includes('倒车灯')) role = 'reverse';
  else if (name.includes('日行灯')) role = 'drl';
  else if (name.includes('示宽灯')) role = 'pos';
  else if (name.includes('尾灯')) role = 'tail';
  return { rig, role };
}

function makeSandbox() {
  const g = {
    console, Math, Date, JSON, Object, Array, String, Number, Boolean, Error,
    Promise, Map, Set, Symbol, RegExp, Function, parseInt, parseFloat, isNaN,
    Float32Array, Float64Array, Uint8Array, Uint8ClampedArray, Uint16Array,
    Uint32Array, Int8Array, Int16Array, Int32Array, ArrayBuffer, DataView,
    TextDecoder: global.TextDecoder, TextEncoder: global.TextEncoder,
    setTimeout, clearTimeout, setInterval, clearInterval,
    performance: global.performance || { now: () => Date.now() },
    WebAssembly,
    Buffer,
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  };
  g.window = g;
  g.self = g;
  g.globalThis = g;
  g.navigator = { userAgent: 'node', platform: 'node' };
  const fakeCanvas = () => ({
    style: {}, getContext: () => null, width: 1, height: 1,
    addEventListener() {}, removeEventListener() {},
  });
  g.document = {
    createElementNS: fakeCanvas,
    createElement: fakeCanvas,
    documentElement: { style: {} },
    addEventListener() {}, removeEventListener() {},
  };
  g.URL = { createObjectURL: () => 'blob:1', revokeObjectURL() {} };
  g.Blob = class Blob { constructor(parts) { this.parts = parts; } };
  g.Image = class Image { addEventListener() {} };
  return g;
}

function loadScripts(ctx) {
  for (const f of ['three.min.js', 'three-addons.js', 'meshopt-decoder.js']) {
    const p = path.join(__dirname, f);
    const code = fs.readFileSync(p, 'utf8');
    vm.runInContext(code, ctx, { filename: f });
  }
}

async function main() {
  const sandbox = makeSandbox();
  const ctx = vm.createContext(sandbox);
  loadScripts(ctx);

  const THREE = sandbox.THREE;
  if (!THREE) throw new Error('THREE 未定义');
  const GLTFLoader = THREE.GLTFLoader || (THREE.Loader && sandbox.GLTFLoader);
  if (!GLTFLoader) throw new Error('GLTFLoader 未定义');
  console.log('THREE', THREE.REVISION, '  GLTFLoader', typeof GLTFLoader);

  const original = fs.readFileSync(GLB);
  const source = USE_V6 ? v6.readGlb(original) : null;
  // GLTFLoader is used for geometry/transforms only; no browser/image stub waits.
  // Original embedded images/materials are copied separately into the v6 asset.
  const buf = USE_V6 ? v6.geometryOnlyGlb(source.json, source.bin) : original;
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);

  if (sandbox.MeshoptDecoder && sandbox.MeshoptDecoder.ready) await sandbox.MeshoptDecoder.ready;
  const gltf = await new Promise((resolve, reject) => {
    const loader = new THREE.GLTFLoader();
    if (sandbox.MeshoptDecoder) loader.setMeshoptDecoder(sandbox.MeshoptDecoder);
    try {
      loader.parse(ab, '', resolve, (e) => reject(e instanceof Error ? e : new Error(String(e))));
    } catch (e) { reject(e); }
  });

  const meshes = [];
  gltf.scene.traverse(obj => { if (obj.isMesh) meshes.push(obj); });
  const modelConfig = gltf.parser.json.asset.extras?.d3car || {};
  const materials = USE_V6 ? v6.collectMaterials(source.json, source.bin, modelConfig) : null;
  const partConfig = modelConfig.parts;
  const hasPartConfig = !!partConfig && typeof partConfig === 'object';
  function meshRefs(refs) {
    const found = new Set();
    for (const ref of (refs || [])) {
      if (typeof ref === 'number' && meshes[ref]) found.add(meshes[ref]);
      else if (typeof ref === 'string') {
        const re = new RegExp(ref);
        for (const mesh of meshes) if (re.test(mesh.name)) found.add(mesh);
      }
    }
    return found;
  }
  const assignments = new Map();
  if (hasPartConfig) {
    for (const rig of Object.keys(KIND).filter(k => k !== 'body')) {
      for (const mesh of meshRefs(partConfig[rig]?.meshes)) {
        assignments.set(mesh, { rig, isWindow: false });
      }
    }
    for (const side of ['FL', 'FR', 'RL', 'RR']) {
      for (const mesh of meshRefs(partConfig['win' + side]?.meshes)) {
        assignments.set(mesh, { rig: 'door' + side, isWindow: true });
      }
    }
  }
  const hiddenMeshes = meshRefs(modelConfig.hide);

  if (USE_V6) {
    // Match the package's load transform: YXZ rotation, scale, center X/Z, ground Y.
    const deg = Math.PI / 180;
    gltf.scene.rotation.set((modelConfig.rotX || 0) * deg,
      (modelConfig.rotY || 0) * deg, (modelConfig.rotZ || 0) * deg, 'YXZ');
    gltf.scene.scale.multiplyScalar(modelConfig.scale || 1);
    gltf.scene.updateMatrixWorld(true);
    const bounds = new THREE.Box3();
    for (const mesh of meshes) {
      if (hiddenMeshes.has(mesh)) continue;
      mesh.geometry.computeBoundingBox();
      bounds.union(mesh.geometry.boundingBox.clone().applyMatrix4(mesh.matrixWorld));
    }
    if (bounds.isEmpty()) throw Error('Model has no visible geometry');
    gltf.scene.position.set(-(bounds.min.x + bounds.max.x) / 2, -bounds.min.y,
      -(bounds.min.z + bounds.max.z) / 2);
  }

  const groups = new Map();       // key -> {p,n,i,rig,role,label}
  const rigBounds = new Map();
  const v3 = new THREE.Vector3();
  const n3 = new THREE.Vector3();
  const nm = new THREE.Matrix3();
  let prims = 0, verts = 0, tris = 0, skipped = 0, hidden = 0;

  gltf.scene.updateMatrixWorld(true);
  gltf.scene.traverse((obj) => {
    if (!obj.isMesh || !obj.geometry) return;
    const geo = obj.geometry;
    const pos = geo.attributes.position;
    if (!pos) return;
    let idx = geo.index ? geo.index.array : null;
    if (!idx && USE_V6) idx = Uint32Array.from({length: pos.count}, (_, i) => i);
    if (!idx) { skipped++; return; }

    const named = classify(obj.name || '');
    if (hiddenMeshes.has(obj) || (!Array.isArray(modelConfig.hide) && named.rig === 'skip')) {
      hidden++; return;
    }
    const cls = hasPartConfig
      ? { role: named.role || 'none', ...(assignments.get(obj) || { rig: 'body', isWindow: false }) }
      : named;

    const matKey = (obj.material && obj.material.name) || 'default';
    const material = USE_V6 ? materials.byName.get(matKey) : null;
    if (USE_V6 && !material) throw Error(`Missing source material: ${matKey}`);
    if (USE_V6) cls.role = material.role;
    const key = `${cls.rig}|${cls.role}|${matKey}`;
    let g = groups.get(key);
    if (!g) {
      g = { p: [], n: [], uv: [], i: [], rig: cls.rig, role: USE_V6 ? material.role : ROLE[cls.role], label: matKey, material };
      groups.set(key, g);
    }

    const mw = obj.matrixWorld;
    nm.getNormalMatrix(mw);
    const norm = geo.attributes.normal;
    const uv = geo.attributes.uv;
    if (USE_V6 && material.textureIndex >= 0 && (!uv || uv.count !== pos.count)) throw Error(`Missing UV: ${matKey}`);
    const count = pos.count;
    const kept = [];
    for (let k = 0; k < count; k++) {
      v3.fromBufferAttribute(pos, k).applyMatrix4(mw);
      if (!isFinite(v3.x) || !isFinite(v3.y) || !isFinite(v3.z)) {
        if (USE_V6) throw Error('Non-finite model vertex');
        kept.push(-1); continue;
      }
      if (Math.abs(v3.x) > MAX_ABS || Math.abs(v3.y) > MAX_ABS || Math.abs(v3.z) > MAX_ABS) {
        if (USE_V6) throw Error('Model vertex outside supported scene bounds');
        kept.push(-1); continue;
      }
      if (norm && norm.count === count) {
        n3.fromBufferAttribute(norm, k).applyMatrix3(nm).normalize();
      } else {
        n3.set(0, 0, 0);
      }
      kept.push(g.p.length / 3);
      g.p.push(v3.x, v3.y, v3.z);
      g.n.push(n3.x, n3.y, n3.z);
      if (USE_V6) g.uv.push(uv ? uv.getX(k) : 0, uv ? uv.getY(k) : 0);
      if (cls.rig !== 'body' && !cls.isWindow) {
        let b = rigBounds.get(cls.rig);
        if (!b) {
          b = { min: [1e9, 1e9, 1e9], max: [-1e9, -1e9, -1e9], pts: [] };
          rigBounds.set(cls.rig, b);
        }
        for (let c = 0; c < 3; c++) {
          const val = c === 0 ? v3.x : (c === 1 ? v3.y : v3.z);
          if (val < b.min[c]) b.min[c] = val;
          if (val > b.max[c]) b.max[c] = val;
        }
        if (k % 8 === 0) b.pts.push([v3.x, v3.y, v3.z]);
      }
    }
    for (let t = 0; t + 2 < idx.length; t += 3) {
      const a = kept[idx[t]], b = kept[idx[t + 1]], c = kept[idx[t + 2]];
      if (a < 0 || b < 0 || c < 0) continue;
      g.i.push(a, b, c);
    }
    prims++;
    verts += count;
    tris += idx.length / 3;
  });

  console.log(`meshes=${prims} skipped=${skipped} hidden=${hidden} verts=${verts.toLocaleString()} tris=${tris.toLocaleString()}`);

  const DEG = Math.PI / 180;
  function rotAround(v, axis, ang) { // axis: 0=X 1=Y
    const c = Math.cos(ang), s = Math.sin(ang);
    if (axis === 0) return { x: v.x, y: v.y * c - v.z * s, z: v.y * s + v.z * c };
    return { x: v.x * c + v.z * s, y: v.y, z: -v.x * s + v.z * c };
  }
  const hinges = {}; // rig -> {kind, axis, angle(signed deg), pivot:[x,y,z]}
  for (const rig of ['doorFL', 'doorFR', 'doorRL', 'doorRR', 'hood', 'trunk']) {
    const b = rigBounds.get(rig);
    if (!b) { console.log(`WARN: no bounds for ${rig}`); continue; }
    const cx = (b.min[0] + b.max[0]) / 2, cy = (b.min[1] + b.max[1]) / 2, cz = (b.min[2] + b.max[2]) / 2;
    let pivot, axis, angle;
    if (rig === 'hood' || rig === 'trunk') {
      const z = rig === 'hood' ? b.min[2] : b.max[2];
      pivot = [0, b.max[1], z];
      axis = 0; angle = rig === 'hood' ? 55 : 50;
    } else {
      const side = (b.min[0] + b.max[0]) > 0 ? 1 : -1;
      const outer = side > 0 ? b.max[0] : b.min[0];
      pivot = [outer, cy, b.max[2]];
      axis = 1; angle = 60;
    }
    const settings = partConfig?.[rig] || {};
    if (Array.isArray(settings.piv)) {
      const side = rig === 'hood' || rig === 'trunk' ? 1 : ((b.min[0] + b.max[0]) > 0 ? 1 : -1);
      for (let c = 0; c < 3; c++) {
        pivot[c] += (settings.piv[c] || 0) * (b.max[c] - b.min[c]) * (c === 0 ? side : 1);
      }
    }
    angle = settings.angle || angle;
    let far = null, farD = -1;
    for (const pt of (b.pts || [])) {
      const d = (pt[0] - pivot[0]) ** 2 + (pt[1] - pivot[1]) ** 2 + (pt[2] - pivot[2]) ** 2;
      if (d > farD) { farD = d; far = pt; }
    }
    if (!far) far = [cx, cy, cz];
    const off = { x: far[0] - pivot[0], y: far[1] - pivot[1], z: far[2] - pivot[2] };
    const a = rotAround(off, axis, 0.5), bb = rotAround(off, axis, -0.5);
    let sign;
    if (axis === 1) {
      const side = pivot[0] > 0 ? 1 : -1;
      sign = (a.x * side) > (bb.x * side) ? 1 : -1;
    } else {
      sign = a.y > bb.y ? 1 : -1;
    }
    if (settings.flip) sign *= -1;
    hinges[rig] = { kind: KIND[rig], axis, angle: angle * sign, pivot };
    console.log(`hinge ${rig}: pivot=(${pivot.map((v) => v.toFixed(2)).join(', ')}) axis=${axis ? 'Y' : 'X'} angle=${(angle * sign).toFixed(0)}deg`);
  }

  if (USE_V6) {
    const result = v6.encodeV6([...groups.values()], hinges, materials.textures);
    fs.writeFileSync(OUT, result.bytes);
    console.log(JSON.stringify({ format: 6, groups: result.groups.length,
      vertices: result.groups.reduce((n,g)=>n+g.p.length/3,0),
      triangles: result.groups.reduce((n,g)=>n+g.i.length/3,0),
      maxGroupVertices: Math.max(...result.groups.map(g=>g.p.length/3)),
      textures: materials.textures.length, bytes: result.bytes.length, bounds: result.bounds }));
    return;
  }

  const keys = [...groups.keys()].filter((k) => groups.get(k).i.length > 0);
  let matId = 0;
  const chunks = [];
  const matNames = [];
  let mn = [1e9, 1e9, 1e9], mx = [-1e9, -1e9, -1e9];
  for (const key of keys) {
    const g = groups.get(key);
    const vc = g.p.length / 3;
    const ic = g.i.length;
    if (vc === 0 || ic === 0) continue;
    const p16 = Buffer.alloc(vc * 6);
    const n8 = Buffer.alloc(vc * 3);
    const positionScale = hasPartConfig && g.p.every(v => Math.abs(v) * 10000 <= 32767)
      ? 10000 : POS_SCALE;
    for (let k = 0; k < vc; k++) {
      for (let c = 0; c < 3; c++) {
        const pv = g.p[k * 3 + c], nv = g.n[k * 3 + c];
        if (pv < mn[c]) mn[c] = pv;
        if (pv > mx[c]) mx[c] = pv;
        p16.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(pv * positionScale))), (k * 3 + c) * 2);
        n8.writeInt8(Math.max(-127, Math.min(127, Math.round(nv * NRM_SCALE))), k * 3 + c);
      }
    }
    const use32 = vc > 65535;
    const ib = Buffer.alloc(ic * (use32 ? 4 : 2));
    for (let k = 0; k < ic; k++) {
      if (use32) ib.writeUInt32LE(g.i[k], k * 4);
      else ib.writeUInt16LE(g.i[k], k * 2);
    }
    const h = hinges[g.rig] || { kind: 0, axis: 1, angle: 0, pivot: [0, 0, 0] };
    const head = Buffer.alloc(36);
    head.writeUInt32LE(matId++, 0);
    head.writeUInt32LE(vc, 4);
    head.writeUInt32LE(ic, 8);
    head.writeUInt32LE(use32 ? 1 : 0, 12);
    head.writeUInt8(h.kind, 16);
    head.writeUInt8(h.axis, 17);
    head.writeInt8(Math.max(-127, Math.min(127, Math.round(h.angle))), 18);
    head.writeUInt8(g.role, 19);
    head.writeFloatLE(h.pivot[0], 20);
    head.writeFloatLE(h.pivot[1], 24);
    head.writeFloatLE(h.pivot[2], 28);
    if (hasPartConfig) head.writeFloatLE(positionScale, 32);
    chunks.push(head, p16, n8, ib);
    matNames.push(g.role !== 0 && g.rig === 'body' ? ROLE_NAME[g.role] : g.label);
  }

  const nameBufs = [];
  for (const nm2 of matNames) {
    const b = Buffer.from(nm2, 'utf8');
    const len = Buffer.alloc(2);
    len.writeUInt16LE(b.length, 0);
    nameBufs.push(len, b);
  }
  const header = Buffer.alloc(12);
  header.write('CARM', 0, 'ascii');
  header.writeUInt32LE(hasPartConfig ? 5 : 4, 4);
  header.writeUInt32LE(chunks.length / 4, 8);
  fs.writeFileSync(OUT, Buffer.concat([header, ...nameBufs, ...chunks]));

  console.log(`groups=${chunks.length / 4}  file=${(fs.statSync(OUT).size / 1e6).toFixed(2)} MB`);
  console.log(`bbox min=[${mn.map((v) => v.toFixed(2))}] max=[${mx.map((v) => v.toFixed(2))}]`);
  console.log('materials:', matNames.join(', '));
}

main().catch((e) => { console.error('FAILED:', e && e.message ? e.message : e); process.exit(1); });
