const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const assets = path.resolve(__dirname, '../../app/src/main/assets');
const bytes = fs.readFileSync(path.join(assets, 'car.bin'));
const manifest = JSON.parse(fs.readFileSync(path.join(assets, 'car-model-source.json'), 'utf8'));
const groups = [], textures = [];
assert.equal(bytes.toString('ascii', 0, 4), 'CARM');
assert.equal(bytes.readUInt32LE(4), 6);
const count = bytes.readUInt32LE(8), names = [];
let off = 12;
for (let i=0; i<count; i++) {
  const n=bytes.readUInt16LE(off); off+=2; names.push(bytes.toString('utf8',off,off+n)); off+=n;
}
for (let i=0; i<count; i++) {
  const vc=bytes.readUInt32LE(off+4), ic=bytes.readUInt32LE(off+8);
  assert.equal(bytes.readUInt32LE(off+12),0);
  const g={ name:names[i], vc, ic, rig:bytes[off+16], role:bytes[off+19],
    pivot:[20,24,28].map(p=>bytes.readFloatLE(off+p)),
    texture:bytes.readInt32LE(off+60), flags:bytes.readUInt32LE(off+64), alpha:bytes.readFloatLE(off+48),
    scale:bytes.readFloatLE(off+32), pos:[], uv:[], idx:[] };
  off+=72;
  for(let k=0;k<vc*3;k++)g.pos.push(bytes.readInt16LE(off+k*2)/g.scale);
  off+=vc*9;
  for(let k=0;k<vc*2;k++)g.uv.push(bytes.readFloatLE(off+k*4));
  off+=vc*8;
  for(let k=0;k<ic;k++)g.idx.push(bytes.readUInt16LE(off+k*2));
  off+=ic*2;groups.push(g);
}
const textureCount=bytes.readUInt32LE(off);off+=4;
for(let i=0;i<textureCount;i++){const n=bytes.readUInt32LE(off);off+=4;textures.push(bytes.subarray(off,off+n));off+=n;}

test('bundled asset is exactly the user Song PLUS V2 source conversion',()=>{
  assert.equal(manifest.sourceGlb.sha256,'4b2ffab4cc240ad6b8d0d958da9038db5a75530480cd6c12c063d42b6cec0b87');
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),manifest.output.sha256);
  assert.equal(off,bytes.length);
  assert.equal(groups.reduce((n,g)=>n+g.vc,0),538436);
  assert.equal(groups.reduce((n,g)=>n+g.ic/3,0),179890);
  assert.equal(textures.length,9);
});

test('every v6 triangle is 16-bit-safe and every texture reference is valid',()=>{
  for(const g of groups){
    assert.ok(g.vc<=65535,g.name);
    assert.ok(g.idx.every(i=>i<g.vc),g.name);
    assert.ok(g.uv.every(Number.isFinite));
    assert.ok(g.texture>=-1 && g.texture<textures.length);
  }
});

test('windows follow their doors and exact mirror-indicator aliases match left and right',()=>{
  for(const rig of [1,2,3,4]){
    const parts=groups.filter(g=>g.rig===rig), glass=parts.filter(g=>g.flags&1);
    assert.ok(parts.length>1 && glass.length>0);
    assert.ok(parts.every(g=>JSON.stringify(g.pivot)===JSON.stringify(parts[0].pivot)));
  }
  assert.ok(groups.some(g=>g.name==='turn_L' && g.rig===1 && g.role===1));
  assert.ok(groups.some(g=>g.name==='turn_R' && g.rig===2 && g.role===2));
  assert.ok(groups.some(g=>g.name==='turn_tail_L' && g.rig===6 && g.role===1));
  assert.ok(groups.some(g=>g.name==='turn_tail_R' && g.rig===6 && g.role===2));
});

test('continuous-alpha lamp textures retain repeat UVs and glass/light distinction',()=>{
  const overlay=groups.find(g=>g.name==='TL_Alpha');
  assert.equal(overlay.flags,2);
  assert.ok(overlay.texture>=0);
  assert.ok(overlay.uv.some(v=>v<0) && overlay.uv.some(v=>v>1));
  assert.ok(groups.filter(g=>g.name==='inner_light_glass').every(g=>!(g.flags&1)&&g.role===5));
  const heads=groups.filter(g=>g.name==='head_lens');
  assert.ok(heads.length>0 && heads.every(g=>g.role===8 && !(g.flags&1)));
  assert.ok(groups.filter(g=>g.name==='fog_R').every(g=>g.role===0));
  assert.ok(groups.filter(g=>g.name==='glass_Alph').every(g=>(g.flags&3)===3));
  const tails=groups.filter(g=>g.role===7);
  assert.ok(tails.some(g=>g.name==='Tail_Light_Emis'));
  assert.ok(tails.every(g=>g.flags===2 && Math.abs(g.alpha-0.7)<1e-6));
});
