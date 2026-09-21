/* 8282 总控台 —— 前端核心：hash 路由、徽章轮询、公共工具。
   各页面模块放在 static/pages/<id>.js，向 window.DashPages 注册：
     DashPages.<id> = { title, mount(host), tick(), interval, badges }
   本文件不包含任何具体页面的渲染逻辑。 */
'use strict';

var WINDOW_SECONDS = 120;

window.DashPages = window.DashPages || {};

var PAGE_TITLES = {
  overview: '概览',
  performance: '性能与电源',
  processes: '进程',
  network: '网络与磁盘',
  services: '服务',
  device: '设备',
  sensors: '传感器'
};

var state = { paused: false, tick: 0, page: null, pageTimer: null };

/* 每个页面各自记「上一次请求是否还在飞」：切后台回前台时 visibilitychange 会立刻补拉，
   setInterval 的 tick 也可能同时在飞，两者用同一个旧 since 会拉到同一批点并各追加一次，
   曲线里就出现倒退的连线。并发触发时直接跳过，等上一次回来。 */
var tickInFlight = {};
var badgesInFlight = false;

/* ---------------- 小工具（页面模块全局可用） ---------------- */

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
}

function fmtRate(bps) {
  if (bps === null || bps === undefined || !isFinite(bps)) return '—';
  var units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  var value = bps, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return (value >= 100 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[i];
}

function fmtMB(mb) {
  if (mb === null || mb === undefined) return '—';
  return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(0) + ' MB';
}

function fmtUptime(seconds) {
  if (!isFinite(seconds)) return '—';
  var days = Math.floor(seconds / 86400);
  var hours = Math.floor((seconds % 86400) / 3600);
  var mins = Math.floor((seconds % 3600) / 60);
  return days > 0 ? days + ' 天 ' + hours + ' 小时' : hours + ' 小时 ' + mins + ' 分钟';
}

function fmtClock(date) {
  var weeks = '日一二三四五六';
  return (date.getMonth() + 1) + '月' + date.getDate() + '日 周' + weeks[date.getDay()] + ' ' +
    String(date.getHours()).padStart(2, '0') + ':' +
    String(date.getMinutes()).padStart(2, '0') + ':' +
    String(date.getSeconds()).padStart(2, '0');
}

function setValue(id, main, unit) {
  var node = document.getElementById(id);
  if (!node) return;
  node.classList.remove('na');
  node.textContent = main;
  if (unit) {
    var span = document.createElement('span');
    span.className = 'unit';
    span.textContent = unit;
    node.appendChild(span);
  }
}

function setSub(id, text) {
  var node = document.getElementById(id);
  if (node) node.textContent = text || '';
}

function setNav(id, text) {
  var node = document.getElementById(id);
  if (node) node.textContent = text === null || text === undefined ? '—' : text;
}

function setLive(text, mode) {
  var box = document.querySelector('.live');
  box.classList.remove('paused', 'error');
  if (mode) box.classList.add(mode);
  document.getElementById('live-text').textContent = text;
}

function fetchJSON(url) {
  return fetch(url, { cache: 'no-store' }).then(function (res) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  });
}

/* 页面轮询前的统一闸门：暂停或后台时跳过（首次渲染除外）。 */
function pollGuard() {
  if (state.paused) return true;
  if (document.hidden && state.tick > 0) return true;
  return false;
}

/* ---------------- 徽章轮询：让侧栏数值在任何页面都保持最新 ---------------- */

function updateBadges(ov) {
  var temp = ov.temp, cpu = ov.cpu, net = ov.net, disk = ov.disk;
  setNav('nav-temp', temp && temp.available ? temp.celsius.toFixed(0) + '°C' : '—');
  setNav('nav-sensor', temp && temp.available ? temp.celsius.toFixed(0) + '°' : '—');
  setNav('nav-cpu', cpu && cpu.available ? cpu.percent.toFixed(0) + '%' : '—');
  setNav('nav-proc', ov.process_count || 0);
  setNav('nav-net', net && net.available ? '↓ ' + fmtRate(net.down_bps) : '—');
  setNav('nav-dev', disk && disk.available ? disk.used_percent + '%' : '—');

  var docker = (ov.services || []).filter(function (item) {
    return item.name === 'Docker';
  })[0];
  setNav('nav-svc', docker ? docker.detail : '—');

  document.getElementById('host').textContent = ov.host || '总控台';
  document.getElementById('uptime').textContent = fmtUptime(ov.uptime_s);
}

function pollBadges() {
  var page = DashPages[state.page];
  if (page && page.badges === false) return;  // 概览页自己全量刷新并更新徽章
  if (state.paused || document.hidden || badgesInFlight) return;
  badgesInFlight = true;
  fetchJSON('/api/overview').then(function (ov) {
    if (ov.ready) updateBadges(ov);
  }).catch(function () { /* 连接状态由当前页面的 tick 负责 */ })
    .then(function () { badgesInFlight = false; });
}

/* 统一的页面轮询入口：合并 setInterval / visibilitychange / 取消暂停 三种触发 */
function runPageTick(page) {
  var id = state.page;
  if (tickInFlight[id]) return;
  tickInFlight[id] = true;
  var done = function () { tickInFlight[id] = false; };
  var result;
  try {
    result = page.tick();
  } catch (err) {
    done();
    throw err;
  }
  Promise.resolve(result).then(done, done);
}

/* ---------------- hash 路由 ---------------- */

function currentPageId() {
  var hash = (location.hash || '').replace(/^#\/?/, '').replace(/\/+$/, '');
  return hash || 'overview';
}

function navigate() {
  var id = currentPageId();
  if (state.page === id) return;
  activate(id);
}

function activate(id) {
  if (state.pageTimer) { clearInterval(state.pageTimer); state.pageTimer = null; }
  state.page = id;
  state.tick = 0;
  document.body.dataset.page = id;  /* CSS 按页面切换布局（概览铺满视口，其余自然高度） */

  var host = document.getElementById('page-host');
  host.innerHTML = '';

  var items = document.querySelectorAll('.nav-item');
  for (var i = 0; i < items.length; i++) {
    items[i].classList.toggle('active', items[i].getAttribute('data-page') === id);
  }
  document.title = (PAGE_TITLES[id] || id) + ' · 总控台';

  if (DashPages[id]) return mountPage(id, host);

  /* 按需加载页面模块；404 或未注册则显示建设中 */
  var script = document.createElement('script');
  script.src = '/static/pages/' + id + '.js';
  script.onload = function () {
    if (state.page !== id) return;  // 用户已切到别的页面
    if (DashPages[id]) mountPage(id, host);
    else renderPlaceholder(host, id);
  };
  script.onerror = function () {
    if (state.page === id) renderPlaceholder(host, id);
  };
  document.head.appendChild(script);
}

function mountPage(id, host) {
  var page = DashPages[id];
  if (page.title) document.title = page.title + ' · 总控台';
  page.mount(host);
  var run = function () { runPageTick(page); };
  run();
  state.pageTimer = setInterval(run, page.interval || 1000);
}

function renderPlaceholder(host, id) {
  host.innerHTML =
    '<section class="card coming">' +
    '<div class="card-head"><span>' + esc(PAGE_TITLES[id] || id) + '</span>' +
    '<span class="head-note">建设中</span></div>' +
    '<div class="card-body"><div class="empty">这一页在后续批次上线。</div></div></section>';
}

/* ---------------- 启动 ---------------- */

document.getElementById('pause').addEventListener('click', function () {
  state.paused = !state.paused;
  this.textContent = state.paused ? '继续更新' : '暂停更新';
  if (state.paused) {
    setLive('已暂停', 'paused');
  } else {
    setLive('实时更新中', '');
    var page = DashPages[state.page];
    if (page) runPageTick(page);
    pollBadges();
  }
});

setInterval(function () {
  document.getElementById('clock').textContent = fmtClock(new Date());
}, 1000);
document.getElementById('clock').textContent = fmtClock(new Date());

window.addEventListener('hashchange', navigate);
navigate();
setInterval(pollBadges, 2000);
pollBadges();

document.addEventListener('visibilitychange', function () {
  if (document.hidden || state.paused) return;
  var page = DashPages[state.page];
  if (page) runPageTick(page);
  pollBadges();
});
