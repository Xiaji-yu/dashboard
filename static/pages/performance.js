/* 性能与电源页：每核占用、温度通道、风扇、核显频率曲线 + 处理器/内存构成/电源三张卡片。 */
'use strict';

(function () {
  var CHART_SPECS = [
    { key: 'cpu', label: 'CPU 占用', hex: '#3ddc97',
      series: [{ key: 'cpu', color: '#3ddc97', fill: true }], fixed: [0, 100] },
    { key: 'cores', label: '每核占用', hex: '#4fc3f7',
      series: [{ key: 'cpu0', color: '#3ddc97' }, { key: 'cpu1', color: '#4fc3f7' },
               { key: 'cpu2', color: '#ffb74d' }, { key: 'cpu3', color: '#ff6b9d' }],
      fixed: [0, 100] },
    { key: 'temp', label: '温度', hex: '#ff8a65',
      series: [{ key: 'temp', color: '#ff8a65', fill: true },
               { key: 'temp_acpi', color: '#ce93d8', fill: false }],
      fixed: [0, 100] },
    { key: 'fan', label: '风扇转速', hex: '#4fc3f7',
      series: [{ key: 'fan_cpu', color: '#4fc3f7', fill: true }], autoMinTop: 600 },
    { key: 'gpu', label: 'GPU 频率', hex: '#ffd54f',
      series: [{ key: 'gpu_mhz', color: '#ffd54f', fill: true }], autoMinTop: 200 }
  ];

  var SERIES_PARAM = 'cpu,cpu0,cpu1,cpu2,cpu3,temp,temp_acpi,fan_cpu,gpu_mhz';
  var BATT_STATUS = {
    'Charging': '充电中',
    'Discharging': '放电中',
    'Not charging': '接通电源·未充电',
    'Full': '已充满',
    'Unknown': '状态未知'
  };

  var charts = {};
  var lastSeriesTs = 0;
  var gpuMax = 1100;
  var threadsBuilt = 0;
  var VISIBLE_CORES = 4;  /* 超过这个数折叠到「展开其余 N 线程」按钮后面 */
  var coresExpanded = false;
  var coreCount = 0;

  /* ---------------- 构建 ---------------- */

  function buildMetricRow(upper, spec) {
    var row = document.createElement('div');
    row.className = 'metric';

    var body = document.createElement('div');
    body.className = 'metric-body';

    var label = document.createElement('div');
    label.className = 'metric-label';
    if (spec.hex) {
      var mark = document.createElement('span');
      mark.className = 'tick';
      mark.style.background = spec.hex;
      label.appendChild(mark);
    }
    label.appendChild(document.createTextNode(spec.label));
    body.appendChild(label);

    var value = document.createElement('div');
    value.className = 'metric-value';
    value.id = 'v-' + spec.key;
    value.textContent = '—';
    body.appendChild(value);

    var sub = document.createElement('div');
    sub.className = 'metric-sub';
    sub.id = 's-' + spec.key;
    body.appendChild(sub);

    var chartHost = document.createElement('div');
    chartHost.className = 'metric-chart';
    var tag = document.createElement('span');
    tag.className = 'range-tag';
    tag.id = 'range-' + spec.key;
    chartHost.appendChild(tag);

    charts[spec.key] = {
      spec: spec,
      chart: window.createChart(chartHost, {
        series: spec.series,
        fixed: spec.fixed || null,
        autoMinTop: spec.autoMinTop || 1,
        window: WINDOW_SECONDS
      }),
      naReason: null
    };

    row.appendChild(body);
    row.appendChild(chartHost);
    upper.appendChild(row);
  }

  function build(host) {
    var upper = document.createElement('section');
    upper.className = 'upper perf-upper';
    CHART_SPECS.forEach(function (spec) { buildMetricRow(upper, spec); });

    var lower = document.createElement('section');
    lower.className = 'lower perf-lower';
    lower.innerHTML =
      '<div class="card">' +
      '<div class="card-head"><span>处理器</span>' +
      '<span class="head-note" id="core-note"></span></div>' +
      '<div class="card-body" id="core-rows"></div></div>' +
      '<div class="card">' +
      '<div class="card-head"><span>内存构成</span>' +
      '<span class="head-note" id="mem-note"></span></div>' +
      '<div class="card-body">' +
      '<div class="mem-stack" id="mem-stack"></div>' +
      '<div class="mem-legend" id="mem-legend"></div>' +
      '</div></div>' +
      '<div class="card wide">' +
      '<div class="card-head"><span>电源与负载</span></div>' +
      '<div class="card-body" id="power-rows"></div></div>';

    host.appendChild(upper);
    host.appendChild(lower);
  }

  function buildCoreRows(count) {
    coreCount = count;
    coresExpanded = false;
    var host = document.getElementById('core-rows');
    var html = '';
    for (var i = 0; i < count; i++) {
      html += '<div class="core-row' + (i >= VISIBLE_CORES ? ' is-hidden' : '') + '">' +
        '<span class="name">线程 ' + i + '</span>' +
        '<span class="meter" id="meter-core-' + i + '"><i></i></span>' +
        '<span class="pct" id="pct-core-' + i + '">—</span>' +
        '<span class="freq" id="freq-core-' + i + '">—</span>' +
        '</div>';
    }
    if (count > VISIBLE_CORES) {
      html += '<button class="core-toggle" id="core-toggle" type="button"></button>';
    }
    host.innerHTML = html;
    updateCoreVisibility();
    updateCoreToggle();
    var button = document.getElementById('core-toggle');
    if (button) {
      button.addEventListener('click', function () {
        coresExpanded = !coresExpanded;
        updateCoreVisibility();
        updateCoreToggle();
      });
    }
    threadsBuilt = count;
  }

  function updateCoreVisibility() {
    var rows = document.querySelectorAll('#core-rows .core-row');
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.toggle('is-hidden', !coresExpanded && i >= VISIBLE_CORES);
    }
  }

  function updateCoreToggle() {
    var button = document.getElementById('core-toggle');
    if (!button) return;
    button.textContent = coresExpanded
      ? '收起，只显示前 ' + VISIBLE_CORES + ' 个线程 ▴'
      : '展开其余 ' + (coreCount - VISIBLE_CORES) + ' 个线程 ▾';
  }

  function buildPowerRows() {
    document.getElementById('power-rows').innerHTML =
      '<div class="row"><span class="dot-s ok"></span><span class="name">电池</span>' +
      '<span class="svc-detail" id="pw-batt">—</span></div>' +
      '<div class="row"><span class="dot-s unknown"></span><span class="name">整机功耗</span>' +
      '<span class="svc-detail" id="pw-rapl">—</span></div>' +
      '<div class="row"><span class="dot-s ok"></span><span class="name">负载 1/5/15</span>' +
      '<span class="svc-detail" id="pw-load">—</span></div>';
  }

  /* ---------------- 渲染 ---------------- */

  function markChartUnavailable(key, reason) {
    var inst = charts[key];
    var value = document.getElementById('v-' + key);
    if (value) {
      value.classList.add('na');
      value.textContent = '不可用';
    }
    setSub('s-' + key, '');
    var tag = document.getElementById('range-' + key);
    if (tag) tag.textContent = '';
    if (inst && inst.naReason !== reason) {
      inst.chart.setUnavailable(reason);
      inst.naReason = reason;
    }
  }

  function avg(list) {
    return list.reduce(function (sum, item) { return sum + item; }, 0) / list.length;
  }

  /* 「机身温区」->「机身」、「CPU 核心 0」->「核心 0」，让副标题在窄栏里放得下 */
  function compactTempLabel(label) {
    return String(label).replace(/^CPU\s*/, '').replace(/温区$/, '').trim();
  }

  function render(perf) {
    var cpu = perf.cpu || {};

    /* CPU 总占用 */
    if (cpu.available) {
      setValue('v-cpu', cpu.percent.toFixed(0), '%');
      setSub('s-cpu', cpu.freq_mhz ? '平均 ' + cpu.freq_mhz + ' MHz' : '');
    } else {
      markChartUnavailable('cpu', cpu.reason);
    }

    /* 每核占用：曲线 + 卡片里的线程条 */
    var cores = cpu.per_core || {};
    if (cores.available && cores.per_cpu && cores.per_cpu.length) {
      var list = cores.per_cpu;
      setValue('v-cores', avg(list).toFixed(0), '%');
      var physical = (cores.topology || []).filter(function (id, index, all) {
        return id >= 0 && all.indexOf(id) === index;
      }).length;
      setSub('s-cores', (physical || list.length) + ' 核 ' + list.length + ' 线程');
      if (threadsBuilt !== list.length) buildCoreRows(list.length);
      else updateCoreVisibility();
      list.forEach(function (percent, index) {
        var meter = document.getElementById('meter-core-' + index);
        if (meter) {
          meter.classList.toggle('hot', percent >= 80);
          meter.firstElementChild.style.width = Math.min(100, percent).toFixed(0) + '%';
        }
        var pct = document.getElementById('pct-core-' + index);
        if (pct) pct.textContent = percent.toFixed(0) + '%';
        var freq = document.getElementById('freq-core-' + index);
        if (freq) {
          freq.textContent = (cores.freq_mhz && cores.freq_mhz[index])
            ? cores.freq_mhz[index] + ' MHz' : '—';
        }
      });
      document.getElementById('core-note').textContent =
        '共 ' + list.length + ' 线程 · 2 秒内刷新';
    } else if (!cores.available) {
      setSub('s-cores', cores.reason || '不可用');
    }

    /* 温度：主值为封装温度，副值给最高的其他通道（标签压缩，避免被截断） */
    var temps = perf.temps || {};
    if (temps.available && temps.list && temps.list.length) {
      var channels = temps.list;
      var main = channels.filter(function (item) { return item.key === 'package'; })[0] || channels[0];
      setValue('v-temp', main.celsius.toFixed(0), '°C');
      var hottest = channels.filter(function (item) { return item !== main; })
        .sort(function (a, b) { return b.celsius - a.celsius; })[0];
      setSub('s-temp', channels.length + ' 路传感器' +
        (hottest ? ' · 最高 ' + compactTempLabel(hottest.label) + ' ' + hottest.celsius.toFixed(0) + '°C' : ''));
    } else {
      markChartUnavailable('temp', temps.reason || '不可用');
    }

    /* 风扇：主值为 CPU 风扇，副值列其余风扇 */
    var fans = perf.fans || {};
    if (fans.available && fans.list && fans.list.length) {
      var cpuFan = fans.list.filter(function (item) { return item.key === 'cpu_fan'; })[0];
      if (cpuFan) {
        setValue('v-fan', cpuFan.rpm, 'RPM');
      } else {
        setValue('v-fan', fans.list[0].rpm, 'RPM');
      }
      var rest = fans.list
        .filter(function (item) { return item !== cpuFan; })
        .map(function (item) {
          return item.label + ' ' + item.rpm + ' RPM' + (item.rpm === 0 ? '（停转）' : '');
        });
      setSub('s-fan', rest.length ? rest.join(' · ') : fans.list[0].label);
    } else {
      markChartUnavailable('fan', fans.reason || '不可用');
    }

    /* GPU 频率 */
    var gpu = perf.gpu || {};
    if (gpu.available) {
      if (gpu.max_mhz) {
        gpuMax = gpu.max_mhz;
        charts.gpu.chart.setFixed([0, gpu.max_mhz]);
      }
      setValue('v-gpu', gpu.freq_mhz, 'MHz');
      setSub('s-gpu', '核显 · 最大 ' + (gpu.max_mhz || '—') + ' MHz');
    } else {
      markChartUnavailable('gpu', gpu.reason);
    }

    /* 内存构成：按 Linux 经典口径拆成不重叠的四段，正好加满总量 */
    var mem = perf.memory || {};
    if (mem.available) {
      /* 不能拿 psutil 的 used 再减缓冲/缓存：它等于「总量 - available」，已扣过可回收缓存 */
      var appGb = Math.max(0, mem.total_gb - mem.free_gb - mem.buffers_gb - mem.cached_gb);
      var segments = [
        ['应用与内核', appGb, '#4fc3f7'],
        ['缓冲', mem.buffers_gb, '#9575cd'],
        ['缓存', mem.cached_gb, '#81c784'],
        ['空闲', mem.free_gb, 'rgba(255,255,255,0.08)']
      ];
      var total = mem.total_gb || 1;
      document.getElementById('mem-stack').innerHTML = segments.map(function (seg) {
        return '<i style="width:' + (seg[1] / total * 100).toFixed(2) + '%;background:' + seg[2] + '"></i>';
      }).join('');
      document.getElementById('mem-legend').innerHTML = segments.map(function (seg) {
        return '<span><i style="background:' + seg[2] + '"></i>' +
          seg[0] + ' ' + seg[1].toFixed(1) + ' GB</span>';
      }).join('');
      document.getElementById('mem-note').textContent =
        '已用 ' + mem.percent.toFixed(0) + '% · 交换 ' +
        mem.swap_used_gb.toFixed(1) + ' / ' + mem.swap_total_gb.toFixed(1) + ' GB';
    } else {
      document.getElementById('mem-note').textContent = mem.reason || '不可用';
      document.getElementById('mem-stack').innerHTML = '';
      document.getElementById('mem-legend').innerHTML = '<span>—</span>';
    }

    /* 电源与负载 */
    var batt = perf.battery || {};
    var battText = '—';
    if (batt.available) {
      var parts = [batt.percent.toFixed(1) + '%'];
      var statusText = BATT_STATUS[batt.status] ||
        (batt.plugged ? '已接通电源' : '放电中');
      parts.push(statusText);
      if (batt.cycles) parts.push('循环 ' + batt.cycles + ' 次');
      if (batt.power_w) parts.push('放电 ' + batt.power_w.toFixed(1) + ' W');
      if (batt.secsleft) parts.push('剩余约 ' + fmtUptime(batt.secsleft));
      battText = parts.join(' · ');
    } else {
      battText = batt.reason || '—';
    }
    var power = perf.power || {};
    var raplText = power.available
      ? power.watts.toFixed(1) + ' W（Intel RAPL）'
      : (power.reason || '不可用');
    var load = perf.load || {};
    var loadText = load.available
      ? load.avg1 + ' / ' + load.avg5 + ' / ' + load.avg15
      : (load.reason || '—');

    if (document.getElementById('pw-batt')) {
      document.getElementById('pw-batt').textContent = battText;
      document.getElementById('pw-rapl').textContent = raplText;
      document.getElementById('pw-load').textContent = loadText;
    }
  }

  function updateTags(key, peak) {
    var tag = document.getElementById('range-' + key);
    if (!tag) return;
    if (key === 'cpu' || key === 'cores') {
      tag.textContent = '100%';
    } else if (key === 'temp') {
      tag.textContent = peak > 0 ? '峰值 ' + peak.toFixed(0) + '°C' : '100°C';
    } else if (key === 'fan') {
      tag.textContent = peak > 0 ? '峰值 ' + peak.toFixed(0) + ' RPM' : '';
    } else if (key === 'gpu') {
      tag.textContent = '最大 ' + gpuMax + ' MHz';
    }
  }

  /* ---------------- 轮询 ---------------- */

  function applySeries(data) {
    var series = data.series || {};
    if (data.ts) lastSeriesTs = data.ts;
    if (data.interval) {
      var gap = Math.max(6, data.interval * 4);
      Object.keys(charts).forEach(function (name) { charts[name].chart.setGap(gap); });
    }
    Object.keys(charts).forEach(function (name) {
      var inst = charts[name];
      inst.spec.series.forEach(function (spec) {
        var pts = series[spec.key];
        if (pts && pts.length) inst.chart.append(spec.key, pts);
      });
      var peak = inst.chart.redraw(data.ts);
      updateTags(inst.spec.key, peak);
    });
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();

    var seriesUrl = '/api/series?keys=' + SERIES_PARAM +
      (lastSeriesTs > 0 ? '&since=' + lastSeriesTs : '');

    return Promise.all([
      fetchJSON('/api/performance'),
      fetchJSON(seriesUrl)
    ]).then(function (results) {
      var perf = results[0];
      var data = results[1];

      if (perf.ready) {
        setLive('实时更新中', '');
        render(perf);
      }
      applySeries(data);
      state.tick++;
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.performance = {
    title: '性能与电源',
    interval: 1000,
    mount: function (host) {
      charts = {};
      lastSeriesTs = 0;
      threadsBuilt = 0;
      coresExpanded = false;
      build(host);
      buildPowerRows();
    },
    tick: tick,
    render: render,            // 公开给探针/测试：直接渲染一份 performance 载荷
    applySeries: applySeries
  };
})();
