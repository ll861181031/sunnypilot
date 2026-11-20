// CARM v6: explicit materials, original UVs, shared embedded base-color images.
// Source GLB is data only. No JavaScript from an imported package is evaluated.
'use strict';

function readGlb(bytes) {
  if (bytes.length < 28 || bytes.toString('ascii', 0, 4) !== 'glTF' ||
      bytes.readUInt32LE(4) !== 2 || bytes.readUInt32LE(8) !== bytes.length) throw Error('Invalid GLB header');
  let json, bin;
  for (let off = 12; off < bytes.length;) {
    if (off + 8 > bytes.length) throw Error('Truncated GLB chunk');
    const len = bytes.readUInt32LE(off), type = bytes.readUInt32LE(off + 4); off += 8;
    if (off + len > bytes.length || len % 4) throw Error('Invalid GLB chunk length');
    if (type === 0x4e4f534a) json = JSON.parse(bytes.toString('utf8', off, off + len));
    if (type === 0x004e4942) bin = bytes.subarray(off, off + len);
    off += len;
  }
  if (!json || !bin) throw Error('GLB requires JSON and BIN chunks');
  return { json, bin };
}

function packGlb(json, bin) {
  const j = Buffer.from(JSON.stringify(json));
  const jp = Buffer.alloc(Math.ceil(j.length / 4) * 4, 32); j.copy(jp);
  const bp = Buffer.alloc(Math.ceil(bin.length / 4) * 4); bin.copy(bp);
  const h = Buffer.alloc(20), bh = Buffer.alloc(8);
  h.write('glTF'); h.writeUInt32LE(2, 4); h.writeUInt32LE(28 + jp.length + bp.length, 8);
  h.writeUInt32LE(jp.length, 12); h.writeUInt32LE(0x4e4f534a, 16);
  bh.writeUInt32LE(bp.length); bh.writeUInt32LE(0x004e4942, 4);
  return Buffer.concat([h, jp, bh, bp]);
}

function geometryOnlyGlb(source, bin) {
  const json = JSON.parse(JSON.stringify(source));
  function stripTextures(value) {
    if (!value || typeof value !== 'object') return;
    for (const key of Object.keys(value)) {
      if (/Texture$/.test(key)) delete value[key]; else stripTextures(value[key]);
    }
  }
  for (const material of json.materials || []) stripTextures(material);
  delete json.images; delete json.textures; delete json.samplers;
  return packGlb(json, bin);
}

function materialFor(source, config, textureIndex) {
  const is = key => (config.mats?.[key] || []).includes(source.name);
  const pbr = source.pbrMetallicRoughness || {};
  // The existing scene shades directly in display space; convert only source factors,
  // leaving the road/sky shader unchanged. This is a Blinn-Phong approximation, not PBR.
  const srgb = c => c <= .0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - .055;
  const base = pbr.baseColorFactor || [1, 1, 1, 1];
  let rgba = [...base.slice(0, 3).map(srgb), source.alphaMode === 'BLEND' ? base[3] : 1];
  let flags = source.alphaMode === 'BLEND' ? 2 : source.alphaMode === 'MASK' ? 4 : 0;
  if (source.alphaMode === 'MASK') rgba[3] = base[3];
  let spec = .06 + .4 * (pbr.metallicFactor ?? 1);
  let gloss = Math.max(.06, Math.min(.95, 1 - (pbr.roughnessFactor ?? 1)));
  if (is('paint')) { rgba = [.95, .96, .97, 1]; spec = .65; gloss = .78; }
  if (is('glass')) { rgba[3] = .78; flags = 3; spec = .9; gloss = .95; }
  if (config.lens && is('head')) { rgba[3] = .22; flags |= 2; }
  // The package replaces every configured tail material with its translucent
  // lens, including Tail_Light_Emis; source alphaMode does not opt out.
  if (config.lens && is('tail')) { rgba[3] = .7; flags = (flags & ~4) | 2; }
  let role = 0;
  for (const [key, value] of [['turnL', 1], ['turnR', 2], ['brake', 3], ['reverse', 4], ['led', 5], ['side', 6], ['tail', 7], ['head', 8]]) {
    if (is(key)) { role = value; break; }
  }
  // Head geometry is explicit; the app's default presentation light is not real headlight telemetry.
  // Rear fog remains unassigned because there is no rear-fog signal or requested display effect.
  return { rgba, spec, gloss, textureIndex, flags, alphaCutoff: source.alphaCutoff ?? .5, role };
}

function collectMaterials(json, bin, config) {
  const textures = [], imageToTexture = new Map(), byName = new Map();
  for (const source of json.materials || []) {
    const info = source.pbrMetallicRoughness?.baseColorTexture;
    let textureIndex = -1;
    if (info) {
      if ((info.texCoord ?? 0) !== 0 || info.extensions) throw Error('Unsupported texture coordinate transform');
      const texture = json.textures?.[info.index], image = json.images?.[texture?.source];
      const sampler = json.samplers?.[texture?.sampler];
      if (sampler && ((sampler.wrapS ?? 10497) !== 10497 || (sampler.wrapT ?? 10497) !== 10497)) throw Error('v6 importer expects repeat texture wrapping');
      if (!image || image.bufferView === undefined || !['image/png','image/jpeg'].includes(image.mimeType)) throw Error('Expected embedded PNG/JPEG base color');
      if (!imageToTexture.has(texture.source)) {
        const view = json.bufferViews[image.bufferView], off = view.byteOffset || 0;
        if (view.buffer !== 0 || off + view.byteLength > bin.length) throw Error('Invalid image buffer view');
        imageToTexture.set(texture.source, textures.length);
        textures.push(Buffer.from(bin.subarray(off, off + view.byteLength)));
      }
      textureIndex = imageToTexture.get(texture.source);
    }
    if (byName.has(source.name)) throw Error('Material names must be unique for v6 import');
    byName.set(source.name, materialFor(source, config, textureIndex));
  }
  return { textures, byName };
}

function splitGroups(groups, limit = 65535) {
  const out = [];
  for (const g of groups) {
    const vc = g.p.length / 3;
    if (g.n.length !== vc * 3 || g.uv.length !== vc * 2 || g.i.length % 3 ||
        !g.i.every(i => Number.isInteger(i) && i >= 0 && i < vc)) throw Error('Invalid mesh data');
    if (vc <= limit) { out.push(g); continue; }
    let map = new Map(), chunk;
    const start = () => { chunk = { ...g, p: [], n: [], uv: [], i: [] }; map = new Map(); };
    start();
    for (let off = 0; off < g.i.length; off += 3) {
      const tri = g.i.slice(off, off + 3);
      const additional = new Set(tri.filter(i => !map.has(i))).size;
      if (map.size + additional > limit) { out.push(chunk); start(); }
      for (const i of tri) {
        if (!map.has(i)) {
          map.set(i, map.size);
          chunk.p.push(...g.p.slice(i * 3, i * 3 + 3));
          chunk.n.push(...g.n.slice(i * 3, i * 3 + 3));
          chunk.uv.push(...g.uv.slice(i * 2, i * 2 + 2));
        }
        chunk.i.push(map.get(i));
      }
    }
    if (chunk.i.length) out.push(chunk);
  }
  return out;
}

function encodeV6(input, hinges, textures) {
  const groups = splitGroups(input.filter(g => g.i.length));
  const names = [], chunks = [], bounds = { min: [Infinity,Infinity,Infinity], max: [-Infinity,-Infinity,-Infinity] };
  for (let id = 0; id < groups.length; id++) {
    const g = groups[id], vc = g.p.length / 3, m = g.material;
    if (!Number.isInteger(g.role) || g.role < 0 || g.role > 8) throw Error('Invalid v6 lamp role');
    const scale = g.p.every(v => Math.abs(v) * 10000 <= 32767) ? 10000 : 1000;
    if (![...g.p, ...g.n, ...g.uv, ...m.rgba, m.spec, m.gloss, m.alphaCutoff].every(Number.isFinite) ||
        g.p.some(v => Math.abs(v) * scale > 32767)) throw Error('Invalid or out-of-range vertex/material');
    const name = Buffer.from(g.label, 'utf8'), nl = Buffer.alloc(2); nl.writeUInt16LE(name.length); names.push(nl, name);
    const h = hinges[g.rig] || { kind:0, axis:1, angle:0, pivot:[0,0,0] };
    const head = Buffer.alloc(72);
    head.writeUInt32LE(id); head.writeUInt32LE(vc,4); head.writeUInt32LE(g.i.length,8);
    head[16]=h.kind; head[17]=h.axis; head.writeInt8(Math.max(-127,Math.min(127,Math.round(h.angle))),18); head[19]=g.role;
    h.pivot.forEach((v,i)=>head.writeFloatLE(v,20+i*4)); head.writeFloatLE(scale,32);
    m.rgba.forEach((v,i)=>head.writeFloatLE(v,36+i*4)); head.writeFloatLE(m.spec,52); head.writeFloatLE(m.gloss,56);
    head.writeInt32LE(m.textureIndex,60); head.writeUInt32LE(m.flags,64); head.writeFloatLE(m.alphaCutoff,68);
    const pos=Buffer.alloc(vc*6), norm=Buffer.alloc(vc*3), uv=Buffer.alloc(vc*8), idx=Buffer.alloc(g.i.length*2);
    g.p.forEach((v,i)=> { pos.writeInt16LE(Math.round(v*scale),i*2); bounds.min[i%3]=Math.min(bounds.min[i%3],v); bounds.max[i%3]=Math.max(bounds.max[i%3],v); });
    g.n.forEach((v,i)=>norm.writeInt8(Math.max(-127,Math.min(127,Math.round(v*127))),i));
    g.uv.forEach((v,i)=>uv.writeFloatLE(v,i*4)); g.i.forEach((v,i)=>idx.writeUInt16LE(v,i*2));
    chunks.push(head,pos,norm,uv,idx);
  }
  const header=Buffer.alloc(12); header.write('CARM'); header.writeUInt32LE(6,4); header.writeUInt32LE(groups.length,8);
  const tc=Buffer.alloc(4); tc.writeUInt32LE(textures.length);
  const textureChunks=[tc];
  for (const texture of textures) { const n=Buffer.alloc(4);n.writeUInt32LE(texture.length);textureChunks.push(n,texture); }
  return { bytes:Buffer.concat([header,...names,...chunks,...textureChunks]), groups, bounds };
}

module.exports = { readGlb, packGlb, geometryOnlyGlb, materialFor, collectMaterials, splitGroups, encodeV6 };
