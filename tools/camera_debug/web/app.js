'use strict';

// Camera debug UI. No build step and no CDN: the page has to work on the car
// PC with no internet, so it is plain HTML/CSS/JS talking to the tool's JSON API.
//
// Live numbers are polled rather than pushed: the capture threads must stay
// free of any HTTP work, and 4 polls a second is enough to read a rate.

const POLL_MS = 250;
const RESCAN_MS = 5000;

const state = {
  devices: [],
  signature: '',
  running: false,
  slots: [newSlot(), newSlot(false)],
  live: [],
  lastStats: null,
  pollTimer: null,
  rescanTimer: null,
  combined: null,
};

// buffersize defaults to 2, not 1: with a single V4L2 buffer the driver cannot
// queue the next frame while the app holds the current one, and the second
// camera then silently delivers about half the requested rate. Measured
// 2026-10-16 on this rig, 1920x1080@30 MJPG: buffersize=1 -> 30 fps + 15 fps,
// buffersize=2 -> 30 fps + 30 fps (frame counts within 1 frame over 1191).
// The driving stack uses 1 for the narrow camera and 2 for the wide one; set 1
// by hand when reproducing the stack's exact behaviour.
function newSlot(enabled = true) {
  return { enabled, device: '', fourcc: 'MJPG', width: 1920, height: 1080, fps: 30, buffersize: 2, decode: false, preview: true };
}

// Cameras advertise their modes largest-first (the DECXIN lists 3840x2160
// first), so "first advertised mode" is the worst possible default: on a shared
// USB 2.0 bus a 4K MJPEG request forces the biggest altsetting and the kernel
// can refuse the second stream outright. Prefer what the driving stack uses.
function defaultSize(sizes) {
  if (!sizes.length) return null;
  // 1920x1080 first: measured 2026-10-16 on this rig, two 1080p streams with
  // buffersize 2 held the same rate to within one frame over 40 s, while
  // 1280x720@20 came out 18 vs 20 fps.
  const preferred = sizes.find((s) => s.w === 1920 && s.h === 1080)
    || sizes.find((s) => s.w === 1280 && s.h === 720);
  if (preferred) return preferred;
  return sizes.slice().sort((a, b) => (a.w * a.h) - (b.w * b.h))[0];
}

function defaultFps(size) {
  if (!size || !size.fps.length) return null;
  // 30 first (both cameras hit exactly 30.0), then the closest below it.
  return size.fps.slice().sort((a, b) => Math.abs(a - 30) - Math.abs(b - 30))[0];
}

const $ = (sel) => document.querySelector(sel);

function fmt(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return typeof v === 'number' ? Number(v.toFixed(digits)).toString() : String(v);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------------------------------------------------------------- devices --

function usbLabel(cam) {
  const u = cam.usb || {};
  const where = u.port || u.root_port;
  if (!u.bus || !where) return '';
  return `usb${u.bus}:${where}${u.behind_hub ? '（经 Hub）' : ''}`;
}

function deviceLabel(cam) {
  const name = cam.by_id ? `${cam.card}` : `${cam.card} (${cam.node})`;
  const held = cam.holders && cam.holders.length ? ` ⚠ 被 PID ${cam.holders.join(',')} 占用` : '';
  const usb = usbLabel(cam);
  const where = usb ? `${cam.node} · ${usb}` : cam.node;
  return cam.fake ? `${name} · 虚拟${held}` : `${name} · ${where}${held}`;
}

// Two cameras on one USB 2.0 bus share one isochronous budget, and the driver
// reserves it per altsetting rather than per measured bitrate - so whether both
// can stream together is not something this page can predict. State the fact and
// point at the kernel log; do not claim a resolution is required. Measured
// 2026-10-16 on this rig: 1920x1080 + 1920x1080 MJPG streamed together fine
// (20.0 / 15.0 fps), while an earlier attempt with the DECXIN behind the hub was
// refused with "Not enough bandwidth for altsetting 11".
function sharedBusWarning() {
  const chosen = state.slots.filter((s) => s.enabled && s.device);
  if (chosen.length !== 2) return '';
  const usbs = chosen.map((s) => {
    const cam = state.devices.find((c) => c.node === s.device);
    return (cam && cam.usb) || {};
  });
  if (!usbs[0].bus || usbs[0].bus !== usbs[1].bus) return '';
  const scope = usbs[0].root_port === usbs[1].root_port ? '同一个上游端口之下' : '同一条 USB 总线';
  return `两台相机在${scope}（usb${usbs[0].bus}），共用一条总线的等时带宽；`
       + '本机实测 1080p+1080p 可以同时跑。若哪一路起不来（内核报 Not enough bandwidth），'
       + '再降分辨率/帧率或把一台换到另一条 USB 总线。';
}

function listSignature() {
  return state.devices.map((c) => `${c.node}:${c.capture}:${(c.holders || []).join(',')}`).join('|');
}

async function refreshDevices(force) {
  const data = await api('/api/devices');
  state.devices = data.cameras.filter((c) => c.capture);
  $('#v4l2Hint').textContent = data.v4l2_ctl ? '' : '（未找到 v4l2-ctl：模式列表不可用，请手动填写分辨率与帧率）';

  const busy = state.devices.filter((c) => c.holders && c.holders.length);
  $('#stackHint').textContent = busy.length
    ? `检测到 ${busy.map((c) => c.node).join(', ')} 被 PID ${[].concat(...busy.map((c) => c.holders)).join(',')} 占用（很可能是行车栈的 camerad）`
    : '';

  const signature = listSignature();
  if (force || signature !== state.signature) {
    state.signature = signature;
    buildSlots();
  }
}

function cameraModes(node) {
  return (node && node.modes) || [];
}

// ------------------------------------------------------------------ slots --

function buildSlots() {
  const host = $('#slots');
  host.innerHTML = '';
  state.slots.forEach((slot, i) => host.appendChild(buildSlot(slot, i)));
  // refreshSlotUI() looks its controls up with getElementById, so it can only
  // run once the boxes are in the document - calling it from inside buildSlot()
  // silently did nothing and left every dropdown (resolution, fps) empty.
  state.slots.forEach((_, i) => refreshSlotUI(i));
}

function buildSlot(slot, i) {
  const box = el('div', 'slot');
  box.id = `slot${i}`;

  const head = el('h3');
  head.appendChild(el('span', null, `相机 ${i + 1}`));
  const enable = el('label', 'tag');
  const enableBox = el('input');
  enableBox.type = 'checkbox';
  enableBox.checked = slot.enabled;
  enableBox.id = `slot${i}-enabled`;
  enableBox.addEventListener('change', () => {
    slot.enabled = enableBox.checked;
    refreshSlotUI(i);
  });
  enable.append(enableBox, document.createTextNode(' 使用'));
  head.appendChild(enable);
  const tag = el('span', 'tag', '');
  tag.id = `slot${i}-device-tag`;
  head.appendChild(tag);
  box.appendChild(head);

  const grid = el('div', 'grid');
  const field = (label, control) => {
    grid.append(el('label', null, label), control);
  };

  const device = el('select');
  device.id = `slot${i}-device`;
  state.devices.forEach((cam) => {
    const opt = el('option', null, deviceLabel(cam));
    opt.value = cam.node;
    device.appendChild(opt);
  });
  device.addEventListener('change', () => {
    slot.device = device.value;
    refreshSlotUI(i);
  });

  const fourcc = el('select');
  fourcc.id = `slot${i}-fourcc`;
  ['MJPG', 'YUYV'].forEach((name) => {
    const opt = el('option', null, name);
    opt.value = name;
    fourcc.appendChild(opt);
  });
  fourcc.addEventListener('change', () => {
    slot.fourcc = fourcc.value;
    refreshSlotUI(i);
  });

  const res = el('select');
  res.id = `slot${i}-res`;

  const fps = el('input');
  fps.type = 'number';
  fps.step = '0.01';
  fps.min = '1';
  fps.max = '120';
  fps.id = `slot${i}-fps`;
  fps.addEventListener('change', () => { slot.fps = parseFloat(fps.value) || 30; });

  const buffer = el('input');
  buffer.type = 'number';
  buffer.min = '1';
  buffer.max = '8';
  buffer.id = `slot${i}-buf`;
  buffer.addEventListener('change', () => { slot.buffersize = parseInt(buffer.value, 10) || 1; });

  const options = el('div', 'inline');
  const decode = el('input');
  decode.type = 'checkbox';
  decode.id = `slot${i}-decode`;
  decode.addEventListener('change', () => { slot.decode = decode.checked; });
  const decodeLabel = el('label', null, '测 PyAV 解码耗时');
  decodeLabel.prepend(decode);

  const preview = el('input');
  preview.type = 'checkbox';
  preview.checked = true;
  preview.id = `slot${i}-preview`;
  preview.addEventListener('change', () => { slot.preview = preview.checked; });
  const previewLabel = el('label', null, '预览');
  previewLabel.prepend(preview);
  options.append(decodeLabel, previewLabel);

  field('设备', device);
  field('格式', fourcc);
  field('分辨率', res);
  field('帧率', fps);
  field('缓冲', buffer);
  field('测量', options);
  box.appendChild(grid);

  const list = el('datalist');
  list.id = `slot${i}-fps-list`;
  fps.setAttribute('list', `slot${i}-fps-list`);
  box.appendChild(list);

  const note = el('div', 'small muted');
  note.id = `slot${i}-note`;
  box.appendChild(note);

  // buildSlots() calls refreshSlotUI() for every slot once the boxes are
  // attached; doing it here would hit the `if (!box) return` guard.
  return box;
}

// Repopulate the derived controls (format/resolution/rate list) for one slot
// from whatever the selected device actually advertises.
function refreshSlotUI(i) {
  const slot = state.slots[i];
  const box = document.getElementById(`slot${i}`);
  if (!box) return;
  const device = document.getElementById(`slot${i}-device`);
  const fourcc = document.getElementById(`slot${i}-fourcc`);
  const res = document.getElementById(`slot${i}-res`);
  const fps = document.getElementById(`slot${i}-fps`);
  const buffer = document.getElementById(`slot${i}-buf`);
  const list = document.getElementById(`slot${i}-fps-list`);
  const note = document.getElementById(`slot${i}-note`);
  const enabled = document.getElementById(`slot${i}-enabled`);
  const tag = document.getElementById(`slot${i}-device-tag`);

  const cams = state.devices;
  if (!slot.device || !cams.some((c) => c.node === slot.device)) {
    slot.device = cams.length ? cams[Math.min(i, cams.length - 1)].node : '';
  }
  device.value = slot.device;
  const cam = cams.find((c) => c.node === slot.device);
  const modes = cameraModes(cam);
  tag.textContent = cam ? (cam.fake ? '虚拟相机' : cam.node) : '';

  const formats = [...new Set(modes.map((m) => m.fourcc))];
  [...fourcc.options].forEach((opt) => { opt.hidden = formats.length > 0 && !formats.includes(opt.value); });
  if (formats.length && !formats.includes(slot.fourcc)) slot.fourcc = formats[0];
  fourcc.value = slot.fourcc;

  const sizes = [];
  modes.filter((m) => m.fourcc === slot.fourcc).forEach((m) => {
    if (!sizes.some((s) => s.w === m.width && s.h === m.height)) sizes.push({ w: m.width, h: m.height, fps: m.fps });
  });
  res.innerHTML = '';
  if (!sizes.length) {
    const opt = el('option', null, `${slot.width}x${slot.height}（手填）`);
    opt.value = `${slot.width}x${slot.height}`;
    res.appendChild(opt);
    note.textContent = '该设备未上报模式列表（缺少 v4l2-ctl 或非 V4L2 后端）：请手动填写分辨率/帧率，实际结果仍以「协商」为准。';
    list.innerHTML = '';
  } else {
    sizes.forEach((s) => {
      const opt = el('option', null, `${s.w} × ${s.h}${s.fps.length ? `  (${s.fps.join(' / ')} fps)` : ''}`);
      opt.value = `${s.w}x${s.h}`;
      res.appendChild(opt);
    });
    note.textContent = '';
  }

  res.onchange = () => {
    const [w, h] = res.value.split('x').map((v) => parseInt(v, 10));
    slot.width = w || slot.width;
    slot.height = h || slot.height;
    const size = sizes.find((s) => s.w === slot.width && s.h === slot.height);
    if (size && size.fps.length) slot.fps = size.fps[0];
    refreshSlotUI(i);
  };
  const current = sizes.find((s) => s.w === slot.width && s.h === slot.height) || defaultSize(sizes);
  if (current) {
    res.value = `${current.w}x${current.h}`;
    slot.width = current.w;
    slot.height = current.h;
  }
  if (!fps.value || !(current && current.fps).includes(parseFloat(fps.value))) {
    slot.fps = defaultFps(current) || slot.fps;
  }
  fps.value = slot.fps;
  buffer.value = slot.buffersize;
  list.innerHTML = '';
  if (current) {
    current.fps.forEach((f) => {
      const opt = el('option');
      opt.value = String(f);
      list.appendChild(opt);
    });
  }
  box.classList.toggle('disabled', !enabled.checked);
  enabled.checked = slot.enabled;

  const busHint = document.getElementById('busHint');
  if (busHint) {
    const text = sharedBusWarning();
    busHint.textContent = text;
    busHint.hidden = !text;
  }
}

// ------------------------------------------------------------------ live --

function buildLive(cams) {
  const host = $('#live');
  host.innerHTML = '';
  state.live = cams.map((cam, i) => {
    const box = el('div', 'livecam');
    const head = el('div', 'livehead');
    const dot = el('span', 'dot starting');
    const usb = usbLabel(cam);
    const title = el('span', null, `${cam.label} · ${cam.device}${usb ? ` · ${usb}` : ''}`);
    head.append(dot, title);

    const img = el('img', 'preview');
    img.alt = `${cam.label} 预览`;
    if (cam.requested.preview) img.src = `/preview/${i}?fps=10`;

    const big = el('div', 'bigfps');
    const value = el('span', 'value', '—');
    const unit = el('span', 'unit', '帧/秒（实测读取）');
    big.append(value, unit);

    const spark = document.createElement('canvas');
    spark.className = 'spark';

    const kv = el('table', 'kv');
    const rows = {};
    const addRow = (key, label) => {
      const tr = el('tr');
      const k = el('td', 'k', label);
      const v = el('td', 'v');
      tr.append(k, v);
      kv.appendChild(tr);
      rows[key] = v;
    };
    addRow('state', '状态');
    addRow('mode', '配置 / 协商');
    addRow('frames', '帧数 / 读取失败');
    addRow('interval', '帧间隔 p50 / p90 / p99');
    addRow('block', 'read() 阻塞 p50 / p99');
    addRow('decode', 'PyAV 解码 p50 / p99');
    addRow('bandwidth', '单帧大小 / 码率 / 累计');
    addRow('cpu', '采集线程 CPU');

    box.append(head, img, big, spark, kv);
    host.appendChild(box);
    return { box, dot, value, spark, rows, title };
  });

  if (!state.combined) {
    state.combined = el('div', 'combined');
    $('#live').parentElement.appendChild(state.combined);
  }
}

// Clearing the live area on stop matters: a stale MJPEG <img> keeps showing the
// last frame forever, which reads as "still running".
function clearLive(message) {
  const host = $('#live');
  host.innerHTML = '';
  state.live = [];
  host.appendChild(el('div', 'muted', message || '未在采集。'));
  if (state.combined) {
    state.combined.textContent = '';
    state.combined.remove();
    state.combined = null;
  }
}

function drawSpark(canvas, series, requested) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 400;
  const h = 54;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const values = series.filter((v) => v !== null && v !== undefined);
  if (!values.length) return;
  const top = Math.max(requested * 1.2, ...values) * 1.05;

  // Reference line at the requested rate: the gap between it and the curve is
  // what "the camera cannot keep up" looks like.
  ctx.strokeStyle = '#3a4454';
  ctx.setLineDash([4, 4]);
  const y = h - (requested / top) * h;
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(w, y);
  ctx.stroke();
  ctx.setLineDash([]);

  const step = w / (series.length - 1 || 1);
  ctx.strokeStyle = '#3ddc97';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  series.forEach((v, idx) => {
    if (v === null || v === undefined) return;
    const px = idx * step;
    const py = h - (v / top) * h;
    if (!started) {
      ctx.moveTo(px, py);
      started = true;
    } else {
      ctx.lineTo(px, py);
    }
  });
  ctx.stroke();
  ctx.fillStyle = '#93a0b4';
  ctx.font = '10px ui-monospace, monospace';
  ctx.fillText(`${top.toFixed(0)} fps`, 4, 11);
}

function renderStats(stats) {
  state.lastStats = stats;
  const cams = stats.cameras || [];
  if (state.live.length !== cams.length) buildLive(cams);
  cams.forEach((cam, i) => {
    const ui = state.live[i];
    if (!ui) return;
    const m = cam.metrics;
    const requested = cam.requested;
    const neg = cam.negotiated || {};
    ui.dot.className = `dot ${cam.state}`;
    const title = `${cam.label} · ${cam.device} · ${neg.width}x${neg.height} ${neg.fourcc} @${neg.fps}`;
    if (ui.title.textContent !== title) ui.title.textContent = title;

    ui.value.textContent = fmt(m.fps_window, 1);
    ui.value.className = 'value';
    if (cam.state === 'failed') ui.value.classList.add('dead');
    else if (m.fps_window !== null && requested.fps && m.fps_window < requested.fps * 0.6) ui.value.classList.add('low');

    ui.rows.state.textContent = cam.error ? `失败：${cam.error}` : `${cam.state}${cam.passthrough ? ' · JPEG 直通预览' : ''}`;
    ui.rows.mode.textContent = `${requested.width}x${requested.height} @${requested.fps} ${requested.fourcc} buf${requested.buffersize}  →  ${neg.width || '?'}x${neg.height || '?'} @${neg.fps || '?'} ${neg.fourcc || ''}`;
    ui.rows.frames.textContent = `${m.frames} / ${m.failures}`;
    ui.rows.interval.textContent = `${fmt(m.dt_p50)} / ${fmt(m.dt_p90)} / ${fmt(m.dt_p99)} ms  (抖动 ${fmt(m.jitter)}, 最大 ${fmt(m.dt_max)})`;
    ui.rows.block.textContent = `${fmt(m.read_ms_p50)} / ${fmt(m.read_ms_p99)} ms`;
    ui.rows.decode.textContent = requested.decode
      ? `${fmt(m.decode_ms_p50)} / ${fmt(m.decode_ms_p99)} ms  (平均 ${fmt(m.fps_avg)} 帧/秒)`
      : '未开启（在「测量」里勾选）';
    ui.rows.bandwidth.textContent = `${fmt(m.kb_avg)} KB · ${fmt(m.mbps_window, 2)} MB/s · 累计 ${fmt(m.mb_total)} MB`;
    ui.rows.cpu.textContent = `${fmt(m.cpu_pct)} %`;
    drawSpark(ui.spark, m.series || [], requested.fps || 10);
  });

  const comb = stats.combined || {};
  state.combined.textContent = '';
  if ((comb.n_cameras || 0) > 0) {
    state.combined.append(
      `同时采集 ${comb.n_cameras} 路（${(comb.devices || []).join(' + ')}）· 合计读取速率 `,
    );
    const b = el('b', null, `${fmt(comb.read_fps_window)} 帧/秒`);
    state.combined.append(b);
    state.combined.append(
      ` · 平均 ${fmt(comb.read_fps_avg)} 帧/秒 · 合计码率 ${fmt(comb.mbps_window, 2)} MB/s · 读取失败 ${comb.failures || 0} 次 · 已运行 ${fmt(stats.elapsed_s, 0)} 秒`,
    );
  } else {
    state.combined.textContent = '未采集。';
  }
}

// --------------------------------------------------------------- sessions --

async function refreshSessions() {
  const data = await api('/api/sessions');
  renderCompare(data.comparisons || []);
  renderSessions(data.sessions || []);
}

function renderCompare(rows) {
  const host = $('#compare');
  if (!rows.length) {
    host.className = 'muted small';
    host.textContent = '还没有记录。先只跑一个相机，再跑两个，这里就会出现同一设备的「单独 vs 同时」。';
    return;
  }
  host.className = '';
  host.innerHTML = '';
  host.appendChild(el('div', 'small muted', '同一设备 + 同一模式：单独跑 vs 与另一台同时跑。'));
  const table = el('table', 'report');
  table.innerHTML = `<tr><th>设备 / 模式</th><th>单独 帧/秒</th><th>同时 帧/秒</th><th>变化</th><th>同时路数</th><th>单独 MB/s</th><th>同时 MB/s</th><th>记录时间</th></tr>`;
  rows.forEach((row) => {
    const tr = el('tr');
    const delta = row.delta_pct;
    const deltaText = delta === null || delta === undefined ? '—' : `${delta > 0 ? '+' : ''}${delta}%`;
    const deltaCls = delta === null || delta === undefined ? '' : (delta < -2 ? 'down' : delta > 2 ? 'up' : '');
    tr.innerHTML = `
      <td>${row.label || ''} ${row.device} · ${row.width}x${row.height} ${row.fourcc}</td>
      <td class="num">${fmt(row.alone && row.alone.fps_avg)}</td>
      <td class="num">${fmt(row.together && row.together.fps_avg)}</td>
      <td class="num ${deltaCls}">${deltaText}</td>
      <td class="num">${row.together ? row.together.n_cameras : '—'}</td>
      <td class="num">${fmt(row.alone && row.alone.mbps_window, 2)}</td>
      <td class="num">${fmt(row.together && row.together.mbps_window, 2)}</td>
      <td class="small muted">${fmtTime(row.together ? row.together.at : row.alone && row.alone.at)}</td>`;
    table.appendChild(tr);
  });
  host.appendChild(table);
}

function renderSessions(sessions) {
  const host = $('#sessions');
  if (!sessions.length) {
    host.textContent = '（空）';
    return;
  }
  const table = el('table', 'report');
  table.innerHTML = '<tr><th>时间</th><th>时长</th><th>路数</th><th>各相机 平均帧/秒</th><th>合计帧/秒</th><th>合计 MB/s</th></tr>';
  sessions.slice().reverse().forEach((sess) => {
    const tr = el('tr');
    const perCam = sess.cameras
      .map((c) => `${c.device.split('/').pop()}:${fmt(c.metrics.fps_avg)}${(c.metrics.error ? ' ✗' : '')}`)
      .join(' · ');
    tr.innerHTML = `
      <td class="small">${fmtTime(sess.started_at)}</td>
      <td class="num">${fmt(sess.duration_s, 0)}s</td>
      <td class="num">${sess.cameras.length}</td>
      <td class="num">${perCam}</td>
      <td class="num">${fmt(sess.combined.read_fps_avg)}</td>
      <td class="num">${fmt(sess.combined.mbps_window, 2)}</td>`;
    table.appendChild(tr);
  });
  host.innerHTML = '';
  host.appendChild(table);
}

function fmtTime(epochSeconds) {
  if (!epochSeconds) return '—';
  const d = new Date(epochSeconds * 1000);
  const p = (v) => String(v).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// --------------------------------------------------------------- controls --

function selectedCams() {
  return state.slots.filter((s) => s.enabled && s.device).map((s) => ({
    device: s.device, width: s.width, height: s.height, fps: s.fps,
    fourcc: s.fourcc, buffersize: s.buffersize, decode: s.decode, preview: s.preview,
  }));
}

function setRunning(running) {
  state.running = running;
  $('#btnStart').disabled = running;
  $('#btnStop').disabled = !running;
  document.querySelectorAll('#slots select, #slots input').forEach((n) => { n.disabled = running; });
}

function showMessage(text, kind) {
  const node = $('#startMsg');
  node.textContent = text;
  node.className = `msg ${kind || ''}`;
}

async function startCapture() {
  const cams = selectedCams();
  if (!cams.length) return showMessage('请先选择至少一个相机', 'bad');
  showMessage('正在打开相机…');
  $('#warnBox').classList.add('hidden');
  try {
    const res = await api('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cameras: cams }),
    });
    const failed = (res.cameras || []).filter((c) => c.state === 'failed');
    if (res.warnings && res.warnings.length) {
      const box = $('#warnBox');
      box.textContent = res.warnings.join('\n');
      box.classList.remove('hidden');
    }
    setRunning(true);
    showMessage(failed.length ? '有相机打开失败，见下方提示' : '采集中', failed.length ? 'bad' : 'good');
    buildLive((res.cameras || []).map((c, i) => ({
      label: `相机${i + 1}`, device: c.device,
      requested: cams[i], negotiated: {}, state: c.state, error: c.error,
    })));
    pollStats();
  } catch (err) {
    showMessage(err.message, 'bad');
  }
}

async function stopCapture() {
  try {
    await api('/api/stop', { method: 'POST' });
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  setRunning(false);
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  clearLive('已停止。历史上面的「单独 ↔ 同时」对比里可以看到刚才那一段。');
  showMessage('已停止，相机已释放');
  await refreshSessions();
}

async function pollStats() {
  clearInterval(state.pollTimer);
  const tick = async () => {
    if (!state.running) return;
    try {
      const stats = await api('/api/stats');
      renderStats(stats);
      if (!stats.running) {
        setRunning(false);
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        clearLive('采集已结束。');
        showMessage('采集已结束（相机掉线、或已在别处停止）');
        await refreshSessions();
      }
    } catch (err) {
      showMessage(err.message, 'bad');
    }
  };
  await tick();
  state.pollTimer = setInterval(tick, POLL_MS);
}

async function copySummary() {
  const data = await api('/api/sessions');
  const lines = ['摄像头读取速率记录', ''];
  (data.comparisons || []).forEach((row) => {
    lines.push(`${row.device} ${row.width}x${row.height} ${row.fourcc}: 单独 ${fmt(row.alone && row.alone.fps_avg)} 帧/秒` +
      (row.together ? `, 同时(${row.together.n_cameras} 路) ${fmt(row.together.fps_avg)} 帧/秒 (${row.delta_pct > 0 ? '+' : ''}${row.delta_pct}%)` : ''));
  });
  lines.push('', '完整会话：');
  (data.sessions || []).forEach((s) => {
    lines.push(`${fmtTime(s.started_at)}  ${s.duration_s}s  ${s.cameras.length} 路  ` +
      s.cameras.map((c) => `${c.device}:${fmt(c.metrics.fps_avg)}fps@${c.negotiated.width}x${c.negotiated.height}`).join(' + ') +
      `  合计 ${fmt(s.combined.read_fps_avg)}fps / ${fmt(s.combined.mbps_window, 2)}MB/s`);
  });
  const text = lines.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    $('#copyMsg').textContent = '已复制到剪贴板';
  } catch (err) {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    $('#copyMsg').textContent = '已复制（兼容模式）';
  }
  setTimeout(() => { $('#copyMsg').textContent = ''; }, 3000);
}

function wireControls() {
  $('#btnStart').addEventListener('click', startCapture);
  $('#btnStop').addEventListener('click', stopCapture);
  $('#btnRefresh').addEventListener('click', () => refreshDevices(true));
  $('#btnExport').addEventListener('click', () => { window.location.href = '/api/export'; });
  $('#btnCopy').addEventListener('click', () => copySummary().catch((e) => { $('#copyMsg').textContent = e.message; }));
  $('#btnClear').addEventListener('click', async () => {
    await api('/api/sessions/clear', { method: 'POST' });
    await refreshSessions();
  });
  window.addEventListener('resize', () => {
    // Redraw from the last poll instead of restarting the poll: the sparkline
    // is sized in CSS pixels, so a resize needs a redraw, not a new request.
    const stats = state.lastStats;
    if (!stats) return;
    (stats.cameras || []).forEach((cam, i) => {
      const ui = state.live[i];
      if (ui) drawSpark(ui.spark, cam.metrics.series || [], cam.requested.fps || 10);
    });
  });
}

async function main() {
  wireControls();
  try {
    await refreshDevices(true);
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  await refreshSessions().catch(() => {});
  // Adopt a session that is already running: a page reload (or a second tab)
  // must not lose the live view of a capture that is still going.
  try {
    const stats = await api('/api/stats');
    if (stats.running) {
      setRunning(true);
      renderStats(stats);
      showMessage('正在采集（页面刷新后重新连接）', 'good');
      pollStats();
    }
  } catch (err) {
    showMessage(err.message, 'bad');
  }
  state.rescanTimer = setInterval(() => {
    if (!state.running) refreshDevices(false).catch(() => {});
  }, RESCAN_MS);
}

main();