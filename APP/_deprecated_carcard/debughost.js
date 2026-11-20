(function () {
  'use strict';
  if (window.uiplus) return;

  var LS = 'd3-debug', SS = 'd3-debug-host';
  function store(ss) { try { return ss ? window.sessionStorage : window.localStorage; } catch (e) { return null; } }
  function rd(k, ss) { try { return JSON.parse(store(ss).getItem(k) || 'null'); } catch (e) { return null; } }
  function wr(k, v, ss) { try { store(ss).setItem(k, JSON.stringify(v)); } catch (e) {} }
  function copy(o) { return o === undefined ? undefined : JSON.parse(JSON.stringify(o)); }
  function E(code, message, details) { var e = { code: code, message: message || code }; if (details) e.details = details; return e; }
  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }

  var CAT = null, catP = null, VF = {}, SF = {}, AF = {}, COMP = {}, TOOL = {};
  function load(src) {
    return new Promise(function (ok, no) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = ok;
      s.onerror = function () { no(E('BACKEND_UNAVAILABLE', '调试宿主没加载到 ' + src)); };
      (document.head || document.documentElement).appendChild(s);
    });
  }
  function base(k) { return k.slice(0, k.lastIndexOf('.') + 1); }
  function catalog() {
    if (!catP) catP = (window.D3Fields ? Promise.resolve() : load('uiplus-fields.js')).then(function () { return load('debug-catalog.js'); }).then(function () {
      CAT = window.D3DebugCatalog;
      CAT.vehicle = window.D3Fields.vehicle; CAT.media = window.D3Fields.media; CAT.system = window.D3Fields.system;
      CAT.vehicle.forEach(function (f) { VF[f.k] = f; if (f.c) COMP[base(f.k) + f.c.k] = f.k; });
      CAT.system.forEach(function (f) { SF[f.k] = f; });
      CAT.actions.forEach(function (a) { AF[a.m] = a; });
      CAT.tools.forEach(function (t) { TOOL[t.function.name] = t.function; });
      return CAT;
    });
    return catP;
  }

  var h0 = new Date().getHours();
  var DEF_V = {
    'driving.speed': 0, 'driving.gear': 3, 'driving.sessionDuration': 0, 'driving.sessionDistance': 0, 'driving.journeyDistance': 0, 'driving.journeyDistanceUnit': 1,
    'driving.totalDistance': 12873000, 'driving.totalDistanceUnit': 'm', 'driving.evTotalDistance': 6210, 'driving.hevTotalDistance': 6663,
    'driving.evSessionDistance': 0, 'driving.hevSessionDistance': 0, 'driving.averageSpeed': 32, 'power.level': 3, 'identity.vin': 'LGXCE4CB0R0123456',
    'energy.batteryPercent': 76, 'energy.fuelPercent': 58, 'energy.electricRange': 82, 'energy.fuelRange': 640, 'energy.wirelessCharging': 2, 'energy.socSave': 1,
    'energy.socTarget': 25, 'energy.regenerationLevel': 1, 'energy.operationMode': 1, 'energy.mode': 1, 'energy.simulator': 0, 'energy.chargingState': 1,
    'energy.connectorState': 1, 'energy.charging.power': 0, 'energy.charging.capacity': 0, 'energy.charging.remainingTime': null, 'energy.charging.type': 1,
    'energy.consumption.averageElectric': 15.2, 'energy.consumption.averageElectricUnit': 1, 'energy.consumption.recentElectric': 14.8,
    'energy.consumption.instantElectric': 0, 'energy.consumption.averageFuel': 5.1, 'energy.consumption.recentFuel': 4.9, 'energy.consumption.instantFuel': 0,
    'energy.consumption.instantFuelUnit': 1, 'energy.wirelessChargingState': 3,
    'ac.power': 0, 'ac.compressor': 0, 'ac.driverTemperature': 24, 'ac.driverTemperatureUnit': 1, 'ac.indoorTemperature': 24, 'ac.indoorTemperatureUnit': 1,
    'ac.outdoorTemperature': 21, 'ac.outdoorTemperatureUnit': 1, 'ac.fanLevel': 0, 'ac.circulation': 0, 'ac.frontDefrost': 0, 'ac.rearDefrost': 0,
    'seat.driver.heating': 1, 'seat.passenger.heating': 1, 'seat.driver.ventilation': 1, 'seat.passenger.ventilation': 1, 'seat.driver.beltFastened': 1,
    'body.windows.anyOpen': 0, 'body.windows.ventilation': 0, 'body.windows.halfOpen': 0, 'body.sunroof.position': 0, 'body.sunshade.position': 0,
    'body.tailgate.state': 0, 'body.doors.frontLeft.state': 0, 'body.doors.frontRight.state': 0, 'body.doors.rearLeft.state': 0, 'body.doors.rearRight.state': 0,
    'body.hood.state': 0, 'body.windows.frontLeft.position': 0, 'body.windows.frontRight.position': 0, 'body.windows.rearLeft.position': 0, 'body.windows.rearRight.position': 0,
    'light.daytimeRunning': 1, 'light.side': 0, 'light.lowBeam': 0, 'light.dynamicColorful': 2, 'light.ambient.area': 3, 'light.ambient.brightness': 4,
    'light.ambient.color': 1, 'light.ambient.musicMode': 2, 'light.highBeam': 0, 'light.turnSignal.left': 0, 'light.turnSignal.right': 0,
    'light.frontFog': 0, 'light.rearFog': 0, 'light.hazard': 2, 'light.brake': 3, 'body.pedals.accelerator': 0, 'body.pedals.brake': 0,
    'environment.pm25.indoor': 12, 'environment.pm25.outdoor': 35, 'environment.pm25.indoorLevel': 1, 'environment.pm25.outdoorLevel': 2, 'environment.slope': 0,
    'tyre.displayUnit': 1, 'display.dayNightMode': h0 >= 19 || h0 < 6 ? 32 : 16, 'engine.rpm': 0, 'engine.coolantTemperature': 85,
    'warning.lowVoltageBatteryFault': 0, 'warning.keyBatteryLow': 2, 'audio.volume': 18, 'audio.navigationVolume': 12, 'audio.muted': 0,
    'maintenance.distance': 5000, 'maintenance.days': 120
  };
  ['frontLeft', 'frontRight', 'rearLeft', 'rearRight'].forEach(function (w, i) {
    var t = 'tyre.' + w + '.';
    DEF_V[t + 'pressure'] = i === 2 ? 24 : 25; DEF_V[t + 'pressureUnit'] = 1;
    DEF_V[t + 'state'] = 0; DEF_V[t + 'stateSource'] = 'tyre'; DEF_V[t + 'temperature'] = 28;
  });
  var DEF_S = {
    'network.connected': true, 'network.transport': 'wifi', 'network.validated': true, 'network.captivePortal': false, 'network.metered': false, 'network.vpn': false,
    'wifi.state': 3, 'wifi.enabled': true, 'wifi.connected': true, 'wifi.ssid': 'MyPhone', 'wifi.rssi': -55, 'wifi.signalLevel': 3, 'wifi.linkSpeed': 144, 'wifi.frequency': 5180,
    'bluetooth.state': 12, 'bluetooth.enabled': true, 'bluetooth.connectionState': 2, 'bluetooth.mediaConnectionState': 2, 'bluetooth.phoneConnectionState': 2,
    'cellular.dataEnabled': true, 'cellular.dataState': 2, 'cellular.networkType': 13, 'cellular.networkGeneration': '4G', 'cellular.displayType': '4G', 'cellular.simState': 5,
    'cellular.operator': '中国移动', 'cellular.roaming': false, 'cellular.signalLevel': 3, 'cellular.signalDbm': -95, 'airplane.enabled': false, 'hotspot.state': 11, 'hotspot.enabled': false,
    'gps.permissionGranted': true, 'gps.enabled': true, 'gps.latitude': 22.6847, 'gps.longitude': 114.3542, 'gps.altitude': 38, 'gps.speed': 0, 'gps.bearing': 0, 'gps.accuracy': 6,
    'gps.timestamp': null, 'gps.ageMs': 1000
  };
  var PROPS = { 'ro.build.version.release': '10', 'ro.build.version.sdk': '29', 'ro.product.manufacturer': 'BYD', 'ro.product.model': 'DiLink', 'ro.build.type': 'user' };

  var TRACKS = [
    { title: '夜航星', artist: '3xCard 调试', colors: ['#2a3f7a', '#0c1230', '#f6e9bd'],
      lines: ['把星星装进后备箱', '沿着环路慢慢开', '车灯照亮一小段路', '剩下的交给月亮', '收音机里有人在唱', '唱一首没有名字的歌', '红绿灯一盏一盏亮', '像在给我数着心跳', '夜航星 夜航星', '你在高处陪我走', '不管开出多远', '抬头就能看见你', '隧道把城市藏起来',
        '出口又把灯火还给我', '夜航星 夜航星', '你在高处陪我走', '不管开出多远', '抬头就能看见你', '熄火之前再看一眼', '今晚的天很亮'] },
    { title: '早高峰', artist: '3xCard 调试', colors: ['#ff9a52', '#c8325a', '#ffe48f'],
      lines: ['红灯还有三十秒', '收音机在放老歌', '前车的尾灯一闪一闪', '像在打着拍子', '咖啡还有一点烫', '导航说前面在修路', '那就换一条小路', '路边的早餐摊冒着热气', '慢一点也没关系', '今天的风刚刚好', '车窗开一条缝', '让早晨进来', '慢一点也没关系',
        '今天的风刚刚好', '绿灯亮了', '我们出发'] },
    { title: '回家', artist: '3xCard 调试', colors: ['#3a9b8c', '#123446', '#ffd27a'],
      lines: ['熄火之前', '再听完这一首', '车库的灯亮了', '今天也辛苦了', '后视镜里的自己', '有一点累 也有一点笑', '手机里有条消息', '说饭已经做好', '回家 回家', '路再远也有尽头', '回家 回家', '有人在等我', '把座椅调回原位', '把音乐调小一点',
        '回家 回家', '有人在等我'] }
  ];
  var LINE = 3500, covers = {};
  function cover(i) {
    if (own(covers, i)) return covers[i];
    var t = TRACKS[i], n = 256, c = document.createElement('canvas');
    c.width = c.height = n;
    var x = c.getContext('2d'), g = x.createLinearGradient(0, 0, 0, n);
    g.addColorStop(0, t.colors[0]); g.addColorStop(1, t.colors[1]);
    x.fillStyle = g; x.fillRect(0, 0, n, n);
    x.fillStyle = t.colors[2]; x.beginPath(); x.arc(n * 0.66, n * 0.38, n * 0.15, 0, Math.PI * 2); x.fill();
    [[0.62, 'rgba(0,0,0,.22)', 0.9], [0.74, 'rgba(0,0,0,.38)', 1.6]].forEach(function (hh) {
      x.fillStyle = hh[1]; x.beginPath(); x.moveTo(0, n);
      for (var k = 0; k <= 16; k++) x.lineTo(k / 16 * n, n * hh[0] + Math.sin(k * hh[2] + i) * n * 0.05);
      x.lineTo(n, n); x.closePath(); x.fill();
    });
    x.fillStyle = '#fff'; x.font = '600 28px "MiSans","PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif';
    x.fillText(t.title, 20, n - 24);
    try { covers[i] = c.toDataURL('image/png'); } catch (e) { covers[i] = ''; }
    return covers[i];
  }
  function lyricLines(i) { return TRACKS[i].lines.map(function (s, k) { return { text: s, timeMs: k * LINE }; }); }
  function duration(i) { return TRACKS[i].lines.length * LINE + LINE; }

  var saved = rd(LS) || {};
  var ST = { v: saved.v || {}, s: saved.s || {}, m: saved.m || {}, sel: Array.isArray(saved.sel) ? saved.sel : [], auto: saved.auto || {},
    track: TRACKS[saved.track] ? saved.track : 0, lyrOff: !!saved.lyrOff, control: saved.control !== false, failCode: saved.failCode === 'RESOURCE_LIMIT' ? 'RESOURCE_LIMIT' : 'BUSY' };
  var hs = rd(SS, true) || {};
  var HOST = { type: /^(preview|dualPip|floating|fullscreen)$/.test(hs.type) ? hs.type : 'dualPip', from: hs.from || 'dualPip', online: true, fail: 0, visible: true, bar: true, nav: true, mask: null, maskTimer: 0 };
  function save() { wr(LS, { v: ST.v, s: ST.s, m: ST.m, sel: ST.sel, auto: ST.auto, track: ST.track, lyrOff: ST.lyrOff, control: ST.control, failCode: ST.failCode }); }
  var DEF = { v: DEF_V, s: DEF_S };
  function value(dom, k) {
    var o = ST[dom];
    if (dom === 's' && k === 'gps.timestamp' && !own(o, k)) { var age = value('s', 'gps.ageMs'); return age === null ? null : Date.now() - age; }
    return own(o, k) ? o[k] : own(DEF[dom], k) ? DEF[dom][k] : null;
  }

  var fns = [], LOG = [];
  function emit(what) { fns.slice().forEach(function (f) { try { f(what); } catch (e) {} }); }
  function brief(v) {
    var s;
    if (v && typeof v.unsubscribe === 'function') return '订阅句柄';
    try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    if (s === undefined) s = '';
    return s.length > 160 ? s.slice(0, 157) + '…' : s;
  }
  function log(m, args, ok, r) {
    LOG.unshift({ t: Date.now(), m: m, a: brief(args), ok: ok, r: ok ? brief(r) : (r && r.code) || 'ERROR', msg: ok ? '' : (r && r.message) || '' });
    if (LOG.length > 300) LOG.length = 300;
    emit('log');
  }
  function call(m, args, fn, always) {
    var p = new Promise(function (ok) {
      if (!always && !HOST.online) throw E('BACKEND_UNAVAILABLE', '连接已断开（调试面板里模拟的）');
      ok(fn());
    });
    p.then(function (r) { log(m, args, true, r); }, function (e) { log(m, args, false, e); });
    return p;
  }
  function args(a) { return Array.prototype.slice.call(a); }

  var subs = [], seq = 0, frame = 0, MAX_SUBS = 32;
  function put(d, k, v) { var p = k.split('.'), n = d; for (var i = 0; i < p.length - 1; i++) n = n[p[i]] = n[p[i]] || {}; n[p[p.length - 1]] = v; }
  function tree(dom, keys) {
    var d = {};
    keys.forEach(function (k) {
      put(d, k, copy(value(dom, k)));
      var f = dom === 'v' && VF[k];
      if (f && f.c) put(d, base(k) + f.c.k, copy(value(dom, base(k) + f.c.k)));
    });
    return { data: d };
  }
  function live() { return subs.filter(function (s) { return s.live; }).length; }
  function flush() {
    frame = 0;
    subs.forEach(function (s) {
      if (!s.live || !s.pend) return;
      var keys = Object.keys(s.pend);
      s.pend = null;
      if (s.dom === 'm') s.cb({ type: 'change', sequence: ++seq, values: { media: snap() } });
      else s.cb(tree(s.dom, keys));
    });
  }
  function later() {
    if (frame) return;
    frame = 1;
    if (!HOST.visible || document.hidden) setTimeout(flush, 0);
    else requestAnimationFrame(flush);
  }
  function changed(dom, keys) {
    if (!HOST.online) return;
    subs.forEach(function (s) {
      if (!s.live || s.dom !== dom) return;
      keys.forEach(function (k) {
        var main = dom === 'v' && COMP[k] ? COMP[k] : k;
        if (dom === 'm' || s.fields.indexOf(main) >= 0) (s.pend = s.pend || {})[main] = 1;
      });
    });
    later();
  }
  function subscribe(dom, m, fields, cb) {
    return call(m, dom === 'm' ? [] : [fields], function () {
      if (typeof cb !== 'function') throw E('INVALID_ARGUMENT', '缺少回调');
      return catalog().then(function () {
        if (dom !== 'm') {
          if (!Array.isArray(fields) || !fields.length || fields.some(function (k) { return typeof k !== 'string'; })) throw E('INVALID_ARGUMENT', '字段必须是非空的字符串数组');
          var bad = fields.filter(function (k) { return !(dom === 'v' ? VF : SF)[k]; });
          if (bad.length) throw E('UNKNOWN_FIELD', '未知字段：' + bad.join('、'));
          if (dom === 'v' && fields.some(function (k) { return VF[k].m === 'none'; })) throw E('UNSUPPORTED', '只能主动查询的字段不能订阅');
        }
        if (HOST.fail > 0) { HOST.fail--; emit('host'); throw E(ST.failCode, '调试面板里模拟的订阅失败'); }
        if (live() >= MAX_SUBS) throw E('RESOURCE_LIMIT', '每页最多 32 个订阅');
        var s = { dom: dom, fields: dom === 'm' ? ['media'] : fields.slice(), cb: cb, live: true, pend: null };
        subs.push(s);
        setTimeout(function () {
          if (!s.live || !HOST.online) return;
          if (dom === 'm') cb({ type: 'snapshot', sequence: ++seq, values: { media: snap() } });
          else cb(tree(dom, s.fields));
        }, 15);
        emit('subs');
        return { unsubscribe: function () { if (s.live) { s.live = false; emit('subs'); } } };
      });
    });
  }
  function query(dom, m, keys) {
    return call(m, [keys], function () {
      return catalog().then(function () {
        if (!Array.isArray(keys) || !keys.length || keys.some(function (k) { return typeof k !== 'string'; })) throw E('INVALID_ARGUMENT', '字段必须是非空的字符串数组');
        var bad = keys.filter(function (k) { return !(dom === 'v' ? VF : SF)[k]; });
        if (bad.length) throw E('UNKNOWN_FIELD', '未知字段：' + bad.join('、'));
        return tree(dom, keys);
      });
    });
  }

  var MP = { playing: false, pos: 0, at: 0, timer: 0 };
  function pos() { return MP.playing ? Math.min(duration(ST.track), MP.pos + Date.now() - MP.at) : MP.pos; }
  function snap() {
    var t = TRACKS[ST.track], p = pos(), n = Math.min(t.lines.length - 1, Math.floor(p / LINE));
    var o = { status: 'ok', trackId: 'debug-' + ST.track, title: t.title, artist: t.artist, packageName: 'com.byd.mediacenter', playing: MP.playing,
      positionMs: Math.round(p), durationMs: duration(ST.track), lyric: t.lines[n], coverUrl: cover(ST.track), updatedAt: Date.now() };
    for (var k in ST.m) if (own(o, k)) o[k] = ST.m[k];
    if (o.status !== 'ok') Object.keys(o).forEach(function (k2) { if (k2 !== 'status' && k2 !== 'updatedAt') o[k2] = null; });
    return o;
  }
  function tick() {
    clearTimeout(MP.timer);
    if (!MP.playing) return;
    var p = pos(), end = duration(ST.track);
    if (p >= end) { setTrack(ST.track + 1, true); return; }
    var next = Math.min(end, (Math.floor(p / LINE) + 1) * LINE);
    MP.timer = setTimeout(function () { changed('m', ['media']); tick(); }, Math.max(30, next - p + 5));
  }
  function setPlaying(on) {
    if (MP.playing === on) return;
    MP.pos = pos(); MP.at = Date.now(); MP.playing = on;
    tick(); changed('m', ['media']); emit('media');
  }
  function setTrack(i, keep) {
    ST.track = (i + TRACKS.length) % TRACKS.length;
    ['trackId', 'title', 'artist', 'durationMs', 'lyric', 'coverUrl', 'positionMs'].forEach(function (k) { delete ST.m[k]; });
    MP.pos = 0; MP.at = Date.now();
    if (!keep) MP.playing = false;
    save(); tick(); changed('m', ['media']); emit('media');
  }

  var autoTimer = 0, autoT0 = Date.now();
  function opts(dom, k) {
    var f = dom === 'v' ? VF[k] : SF[k];
    if (!f) return null;
    if (f.es) return f.es[value('v', base(k) + 'stateSource')] ? f.es[value('v', base(k) + 'stateSource')].map(function (x) { return x[0]; }) : null;
    if (f.e) return f.e.map(function (x) { return x[0]; });
    if (f.t === 'boolean') return [false, true];
    return null;
  }
  function autoStep() {
    var t = Date.now() - autoT0, ch = { v: [], s: [] };
    Object.keys(ST.auto).forEach(function (dk) {
      var dom = dk.charAt(0), k = dk.slice(2), per = ST.auto[dk] * 1000, f = dom === 'v' ? VF[k] : SF[k], o = opts(dom, k), v;
      if (!f) return;
      if (o) v = o[Math.floor(t / per) % o.length];
      else if (f.r) {
        var ph = (t % per) / per, a = ph < 0.5 ? ph * 2 : 2 - ph * 2, st = f.r[2] || 1;
        v = Math.round((f.r[0] + (f.r[1] - f.r[0]) * a) / st) * st;
        v = Math.round(v * 1000) / 1000;
      } else return;
      if (ST[dom][k] !== v) { ST[dom][k] = v; ch[dom].push(k); }
    });
    if (ch.v.length) changed('v', ch.v);
    if (ch.s.length) changed('s', ch.s);
    if (ch.v.length || ch.s.length) emit('auto');
  }
  function autoRun() {
    var on = Object.keys(ST.auto).length > 0;
    if (on && !autoTimer) { autoT0 = Date.now(); autoTimer = setInterval(autoStep, 250); }
    if (!on && autoTimer) { clearInterval(autoTimer); autoTimer = 0; }
  }

  var css = null;
  function styleOnce() {
    if (css) return;
    css = document.createElement('style');
    css.textContent = '.dbg-sys{position:fixed;left:0;right:0;z-index:2147483000;pointer-events:none;font:600 13px/1 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;color:#fff;display:flex;align-items:center;padding:0 16px;box-sizing:border-box}' +
      '.dbg-bar{top:0;height:28px;justify-content:space-between;text-shadow:0 1px 2px rgba(0,0,0,.6)}.dbg-nav{bottom:0;height:44px;background:#000;justify-content:center;letter-spacing:.3em}' +
      '.dbg-mask{position:fixed;inset:0;top:0;left:0;right:0;bottom:0;z-index:2147482999;background:rgba(10,12,16,.82);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;font:600 16px system-ui,"PingFang SC","Microsoft YaHei",sans-serif}' +
      '.dbg-mask i{width:36px;height:36px;border-radius:50%;border:4px solid rgba(255,255,255,.25);border-top-color:#fff;margin-bottom:14px;animation:dbgspin 1s linear infinite}@keyframes dbgspin{to{transform:rotate(360deg)}}' +
      '.dbg-toast{position:fixed;left:50%;bottom:64px;width:360px;margin-left:-180px;z-index:2147483001;pointer-events:none;background:rgba(30,30,30,.92);color:#fff;border-radius:22px;padding:10px 18px;text-align:center;font:500 15px/1.4 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;transition:opacity .3s}';
    document.head.appendChild(css);
  }
  var bars = null;
  function drawBars() {
    var full = HOST.type === 'fullscreen';
    if (!full && !bars) return;
    styleOnce();
    if (!bars) {
      bars = { top: document.createElement('div'), nav: document.createElement('div') };
      bars.top.className = 'dbg-sys dbg-bar'; bars.nav.className = 'dbg-sys dbg-nav';
      bars.nav.textContent = '◁ ○ □（模拟导航栏）';
      document.body.appendChild(bars.top); document.body.appendChild(bars.nav);
      setInterval(function () { if (bars) bars.top.innerHTML = '<span>' + new Date().toTimeString().slice(0, 5) + '</span><span>模拟状态栏　4G　Wi-Fi　76%</span>'; }, 10000);
      bars.top.innerHTML = '<span>' + new Date().toTimeString().slice(0, 5) + '</span><span>模拟状态栏　4G　Wi-Fi　76%</span>';
    }
    bars.top.hidden = !full || !HOST.bar;
    bars.nav.hidden = !full || !HOST.nav;
  }
  function drawMask() {
    styleOnce();
    if (HOST.mask && !HOST.mask.parentNode) document.body.appendChild(HOST.mask);
  }
  var toastEl = null, toastT = 0;
  function toast(text) {
    styleOnce();
    if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'dbg-toast'; document.body.appendChild(toastEl); }
    toastEl.textContent = text; toastEl.style.opacity = '1';
    clearTimeout(toastT);
    toastT = setTimeout(function () { toastEl.style.opacity = '0'; }, 2500);
  }

  function arity(a, n, m) { if (a.length !== n) throw E('INVALID_ARGUMENT', m + ' 要 ' + n + ' 个参数，传了 ' + a.length + ' 个'); }
  function check(sc, v, name) {
    var ok = true;
    if (sc.type === 'boolean') ok = typeof v === 'boolean';
    else if (sc.type === 'integer') ok = typeof v === 'number' && isFinite(v) && Math.floor(v) === v;
    else if (sc.type === 'number') ok = typeof v === 'number' && isFinite(v);
    else if (sc.type === 'string') ok = typeof v === 'string' && v.replace(/^\s+|\s+$/g, '') !== '';
    else if (sc.type === 'array') ok = Array.isArray(v);
    if (ok && sc.enum) ok = sc.enum.indexOf(v) >= 0;
    if (ok && typeof v === 'number' && sc.minimum !== undefined) ok = v >= sc.minimum;
    if (ok && typeof v === 'number' && sc.maximum !== undefined) ok = v <= sc.maximum;
    if (ok && typeof v === 'string' && sc.maxLength) ok = v.length <= sc.maxLength;
    if (ok && name === 'packageName') ok = /^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$/.test(v);
    if (!ok) throw E('INVALID_ARGUMENT', '参数 ' + name + ' 不合要求：' + brief(v));
  }

  function V(k) { return value('v', k); }
  function setV(k, v) { ST.v[k] = v; return k; }
  function need(k) { var v = V(k); if (v === null || v === undefined) throw E('DEVICE_ERROR', '读不到 ' + k + '，不执行'); return v; }
  function flip01(k) { return setV(k, need(k) === 1 ? 0 : 1); }
  function flip12(k) { return setV(k, need(k) === 1 ? 2 : 1); }
  function cyc(k, list) { var i = list.indexOf(need(k)); if (i < 0) throw E('DEVICE_ERROR', k + ' 编码不认识'); return setV(k, list[(i + 1) % list.length]); }
  function windows(mode) {
    var p = { open: 100, close: 0, half: 50, vent: 12 }[mode], ks = [];
    if (p === undefined) return ks;
    ['frontLeft', 'frontRight', 'rearLeft', 'rearRight'].forEach(function (w) { ks.push(setV('body.windows.' + w + '.position', p)); });
    ks.push(setV('body.windows.anyOpen', p > 0 ? 1 : 0), setV('body.windows.halfOpen', mode === 'half' ? 1 : 0), setV('body.windows.ventilation', mode === 'vent' ? 1 : 0));
    return ks;
  }
  function temp(c) {
    var u = V('ac.driverTemperatureUnit');
    if (u === 1) { if (Math.floor(c) !== c || c < 17 || c > 33) throw E('INVALID_ARGUMENT', '设温 17～33 °C 的整数'); return setV('ac.driverTemperature', c); }
    if (u === 0) { var f = c * 9 / 5 + 32; if (Math.abs(f - Math.round(f)) > 1e-6 || f < 64 || f > 91) throw E('INVALID_ARGUMENT', '换算成 °F 要是 64～91 的整数'); return setV('ac.driverTemperature', Math.round(f)); }
    throw E('DEVICE_ERROR', '设温单位读不到');
  }
  var COLORS = ['iceBlue', 'coldWhite', 'warmWhite', 'redOrange', 'red', 'redPurple', 'darkBlue'];
  var EFF = {
    'vehicle.ac.setPower': function (b) { return setV('ac.power', b ? 1 : 0); },
    'vehicle.ac.setCompressor': function (b) { return setV('ac.compressor', b ? 1 : 0); },
    'vehicle.ac.setDriverTemperature': temp,
    'vehicle.ac.setFanLevel': function (l) { return setV('ac.fanLevel', l); },
    'vehicle.ac.setCirculation': function (m) { return setV('ac.circulation', m === 'inside' ? 1 : 0); },
    'vehicle.ac.setFrontDefrost': function (b) { return setV('ac.frontDefrost', b ? 1 : 0); },
    'vehicle.ac.setRearDefrost': function (b) { return setV('ac.rearDefrost', b ? 1 : 0); },
    'vehicle.seat.setHeating': function (p, l) { return setV('seat.' + p + '.heating', l + 1); },
    'vehicle.seat.setVentilation': function (p, l) { return setV('seat.' + p + '.ventilation', l + 1); },
    'vehicle.body.setWindowsMode': windows,
    'vehicle.body.operateSunroof': function (a) { var p = { close: 0, open: 100, comfort: 80, vent: 15 }[a]; return p === undefined ? null : setV('body.sunroof.position', p); },
    'vehicle.body.operateSunshade': function (a) { var p = { close: 0, open: 100 }[a]; return p === undefined ? null : setV('body.sunshade.position', p); },
    'vehicle.body.operateTailgate': function (a) { if (V('driving.gear') !== 3) throw E('DEVICE_ERROR', '不在 P 挡，尾门不动'); return setV('body.tailgate.state', a === 'open' ? 1 : 0); },
    'vehicle.light.setDaytimeRunning': function (b) { return setV('light.daytimeRunning', b ? 1 : 2); },
    'vehicle.light.setLowBeam': function (b) { return setV('light.lowBeam', b ? 1 : 0); },
    'vehicle.light.setDynamicColorful': function (b) { return setV('light.dynamicColorful', b ? 1 : 2); },
    'vehicle.light.setAmbientBrightness': function (v) { return setV('light.ambient.brightness', v); },
    'vehicle.light.setAmbientColor': function (c) { return setV('light.ambient.color', COLORS.indexOf(c) + 1); },
    'vehicle.light.setAmbientMusicMode': function (m) { return setV('light.ambient.musicMode', m === 'on' ? 1 : 2); },
    'vehicle.energy.setWirelessCharging': function (b) { return setV('energy.wirelessCharging', b ? 1 : 2); },
    'vehicle.energy.setSocSave': function (b) { return setV('energy.socSave', b ? 2 : 1); },
    'vehicle.energy.setSocTarget': function (v) { return setV('energy.socTarget', v); },
    'vehicle.energy.setRegenerationLevel': function (l) { return setV('energy.regenerationLevel', l === 'high' ? 2 : 1); },
    'vehicle.energy.setOperationMode': function (m) { return setV('energy.operationMode', { eco: 1, sport: 2, comfort: 3 }[m]); },
    'vehicle.energy.setMode': function (m) { return setV('energy.mode', m === 'hev' ? 3 : 1); },
    'vehicle.energy.setSimulator': function (m) { return setV('energy.simulator', m === 'on' ? 1 : 0); },
    'vehicle.ac.togglePower': function () { return flip01('ac.power'); },
    'vehicle.ac.toggleCompressor': function () { return flip01('ac.compressor'); },
    'vehicle.ac.adjustDriverTemperature': function (d) {
      var u = need('ac.driverTemperatureUnit'), lo = u === 1 ? 17 : 64, hi = u === 1 ? 33 : 91, v = need('ac.driverTemperature') + d;
      if (v < lo || v > hi) throw E('LIMIT_REACHED', '设温到头了');
      return setV('ac.driverTemperature', v);
    },
    'vehicle.ac.adjustFanLevel': function (d) { var v = need('ac.fanLevel') + d; if (v < 0 || v > 7) throw E('LIMIT_REACHED', '风量到头了'); return setV('ac.fanLevel', v); },
    'vehicle.ac.toggleCirculation': function () { return flip01('ac.circulation'); },
    'vehicle.ac.toggleFrontDefrost': function () { return flip01('ac.frontDefrost'); },
    'vehicle.ac.toggleRearDefrost': function () { return flip01('ac.rearDefrost'); },
    'vehicle.seat.cycleHeating': function (p) { return cyc('seat.' + p + '.heating', [1, 2, 3]); },
    'vehicle.seat.cycleVentilation': function (p) { return cyc('seat.' + p + '.ventilation', [1, 2, 3]); },
    'vehicle.body.toggleWindowsMode': function (m) { var f = { open: 'body.windows.anyOpen', half: 'body.windows.halfOpen', vent: 'body.windows.ventilation' }[m]; return windows(need(f) === 1 ? 'close' : m); },
    'vehicle.body.toggleSunroof': function () { return setV('body.sunroof.position', need('body.sunroof.position') > 0 ? 0 : 100); },
    'vehicle.body.toggleSunshade': function () { return setV('body.sunshade.position', need('body.sunshade.position') > 0 ? 0 : 100); },
    'vehicle.light.toggleDaytimeRunning': function () { return flip12('light.daytimeRunning'); },
    'vehicle.energy.toggleWirelessCharging': function () { return flip12('energy.wirelessCharging'); },
    'vehicle.energy.toggleSocSave': function () { return flip12('energy.socSave'); },
    'vehicle.energy.toggleRegenerationLevel': function () { return flip12('energy.regenerationLevel'); },
    'vehicle.energy.toggleSimulator': function () { return flip01('energy.simulator'); },
    'vehicle.energy.cycleOperationMode': function () { return cyc('energy.operationMode', [1, 2, 3]); },
    'media.play': function () { setPlaying(true); },
    'media.pause': function () { setPlaying(false); },
    'media.next': function () { setTrack(ST.track + 1, true); },
    'media.previous': function () { setTrack(ST.track - 1, true); },
    'media.togglePlayPause': function () { setPlaying(!MP.playing); },
    'media.restoreLastSource': function () { setPlaying(true); },
    'ui.toast': function (t) { toast(t); },
    'ui.announce': function (t) { toast('（语音播报）' + t); }
  };
  function run(m, a) {
    var f = EFF[m], r = f ? f.apply(null, a) : null;
    var ks = r === null || r === undefined ? [] : [].concat(r);
    if (ks.length) { save(); changed('v', ks); emit('values'); }
    return { accepted: true };
  }
  function action(m, a) {
    return call(m, a, function () {
      return catalog().then(function () {
        var d = AF[m];
        if (!d) throw E('UNSUPPORTED', '没有登记的方法：' + m);
        arity(a, d.p.length, m);
        d.p.forEach(function (p, i) { check(p[1], a[i], p[0]); });
        if (!ST.control) throw E('PERMISSION_DENIED', '卡片没开车控权限（调试面板里模拟的）');
        if (m === 'actions.runSequence') return sequence(a[0]);
        return run(m, a);
      });
    });
  }
  var seqBusy = false;
  function sequence(steps) {
    if (!Array.isArray(steps) || steps.length < 1 || steps.length > 12) throw E('INVALID_ARGUMENT', 'steps 必须是 1～12 步的数组');
    steps.forEach(function (s, i) {
      var delay = s && own(s, 'delayMs');
      if (!s || typeof s !== 'object') throw E('INVALID_ARGUMENT', '第 ' + i + ' 步不是对象');
      if (delay) {
        if (typeof s.delayMs !== 'number' || Math.floor(s.delayMs) !== s.delayMs || s.delayMs < 100 || s.delayMs > 30000) throw E('INVALID_ARGUMENT', '延时 100～30000 毫秒');
        if (i === 0 || i === steps.length - 1 || own(steps[i - 1], 'delayMs')) throw E('INVALID_ARGUMENT', '不许首尾延时、连续延时');
        return;
      }
      if (s.method === 'actions.runSequence' || !AF[s.method]) throw E('UNSUPPORTED', '第 ' + i + ' 步方法不能组合：' + s.method);
    });
    if (seqBusy) throw E('BUSY', '每页最多一个组合');
    seqBusy = true;
    var done = 0, i = 0;
    return new Promise(function (ok, no) {
      function next() {
        if (i >= steps.length) { seqBusy = false; ok({ accepted: true, completedSteps: done }); return; }
        var s = steps[i];
        if (own(s, 'delayMs')) { i++; setTimeout(next, s.delayMs); return; }
        try {
          var d = AF[s.method], p = s.params || {};
          var a = d.p.map(function (q) { return p[q[0]]; });
          Object.keys(p).forEach(function (k) { if (!d.p.some(function (q) { return q[0] === k; })) throw E('INVALID_ARGUMENT', '多余参数 ' + k); });
          d.p.forEach(function (q, j) { check(q[1], a[j], q[0]); });
          run(s.method, a);
          done++; i++;
          setTimeout(next, 0);
        } catch (e) { seqBusy = false; e.details = { stepIndex: i, completedSteps: done }; no(e); }
      }
      next();
    });
  }

  function go(type) {
    setTimeout(function () { wr(SS, { type: type, from: HOST.type === 'fullscreen' ? HOST.from : HOST.type }, true); location.reload(); }, 400);
    return { accepted: true };
  }
  function fullOnly(m) { if (HOST.type !== 'fullscreen') throw E('UNSUPPORTED', m + ' 只有全屏页能用（现在是 ' + HOST.type + '）'); }
  function bool1(a, m) { arity(a, 1, m); if (typeof a[0] !== 'boolean') throw E('INVALID_ARGUMENT', m + ' 只认一个布尔值'); }
  function barsTo(top, nav) { HOST.bar = top; HOST.nav = nav; drawBars(); emit('host'); return { accepted: true }; }
  var WIN = {
    openFullscreen: function (a) { arity(a, 0, 'openFullscreen'); return HOST.type === 'fullscreen' ? { accepted: true } : go('fullscreen'); },
    restoreSmall: function (a) { arity(a, 0, 'restoreSmall'); fullOnly('restoreSmall'); return go(HOST.from || 'dualPip'); },
    toggleFullscreen: function (a) { arity(a, 0, 'toggleFullscreen'); return go(HOST.type === 'fullscreen' ? HOST.from || 'dualPip' : 'fullscreen'); },
    setStatusBarVisible: function (a) { bool1(a, 'setStatusBarVisible'); fullOnly('setStatusBarVisible'); return barsTo(a[0], HOST.nav); },
    toggleStatusBar: function (a) { arity(a, 0, 'toggleStatusBar'); fullOnly('toggleStatusBar'); return barsTo(!HOST.bar, HOST.nav); },
    setNavigationBarVisible: function (a) { bool1(a, 'setNavigationBarVisible'); fullOnly('setNavigationBarVisible'); return barsTo(HOST.bar, a[0]); },
    toggleNavigationBar: function (a) { arity(a, 0, 'toggleNavigationBar'); fullOnly('toggleNavigationBar'); return barsTo(HOST.bar, !HOST.nav); },
    setImmersiveMode: function (a) { bool1(a, 'setImmersiveMode'); fullOnly('setImmersiveMode'); return barsTo(!a[0], !a[0]); },
    toggleImmersiveMode: function (a) { arity(a, 0, 'toggleImmersiveMode'); fullOnly('toggleImmersiveMode'); var both = !HOST.bar && !HOST.nav; return barsTo(both, both); }
  };

  function maskOff() { clearTimeout(HOST.maskTimer); if (HOST.mask && HOST.mask.parentNode) HOST.mask.parentNode.removeChild(HOST.mask); HOST.mask = null; emit('host'); }
  var PAGE = {
    loading: function (a) {
      arity(a, 1, 'page.loading');
      var ms = a[0];
      if (typeof ms !== 'number' || Math.floor(ms) !== ms || ms < 1000 || ms > 120000) throw E('INVALID_ARGUMENT', 'timeoutMs 是 1000～120000 的整数');
      if (!HOST.visible) return new Promise(function (ok, no) { setTimeout(function () { no(E('TIMEOUT', '页面藏着时不显示遮罩')); }, 5000); });
      maskOff();
      HOST.mask = document.createElement('div');
      HOST.mask.className = 'dbg-mask';
      HOST.mask.innerHTML = '<i></i><span>宿主加载遮罩（调试宿主模拟，最长 ' + Math.round(ms / 1000) + ' 秒）</span>';
      drawMask();
      HOST.maskTimer = setTimeout(maskOff, ms);
      emit('host');
      return { accepted: true };
    },
    ready: function (a) { arity(a, 0, 'page.ready'); maskOff(); return { accepted: true }; }
  };

  var visFns = [];
  function setVisible(v) {
    var changedV = HOST.visible !== v;
    HOST.visible = v;
    document.documentElement.setAttribute('data-uiplus-visibility', v ? 'visible' : 'hidden');
    if (!changedV) return;
    var ev;
    try { ev = new CustomEvent('uiplusvisibilitychange', { detail: { visible: v, hidden: !v } }); } catch (e) { ev = null; }
    if (ev) document.dispatchEvent(ev);
    visFns.slice().forEach(function (f) { try { f({ visible: v, hidden: !v }); } catch (e) {} });
    emit('host');
  }
  document.documentElement.setAttribute('data-uiplus-visibility', 'visible');

  function setOnline(on) {
    if (HOST.online === on) return;
    HOST.online = on;
    subs.forEach(function (s) {
      if (!s.live) return;
      if (!on) { s.pend = null; if (s.dom !== 'm') s.cb({ error: { code: 'BACKEND_UNAVAILABLE', message: '连接已断开（调试面板里模拟的）' } }); }
      else if (s.dom === 'm') s.cb({ type: 'snapshot', sequence: ++seq, values: { media: snap() } });
      else s.cb(tree(s.dom, s.fields));
    });
    emit('host');
  }

  // ================= window.uiplus =================
  var U = {
    version: '1.0',
    __debug: true,
    ready: function () { return Promise.resolve(); },
    getInfo: function () { return call('getInfo', [], function () { return { apiVersion: '1.0', appVersion: '调试宿主', webViewVersion: (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1], hostType: HOST.type }; }); },
    getCapabilities: function () {
      return call('getCapabilities', [], function () {
        return catalog().then(function (c) {
          return {
            apiVersion: '1.0',
            fields: c.vehicle.map(function (f) { var o = { name: f.k, type: f.t, subscriptionMode: f.m }; if (f.u) o.unit = f.u; if (f.e && f.m === 'poll') o.allowedValues = f.e.map(function (x) { return x[0]; }); return o; }),
            controls: [],
            actions: c.actions.map(function (a) { return { method: a.m, description: a.n, params: a.p.map(function (p) { return p[0]; }) }; }),
            systemFields: c.system.map(function (f) { var o = { name: f.k, type: f.t, description: f.n, subscriptionMode: 'native' }; if (f.u) o.unit = f.u; return o; })
          };
        });
      });
    },
    getFunctionTools: function () { return call('getFunctionTools', [], function () { return catalog().then(function (c) { return copy(c.tools); }); }); },
    callFunction: function (name, a) {
      var raw = a;
      return call('callFunction', [name, raw], function () {
        return catalog().then(function () {
          var fn = TOOL[name];
          if (!fn) throw E('UNSUPPORTED', '没有这个工具：' + name);
          if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { throw E('INVALID_ARGUMENT', '参数不是 JSON'); } }
          a = a || {};
          if (typeof a !== 'object' || Array.isArray(a)) throw E('INVALID_ARGUMENT', '参数要是对象');
          var props = (fn.parameters && fn.parameters.properties) || {}, names = Object.keys(props);
          Object.keys(a).forEach(function (k) { if (names.indexOf(k) < 0) throw E('INVALID_ARGUMENT', '多余参数 ' + k); });
          ((fn.parameters && fn.parameters.required) || []).forEach(function (k) { if (!own(a, k)) throw E('INVALID_ARGUMENT', '缺参数 ' + k); });
          var pos = names.map(function (k) { return a[k]; });
          var path = name.replace(/^uiplus_/, '').split('_'), m = path.join('.');
          if (m === 'getInfo') return U.getInfo();
          if (m === 'getCapabilities') return U.getCapabilities();
          if (m === 'vehicle.get') return U.vehicle.get(a.keys);
          if (m === 'system.get') return U.system.get(a.keys);
          if (m === 'system.getProperty') return U.system.getProperty(a.name);
          if (m === 'media.get') return U.media.get();
          if (m === 'media.getLyrics') return U.media.getLyrics();
          return action(m, pos);
        });
      });
    },
    vehicle: {
      get: function (keys) { return query('v', 'vehicle.get', keys); },
      subscribe: function (fields, cb) { return subscribe('v', 'vehicle.subscribe', fields, cb); }
    },
    media: {
      get: function () { return call('media.get', [], function () { return snap(); }); },
      subscribe: function (cb) { return subscribe('m', 'media.subscribe', null, cb); },
      getLyrics: function () {
        return call('media.getLyrics', [], function () {
          if (ST.lyrOff || snap().status !== 'ok') return { trackId: null, lines: [] };
          return { trackId: snap().trackId, lines: lyricLines(ST.track) };
        });
      },
      floatingLyrics: {}
    },
    system: {
      get: function (keys) { return query('s', 'system.get', keys); },
      subscribe: function (fields, cb) { return subscribe('s', 'system.subscribe', fields, cb); },
      getProperty: function (name) {
        return call('system.getProperty', [name], function () {
          if (typeof name !== 'string' || !/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(name)) throw E('INVALID_ARGUMENT', '属性名不对：' + name);
          return { name: name, value: own(PROPS, name) ? PROPS[name] : null };
        });
      }
    },
    windows: {},
    page: {},
    lifecycle: {
      isVisible: function () { return HOST.visible; },
      onVisibilityChange: function (cb) {
        if (typeof cb !== 'function') throw E('INVALID_ARGUMENT', '缺少可见性回调');
        visFns.push(cb);
        return { unsubscribe: function () { var i = visFns.indexOf(cb); if (i >= 0) visFns.splice(i, 1); } };
      }
    },
    actions: { runSequence: function () { return action('actions.runSequence', args(arguments)); } }
  };
  Object.keys(WIN).forEach(function (k) { U.windows[k] = function () { var a = args(arguments); return call('windows.' + k, a, function () { return WIN[k](a); }); }; });
  Object.keys(PAGE).forEach(function (k) { U.page[k] = function () { var a = args(arguments); return call('page.' + k, a, function () { return PAGE[k](a); }, k === 'ready'); }; });
  ('apps.launch system.back system.home system.recents system.screenshot system.sleep ui.announce ui.toast windows.openPip windows.toggleFloating windows.togglePip ' +
    'media.floatingLyrics.setMode media.next media.pause media.play media.previous media.restoreLastSource media.togglePlayPause ' +
    'vehicle.ac.adjustDriverTemperature vehicle.ac.adjustFanLevel vehicle.ac.setCirculation vehicle.ac.setCompressor vehicle.ac.setDriverTemperature vehicle.ac.setFanLevel ' +
    'vehicle.ac.setFrontDefrost vehicle.ac.setPower vehicle.ac.setRearDefrost vehicle.ac.toggleCirculation vehicle.ac.toggleCompressor vehicle.ac.toggleFrontDefrost ' +
    'vehicle.ac.togglePower vehicle.ac.toggleRearDefrost vehicle.body.operateSunroof vehicle.body.operateSunshade vehicle.body.operateTailgate vehicle.body.setWindowsMode ' +
    'vehicle.body.toggleSunroof vehicle.body.toggleSunshade vehicle.body.toggleWindowsMode vehicle.body.unlockDoors vehicle.energy.cycleOperationMode vehicle.energy.setMode ' +
    'vehicle.energy.setOperationMode vehicle.energy.setRegenerationLevel vehicle.energy.setSimulator vehicle.energy.setSocSave vehicle.energy.setSocTarget ' +
    'vehicle.energy.setWirelessCharging vehicle.energy.toggleRegenerationLevel vehicle.energy.toggleSimulator vehicle.energy.toggleSocSave vehicle.energy.toggleWirelessCharging ' +
    'vehicle.light.operateReadingLight vehicle.light.setAmbientBrightness vehicle.light.setAmbientColor vehicle.light.setAmbientMusicMode vehicle.light.setDaytimeRunning ' +
    'vehicle.light.setDynamicColorful vehicle.light.setLowBeam vehicle.light.toggleDaytimeRunning vehicle.seat.cycleHeating vehicle.seat.cycleVentilation ' +
    'vehicle.seat.setHeating vehicle.seat.setVentilation display.setAmbientBreathBrightness display.setAmbientBreathMode display.setAmbientMusicControl display.setRotation ' +
    'instrument.setBacklight instrument.setMapZoom').split(' ').forEach(function (m) {
    var p = m.split('.'), n = U;
    for (var i = 0; i < p.length - 1; i++) n = n[p[i]] = n[p[i]] || {};
    n[p[p.length - 1]] = function () { return action(m, args(arguments)); };
  });
  window.uiplus = U;
  drawBarsWhenReady();
  function drawBarsWhenReady() { if (document.body) drawBars(); else document.addEventListener('DOMContentLoaded', drawBars); }
  autoRun();

  function mainKey(dom, k) { return dom === 'v' && COMP[k] ? COMP[k] : k; }
  function select(dk, on) {
    var i = ST.sel.indexOf(dk);
    if (on && i < 0) ST.sel.push(dk);
    if (!on && i >= 0) ST.sel.splice(i, 1);
  }
  window.D3Debug = {
    catalog: catalog,
    tracks: function () { return TRACKS.map(function (t, i) { return { title: t.title, artist: t.artist, cover: cover(i) }; }); },
    get: function (dom, k) { return dom === 'm' ? snap()[k] : value(dom, k); },
    def: function (dom, k) { return dom === 'm' ? undefined : own(DEF[dom], k) ? DEF[dom][k] : null; },
    set: function (dom, k, v) {
      if (dom === 'm') {
        if (k === 'playing') { delete ST.m.playing; setPlaying(!!v); }
        else { ST.m[k] = v; changed('m', ['media']); }
      } else { ST[dom][k] = v; changed(dom, [k]); }
      select(dom + ':' + mainKey(dom, k), true);
      save(); emit('values');
    },
    pick: function (dom, k, on) {
      var dk = dom + ':' + k;
      if (on) { select(dk, true); save(); emit('values'); return; }
      select(dk, false);
      delete ST.auto[dk];
      var ks = [k];
      if (dom === 'v' && VF[k] && VF[k].c) ks.push(base(k) + VF[k].c.k);
      if (dom === 'm') {
        if (k === 'track') setTrack(0, MP.playing);
        else if (k === 'lyrics') ST.lyrOff = false;
        else { delete ST.m[k]; if (k === 'playing') setPlaying(false); changed('m', ['media']); }
      }
      else { ks.forEach(function (x) { delete ST[dom][x]; }); changed(dom, ks); }
      autoRun(); save(); emit('values');
    },
    picked: function () { return ST.sel.slice(); },
    isPicked: function (dom, k) { return ST.sel.indexOf(dom + ':' + k) >= 0; },
    auto: function (dom, k, sec) {
      var dk = dom + ':' + k;
      if (sec > 0) { ST.auto[dk] = sec; select(dk, true); } else delete ST.auto[dk];
      autoRun(); save(); emit('values');
    },
    autoOf: function (dom, k) { return ST.auto[dom + ':' + k] || 0; },
    resetAll: function () {
      var vk = Object.keys(ST.v), sk = Object.keys(ST.s);
      ST.v = {}; ST.s = {}; ST.m = {}; ST.sel = []; ST.auto = {}; ST.lyrOff = false;
      setTrack(0); changed('v', vk); changed('s', sk); autoRun(); save(); emit('values');
    },
    track: function (i) { if (i === undefined) return ST.track; setTrack(i, MP.playing); select('m:track', true); save(); emit('values'); },
    lyricsOff: function (on) { if (on === undefined) return ST.lyrOff; ST.lyrOff = !!on; select('m:lyrics', !!on); save(); emit('values'); },
    host: function () {
      return { type: HOST.type, control: ST.control, online: HOST.online, fail: HOST.fail, failCode: ST.failCode, visible: HOST.visible, bar: HOST.bar, nav: HOST.nav,
        mask: !!HOST.mask, subs: subs.filter(function (s) { return s.live; }).map(function (s) { return { dom: s.dom, n: s.fields.length, fields: s.fields.slice() }; }) };
    },
    setHost: function (k, v) {
      if (k === 'type') { if (/^(preview|dualPip|floating|fullscreen)$/.test(v) && v !== HOST.type) go(v); return; }
      if (k === 'control') { ST.control = !!v; save(); }
      if (k === 'online') setOnline(!!v);
      if (k === 'fail') HOST.fail = Math.max(0, v | 0);
      if (k === 'failCode') { ST.failCode = v === 'RESOURCE_LIMIT' ? 'RESOURCE_LIMIT' : 'BUSY'; save(); }
      if (k === 'visible') setVisible(!!v);
      emit('host');
    },
    log: function () { return LOG; },
    clearLog: function () { LOG.length = 0; emit('log'); },
    on: function (fn) { fns.push(fn); return function () { var i = fns.indexOf(fn); if (i >= 0) fns.splice(i, 1); }; },
    open: function () {
      return catalog().then(function () { return window.D3DebugPanel ? null : load('debugpanel.js'); }).then(function () { window.D3DebugPanel.open(); });
    }
  };
})();
