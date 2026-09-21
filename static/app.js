/* 8282 总控台 —— 概览页渲染与轮询 */
'use strict';

var WINDOW_SECONDS = 120;

/* 上半部分：去掉示意图后的 5 条指标，曲线铺满整行 */
var METRICS = [
  { key: 'cpu', label: 'CPU 占用', hex: '#3ddc97',
    series: [{ key: 'cpu', color: '#3ddc97', fill: true }], fixed: [0, 100] },
  { key: 'mem', label: '内存', hex: '#4fc3f7',
    series: [{ key: 'mem_used', color: '#4fc3f7', fill: true }], autoMinTop: 1 },
  { key: 'power', label: '整机功耗', hex: '#ffb74d',
    series: [{ key: 'power', color: '#ffb74d', fill: true }], autoMinTop: 10 },
  { key: 'net', label: '网速', hex: null,
    series: [{ key: 'net_down', color: '#3ddc97', fill: false },
             { key: 'net_up', color: '#ff6b9d', fill: false }], autoMinTop: 4096 },
  { key: 'disk', label: '磁盘剩余', hex: '#ffd54f', bar: true }
];

var state = { charts: {}, lastSeriesTs: 0, paused: false, tick: 0 };

/* ---------------- 小工具 ---------------- */

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

/* ---------------- 构建上半部分 ---------------- */

function buildUpper() {
  var upper = document.getElementById('upper');

  METRICS.forEach(function (spec) {
    var row = document.createElement('div');
    row.className = 'metric';

    var body = document.createElement('div');
    body.className = 'metric-body';

    var label = document.createElement('div');
    label.className = 'metric-label';
    if (spec.hex) {
      var tick = document.createElement('span');
      tick.className = 'tick';
      tick.style.background = spec.hex;
      label.appendChild(tick);
    }
    label.appendChild(document.createTextNode(spec.label));
    body.appendChild(label);

    if (spec.key === 'net') {
      var wrap = document.createElement('div');
      wrap.className = 'net-values';
      [['down', '下载', '#3ddc97'], ['up', '上传', '#ff6b9d']].forEach(function (item) {
        var line = document.createElement('div');
        line.className = 'net-line';
        var mark = document.createElement('span');
        mark.className = 'tick';
        mark.style.background = item[2];
        var val = document.createElement('b');
        val.id = 'v-net-' + item[0];
        val.textContent = '—';
        line.appendChild(mark);
        line.appendChild(document.createTextNode(item[1]));
        line.appendChild(val);
        wrap.appendChild(line);
      });
      body.appendChild(wrap);
    } else {
      var value = document.createElement('div');
      value.className = 'metric-value';
      value.id = 'v-' + spec.key;
      value.textContent = '—';
      body.appendChild(value);
    }

    var sub = document.createElement('div');
    sub.className = 'metric-sub';
    sub.id = 's-' + spec.key;
    body.appendChild(sub);

    var chartHost = document.createElement('div');
    chartHost.className = 'metric-chart';
    chartHost.id = 'c-' + spec.key;
    var tag = document.createElement('span');
    tag.className = 'range-tag';
    tag.id = 'range-' + spec.key;
    chartHost.appendChild(tag);

    if (spec.bar) {
      var track = document.createElement('div');
      track.className = 'bar-track';
      var fill = document.createElement('div');
      fill.className = 'bar-fill';
      fill.id = 'bar-disk';
      track.appendChild(fill);
      chartHost.appendChild(track);
      state.charts[spec.key] = { spec: spec, chart: null };
    } else {
      var chart = window.createChart(chartHost, {
        series: spec.series,
        fixed: spec.fixed || null,
        autoMinTop: spec.autoMinTop || 1,
        window: WINDOW_SECONDS
      });
      state.charts[spec.key] = { spec: spec, chart: chart, naReason: null };
    }

    row.appendChild(body);
    row.appendChild(chartHost);
    upper.appendChild(row);
  });
}

/* ---------------- 渲染 ---------------- */

function markUnavailable(key, reason) {
  var inst = state.charts[key];
  var value = document.getElementById('v-' + key);
  if (value) {
    value.classList.add('na');
    value.textContent = '不可用';
  }
  /* 有曲线区的指标把原因显示在图表位置，避免和副标题重复；磁盘这种没有曲线区的则写在副标题 */
  var hasChart = !!(inst && inst.chart);
  setSub('s-' + key, hasChart ? '' : (reason || ''));
  var tag = document.getElementById('range-' + key);
  if (tag) tag.textContent = '';
  if (hasChart && inst.naReason !== reason) {
    inst.chart.setUnavailable(reason);
    inst.naReason = reason;
  }
}

function renderMetrics(ov) {
  var cpu = ov.cpu;
  if (cpu.available) {
    setValue('v-cpu', cpu.percent.toFixed(0), '%');
    setSub('s-cpu', ov.cores + ' 核' + (cpu.freq_mhz ? ' · ' + cpu.freq_mhz + ' MHz' : ''));
  } else {
    markUnavailable('cpu', cpu.reason);
  }

  var mem = ov.memory;
  if (mem.available) {
    setValue('v-mem', mem.used_gb.toFixed(1), '/ ' + mem.total_gb.toFixed(1) + ' GB');
    setSub('s-mem', '已用 ' + mem.percent + '% · 交换 ' + mem.swap_used_gb.toFixed(1) + ' GB');
    state.charts.mem.chart.setFixed([0, mem.total_gb]);
  } else {
    markUnavailable('mem', mem.reason);
  }

  var power = ov.power;
  if (power.available) {
    setValue('v-power', power.watts.toFixed(1), 'W');
    setSub('s-power', 'Intel RAPL 整机功耗');
  } else {
    markUnavailable('power', power.reason);
    setSub('s-power', power.reason.indexOf('root') >= 0 ? '以 root 运行可显示' : '');
  }

  var net = ov.net;
  if (net.available) {
    document.getElementById('v-net-down').textContent = fmtRate(net.down_bps);
    document.getElementById('v-net-up').textContent = fmtRate(net.up_bps);
    setSub('s-net', '网卡 ' + net.nic);
  } else {
    document.getElementById('v-net-down').textContent = '不可用';
    document.getElementById('v-net-up').textContent = '—';
    setSub('s-net', net.reason);
  }

  var disk = ov.disk;
  if (disk.available) {
    setValue('v-disk', disk.free_gb.toFixed(disk.free_gb >= 100 ? 0 : 1),
      '/ ' + disk.total_gb.toFixed(0) + ' GB');
    setSub('s-disk', '挂载 ' + disk.path + ' · 已用 ' + disk.used_percent + '%');
    document.getElementById('bar-disk').style.width = disk.used_percent + '%';
    var diskTag = document.getElementById('range-disk');
    if (diskTag) diskTag.textContent = '已用 ' + disk.used_percent + '%';
  } else {
    markUnavailable('disk', disk.reason);
  }

  var temp = ov.temp;
  setNav('nav-temp', temp.available ? temp.celsius.toFixed(0) + '°C' : '—');
  setNav('nav-sensor', temp.available ? temp.celsius.toFixed(0) + '°' : '—');
  setNav('nav-cpu', cpu.available ? cpu.percent.toFixed(0) + '%' : '—');
  setNav('nav-proc', ov.process_count || 0);
  setNav('nav-net', net.available ? '↓ ' + fmtRate(net.down_bps) : '—');
  setNav('nav-dev', disk.available ? disk.used_percent + '%' : '—');

  var docker = (ov.services || []).filter(function (item) { return item.name === 'Docker'; })[0];
  setNav('nav-svc', docker ? docker.detail : '—');

  document.getElementById('host').textContent = ov.host || '总控台';
  document.getElementById('uptime').textContent = fmtUptime(ov.uptime_s);
}

function renderProcesses(list, count) {
  var host = document.getElementById('procs');
  document.getElementById('proc-note').textContent = count ? '共 ' + count + ' 个进程' : '';
  if (!list || !list.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  host.innerHTML = list.map(function (proc) {
    return '<div class="row">' +
      '<span class="name">' + esc(proc.name) + '</span>' +
      '<span class="num">' + proc.cpu.toFixed(1) + '%</span>' +
      '<span class="num rss">' + fmtMB(proc.rss_mb) + '</span>' +
      '</div>';
  }).join('');
}

function renderServices(list) {
  var host = document.getElementById('services');
  if (!list || !list.length) {
    host.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  var html = '';
  var group = null;
  list.forEach(function (item) {
    if (item.group !== group) {
      group = item.group;
      html += '<div class="group-title"><span>' + esc(group) + '</span><span>' +
        esc(item.groupNote || '') + '</span></div>';
    }
    html += '<div class="row">' +
      '<span class="dot-s ' + esc(item.status) + '"></span>' +
      '<span class="name">' + esc(item.name) + '</span>' +
      '<span class="svc-detail">' + esc(item.detail) + '</span>' +
      '</div>';
  });
  host.innerHTML = html;
}

function updateRange(spec, peak, ov) {
  var tag = document.getElementById('range-' + spec.key);
  if (!tag) return;
  if (spec.key === 'cpu') {
    tag.textContent = '100%';
  } else if (spec.key === 'mem') {
    tag.textContent = ov.memory.available ? ov.memory.total_gb.toFixed(1) + ' GB' : '';
  } else if (spec.key === 'power') {
    tag.textContent = ov.power.available ? '峰值 ' + peak.toFixed(1) + ' W' : '';
  } else if (spec.key === 'net') {
    tag.textContent = peak > 0 ? '峰值 ' + fmtRate(peak) : '';
  }
}

/* ---------------- 轮询 ---------------- */

function fetchJSON(url) {
  return fetch(url, { cache: 'no-store' }).then(function (res) {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  });
}

function tick() {
  if (state.paused) return Promise.resolve();
  /* 手机上切后台/锁屏时跳过轮询，省电省流量；但首次渲染必须执行，
     否则在后台标签页里打开会一直白屏。回前台由 visibilitychange 立即补一次。 */
  if (document.hidden && state.tick > 0) return Promise.resolve();

  return fetchJSON('/api/overview').then(function (ov) {
    if (!ov.ready) return null;
    setLive('实时更新中', '');
    renderMetrics(ov);
    renderProcesses(ov.processes, ov.process_count);
    if (state.tick % 2 === 0) renderServices(ov.services);

    var url = state.lastSeriesTs > 0
      ? '/api/series?since=' + state.lastSeriesTs
      : '/api/series';

    return fetchJSON(url).then(function (data) {
      var series = data.series || {};
      if (data.ts) state.lastSeriesTs = data.ts;
      Object.keys(state.charts).forEach(function (name) {
        var inst = state.charts[name];
        if (!inst.chart) return;
        inst.spec.series.forEach(function (spec) {
          var pts = series[spec.key];
          if (pts && pts.length) inst.chart.append(spec.key, pts);
        });
        var peak = inst.chart.redraw(data.ts);
        updateRange(inst.spec, peak, ov);
      });
      state.tick++;
    });
  }).catch(function () {
    setLive('连接中断，重试中…', 'error');
  });
}

/* ---------------- 启动 ---------------- */

buildUpper();

document.getElementById('pause').addEventListener('click', function () {
  state.paused = !state.paused;
  this.textContent = state.paused ? '继续更新' : '暂停更新';
  if (state.paused) {
    setLive('已暂停', 'paused');
  } else {
    setLive('实时更新中', '');
    tick();
  }
});

setInterval(function () {
  document.getElementById('clock').textContent = fmtClock(new Date());
}, 1000);
document.getElementById('clock').textContent = fmtClock(new Date());

tick();
setInterval(tick, 1000);

document.addEventListener('visibilitychange', function () {
  if (!document.hidden && !state.paused) tick();
});
