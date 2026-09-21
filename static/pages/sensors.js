/* 传感器页：温度通道、风扇转速、每核频率与占用，各带 2 分钟曲线。
 *
 * 数据直接复用 /api/performance（温度/风扇/每核）与 /api/series，没有新增接口。
 */
'use strict';

(function () {
  var SERIES_PARAM = 'temp,temp_acpi,fan_cpu,cpu0,cpu1,cpu2,cpu3';
  var CORE_COLORS = ['#3ddc97', '#4fc3f7', '#ffb74d', '#ff6b9d'];
  var charts = {};
  var lastSeriesTs = 0;

  function build(host) {
    var top = document.createElement('section');
    top.className = 'sensor-top';
    top.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>温度</span>' +
        '<span class="head-note" id="sen-temp-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="temp-rows" id="sen-temp-rows"></div>' +
          '<div class="mini-chart" id="c-sen-temp"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>风扇</span>' +
        '<span class="head-note" id="sen-fan-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="big-value"><span id="sen-fan-value">—</span></div>' +
          '<div class="kv-rows" id="sen-fan-rows"></div>' +
          '<div class="mini-chart" id="c-sen-fan"></div>' +
        '</div>' +
      '</div>';

    var bottom = document.createElement('section');
    bottom.className = 'card sensor-bottom';
    bottom.innerHTML =
      '<div class="card-head"><span>每核频率与占用</span>' +
      '<span class="head-note" id="sen-core-note"></span></div>' +
      '<div class="card-body">' +
        '<div class="core-rows" id="sen-core-rows"></div>' +
        '<div class="core-chart" id="c-sen-core"></div>' +
      '</div>';

    host.appendChild(top);
    host.appendChild(bottom);

    charts.temp = window.createChart(document.getElementById('c-sen-temp'), {
      series: [{ key: 'temp', color: '#ff8a65', fill: true },
               { key: 'temp_acpi', color: '#ce93d8', fill: false, width: 1.6 }],
      fixed: [0, 100], window: WINDOW_SECONDS
    });
    charts.fan = window.createChart(document.getElementById('c-sen-fan'), {
      series: [{ key: 'fan_cpu', color: '#4fc3f7', fill: true }],
      autoMinTop: 600, window: WINDOW_SECONDS
    });
    charts.core = window.createChart(document.getElementById('c-sen-core'), {
      series: [{ key: 'cpu0', color: CORE_COLORS[0], fill: false },
               { key: 'cpu1', color: CORE_COLORS[1], fill: false },
               { key: 'cpu2', color: CORE_COLORS[2], fill: false },
               { key: 'cpu3', color: CORE_COLORS[3], fill: false }],
      fixed: [0, 100], window: WINDOW_SECONDS
    });
  }

  function tempColor(celsius) {
    if (celsius >= 85) return '#ff6b9d';
    if (celsius >= 70) return '#ffb74d';
    return '#3ddc97';
  }

  function renderTemps(temps) {
    var host = document.getElementById('sen-temp-rows');
    var note = document.getElementById('sen-temp-note');
    if (!temps || !temps.available || !temps.list || !temps.list.length) {
      note.textContent = '不可用';
      host.innerHTML = '<div class="empty">' + esc((temps && temps.reason) || '没有温度传感器') + '</div>';
      return;
    }
    var hottest = temps.list.reduce(function (max, item) {
      return item.celsius > max.celsius ? item : max;
    }, temps.list[0]);
    note.textContent = temps.list.length + ' 路 · 最高 ' + hottest.celsius.toFixed(0) + '°C';
    host.innerHTML = temps.list.map(function (item) {
      var color = tempColor(item.celsius);
      return '<div class="temp-row">' +
        '<span class="name">' + esc(item.label) + '</span>' +
        '<span class="meter"><i style="width:' + Math.min(100, item.celsius).toFixed(0) +
          '%;background:' + color + '"></i></span>' +
        '<b class="value">' + item.celsius.toFixed(1) + '°C</b>' +
        '</div>';
    }).join('');
  }

  function renderFans(fans) {
    var note = document.getElementById('sen-fan-note');
    var value = document.getElementById('sen-fan-value');
    var host = document.getElementById('sen-fan-rows');
    if (!fans || !fans.available || !fans.list || !fans.list.length) {
      note.textContent = '不可用';
      value.textContent = '—';
      host.innerHTML = '<div class="empty">' + esc((fans && fans.reason) || '没有风扇转速') + '</div>';
      return;
    }
    var list = fans.list;
    var main = list.filter(function (item) { return item.key === 'cpu_fan'; })[0] || list[0];
    note.textContent = list.length + ' 路风扇';
    value.innerHTML = '';
    value.appendChild(document.createTextNode(String(main.rpm)));
    var unit = document.createElement('span');
    unit.className = 'unit';
    unit.textContent = ' RPM';
    value.appendChild(unit);
    host.innerHTML = list.filter(function (item) { return item !== main; }).map(function (item) {
      return '<div class="kv-row"><span>' + esc(item.label) + '</span><b>' +
        item.rpm + ' RPM' + (item.rpm === 0 ? '（停转）' : '') + '</b></div>';
    }).join('') || '<div class="kv-row"><span>其他风扇</span><b>—</b></div>';
  }

  function renderCores(perCore) {
    var host = document.getElementById('sen-core-rows');
    var note = document.getElementById('sen-core-note');
    if (!perCore || !perCore.available || !perCore.per_cpu || !perCore.per_cpu.length) {
      note.textContent = '不可用';
      host.innerHTML = '<div class="empty">' + esc((perCore && perCore.reason) || '每核数据不可用') + '</div>';
      return;
    }
    var topology = perCore.topology || [];
    var physical = topology.filter(function (id, index, all) {
      return id >= 0 && all.indexOf(id) === index;
    }).length;
    note.textContent = (physical || perCore.per_cpu.length) + ' 核 ' + perCore.per_cpu.length + ' 线程';
    host.innerHTML = perCore.per_cpu.map(function (percent, index) {
      var freq = perCore.freq_mhz && perCore.freq_mhz[index]
        ? perCore.freq_mhz[index] + ' MHz' : '—';
      var core = topology[index];
      var label = core >= 0 ? '核心 ' + core + ' · 线程 ' + index : '线程 ' + index;
      return '<div class="core-row">' +
        '<span class="name">' + label + '</span>' +
        '<span class="meter"><i style="width:' + Math.min(100, percent).toFixed(0) +
          '%;background:' + CORE_COLORS[index % CORE_COLORS.length] + '"></i></span>' +
        '<b class="pct">' + percent.toFixed(0) + '%</b>' +
        '<span class="freq">' + freq + '</span>' +
        '</div>';
    }).join('');
  }

  function render(payload) {
    renderTemps(payload.temps);
    renderFans(payload.fans);
    renderCores((payload.cpu || {}).per_core);
  }

  function applySeries(data) {
    var series = data.series || {};
    if (data.ts) lastSeriesTs = data.ts;
    if (data.interval) {
      var gap = Math.max(6, data.interval * 4);
      Object.keys(charts).forEach(function (name) { charts[name].setGap(gap); });
    }
    Object.keys(charts).forEach(function (name) {
      var chart = charts[name];
      Object.keys(series).forEach(function (key) {
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
  DashPages.sensors = {
    title: '传感器',
    interval: 1000,
    mount: function (host) {
      charts = {};
      lastSeriesTs = 0;
      build(host);
    },
    tick: tick,
    render: render,
    applySeries: applySeries
  };
})();
