const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

// Real GLTFLoader + exporter, including a non-mesh node before mesh 0.
function exportFixture(config, yShift = 0) {
  const names = ['anonymous-door', 'anonymous-window', 'anonymous-tail',
    'tail-trim', 'Trunk_unassigned', 'hidden-by-id', 'Hood_panel'];
  const chunks = [], views = [], accessors = [];
  let byteLength = 0;
  const meshes = names.map((name, i) => {
    const vertices = (i === 1 ? [1, 1, 1, 1.2, 1, 0, 1, 2, 0] :
      [1, 0, 1, 1.2, 0, 0, 1, 1, 0]).map((v, j) => v + (j % 3 === 1 ? yShift : 0));
    const p = Buffer.alloc(36);
    vertices.forEach((v, j) => p.writeFloatLE(v, j * 4));
    const ix = Buffer.alloc(8);
    [0, 1, 2].forEach((v, j) => ix.writeUInt16LE(v, j * 2));
    const a = accessors.length;
    accessors.push({ bufferView: views.length, componentType: 5126, count: 3, type: 'VEC3',
      min: [1, (i === 1 ? 1 : 0) + yShift, 0], max: [1.2, (i === 1 ? 2 : 1) + yShift, 1] });
    views.push({ buffer: 0, byteOffset: byteLength, byteLength: p.length });
    chunks.push(p); byteLength += p.length;
    accessors.push({ bufferView: views.length, componentType: 5123, count: 3, type: 'SCALAR' });
    views.push({ buffer: 0, byteOffset: byteLength, byteLength: 6 });
    chunks.push(ix); byteLength += ix.length;
    return { primitives: [{ attributes: { POSITION: a }, indices: a + 1, material: i }] };
  });
  const doc = { asset: { version: '2.0', ...(config ? { extras: { d3car: config } } : {}) },
    scene: 0, scenes: [{ nodes: [0] }],
    nodes: [{ name: 'non-mesh-root', children: names.map((_, i) => i + 1) },
      ...names.map((name, mesh) => ({ name, mesh }))], meshes,
    materials: names.map((_, i) => ({ name: 'material-' + i })),
    buffers: [{ byteLength }], bufferViews: views, accessors };
  const json = Buffer.from(JSON.stringify(doc));
  const paddedJson = Buffer.alloc(Math.ceil(json.length / 4) * 4, 32);
  json.copy(paddedJson);
  const header = Buffer.alloc(20);
  header.writeUInt32LE(0x46546c67, 0); header.writeUInt32LE(2, 4);
  header.writeUInt32LE(28 + paddedJson.length + byteLength, 8);
  header.writeUInt32LE(paddedJson.length, 12); header.writeUInt32LE(0x4e4f534a, 16);
  const binHeader = Buffer.alloc(8);
  binHeader.writeUInt32LE(byteLength); binHeader.writeUInt32LE(0x004e4942, 4);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'car-export-test-'));
  try {
    const input = path.join(dir, 'fixture.glb'), output = path.join(dir, 'fixture.bin');
    fs.writeFileSync(input, Buffer.concat([header, paddedJson, binHeader, ...chunks]));
    const result = spawnSync(process.execPath, [path.join(__dirname, 'export_car_node.js'), input, output], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr + result.stdout);
    return readGroups(fs.readFileSync(output));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

function readGroups(b) {
  const count = b.readUInt32LE(8), names = [], groups = [];
  let off = 12;
  for (let i = 0; i < count; i++) {
    const len = b.readUInt16LE(off); off += 2;
    names.push(b.toString('utf8', off, off + len)); off += len;
  }
  for (let i = 0; i < count; i++) {
    const vc = b.readUInt32LE(off + 4), ic = b.readUInt32LE(off + 8);
    groups.push({ name: names[i], rig: b[off + 16],
      positionScale: b.readFloatLE(off + 32),
      firstNormal: [0, 1, 2].map(d => b.readInt8(off + 36 + vc * 6 + d)),
      pivot: [20, 24, 28].map(d => b.readFloatLE(off + d)),
      firstY: b.readInt16LE(off + 38) / (b.readFloatLE(off + 32) || 1000) });
    off += 36 + vc * 9 + ic * (b.readUInt32LE(off + 12) ? 4 : 2);
  }
  assert.equal(off, b.length);
  return { version: b.readUInt32LE(4), groups };
}

const config = { hide: [5], parts: {
  doorFL: { meshes: [0], piv: [-0.25, 0.1, 0.2] },
  winFL: { meshes: [1] }, trunk: { meshes: [2, '^tail-trim$'] },
  hood: { meshes: [6] },
} };

test('GLB part references own anonymous trim, windows and hidden meshes', () => {
  const { groups } = exportFixture(config);
  assert.deepEqual(groups.map(g => [g.name, g.rig]), [
    ['material-0', 1], ['material-1', 1], ['material-2', 6],
    ['material-3', 6], ['material-4', 0], ['material-6', 5],
  ]);
});

test('configured door pivot excludes window bounds and is shared with its window', () => {
  const { groups } = exportFixture(config);
  const door = groups.find(g => g.name === 'material-0');
  const window = groups.find(g => g.name === 'material-1');
  [1.15, 0.6, 1.2].forEach((v, i) => assert.ok(Math.abs(door.pivot[i] - v) < 1e-5, JSON.stringify(door)));
  assert.deepEqual(window.pivot, door.pivot);
});

test('explicit part assignments mark the asset as CARM v5', () => {
  assert.equal(exportFixture(config).version, 5);
});

test('missing source normals are marked for geometric fallback in the loader', () => {
  for (const group of exportFixture(config).groups) {
    assert.deepEqual(group.firstNormal, [0, 0, 0]);
  }
});

test('v5 preserves submillimeter layers instead of collapsing them together', () => {
  const { groups } = exportFixture(config, 0.0004);
  assert.equal(groups[0].positionScale, 10000);
  assert.ok(Math.abs(groups[0].firstY - 0.0004) < 0.00001);
});

test('v5 uses millimeter precision for groups outside the fine int16 range', () => {
  const { groups } = exportFixture(config, 4);
  assert.equal(groups[0].positionScale, 1000);
  assert.equal(groups[0].firstY, 4);
});

test('models without part metadata retain legacy name-based rigging', () => {
  const { version, groups } = exportFixture();
  assert.equal(version, 4);
  assert.equal(groups.length, 7);
  assert.equal(groups.find(g => g.name === 'material-4').rig, 6);
  assert.equal(groups.find(g => g.name === 'material-6').rig, 5);
});
