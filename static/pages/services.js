/* 服务页 —— 版式参照 docs/reference/04-services.png：
 *
 *   上排两张卡：远程服务器（可配置探测目标的延迟）/ 容器（名称、状态、镜像）
 *   下排两张卡：本机正在监听的端口（端口 / 进程或常见用途 / 协议 / 谁能访问）+ 系统服务
 *
 * 探测目标在 probes.json 里配置（改完不用重启，mtime 变了自动重载）；
 * 端口归属需要读 /proc/<pid>/fd，非 root 只能拿到自己进程的映射，拿不到就显示常见用途或「—」。
 */
'use strict';

(function () {
  var PROBES_HINT = '在项目目录的 probes.json 里添加目标，改完不用重启';

  var lastProbes = [];

  function build(host) {
    var top = document.createElement('section');
    top.className = 'svc-top';
    top.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>远程服务器</span>' +
        '<span class="head-note" id="probe-note">—</span></div>' +
        '<div class="card-body">' +
          '<div class="probe-rows" id="probe-rows"></div>' +
          '<div class="svc-tip" id="probe-tip"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>容器</span>' +
        '<span class="head-note" id="container-note">—</span></div>' +
        '<div class="card-body"><div class="container-rows" id="container-rows"></div></div>' +
      '</div>';

    var bottom = document.createElement('section');
    bottom.className = 'svc-bottom';
    bottom.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>本机正在监听的端口</span>' +
        '<span class="head-note" id="port-note">—</span></div>' +
        '<div class="card-body">' +
          '<div class="svc-tip">其他程序可以通过这些端口连进来。「仅本机」只有这台电脑能访问，' +
            '「局域网」同网段的设备也能访问。</div>' +
          '<div class="port-head">' +
            '<span>端口</span><span>进程 / 常见用途</span><span>协议</span>' +
            '<span class="num">谁能访问</span>' +
          '</div>' +
          '<div class="port-rows" id="port-rows"></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>系统服务</span>' +
        '<span class="head-note" id="systemd-note">—</span></div>' +
        '<div class="card-body"><div class="systemd-rows" id="systemd-rows"></div></div>' +
      '</div>';

    host.appendChild(top);
    host.appendChild(bottom);
  }

  /* ---------------- 渲染 ---------------- */

  function msText(value) {
    if (value === null || value === undefined) return '不可用';
    return value >= 100 ? value.toFixed(0) + ' 毫秒' : value.toFixed(1) + ' 毫秒';
  }

  function renderProbes(probes) {
    var rows = document.getElementById('probe-rows');
    var tip = document.getElementById('probe-tip');
    var note = document.getElementById('probe-note');
    var list = (probes && probes.list) || [];
    lastProbes = list;

    if (probes && probes.error) {
      note.textContent = '配置有问题';
      rows.innerHTML = '<div class="empty">' + esc(probes.error) + '</div>';
      tip.textContent = PROBES_HINT;
      return;
    }
    if (!list.length) {
      note.textContent = '未配置';
      rows.innerHTML = '<div class="empty">还没有探测目标</div>';
      tip.textContent = PROBES_HINT;
      return;
    }

    var reachable = list.filter(function (item) { return item.ms !== null && item.ms !== undefined; });
    var worst = reachable.reduce(function (max, item) { return Math.max(max, item.ms); }, 0) || 1;
    note.textContent = '每 ' + (probes.interval || 30) + ' 秒一次 · ' +
      reachable.length + '/' + list.length + ' 可达';
    rows.innerHTML = list.map(function (item) {
      var ok = item.ms !== null && item.ms !== undefined;
      var width = ok ? Math.max(4, Math.round(item.ms / worst * 100)) : 0;
      return '<div class="probe-row">' +
        '<span class="dot-s ' + (ok ? 'ok' : 'down') + '"></span>' +
        '<span class="probe-name">' + esc(item.name) + '</span>' +
        '<span class="probe-bar"><i class="' + (ok ? '' : 'down') + '" style="width:' +
          width + '%"></i></span>' +
        '<b class="probe-ms">' + esc(msText(item.ms)) + '</b>' +
        '</div>';
    }).join('');
    tip.textContent = '每 ' + (probes.interval || 30) + ' 秒对目标做一次 TCP 连接，测的是握手耗时；' +
      '走代理时会包含代理的耗时。目标在 probes.json 里配置。';
  }

  function renderContainers(containers) {
    var host = document.getElementById('container-rows');
    var note = document.getElementById('container-note');
    if (!containers || containers.note) {
      note.textContent = '不可用';
      host.innerHTML = '<div class="empty">' + esc((containers && containers.note) || '没有 docker') + '</div>';
      return;
    }
    var list = containers.list || [];
    note.textContent = containers.running + '/' + containers.total + ' 运行中';
    if (!list.length) {
      host.innerHTML = '<div class="empty">没有容器</div>';
      return;
    }
    host.innerHTML = list.map(function (item) {
      return '<div class="container-row">' +
        '<span class="state-chip ' + (item.up ? 'up' : 'down') + '">' +
          (item.up ? '运行中' : '已停止') + '</span>' +
        '<span class="container-name">' + esc(item.name) + '</span>' +
        '<span class="container-status">' + esc(item.status || '') + '</span>' +
        '<span class="container-image">' + esc(item.image || '') + '</span>' +
        '</div>';
    }).join('');
  }

  function renderPorts(ports) {
    var host = document.getElementById('port-rows');
    var note = document.getElementById('port-note');
    if (!ports || !ports.length) {
      note.textContent = '—';
      host.innerHTML = '<div class="empty">没有监听的端口</div>';
      return;
    }
    var exposed = ports.filter(function (item) { return item.scope === '局域网'; }).length;
    note.textContent = ports.length + ' 个 · 局域网可达 ' + exposed;
    host.innerHTML = ports.map(function (item) {
      var owner = item.process
        ? '<span class="port-proc">' + esc(item.process) + '</span>'
        : (item.known ? '<span class="port-known">' + esc(item.known) + '</span>'
                      : '<span class="port-known">—</span>');
      return '<div class="port-row">' +
        '<span class="port-num">' + item.port + '</span>' +
        '<span class="port-owner">' + owner + '</span>' +
        '<span class="port-proto">' + esc(item.proto) + '</span>' +
        '<span class="port-scope ' + (item.scope === '局域网' ? 'wide' : 'local') + '">' +
          esc(item.scope) + '</span>' +
        '</div>';
    }).join('');
  }

  function renderSystemd(systemd) {
    var host = document.getElementById('systemd-rows');
    var note = document.getElementById('systemd-note');
    if (!systemd || !systemd.available) {
      note.textContent = '不可用';
      host.innerHTML = '<div class="empty">' +
        esc((systemd && systemd.reason) || '无法读取 systemd') + '</div>';
      return;
    }
    note.textContent = systemd.total + ' 个运行中';
    host.innerHTML = (systemd.list || []).map(function (item) {
      return '<div class="systemd-row">' +
        '<span class="unit">' + esc(item.unit) + '</span>' +
        '<span class="desc">' + esc(item.description || '') + '</span>' +
        '</div>';
    }).join('');
  }

  function render(payload) {
    renderProbes(payload.probes);
    renderContainers(payload.containers);
    renderPorts(payload.ports);
    renderSystemd(payload.systemd);
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();
    return fetchJSON('/api/services').then(function (payload) {
      if (!payload.ready) return null;
      setLive('实时更新中', '');
      render(payload);
      state.tick++;
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.services = {
    title: '服务',
    interval: 5000,
    mount: function (host) {
      lastProbes = [];
      build(host);
    },
    tick: tick,
    render: render,          /* 公开给探针/测试 */
    probes: function () { return lastProbes; }
  };
})();
