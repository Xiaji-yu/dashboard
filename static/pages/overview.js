/* 概览页：上半部分 5 条指标曲线（铺满）+ 最忙进程 + 服务状态。 */
'use strict';

(function () {
  /* 上半部分：去掉示意图后的 5 条指标，曲线铺满整行 */
  var METRICS = [
    { key: 'cpu', label: 'CPU 占用', hex: '#3ddc97',
      series: [{ key: 'cpu', color: '#3ddc97', fill: true }], fixed: [0, 100] },
    { key: 'mem', label: '内存', hex: '#4fc3f7',
      series: [{ key: 'mem_used', color: '#4fc3f7', fill: true }], autoMinTop: 1 },
    { key: 'power', label: '功耗', hex: '#ffb74d',
      series: [{ key: 'power', color: '#ffb74d', fill: true }], autoMinTop: 10 },
    { key: 'net', label: '网速', hex: null,
      series: [{ key: 'net_down', color: '#3ddc97', fill: false },
               { key: 'net_up', color: '#ff6b9d', fill: false }], autoMinTop: 4096 },
    { key: 'disk', label: '磁盘剩余', hex: '#ffd54f', bar: true }
  ];

  var charts = {};
  var lastSeriesTs = 0;
  var servicesTick = 0;

  /* ---------------- 构建上半部分 ---------------- */

  function buildUpper(host) {
    var upper = document.createElement('section');
    upper.className = 'upper';
    upper.id = 'upper';

    METRICS.forEach(function (spec) {
      var row = document.createElement('div');
      row.className = 'metric';

      var body = document.createElement('div');
      body.className = 'metric-body';

      var label = document.createElement('div');
      label.className = 'metric-label';
      if (spec.hex) {
        var tickMark = document.createElement('span');
        tickMark.className = 'tick';
        tickMark.style.background = spec.hex;
        label.appendChild(tickMark);
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
        charts[spec.key] = { spec: spec, chart: null };
      } else {
        var chart = window.createChart(chartHost, {
          series: spec.series,
          fixed: spec.fixed || null,
          autoMinTop: spec.autoMinTop || 1,
          window: WINDOW_SECONDS
        });
        charts[spec.key] = { spec: spec, chart: chart, naReason: null };
      }

      row.appendChild(body);
      row.appendChild(chartHost);
      upper.appendChild(row);
    });

    /* 下半部分：两张卡片 */
    var lower = document.createElement('section');
    lower.className = 'lower';
    lower.innerHTML =
      '<div class="card">' +
      '<div class="card-head"><span>此刻最忙的程序</span>' +
      '<span class="head-note" id="proc-note"></span></div>' +
      '<div class="card-body" id="procs"></div></div>' +
      '<div class="card">' +
      '<div class="card-head"><span>服务状态</span>' +
      '<span class="head-note" id="svc-note"></span></div>' +
      '<div class="card-body" id="services"></div></div>';

    host.appendChild(upper);
    host.appendChild(lower);
  }

  /* ---------------- 渲染 ---------------- */

  function markUnavailable(key, reason) {
    var inst = charts[key];
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
      charts.mem.chart.setFixed([0, mem.total_gb]);
    } else {
      markUnavailable('mem', mem.reason);
    }

    var power = ov.power;
    if (power.available) {
      setValue('v-power', power.watts.toFixed(1), 'W');
      setSub('s-power', (power.source || 'Intel RAPL') + ' · RAPL');
    } else {
      markUnavailable('power', power.reason);
      setSub('s-power', power.reason.indexOf('root') >= 0 ? '需 root 或 udev 规则' : '');
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
    document.getElementById('svc-note').textContent = '本机 · 每 5 秒刷新';
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

  function applySeries(data, ov) {
    var series = data.series || {};
    if (data.ts) lastSeriesTs = data.ts;
    if (data.interval) syncGaps(data.interval);
    Object.keys(charts).forEach(function (name) {
      var inst = charts[name];
      if (!inst.chart) return;
      inst.spec.series.forEach(function (spec) {
        var pts = series[spec.key];
        if (pts && pts.length) inst.chart.append(spec.key, pts);
      });
      var peak = inst.chart.redraw(data.ts);
      updateRange(inst.spec, peak, ov);
    });
  }

  /* 断线阈值跟随服务端采样间隔（一般采样 1s，阈值 6s 以上） */
  function syncGaps(interval) {
    var gap = Math.max(6, interval * 4);
    Object.keys(charts).forEach(function (name) {
      if (charts[name].chart) charts[name].chart.setGap(gap);
    });
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();

    return fetchJSON('/api/overview').then(function (ov) {
      if (!ov.ready) return null;
      setLive('实时更新中', '');
      renderMetrics(ov);
      updateBadges(ov);
      renderProcesses(ov.processes, ov.process_count);
      if (servicesTick % 2 === 0) renderServices(ov.services);

      var url = lastSeriesTs > 0
        ? '/api/series?since=' + lastSeriesTs
        : '/api/series';

      return fetchJSON(url).then(function (data) {
        applySeries(data, ov);
        state.tick++;
        servicesTick++;
      });
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.overview = {
    title: '概览',
    badges: false,  // 自己 1s 全量刷新，徽章顺带更新，核心不用再拉一遍
    interval: 1000,
    mount: function (host) {
      charts = {};
      lastSeriesTs = 0;
      servicesTick = 0;
      buildUpper(host);
    },
    tick: tick,
    render: renderMetrics,         // 公开给探针/测试
    renderProcesses: renderProcesses,
    renderServices: renderServices,
    applySeries: applySeries
  };
})();
