/* 设备页：与这台机器连接的设备 —— USB、蓝牙、网络接口（含有线/无线）、PCI。
 *
 * 顶部一行是本机摘要（型号、CPU、内存、磁盘、电池等）。
 * 蓝牙与无线在没插硬件时如实显示「没有适配器」；以后插上无线网卡，
 * 「网络接口」会多出带 SSID 与信号强度的条目（数据来自 /proc/net/wireless 与 iwconfig）。
 */
'use strict';

(function () {
  function build(host) {
    var summary = document.createElement('section');
    summary.className = 'card dev-summary';
    summary.innerHTML =
      '<div class="card-head"><span>本机</span>' +
      '<span class="head-note" id="dev-summary-note"></span></div>' +
      '<div class="card-body"><div class="dev-chips" id="dev-summary"></div></div>';

    var grid = document.createElement('section');
    grid.className = 'dev-grid';
    grid.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>USB 设备</span>' +
        '<span class="head-note" id="dev-usb-note">—</span></div>' +
        '<div class="card-body"><div class="list-rows" id="dev-usb"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>蓝牙</span>' +
        '<span class="head-note" id="dev-bt-note">—</span></div>' +
        '<div class="card-body"><div class="list-rows" id="dev-bt"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>网络接口</span>' +
        '<span class="head-note" id="dev-if-note">—</span></div>' +
        '<div class="card-body"><div class="list-rows" id="dev-if"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>局域网设备</span>' +
        '<span class="head-note" id="dev-lan-note">—</span></div>' +
        '<div class="card-body">' +
          '<div class="svc-tip">免凭据发现：邻居表（ARP）+ ICMP 探测 + SSDP 组播查询，' +
            '不需要其他设备的账号密码。想看路由器自己的完整客户端列表，才需要路由器管理密码。</div>' +
          '<div class="list-rows" id="dev-lan"></div>' +
        '</div>' +
      '</div>';

    host.appendChild(summary);
    host.appendChild(grid);
  }

  function chip(label, value) {
    if (!value) return '';
    return '<span class="dev-chip"><i>' + esc(label) + '</i>' + esc(value) + '</span>';
  }

  function uptimeText(seconds) {
    if (!isFinite(seconds)) return '—';
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    return days > 0 ? days + ' 天 ' + hours + ' 小时' : hours + ' 小时';
  }

  function renderSummary(s) {
    document.getElementById('dev-summary-note').textContent = s.os || '';
    var disk = s.disk || {};
    var mounts = (s.mounts || []).map(function (item) {
      return item.device + ' ' + item.fstype + ' → ' + item.mount +
        '（已用 ' + item.used_percent + '%）';
    }).join('；');
    document.getElementById('dev-summary').innerHTML = [
      chip('主机名', s.hostname),
      chip('系统', s.os),
      chip('内核', s.kernel + ' · ' + s.arch),
      chip('机型', [s.vendor, s.product].filter(Boolean).join(' ')),
      chip('BIOS', s.bios),
      chip('CPU', s.cpu),
      chip('核心', s.cores + ' 核 ' + s.threads + ' 线程' + (s.cache_text ? ' · ' + s.cache_text : '')),
      chip('虚拟化', s.virtualization),
      chip('内存', s.memory_gb ? s.memory_gb + ' GB（交换 ' + s.swap_gb + ' GB）' : null),
      chip('核显', s.gpu ? '最大 ' + s.gpu + ' MHz' : null),
      chip('磁盘', [disk.model, disk.size_gb ? disk.size_gb + ' GB' : null,
                    disk.rotational === null || disk.rotational === undefined
                      ? null : (disk.rotational ? '机械硬盘' : '固态硬盘')]
                     .filter(Boolean).join(' · ')),
      chip('分区', mounts),
      chip('已运行', uptimeText(s.uptime_s))
    ].join('');
  }

  function renderUsb(usb) {
    var host = document.getElementById('dev-usb');
    var note = document.getElementById('dev-usb-note');
    var list = (usb && usb.list) || [];
    if (!list.length) {
      note.textContent = '未检测到';
      host.innerHTML = '<div class="empty">没有读到 USB 设备</div>';
      return;
    }
    note.textContent = usb.total + ' 个 · 外接 ' + usb.external;
    host.innerHTML = list.map(function (item) {
      return '<div class="usb-row' + (item.hub ? ' hub' : '') + '">' +
        '<span class="usb-badge">' + (item.hub ? '控制器' : '外接') + '</span>' +
        '<span class="usb-name">' + esc(item.product || '未知设备') + '</span>' +
        '<span class="usb-vendor">' + esc(item.vendor || '') + '</span>' +
        '<span class="usb-id">' + esc(item.id) + '</span>' +
        '<span class="usb-port">bus ' + (item.bus || '?') + ' · 端口 ' + (item.device || '?') + '</span>' +
        '</div>';
    }).join('');
  }

  function renderBluetooth(bt) {
    var host = document.getElementById('dev-bt');
    var note = document.getElementById('dev-bt-note');
    if (!bt || !bt.available) {
      note.textContent = '无适配器';
      host.innerHTML = '<div class="empty">' + esc((bt && bt.reason) || '没有蓝牙适配器') +
        '<br><span class="dim">插上蓝牙适配器后会自动出现在这里</span></div>';
      return;
    }
    note.textContent = bt.adapters.length + ' 个适配器 · ' + bt.devices.length + ' 个设备';
    var rows = bt.adapters.map(function (name) {
      return '<div class="bt-row">' +
        '<span class="usb-badge">适配器</span>' +
        '<span class="bt-name">' + esc(name) + '</span>' +
        '<span class="usb-vendor">' + (bt.devices.length ? '' : '暂无已配对设备') + '</span>' +
        '</div>';
    });
    rows = rows.concat(bt.devices.map(function (item) {
      return '<div class="bt-row">' +
        '<span class="usb-badge">设备</span>' +
        '<span class="bt-name">' + esc(item.name || item.mac) + '</span>' +
        '<span class="usb-vendor">' + esc(item.mac) + '</span>' +
        '</div>';
    }));
    host.innerHTML = rows.join('') || '<div class="empty">没有蓝牙设备</div>';
  }

  function renderInterfaces(interfaces) {
    var host = document.getElementById('dev-if');
    var note = document.getElementById('dev-if-note');
    var physical = (interfaces && interfaces.physical) || [];
    var virtual = (interfaces && interfaces.virtual) || [];
    if (!physical.length && !virtual.length) {
      note.textContent = '未检测到';
      host.innerHTML = '<div class="empty">没有网络接口</div>';
      return;
    }
    note.textContent = physical.length + ' 个物理/无线 · ' + virtual.length + ' 个虚拟';
    var rows = physical.map(function (item) {
      var detail = [];
      if (item.speed_mbps) detail.push(item.speed_mbps + ' Mbps');
      if (item.mtu) detail.push('MTU ' + item.mtu);
      if (item.ipv4) detail.push(item.ipv4);
      if (item.mac) detail.push(item.mac);
      var wireless = item.wireless;
      if (wireless) {
        if (wireless.ssid) detail.push('SSID ' + wireless.ssid);
        if (wireless.signal_dbm !== null && wireless.signal_dbm !== undefined) {
          detail.push('信号 ' + wireless.signal_dbm + ' dBm');
        }
      }
      return '<div class="iface-row">' +
        '<span class="iface-kind ' + (item.kind === '无线' ? 'wifi' : '') + '">' +
          esc(item.kind) + '</span>' +
        '<span class="iface-name">' + esc(item.name) + '</span>' +
        '<span class="iface-state ' + (item.up ? 'up' : 'down') + '">' +
          (item.up ? '已连接' : '未连接') + '</span>' +
        '<span class="iface-detail">' + esc(detail.join(' · ')) + '</span>' +
        '</div>';
    });
    if (virtual.length) {
      rows.push('<div class="iface-row virtual">' +
        '<span class="iface-kind">虚拟</span>' +
        '<span class="iface-name">' + virtual.length + ' 个接口</span>' +
        '<span class="iface-state">—</span>' +
        '<span class="iface-detail">' + esc(virtual.slice(0, 4).map(function (item) {
          return item.name;
        }).join(' · ') + (virtual.length > 4 ? ' …' : '')) + '</span>' +
        '</div>');
    }
    host.innerHTML = rows.join('');
  }

  function renderLan(lan) {
    var host = document.getElementById('dev-lan');
    var note = document.getElementById('dev-lan-note');
    if (!lan || !lan.hosts || !lan.hosts.length) {
      note.textContent = lan && lan.subnet ? lan.subnet : '—';
      host.innerHTML = '<div class="empty">' +
        esc((lan && lan.note) || '这个网段暂时没发现其他设备') + '</div>';
      return;
    }
    note.textContent = lan.hosts.length + ' 台 · ' + (lan.subnet || '') +
      (lan.scanned_at ? ' · 每 ' + Math.round(120) + ' 秒重扫' : '');
    host.innerHTML = lan.hosts.map(function (item) {
      var alive = item.alive === true;
      var tags = (item.sources || []).map(function (source) {
        return '<span class="lan-tag ' + esc(source) + '">' +
          ({ icmp: 'ICMP', arp: 'ARP', ssdp: 'SSDP' }[source] || esc(source)) + '</span>';
      }).join('');
      var name = item.name || item.vendor || '';
      return '<div class="lan-row">' +
        '<span class="lan-state ' + (alive ? 'up' : 'unknown') + '">' +
          (alive ? '在线' : '邻居') + '</span>' +
        '<span class="lan-ip">' + esc(item.ip) + '</span>' +
        '<span class="lan-name">' + esc(name) + '</span>' +
        '<span class="lan-mac">' + esc(item.mac || '—') + '</span>' +
        '<span class="lan-tags">' + tags + '</span>' +
        '</div>';
    }).join('') + (lan.note ? '<div class="svc-tip">' + esc(lan.note) + '</div>' : '');
  }

  function render(payload) {
    renderSummary(payload.summary || {});
    renderUsb(payload.usb || {});
    renderBluetooth(payload.bluetooth || {});
    renderInterfaces(payload.interfaces || {});
    renderLan(payload.lan || {});
    var battery = payload.battery;
    if (battery) {
      document.getElementById('dev-summary').insertAdjacentHTML('beforeend',
        chip('电池', battery.percent.toFixed(1) + '%' +
          (battery.cycles ? ' · 循环 ' + battery.cycles + ' 次' : '')));
    }
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();
    return fetchJSON('/api/device').then(function (payload) {
      if (!payload.ready) return null;
      setLive('实时更新中', '');
      render(payload);
      state.tick++;
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.device = {
    title: '设备',
    interval: 30000,
    mount: function (host) { build(host); },
    tick: tick,
    render: render
  };
})();
