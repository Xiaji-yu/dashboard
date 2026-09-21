/* 网络与磁盘页 —— 版式参照 docs/reference/03-network-disk.png：
 *
 *   上方一张大卡：左侧「下载 / 上传」两个大数字，右侧 2 分钟双线曲线 + 图例 + 最近 2 分钟
 *   下方三张卡：连接（本机 IP / 路由器 / 上网方式 / 本地代理 / 外网延迟 / 连接数）/ 网卡 / 磁盘
 *   再往下：磁盘读写曲线（读 + 写）与挂载点明细、当前连接的对端列表
 *
 * 参考图里的「Wi-Fi 信号 / 附近的 Wi-Fi」在本机（USB 有线网卡）不适用，
 * 按实际能力换成网卡信息与磁盘；没有无线网卡就如实显示有线。
 */
'use strict';

(function () {
  var SERIES_PARAM = 'net_down,net_up,disk_read,disk_write';
  var charts = {};
  var lastSeriesTs = 0;

  /* ---------------- 构建 ---------------- */

  function build(host) {
    var top = document.createElement('section');
    top.className = 'perf-top';
    top.innerHTML =
      '<div class="perf-top-left">' +
        '<div class="card-title"><span class="tick" style="background:#3ddc97"></span>网速</div>' +
        '<div class="net-block"><div class="net-label">下载</div>' +
          '<div class="big-value"><span id="v-down">—</span></div></div>' +
        '<div class="net-block"><div class="net-label">上传</div>' +
          '<div class="big-value"><span id="v-up">—</span></div></div>' +
      '</div>' +
      '<div class="perf-top-right">' +
        '<div class="scale-tag" id="net-scale">—</div>' +
        '<div class="big-chart" id="c-net"></div>' +
        '<div class="chart-foot">' +
          '<span class="legend">' +
            '<i style="background:#3ddc97"></i>下载' +
            '<i style="background:#ff6b9d"></i>上传' +
          '</span>' +
          '<span id="net-foot">最近 2 分钟</span>' +
        '</div>' +
      '</div>';

    var cards = document.createElement('section');
    cards.className = 'perf-bottom';
    cards.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>连接</span>' +
        '<span class="head-note" id="conn-note"></span></div>' +
        '<div class="card-body"><div class="kv-rows" id="conn-rows"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>网卡</span>' +
        '<span class="head-note" id="nic-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="big-value"><span id="v-speed">—</span></div>' +
          '<div class="kv-rows" id="nic-rows"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>磁盘</span>' +
        '<span class="head-note" id="disk-note"></span></div>' +
        '<div class="card-body">' +
          '<div class="big-value" id="v-disk-line"></div>' +
          '<div class="kv-rows" id="disk-rows"></div>' +
        '</div>' +
      '</div>';

    var diskCard = document.createElement('section');
    diskCard.className = 'card';
    diskCard.innerHTML =
      '<div class="card-head"><span>磁盘读写</span>' +
      '<span class="head-note" id="diskio-note"></span></div>' +
      '<div class="card-body">' +
        '<div class="disk-chart" id="c-disk"></div>' +
        '<div class="mount-rows" id="mount-rows"></div>' +
      '</div>';

    var connCard = document.createElement('section');
    connCard.className = 'card';
    connCard.innerHTML =
      '<div class="card-head"><span>当前连接</span>' +
      '<span class="head-note" id="remote-note"></span></div>' +
      '<div class="card-body"><div class="kv-rows" id="remote-rows"></div></div>';

    host.appendChild(top);
    host.appendChild(cards);
    host.appendChild(diskCard);
    host.appendChild(connCard);

    charts.net = window.createChart(document.getElementById('c-net'), {
      series: [{ key: 'net_down', color: '#3ddc97', fill: false },
               { key: 'net_up', color: '#ff6b9d', fill: false }],
      autoMinTop: 4096, window: WINDOW_SECONDS
    });
    charts.disk = window.createChart(document.getElementById('c-disk'), {
      series: [{ key: 'disk_read', color: '#4fc3f7', fill: true, width: 1.6 },
               { key: 'disk_write', color: '#ffb74d', fill: true, width: 1.6 }],
      autoMinTop: 65536, window: WINDOW_SECONDS
    });
  }

  /* ---------------- 渲染 ---------------- */

  /* 速率拆成「大数字 + 小单位」，与参考图一致 */
  function setRate(id, bps) {
    var node = document.getElementById(id);
    if (!node) return;
    node.textContent = '';
    if (bps === null || bps === undefined || !isFinite(bps)) {
      node.textContent = '—';
      return;
    }
    var units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    var value = bps;
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    node.appendChild(document.createTextNode(
      value >= 100 ? value.toFixed(0) : value.toFixed(1)));
    var unit = document.createElement('span');
    unit.className = 'unit';
    unit.textContent = ' ' + units[index];
    node.appendChild(unit);
  }

  function kv(label, value) {
    return '<div class="kv-row"><span>' + esc(label) + '</span><b>' +
      esc(value === null || value === undefined || value === '' ? '—' : value) + '</b></div>';
  }

  function msText(value) {
    if (value === null || value === undefined) return '不可用';
    return value.toFixed(1) + ' 毫秒';
  }

  function renderConnection(conn, nic) {
    var note = document.getElementById('conn-note');
    if (!conn) { note.textContent = '不可用'; return; }
    note.textContent = '已建立 ' + (conn.established || 0) + ' · 监听 ' + (conn.listening || 0);
    var rows = [
      kv('本机 IP', conn.local_ip),
      kv('路由器', conn.gateway),
      kv('上网方式', conn.medium),
      kv('本地代理', conn.proxy_port ? '运行中（端口 ' + conn.proxy_port + '）' : '未检测到'),
      kv('外网延迟', msText(conn.internet_ms)),
      kv('探测目标', conn.internet_target),
      kv('网络连接', (conn.total || 0) + ' 个'),
      kv('网关延迟', msText(conn.gateway_ms))
    ];
    document.getElementById('conn-rows').innerHTML = rows.join('');
  }

  function renderNic(nic) {
    var note = document.getElementById('nic-note');
    var speed = document.getElementById('v-speed');
    if (!nic || !nic.available) {
      note.textContent = (nic && nic.reason) || '不可用';
      speed.textContent = '—';
      document.getElementById('nic-rows').innerHTML = '';
      return;
    }
    note.textContent = nic.wireless ? '无线' : '有线（USB 网卡）';
    speed.textContent = '';
    speed.appendChild(document.createTextNode(
      nic.speed_mbps ? String(nic.speed_mbps) : '—'));
    var unit = document.createElement('span');
    unit.className = 'unit';
    unit.textContent = nic.speed_mbps ? ' Mbps' : '';
    speed.appendChild(unit);

    document.getElementById('nic-rows').innerHTML = [
      kv('链路状态', nic.up ? '已连接' : '已断开'),
      kv('双工模式', nic.duplex),
      kv('MTU', nic.mtu),
      kv('MAC', nic.mac),
      kv('IPv4', nic.ipv4 ? nic.ipv4 + (nic.netmask ? '/' + nic.netmask : '') : null),
      kv('IPv6', nic.ipv6),
      kv('累计收发', (nic.recv_total_gb || 0) + ' GB / ' + (nic.sent_total_gb || 0) + ' GB'),
      kv('丢包', (nic.dropin || 0) + ' / ' + (nic.dropout || 0))
    ].join('');
    document.getElementById('net-foot').textContent =
      '最近 2 分钟 · ' + (nic.wireless ? '无线网卡' : '有线网卡') + ' ' + nic.name;
  }

  function renderDisk(disk) {
    var line = document.getElementById('v-disk-line');
    var note = document.getElementById('disk-note');
    if (!disk || !disk.available) {
      note.textContent = (disk && disk.reason) || '不可用';
      line.textContent = '—';
      document.getElementById('disk-rows').innerHTML = '';
      document.getElementById('mount-rows').innerHTML = '';
      return;
    }
    note.textContent = '已用 ' + disk.used_percent + '%';
    line.innerHTML = '<span>' + disk.free_gb.toFixed(1) + '</span>' +
      '<span class="unit">GB 可用，共 ' + disk.total_gb.toFixed(1) + ' GB</span>';

    document.getElementById('disk-rows').innerHTML = [
      kv('设备', disk.device),
      kv('型号', disk.model),
      kv('容量', disk.size_gb ? disk.size_gb + ' GB' : null),
      kv('类型', disk.rotational === null || disk.rotational === undefined
        ? null : (disk.rotational ? '机械硬盘' : '固态硬盘')),
      kv('挂载点', disk.path),
      kv('累计读写', (disk.read_total_gb || 0) + ' GB / ' + (disk.write_total_gb || 0) + ' GB')
    ].join('');

    document.getElementById('diskio-note').textContent =
      '读 ' + fmtRate(disk.read_bps) + ' · 写 ' + fmtRate(disk.write_bps);
    document.getElementById('mount-rows').innerHTML = (disk.mounts || []).map(function (item) {
      return '<div class="kv-row"><span>' + esc(item.mount) + '</span><b>' +
        esc(item.device + ' · ' + item.fstype + ' · 已用 ' + item.used_percent + '% · 可用 ' +
            item.free_gb + ' GB') + '</b></div>';
    }).join('');
  }

  function renderRemotes(conn) {
    var host = document.getElementById('remote-rows');
    var note = document.getElementById('remote-note');
    if (!conn || !conn.available) {
      note.textContent = (conn && conn.reason) || '不可用';
      host.innerHTML = '';
      return;
    }
    note.textContent = conn.process_attribution
      ? '按对端聚合'
      : '按对端聚合 · 非 root 看不到连接所属进程';
    if (!conn.remotes || !conn.remotes.length) {
      host.innerHTML = '<div class="empty">当前没有已建立的连接</div>';
      return;
    }
    host.innerHTML = conn.remotes.map(function (item) {
      return '<div class="kv-row"><span>' + esc(item.addr) + '</span><b>' +
        item.count + ' 条</b></div>';
    }).join('');
  }

  function renderRates(net) {
    net = net || {};
    if (net.available) {
      setRate('v-down', net.down_bps);
      setRate('v-up', net.up_bps);
    } else {
      setRate('v-down', null);
      setRate('v-up', null);
    }
  }

  function render(payload) {
    renderConnection(payload.connection, payload.nic);
    renderNic(payload.nic);
    renderDisk(payload.disk);
    renderRemotes(payload.connection);
  }

  /* ---------------- 轮询 ---------------- */

  function syncGaps(interval) {
    var gap = Math.max(6, interval * 4);
    Object.keys(charts).forEach(function (name) { charts[name].setGap(gap); });
  }

  function applySeries(data) {
    var series = data.series || {};
    if (data.ts) lastSeriesTs = data.ts;
    if (data.interval) syncGaps(data.interval);
    Object.keys(charts).forEach(function (name) {
      var chart = charts[name];
      Object.keys(series).forEach(function (key) {
        if (series[key] && series[key].length) chart.append(key, series[key]);
      });
      var peak = chart.redraw(data.ts);
      if (name === 'net') {
        document.getElementById('net-scale').textContent = peak > 0 ? '峰值 ' + fmtRate(peak) : '';
      }
    });
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();

    var seriesUrl = '/api/series?keys=' + SERIES_PARAM +
      (lastSeriesTs > 0 ? '&since=' + lastSeriesTs : '');

    return Promise.all([
      fetchJSON('/api/network'),
      fetchJSON(seriesUrl)
    ]).then(function (results) {
      var payload = results[0];
      if (payload.ready) {
        setLive('实时更新中', '');
        render(payload);
        renderRates(payload.net);
      }
      applySeries(results[1]);
      state.tick++;
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.network = {
    title: '网络与磁盘',
    interval: 2000,
    mount: function (host) {
      charts = {};
      lastSeriesTs = 0;
      build(host);
    },
    tick: tick,
    render: render,          /* 公开给探针/测试 */
    renderRates: renderRates,
    applySeries: applySeries
  };
})();
