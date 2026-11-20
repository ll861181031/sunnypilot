"""Minimal control page for the CPlink bench (no external assets)."""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPlink 车架模式</title>
<style>
  :root { color-scheme: dark; --bg:#111418; --card:#1b2027; --line:#2d3540; --fg:#e6edf3;
          --muted:#8b98a5; --acc:#4cc38a; --warn:#e8b21a; --bad:#e5534b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line); display:flex; gap:16px;
           align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; }
  .muted { color:var(--muted); }
  main { display:grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr);
         gap:14px; padding:14px; }
  @media (max-width: 760px) { main { grid-template-columns: 1fr; } }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
  h2 { font-size:13px; letter-spacing:.04em; text-transform:uppercase; color:var(--muted);
       margin:0 0 10px; }
  label { display:flex; justify-content:space-between; align-items:center; gap:10px;
          margin:7px 0; color:var(--muted); }
  input[type=range] { width: 58%; accent-color: var(--acc); }
  input[type=number], select { width: 42%; background:#0e1116; color:var(--fg);
       border:1px solid var(--line); border-radius:6px; padding:4px 6px; }
  input[type=checkbox] { accent-color: var(--acc); }
  .row { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; }
  button { background:#232a33; color:var(--fg); border:1px solid var(--line);
           border-radius:7px; padding:6px 10px; cursor:pointer; }
  button:hover { border-color:var(--acc); }
  button.on { background:var(--acc); color:#07130d; border-color:var(--acc); }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:12px;
        color:var(--muted); max-height:300px; overflow:auto; }
  .ok { color:var(--acc); } .warn { color:var(--warn); } .bad { color:var(--bad); }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>CPlink 车架模式</h1>
  <span class="muted">独立 msgq 前缀 + 独立端口；不影响生产 shim</span>
  <span id="health" class="muted">连接中…</span>
</header>
<main>
  <section>
    <h2>场景</h2>
    <div class="row" id="scenarios"></div>
    <h2>车辆</h2>
    <label>车速 <span><input id="speed" type="range" min="0" max="140" step="1">
      <b id="speedV">0</b> km/h</span></label>
    <label>巡航 <span><input id="cruise" type="range" min="0" max="140" step="1">
      <b id="cruiseV">0</b> km/h</span></label>
    <label>限速 <span><input id="limit" type="range" min="10" max="140" step="5">
      <b id="limitV">0</b> km/h</span></label>
    <label>前车距离 <span><input id="lead" type="range" min="0" max="120" step="1">
      <b id="leadV">0</b> m</span></label>
    <div class="row">
      <button data-toggle="enabled">自驾使能</button>
      <button data-toggle="left_blinker">左转灯</button>
      <button data-toggle="right_blinker">右转灯</button>
      <button data-toggle="left_blindspot">左盲区</button>
      <button data-toggle="right_blindspot">右盲区</button>
    </div>
    <label>挡位 <span><select id="gear">
      <option>park</option><option>drive</option><option>reverse</option><option>neutral</option>
    </select></span></label>
  </section>
  <section>
    <h2>导航 / 灯态</h2>
    <div class="row">
      <button data-toggle="nav_active">导航中</button>
      <button id="scenario-driving">开始巡航场景</button>
      <button id="scenario-red">红灯场景</button>
    </div>
    <label>转弯距离 <span><input id="navDist" type="range" min="0" max="600" step="5">
      <b id="navDistV">0</b> m</span></label>
    <label>转向码 <span><select id="turn">
      <option value="-1">无</option><option value="12">左转 12</option>
      <option value="13">右转 13</option><option value="51">直行 51</option>
      <option value="101">右出口 101</option><option value="102">左出口 102</option>
    </select></span></label>
    <label>车道数 <span><input id="lanes" type="range" min="1" max="5" step="1">
      <b id="lanesV">0</b></span></label>
    <label>红绿灯 <span><select id="color">
      <option value="0">无</option><option value="1">红</option>
      <option value="2">绿</option><option value="3">黄</option>
    </select></span></label>
    <label>灯距 <span><input id="signal" type="range" min="0" max="120" step="1">
      <b id="signalV">0</b> m</span></label>
    <label>GPS <span><input id="gps" type="checkbox"> 有定位</span></label>
  </section>
  <section style="grid-column:1/-1">
    <h2>链路状态</h2>
    <div class="kv" id="stats"></div>
    <pre id="raw"></pre>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const scenarios = ["parked","driving","turn_left","lane_change","red_light","highway","no_gps"];
for (const name of scenarios) {
  const b = document.createElement("button");
  b.textContent = name; b.onclick = () => fetch("/api/bench/scenario",
    {method:"POST", headers:{"content-type":"application/json"},
     body:JSON.stringify({name})}).then(refresh);
  $("scenarios").appendChild(b);
}
let state = {};
function setState(patch) {
  return fetch("/api/bench/set", {method:"POST",
    headers:{"content-type":"application/json"}, body:JSON.stringify(patch)}).then(refresh);
}
$("speed").oninput = e => { $("speedV").textContent = e.target.value; };
$("speed").onchange = e => setState({speed_kph:+e.target.value});
$("cruise").oninput = e => { $("cruiseV").textContent = e.target.value; };
$("cruise").onchange = e => setState({cruise_kph:+e.target.value});
$("limit").oninput = e => { $("limitV").textContent = e.target.value; };
$("limit").onchange = e => setState({road_limit_kph:+e.target.value});
$("lead").oninput = e => { $("leadV").textContent = e.target.value; };
$("lead").onchange = e => setState({lead_distance_m:+e.target.value});
$("navDist").oninput = e => { $("navDistV").textContent = e.target.value; };
$("navDist").onchange = e => setState({nav_distance_m:+e.target.value});
$("lanes").oninput = e => { $("lanesV").textContent = e.target.value; };
$("lanes").onchange = e => setState({lane_count:+e.target.value});
$("signal").oninput = e => { $("signalV").textContent = e.target.value; };
$("signal").onchange = e => setState({signal_distance_m:+e.target.value});
$("turn").onchange = e => setState({nav_turn_type:+e.target.value});
$("color").onchange = e => setState({traffic_color:+e.target.value, can_tla_raw:+e.target.value});
$("gear").onchange = e => setState({gear:e.target.value});
$("gps").onchange = e => setState({gps_fix:e.target.checked});
document.querySelectorAll("[data-toggle]").forEach(b => b.onclick = () =>
  setState({[b.dataset.toggle]: !state[b.dataset.toggle]}));
function sync() {
  $("speed").value = state.speed_kph ?? 0; $("speedV").textContent = $("speed").value;
  $("cruise").value = state.cruise_kph ?? 0; $("cruiseV").textContent = $("cruise").value;
  $("limit").value = state.road_limit_kph ?? 0; $("limitV").textContent = $("limit").value;
  $("lead").value = state.lead_distance_m ?? 0; $("leadV").textContent = $("lead").value;
  $("navDist").value = state.nav_distance_m ?? 0; $("navDistV").textContent = $("navDist").value;
  $("lanes").value = state.lane_count ?? 0; $("lanesV").textContent = $("lanes").value;
  $("signal").value = state.signal_distance_m ?? 0; $("signalV").textContent = $("signal").value;
  $("turn").value = state.nav_turn_type ?? -1;
  $("color").value = state.traffic_color ?? 0;
  $("gear").value = state.gear ?? "park";
  $("gps").checked = !!state.gps_fix;
  document.querySelectorAll("[data-toggle]").forEach(b =>
    b.classList.toggle("on", !!state[b.dataset.toggle]));
}
function refresh() {
  fetch("/api/bench/state").then(r => r.json()).then(d => {
    if (!d.ok) { $("health").textContent = d.error || "bench 未启用"; $("health").className = "bad"; return; }
    state = d.state; sync();
    const app = d.app || {};
    const compact = (app.compact || {});
    const navi = (app.navi || {});
    const lamp = (d.shim || {}).lamp || {};
    $("health").textContent = "运行中";
    $("health").className = "ok";
    $("stats").innerHTML = [
      ["车辆发布", `${d.car.published} 帧` + (d.car.error ? ` · ${d.car.error}` : "")],
      ["手机 compact", `${compact.connected ? "已连" : "未连"} · ${compact.frames || 0} 帧 · ${(compact.seen || compact.services || []).join(",")}`],
      ["手机 7714", `${navi.connected ? "已连" : "未连"} · ${navi.sent || 0} 帧 · ${(navi.streams || []).join(",")}`],
      ["手机 7711", `${(app.lamp || {}).connected ? "已连" : "未连"} · ${(app.lamp || {}).frames || 0} 帧 · 灯色 ${((app.lamp || {}).last || {}).color ?? "-"}`],
      ["shim compact 客户端", `${(d.shim.compact || {}).clients ?? 0}`],
      ["shim 7711 灯色", `${lamp.color ?? "-"}`],
      ["视频 5001", `${app.video || "-"}`],
      ["场景", state.scenario || "-"],
    ].map(([k,v]) => `<span>${k}</span><span>${v}</span>`).join("");
    $("raw").textContent = JSON.stringify({state, app, shim:d.shim}, null, 2);
  }).catch(e => { $("health").textContent = String(e); $("health").className = "bad"; });
}
refresh(); setInterval(refresh, 1000);
</script>
</body>
</html>
"""
