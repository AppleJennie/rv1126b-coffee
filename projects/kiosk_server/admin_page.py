# admin_page.py —— 管理后台页面模板（TASK 34）
#
# 独立简易管理页：内嵌 HTML/JS，无前端框架、无 CDN、无外部资源（板子离线可用）。
# 数据全部来自 GET /api/admin/stats（TASK 35 SQLite 统计 + TASK 24 健康 + TASK 25
# watchdog）；AI 推理速度 ai_host 未接入，页面显示 n/a 占位，不编造数据。
#
# 鉴权：服务端 _admin_ok 校验（CAFE_ADMIN_TOKEN 或未设置时仅 127.0.0.1）。
# 设置了 token 时，页面从 URL ?token=xxx 读取并随每次请求带回（演示用，非强安全）。

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>畔咖啡 · 管理后台</title>
<style>
  body { margin:0; background:#F4F0EB; color:#4A2C1A;
         font-family:"PingFang SC","Microsoft YaHei",sans-serif; }
  header { background:#4A2C1A; color:#FFF6EC; padding:18px 32px;
           display:flex; justify-content:space-between; align-items:center; }
  header h1 { font-size:22px; margin:0; }
  header a { color:#D9822B; text-decoration:none; font-size:14px; }
  main { padding:24px 32px; max-width:1100px; margin:0 auto; }
  .cards { display:flex; flex-wrap:wrap; gap:16px; margin-bottom:24px; }
  .card { flex:1 1 180px; background:#FFF; border-radius:12px; padding:18px 22px;
          box-shadow:0 2px 8px rgba(74,44,26,.08); }
  .card .k { font-size:13px; color:#B9AFA6; }
  .card .v { font-size:30px; font-weight:bold; margin-top:6px; }
  section { background:#FFF; border-radius:12px; padding:18px 22px;
            margin-bottom:24px; box-shadow:0 2px 8px rgba(74,44,26,.08); }
  section h2 { font-size:17px; margin:0 0 12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { padding:8px 10px; border-bottom:1px solid #E8DDD2; text-align:left; }
  th { color:#7A5A42; }
  .ok { color:#4E7A4E; } .bad { color:#C0392B; } .warn { color:#B26A00; }
  .tag { display:inline-block; padding:2px 10px; border-radius:999px;
         font-size:12px; color:#FFF; background:#2E7D32; }
  .tag.warn { background:#B26A00; } .tag.bad { background:#C62828; }
  button { background:#4A2C1A; color:#FFF6EC; border:none; border-radius:8px;
           padding:10px 22px; font-size:14px; cursor:pointer; margin-right:10px; }
  button.ghost { background:#FFF; color:#4A2C1A; border:1px solid #E8DDD2; }
  #msg { font-size:13px; color:#B26A00; margin-top:10px; min-height:18px; }
  .hint { font-size:12px; color:#B9AFA6; margin-top:8px; }
</style>
</head>
<body>
<header>
  <h1>畔咖啡 · 管理后台</h1>
  <a href="/">返回点单屏</a>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="k">今日订单</div><div class="v" id="c-today">-</div></div>
    <div class="card"><div class="k">成功率</div><div class="v" id="c-rate">-</div></div>
    <div class="card"><div class="k">平均制作时长</div><div class="v" id="c-avg">-</div></div>
    <div class="card"><div class="k">AI 推理速度</div><div class="v" id="c-ai">n/a</div></div>
  </div>

  <section>
    <h2>设备状态（/api/health）</h2>
    <div id="health-overall">-</div>
    <table id="health-table"><thead><tr><th>项目</th><th>状态</th><th>说明</th></tr></thead>
    <tbody></tbody></table>
    <div id="watchdog" class="hint">watchdog：-</div>
  </section>

  <section>
    <h2>操作</h2>
    <div>
      当前制作后端：<b id="backend-now">-</b>
      <span id="backend-pending" class="hint"></span><br><br>
      <button onclick="switchMode('SIM')">切到 SIM（仿真）</button>
      <button onclick="switchMode('HYBRID')">切到 HYBRID（混合）</button>
      <button class="ghost" onclick="reinit()">重新初始化设备</button>
      <div id="msg"></div>
      <div class="hint">模式切换只影响<b>下一单</b>（下单时定死，不热切换当前单）；
      重新初始化 = 触发健康巡检重检一轮，断线设备尝试重连。</div>
    </div>
  </section>

  <section>
    <h2>失败原因统计（设备故障归类）</h2>
    <div id="fail-reasons">-</div>
  </section>

  <section>
    <h2>订单历史（最近 50 条）</h2>
    <table id="orders-table">
      <thead><tr><th>时间</th><th>订单号</th><th>饮品</th><th>数量</th><th>金额</th>
      <th>制作时长</th><th>结果</th><th>失败原因</th><th>模式</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>
</main>
<script>
'use strict';
/* token 从 URL ?token= 读取（服务端设置了 CAFE_ADMIN_TOKEN 时） */
var TOKEN = new URLSearchParams(location.search).get('token') || '';
function q(path) { return path + (TOKEN ? (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN) : ''); }
function hdr() { return TOKEN ? { 'X-Admin-Token': TOKEN, 'Content-Type': 'application/json' }
                              : { 'Content-Type': 'application/json' }; }
function esc(s) { var d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }
function fmtTs(ts) { return ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'; }
var RESULT_TEXT = { success: '成功', failed: '失败', cancelled: '已取消' };

function refresh() {
  fetch(q('/api/admin/stats')).then(function (r) {
    if (r.status === 403) { document.body.innerHTML =
      '<p style="padding:40px;font-size:18px;">403 未授权：请携带 token 或从本机访问</p>'; return null; }
    return r.json();
  }).then(function (d) {
    if (!d) return;
    document.getElementById('c-today').textContent = d.today_count;
    document.getElementById('c-rate').textContent =
      d.success_rate == null ? '-' : (d.success_rate * 100).toFixed(0) + '%';
    document.getElementById('c-avg').textContent =
      d.avg_duration_sec == null ? '-' : d.avg_duration_sec + 's';
    /* AI 推理速度：服务端给 null 就显示 n/a，不编造 */
    document.getElementById('c-ai').textContent =
      d.ai_infer_ms == null ? 'n/a' : d.ai_infer_ms + 'ms';

    /* 设备状态 */
    var h = d.health;
    if (h) {
      var cls = h.overall === 'READY' ? 'ok' : (h.overall === 'OFFLINE' ? 'bad' : 'warn');
      document.getElementById('health-overall').innerHTML =
        '<span class="tag ' + (cls === 'ok' ? '' : cls) + '">' + esc(h.headline || h.overall_text) + '</span>';
      var tb = document.querySelector('#health-table tbody');
      tb.innerHTML = '';
      Object.keys(h.items || {}).forEach(function (k) {
        var it = h.items[k];
        var sc = it.status === 'ok' ? 'ok' : (it.status === 'unknown' ? 'warn' : 'bad');
        var tr = document.createElement('tr');
        tr.innerHTML = '<td>' + esc(it.label) + '</td><td class="' + sc + '">' +
          esc(it.status) + '</td><td>' + esc(it.detail) + '</td>';
        tb.appendChild(tr);
      });
    }
    /* watchdog 段（TASK 25） */
    var w = d.watchdog;
    if (w) {
      document.getElementById('watchdog').textContent = 'watchdog：' + w.state +
        (w.reasons && w.reasons.length ? '（' + w.reasons.join('；') + '）' : '') +
        '，上次正常 ' + fmtTs(w.last_healthy_ts);
    }

    /* 制作后端 */
    if (d.backend) {
      document.getElementById('backend-now').textContent = d.backend.mode;
      document.getElementById('backend-pending').textContent =
        d.backend.pending_mode ? '（下一单切换为 ' + d.backend.pending_mode + '）' : '';
    }

    /* 失败原因 */
    var fr = d.fail_reasons || {};
    var parts = Object.keys(fr).map(function (k) { return esc(k) + ' ×' + fr[k]; });
    document.getElementById('fail-reasons').textContent =
      parts.length ? parts.join('；') : '暂无失败记录';

    /* 订单历史 */
    var ob = document.querySelector('#orders-table tbody');
    ob.innerHTML = '';
    (d.recent || []).forEach(function (o) {
      var tr = document.createElement('tr');
      var rc = o.result === 'success' ? 'ok' : (o.result === 'failed' ? 'bad' : 'warn');
      tr.innerHTML = '<td>' + fmtTs(o.ts) + '</td><td>#' + o.order_id + '</td><td>' +
        esc(o.drink_name) + '</td><td>' + o.qty + '</td><td>¥' + o.total + '</td><td>' +
        (o.duration_sec == null ? '-' : o.duration_sec + 's') + '</td><td class="' + rc + '">' +
        esc(RESULT_TEXT[o.result] || o.result) + '</td><td>' + esc(o.fail_reason || '') +
        '</td><td>' + esc(o.mode) + '</td>';
      ob.appendChild(tr);
    });
  }).catch(function () { /* 拉取失败保持现状，下轮再试 */ });
}

function switchMode(mode) {
  fetch(q('/api/admin/mode'), { method: 'POST', headers: hdr(),
    body: JSON.stringify({ mode: mode }) })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      document.getElementById('msg').textContent = d.ok
        ? '已标记：下一单使用 ' + d.pending_mode + ' 后端制作（当前单不受影响）'
        : '切换失败：' + (d.reason || '未知');
      refresh();
    });
}
function reinit() {
  fetch(q('/api/admin/reinit'), { method: 'POST', headers: hdr(), body: '{}' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      document.getElementById('msg').textContent =
        d.ok ? '设备重检完成' : '重检失败：' + (d.reason || '未知');
      refresh();
    });
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
