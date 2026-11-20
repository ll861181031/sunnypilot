const { test } = require('node:test');
const assert = require('node:assert/strict');
const { splitGroups, materialFor, encodeV6, geometryOnlyGlb, readGlb } = require('./car_format_v6.cjs');

test('v6 splits large groups at triangle boundaries without truncating indices or UVs', () => {
  const vc = 65538;
  const group = { p: [], n: [], uv: [], i: [], rig: 'body', role: 0, label: 'large', material: {} };
  for (let i = 0; i < vc; i++) {
    group.p.push(i / vc, 1, 0); group.n.push(0, 1, 0); group.uv.push(i / vc, 0.7); group.i.push(i);
  }
  const parts = splitGroups([group]);
  assert.equal(parts.length, 2);
  assert.equal(parts.reduce((n, g) => n + g.i.length, 0), vc);
  assert.ok(parts.every(g => g.p.length / 3 <= 65535 && g.i.every(i => i < g.p.length / 3)));
  assert.equal(parts[1].p[0], 65535 / vc);
  assert.equal(parts[1].uv[0], 65535 / vc);
});

test('explicit window and light roles override substring guesses and preserve continuous alpha', () => {
  const cfg = { mats: { glass: ['glas', 'glass_Alph'], led: ['inner_light_glass'], tail: ['red_glass'], paint: ['CarPaint'] }, lens: true };
  assert.equal(materialFor({ name: 'glas' }, cfg, -1).flags & 3, 3);
  assert.equal(materialFor({ name: 'glas' }, cfg, -1).rgba[3], 0.78);
  assert.equal(materialFor({ name: 'inner_light_glass' }, cfg, -1).role, 5);
  assert.equal(materialFor({ name: 'inner_light_glass' }, cfg, -1).flags & 1, 0);
  assert.equal(materialFor({ name: 'red_glass', alphaMode: 'BLEND' }, cfg, -1).rgba[3], 0.7);
  const head = materialFor({ name: 'head_lens' }, { mats: { head: ['head_lens'] }, lens: true }, -1);
  assert.equal(head.role, 8);
  assert.equal(head.rgba[3], .22);
  assert.equal(head.flags, 2);
  assert.equal(materialFor({ name: 'head_lens' }, {}, -1).role, 0);
  assert.equal(materialFor({ name: 'TL_Alpha', alphaMode: 'BLEND' }, cfg, 0).flags, 2);
  assert.deepEqual(materialFor({ name: 'CarPaint' }, cfg, -1).rgba.slice(0, 3), [.95, .96, .97]);
});

test('v6 writes explicit material, UV and one shared texture table', () => {
  const material = materialFor({ name: 'trim', alphaMode: 'BLEND' }, {}, 0);
  const g = { p: [0,0,0, 1,0,0, 0,1,0], n: [0,0,1, 0,0,1, 0,0,1], uv: [-1.5,0, 2.5,0, 0,1], i:[0,1,2], rig:'body', role:0, label:'trim', material };
  const texture = Buffer.from([137,80,78,71]);
  const { bytes } = encodeV6([g, g], {}, [texture]);
  assert.equal(bytes.readUInt32LE(4), 6);
  assert.equal(bytes.readUInt32LE(8), 2);
  const start = 12 + 2 * (2 + 4);
  assert.equal(bytes.readInt32LE(start + 60), 0);
  assert.equal(bytes.readUInt32LE(start + 64), 2);
  assert.equal(bytes.readFloatLE(start + 72 + 27), -1.5);
  const tail = start + 2 * (72 + 3 * 17 + 6);
  assert.equal(bytes.readUInt32LE(tail), 1);
  assert.equal(bytes.readUInt32LE(tail + 4), 4);
  assert.deepEqual(bytes.subarray(tail + 8), texture);
});

test('v6 rejects unknown lamp roles instead of silently encoding an unlit mesh', () => {
  const material = materialFor({ name: 'head_lens' }, {mats:{head:['head_lens']}}, -1);
  const g = { p:[0,0,0,1,0,0,0,1,0], n:[0,0,1,0,0,1,0,0,1], uv:[0,0,1,0,0,1], i:[0,1,2], rig:'body', label:'head_lens', material };
  for (const role of [undefined,-1,9,1.5,NaN]) {
    assert.throws(()=>encodeV6([{...g,role}],{},[]), /lamp role/i);
  }
});

test('configured tail lenses blend even when the GLB source is opaque or masked', () => {
  const config = { lens: true, mats: { tail: ['Tail_Light_Emis'] } };
  for (const alphaMode of [undefined, 'OPAQUE', 'BLEND', 'MASK']) {
    const lens = materialFor({ name: 'Tail_Light_Emis', alphaMode }, config, -1);
    assert.equal(lens.role, 7);
    assert.equal(lens.rgba[3], 0.7);
    assert.equal(lens.flags, 2);
  }
  const original = materialFor({ name: 'Tail_Light_Emis' }, { ...config, lens: false }, -1);
  assert.equal(original.rgba[3], 1);
  assert.equal(original.flags, 0);
});

test('geometry-only loading strips image references without executing model JavaScript', () => {
  const source = { asset: { version:'2.0' }, materials:[{ name:'textured', pbrMetallicRoughness:{baseColorTexture:{index:0}}, normalTexture:{index:0} }], textures:[{source:0}], images:[{bufferView:0}], buffers:[{byteLength:4}] };
  const clean = readGlb(geometryOnlyGlb(source, Buffer.from([1,2,3,4])));
  assert.equal(clean.json.materials[0].pbrMetallicRoughness.baseColorTexture, undefined);
  assert.equal(clean.json.materials[0].normalTexture, undefined);
  assert.deepEqual([...clean.bin], [1,2,3,4]);
  assert.deepEqual(source.materials[0].pbrMetallicRoughness.baseColorTexture, {index:0});
});
