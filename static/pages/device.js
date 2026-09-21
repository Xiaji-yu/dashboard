/* 设备页：主机 / 处理器 / 内存与磁盘 / 网络与运行环境。
 *
 * 全是静态信息，后端缓存 60 秒；参考图没有这一页，沿用现有卡片视觉语言。
 */
'use strict';

(function () {
  var host;

  function build(target) {
    host = target;
    var grid = document.createElement('section');
    grid.className = 'dev-grid';
    grid.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>主机</span><span class="head-note" id="dev-host-note"></span></div>' +
        '<div class="card-body"><div class="kv-rows" id="dev-host"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>处理器</span><span class="head-note" id="dev-cpu-note"></span></div>' +
        '<div class="card-body"><div class="kv-rows" id="dev-cpu"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>内存与磁盘</span><span class="head-note" id="dev-disk-note"></span></div>' +
        '<div class="card-body"><div class="kv-rows" id="dev-disk"></div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>网络与运行环境</span>' +
        '<span class="head-note" id="dev-net-note"></span></div>' +
        '<div class="card-body"><div class="kv-rows" id="dev-net"></div></div>' +
      '</div>';
    host.appendChild(grid);
  }

  function kv(label, value) {
    return '<div class="kv-row"><span>' + esc(label) + '</span><b>' +
      esc(value === null || value === undefined || value === '' ? '—' : value) + '</b></div>';
  }

  function uptimeText(seconds) {
    if (!isFinite(seconds)) return '—';
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    if (days > 0) return days + ' 天 ' + hours + ' 小时';
    return hours + ' 小时 ' + mins + ' 分钟';
  }

  function render(payload) {
    var h = payload.host || {};
    var c = payload.cpu || {};
    var m = payload.memory || {};
    var d = payload.disk || {};
    var n = payload.network || {};
    var r = payload.runtime || {};
    var battery = payload.battery;

    document.getElementById('dev-host-note').textContent = h.os || '';
    document.getElementById('dev-host').innerHTML = [
      kv('主机名', h.hostname),
      kv('操作系统', h.os),
      kv('内核', h.kernel),
      kv('架构', h.arch),
      kv('已运行', uptimeText(h.uptime_s)),
      kv('厂商与型号', [h.vendor, h.product].filter(Boolean).join(' ')),
      kv('主板', h.board),
      kv('BIOS', h.bios)
    ].join('');

    document.getElementById('dev-cpu-note').textContent = c.model || '';
    var freq = (c.min_mhz && c.max_mhz) ? c.min_mhz + ' – ' + c.max_mhz + ' MHz' : null;
    document.getElementById('dev-cpu').innerHTML = [
      kv('核心与线程', c.cores ? c.cores + ' 核 ' + c.threads + ' 线程' : null),
      kv('频率范围', freq),
      kv('缓存', c.cache_text),
      kv('虚拟化', c.virtualization),
      kv('核显', c.gpu ? ('Intel 核显 · 当前 ' + c.gpu.freq_mhz + ' / 最大 ' +
        (c.gpu.max_mhz || '—') + ' MHz') : null)
    ].join('');

    document.getElementById('dev-disk-note').textContent = d.block || '';
    document.getElementById('dev-disk').innerHTML = [
      kv('内存', m.total_gb ? m.total_gb + ' GB' : null),
      kv('交换区', m.swap_gb ? m.swap_gb + ' GB' : null),
      kv('磁盘', [d.model, d.size_gb ? d.size_gb + ' GB' : null,
                  d.rotational === null || d.rotational === undefined
                    ? null : (d.rotational ? '机械硬盘' : '固态硬盘')]
                 .filter(Boolean).join(' · ')),
      kv('分区', (d.mounts || []).map(function (item) {
        return item.device + ' ' + item.fstype + ' → ' + item.mount +
          '（' + item.total_gb + ' GB，已用 ' + item.used_percent + '%）';
      }).join('；'))
    ].join('');

    document.getElementById('dev-net-note').textContent = n.wireless ? '无线网卡' : '有线网卡';
    document.getElementById('dev-net').innerHTML = [
      kv('网卡', n.name),
      kv('链路', n.speed_mbps ? (n.speed_mbps + ' Mbps ' + (n.duplex || '')) : null),
      kv('MAC', n.mac),
      kv('IPv4', n.ipv4 ? n.ipv4 + (n.netmask ? '/' + n.netmask : '') : null),
      kv('Python / psutil', [r.python, r.psutil].filter(Boolean).join(' / ')),
      kv('Docker', r.docker),
      kv('电池', battery ? (battery.percent.toFixed(1) + '% · ' +
        ({ 'Charging': '充电中', 'Discharging': '放电中',
           'Not charging': '接通电源·未充电', 'Full': '已充满' }[battery.status] || '') +
        (battery.cycles ? ' · 循环 ' + battery.cycles + ' 次' : '')) : null)
    ].join('');
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
    mount: function (target) { build(target); },
    tick: tick,
    render: render
  };
})();
