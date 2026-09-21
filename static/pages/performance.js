/* 性能与电源页 —— 版式参照 docs/reference/02-performance-power.png：
 *
 *   上半部分：一张大卡
 *     左列  大数字（CPU 占用 %）+ 按物理核分组的占用条 + GPU 频率
 *     右侧  2 分钟大曲线（总占用 + 最高单核），左下角图例、右下角「最近 2 分钟」
 *   下半部分：三张并排小卡
 *     温度        两个大数字（封装 + 最热通道）+ 温度条 + 其余通道 + 小曲线
 *     功耗与风扇  大数字（风扇转速）+ 行式明细（整机功耗/电池/循环/负载）+ 小曲线
 *     内存        大数字（已用/共多少）+ 分段堆叠条 + 两列图例
 *
 * 所有数值都是本机真实采集；采不到的项显示「不可用」与原因。
 */
'use strict';

(function () {
  var SERIES_PARAM = 'cpu,cpu_max,temp,temp_acpi,fan_cpu';
  var GROUP_COLORS = ['#3ddc97', '#4fc3f7', '#ffb74d', '#ff6b9d'];
  var BATT_STATUS = {
    'Charging': '充电中',
    'Discharging': '放电中',
    'Not charging': '接通电源·未充电',
    'Full': '已充满',
    'Unknown': '状态未知'
  };

  var charts = {};
  var lastSeriesTs = 0;
  var coreGroups = [];
  var coreGroupKey = '';
  var sampleInterval = 0;

  /* ---------------- 构建 ---------------- */

  function build(host) {
    var top = document.createElement('section');
    top.className = 'perf-top';
    top.innerHTML =
      '<div class="perf-top-left">' +
        '<div class="card-title"><span class="tick" style="background:#3ddc97"></span>CPU 占用</div>' +
        '<div class="big-value"><span id="v-cpu">—</span></div>' +
        '<div class="core-groups" id="core-groups"></div>' +
        '<div class="side-note dim" id="thread-note">—</div>' +
        '<div class="side-note push-bottom" id="gpu-note">' +
          '<span class="tick" style="background:#ffd54f"></span>GPU 频率 ' +
          '<b id="v-gpu">—</b><span class="dim" id="s-gpu"></span>' +
        '</div>' +
      '</div>' +
      '<div class="perf-top-right">' +
        '<div class="scale-tag">100%</div>' +
        '<div class="big-chart" id="c-cpu"></div>' +
        '<div class="chart-foot">' +
          '<span class="legend">' +
            '<i style="background:#3ddc97"></i>CPU 占用' +
            '<i style="background:#8d84a6"></i>最高单核' +
          '</span>' +
          '<span>最近 2 分钟</span>' +
        '</div>' +
      '</div>';

    var bottom = document.createElement('section');
    bottom.className = 'perf-bottom';
    bottom.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>温度</span>' +
        '<span class="head-note" id="temp-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="temp-pair" id="temp-pair"></div>' +
          '<div class="temp-extra" id="temp-extra"></div>' +
          '<div class="mini-chart" id="c-temp"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>功耗与风扇</span>' +
        '<span class="head-note" id="power-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="big-value"><span id="v-power">—</span></div>' +
          '<div class="kv-rows" id="power-rows"></div>' +
          '<div class="mini-chart" id="c-fan"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>内存</span>' +
        '<span class="head-note" id="mem-note"></span></div>' +        '<div class="card-body">' +
          '<div class="big-value" id="v-mem-line"></div>' +
          '<div class="mem-stack" id="mem-stack"></div>' +
          '<div class="mem-legend" id="mem-legend"></div>' +
        '</div>' +
      '</div>';

    host.appendChild(top);
    host.appendChild(bottom);

    charts.cpu = window.createChart(document.getElementById('c-cpu'), {
      series: [{ key: 'cpu', color: '#3ddc97', fill: true },
               { key: 'cpu_max', color: '#8d84a6', fill: false, width: 1.5 }],
      fixed: [0, 100], window: WINDOW_SECONDS
    });
    charts.temp = window.createChart(document.getElementById('c-temp'), {
      series: [{ key: 'temp', color: '#ff8a65', fill: true },
               { key: 'temp_acpi', color: '#ce93d8', fill: false, width: 1.5 }],
      fixed: [0, 100], window: WINDOW_SECONDS
    });
    charts.fan = window.createChart(document.getElementById('c-fan'), {
      series: [{ key: 'fan_cpu', color: '#4fc3f7', fill: true }],
      autoMinTop: 600, window: WINDOW_SECONDS
    });

    document.getElementById('power-rows').innerHTML = '';
  }

  /* ---------------- 核心分组（按物理核，对应参考图的能效/性能核分组） ---------------- */

  function buildCoreGroups(perCpu, topology) {
    var count = (perCpu || []).length;
    var known = (topology || []).slice(0, count).some(function (id) { return id >= 0; });
    if (!known) {
      return [{ label: '处理器 · ' + count + ' 个线程', indexes: rangeIndexes(count) }];
    }
    var byCore = {};
    var order = [];
    for (var i = 0; i < count; i++) {
      var id = topology[i];
      var key = id >= 0 ? String(id) : 't' + i;
      if (!byCore[key]) { byCore[key] = []; order.push(key); }
      byCore[key].push(i);
    }
    order.sort();
    return order.map(function (key) {
      return { label: '核心 ' + key + ' · ' + byCore[key].length + ' 个线程',
               indexes: byCore[key] };
    });
  }

  function rangeIndexes(count) {
    var list = [];
    for (var i = 0; i < count; i++) list.push(i);
    return list;
  }

  function renderCoreGroups(perCpu, freqs, topology) {
    var host = document.getElementById('core-groups');
    var key = (topology || []).join(',') + '/' + perCpu.length;
    if (key !== coreGroupKey) {
      coreGroupKey = key;
      coreGroups = buildCoreGroups(perCpu, topology);
      host.innerHTML = coreGroups.map(function (group, gi) {
        var color = GROUP_COLORS[gi % GROUP_COLORS.length];
        var bars = group.indexes.map(function () {
          return '<i><b style="background:' + color + '"></b></i>';
        }).join('');
        return '<div class="core-group">' +
          '<div class="core-group-head">' + esc(group.label) + '，' +
          '<span data-freq="' + gi + '">—</span></div>' +
          '<div class="core-bars">' + bars + '</div></div>';
      }).join('');
    }

    var groups = host.querySelectorAll('.core-group');
    coreGroups.forEach(function (group, gi) {
      var node = groups[gi];
      if (!node) return;
      var bars = node.querySelectorAll('.core-bars b');
      group.indexes.forEach(function (threadIndex, barIndex) {
        if (bars[barIndex]) {
          bars[barIndex].style.width = Math.min(100, perCpu[threadIndex] || 0).toFixed(0) + '%';
        }
      });
      var freqNode = node.querySelector('[data-freq]');
      if (freqNode) {
        var values = group.indexes
          .map(function (index) { return freqs ? freqs[index] : null; })
          .filter(function (value) { return value; });
        freqNode.textContent = values.length ? Math.round(avg(values)) + ' MHz' : '—';
      }
    });
  }

  /* ---------------- 渲染 ---------------- */

  function avg(list) {
    return list.reduce(function (sum, item) { return sum + item; }, 0) / list.length;
  }

  /* 「机身温区」->「机身」、「CPU 核心 0」->「核心 0」、「芯片组 PCH」->「芯片组」 */
  function compactLabel(label) {
    return String(label)
      .replace(/^CPU\s*/, '')
      .replace(/温区$/, '')
      .replace(/\s*PCH$/, '')
      .trim();
  }

  function tempColor(celsius) {
    if (celsius >= 85) return '#ff6b9d';
    if (celsius >= 70) return '#ffb74d';
    return '#3ddc97';
  }

  function renderTemps(temps) {
    var pair = document.getElementById('temp-pair');
    var extra = document.getElementById('temp-extra');
    if (!temps.available || !temps.list || !temps.list.length) {
      pair.innerHTML = '<div class="temp-cell"><div class="big-value na">不可用</div></div>';
      extra.textContent = temps.reason || '';
      document.getElementById('temp-note').textContent = '';
      return;
    }
    var list = temps.list;
    var main = list.filter(function (item) { return item.key === 'package'; })[0];
    var others = list.filter(function (item) { return item !== main; });
    var cells = [];
    if (main) cells.push({ label: 'CPU 封装', item: main });
    var hottest = others.slice().sort(function (a, b) { return b.celsius - a.celsius; })[0];
    if (hottest) cells.push({ label: compactLabel(hottest.label), item: hottest });

    pair.innerHTML = cells.map(function (cell) {
      var color = tempColor(cell.item.celsius);
      return '<div class="temp-cell">' +
        '<div class="temp-label">' + esc(cell.label) + '</div>' +
        '<div class="big-value"><span>' + cell.item.celsius.toFixed(1) + '</span>' +
        '<span class="unit">°C</span></div>' +
        '<div class="temp-bar"><b style="width:' +
        Math.min(100, cell.item.celsius).toFixed(0) + '%;background:' + color + '"></b></div>' +
        '</div>';
    }).join('');

    var rest = list.filter(function (item) {
      return item !== main && item !== hottest;
    }).map(function (item) {
      return compactLabel(item.label) + ' ' + item.celsius.toFixed(0) + '°C';
    });
    var hottestValue = others.reduce(function (max, item) {
      return Math.max(max, item.celsius);
    }, 0);
    extra.textContent = (hottestValue >= 85 ? '有过热风险' : '温度正常') +
      (rest.length ? ' · 另有 ' + rest.join(' · ') : '');
    document.getElementById('temp-note').textContent = list.length + ' 路传感器';
  }

  function kvRow(label, value) {
    return '<div class="kv-row"><span>' + esc(label) + '</span><b>' + esc(value) + '</b></div>';
  }

  function renderPower(perf) {
    var power = perf.power || {};
    var fans = perf.fans || {};
    var list = (fans.available && fans.list) || [];
    var cpuFan = list.filter(function (item) { return item.key === 'cpu_fan'; })[0] || list[0];
    var note = document.getElementById('power-note');
    var valueNode = document.getElementById('v-power');
    valueNode.classList.remove('na');

    /* 大数字：RAPL 可读时显示功耗瓦数（与参考图一致），不可读时退化为风扇转速 */
    if (power.available) {
      setValue('v-power', power.watts.toFixed(1), 'W');
      note.textContent = (power.source || 'RAPL') + ' · Intel RAPL' +
        (power.skipped && power.skipped.length ? ' · ' + power.skipped.length + ' 项无权限' : '');
    } else if (cpuFan) {
      setValue('v-power', cpuFan.rpm, 'RPM');
      note.textContent = power.reason || '';
    } else {
      valueNode.classList.add('na');
      valueNode.textContent = '不可用';
      note.textContent = power.reason || fans.reason || '';
    }

    var rows = [];
    /* 功耗构成：主值已是大数字，其余域列成明细（参考图里的「GPU / CPU 等」那种分解） */
    (power.domains || []).slice(1).forEach(function (domain) {
      rows.push(kvRow(domain.label, domain.watts.toFixed(2) + ' W'));
    });

    var batt = perf.battery || {};
    if (batt.available) {
      var status = BATT_STATUS[batt.status] || (batt.plugged ? '已接通电源' : '放电中');
      var bits = [batt.percent.toFixed(1) + '%', status];
      if (batt.power_w) bits.push(batt.power_w.toFixed(1) + ' W');
      if (batt.secsleft) bits.push('剩 ' + fmtUptime(batt.secsleft));
      rows.push(kvRow('电池', bits.join(' · ')));
      rows.push(kvRow('循环次数', batt.cycles ? batt.cycles + ' 次' : '—'));
    } else {
      rows.push(kvRow('电池', batt.reason || '不可用'));
    }

    var otherFans = list.filter(function (item) { return item !== cpuFan; });
    rows.push(kvRow('其他风扇', otherFans.length
      ? otherFans.map(function (item) {
          return item.label + ' ' + item.rpm + ' RPM' + (item.rpm === 0 ? '（停转）' : '');
        }).join(' · ')
      : '—'));

    var load = perf.load || {};
    rows.push(kvRow('负载 1/5/15', load.available
      ? load.avg1 + ' / ' + load.avg5 + ' / ' + load.avg15
      : (load.reason || '—')));

    document.getElementById('power-rows').innerHTML = rows.join('');
  }

  function renderMemory(mem) {
    var stack = document.getElementById('mem-stack');
    var legend = document.getElementById('mem-legend');
    var line = document.getElementById('v-mem-line');
    if (!mem.available) {
      line.innerHTML = '<span class="na">不可用</span>';
      document.getElementById('mem-note').textContent = mem.reason || '';
      stack.innerHTML = '';
      legend.innerHTML = '';
      return;
    }
    line.innerHTML = '<span>' + mem.used_gb.toFixed(1) + '</span>' +
      '<span class="unit">GB 已用，共 ' + mem.total_gb.toFixed(1) + ' GB</span>';

    /* Linux 经典口径：总量 = 进程与内核 + 缓冲 + 缓存 + 空闲（不重叠，正好加满） */
    var appGb = Math.max(0, mem.total_gb - mem.free_gb - mem.buffers_gb - mem.cached_gb);
    var segments = [
      { label: '应用与内核', value: appGb, color: '#4fc3f7' },
      { label: '缓冲', value: mem.buffers_gb, color: '#9575cd' },
      { label: '缓存', value: mem.cached_gb, color: '#81c784' },
      { label: '空闲', value: mem.free_gb, color: 'rgba(255,255,255,0.08)' }
    ];
    var total = mem.total_gb || 1;
    stack.innerHTML = segments.map(function (seg) {
      return '<i style="width:' + (seg.value / total * 100).toFixed(2) +
        '%;background:' + seg.color + '"></i>';
    }).join('');
    var items = segments.map(function (seg) {
      return '<span><i style="background:' + seg.color + '"></i>' + seg.label + ' ' +
        seg.value.toFixed(1) + ' GB</span>';
    });
    items.push('<span><i style="background:#ff6b9d"></i>交换 ' +
      mem.swap_used_gb.toFixed(1) + ' GB</span>');
    legend.innerHTML = items.join('');
    document.getElementById('mem-note').textContent = '已用 ' + mem.percent.toFixed(0) + '%';
  }

  /* 左列底部那行「2 核 4 线程 · 采样 1s」 */
  function updateThreadNote(threads, topology) {
    var seen = {};
    var physical = 0;
    (topology || []).forEach(function (id) {
      if (id >= 0 && !seen[id]) { seen[id] = true; physical++; }
    });
    var parts = [(physical || threads) + ' 核 ' + threads + ' 线程'];
    if (sampleInterval) parts.push('采样 ' + sampleInterval + 's');
    document.getElementById('thread-note').textContent = parts.join(' · ');
  }

  function render(perf) {
    var cpu = perf.cpu || {};
    if (cpu.available) {
      setValue('v-cpu', cpu.percent.toFixed(0), '%');
    } else {
      var cpuNode = document.getElementById('v-cpu');
      cpuNode.classList.add('na');
      cpuNode.textContent = '不可用';
    }

    var cores = cpu.per_core || {};
    if (cores.available && cores.per_cpu && cores.per_cpu.length) {
      renderCoreGroups(cores.per_cpu, cores.freq_mhz, cores.topology || []);
      updateThreadNote(cores.per_cpu.length, cores.topology || []);
    }

    var gpu = perf.gpu || {};
    var gpuNode = document.getElementById('v-gpu');
    if (gpu.available) {
      gpuNode.textContent = gpu.freq_mhz;
      document.getElementById('s-gpu').textContent =
        ' MHz · 最大 ' + (gpu.max_mhz || '—') + ' MHz';
    } else {
      gpuNode.textContent = '不可用';
      document.getElementById('s-gpu').textContent = '';
    }

    renderTemps(perf.temps || {});
    renderPower(perf);
    renderMemory(perf.memory || {});
  }

  /* ---------------- 轮询 ---------------- */

  function syncGaps(interval) {
    var gap = Math.max(6, interval * 4);
    Object.keys(charts).forEach(function (name) { charts[name].setGap(gap); });
  }

  function applySeries(data) {
    var series = data.series || {};
    if (data.ts) lastSeriesTs = data.ts;
    if (data.interval) {
      sampleInterval = data.interval;
      syncGaps(data.interval);
    }
    Object.keys(charts).forEach(function (name) {
      var chart = charts[name];
      Object.keys(series).forEach(function (key) {
        /* 不属于本图的键会被 append 内部忽略 */
        if (series[key] && series[key].length) chart.append(key, series[key]);
      });
      chart.redraw(data.ts);
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
      if (results[0].ready) {
        setLive('实时更新中', '');
        render(results[0]);
      }
      applySeries(results[1]);
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
      coreGroups = [];
      coreGroupKey = '';
      sampleInterval = 0;
      build(host);
    },
    tick: tick,
    render: render,          /* 公开给探针/测试 */
    applySeries: applySeries
  };
})();
