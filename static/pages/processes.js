/* 进程页：全量进程表 + 搜索 + 点列头排序（锁定排序）+ 点行展开。
 *
 * 口径（与概览页一致）：CPU 按逻辑核心数归一化到 0–100%，top/ps 的原始值约为其 4 倍。
 * 锁定排序：刷新只更新数值，不重排行；换列或再点一次列头才重排，新出现的进程追加到末尾并高亮。
 * 详情：命令行、启动时间、状态、线程数、用户、所属容器（容器归属来自 /proc/<pid>/cgroup）。
 */
'use strict';

(function () {
  var REFRESH_MS = 2000;
  var NEW_HIGHLIGHT_MS = 6000;
  var STATUS_TEXT = {
    'running': '运行中',
    'sleeping': '休眠',
    'disk-sleep': '等待磁盘',
    'idle': '空闲',
    'stopped': '已停止',
    'tracing-stop': '被跟踪',
    'zombie': '僵尸',
    'dead': '已退出',
    'waking': '唤醒中',
    'parked': '已停放',
    'locked': '已锁定',
    'waiting': '等待中'
  };
  var COLUMNS = [
    { key: 'name', label: '名称', cls: 'col-name' },
    { key: 'pid', label: 'PID', cls: 'col-pid' },
    { key: 'user', label: '用户', cls: 'col-user' },
    { key: 'cpu', label: 'CPU', cls: 'col-cpu' },
    { key: 'rss_mb', label: '内存', cls: 'col-mem' }
  ];

  var rowsByPid = {};
  var order = [];
  var itemsByPid = {};
  var seenPids = {};
  var sortKey = 'cpu';
  var sortDir = -1;          /* -1 降序，1 升序 */
  var expandedPid = null;
  var filter = '';

  /* ---------------- 小工具 ---------------- */

  function fmtStarted(epochSeconds) {
    if (!epochSeconds) return '—';
    var date = new Date(epochSeconds * 1000);
    var now = new Date();
    var clock = String(date.getHours()).padStart(2, '0') + ':' +
      String(date.getMinutes()).padStart(2, '0');
    if (date.toDateString() === now.toDateString()) return '今天 ' + clock;
    return (date.getMonth() + 1) + '月' + date.getDate() + '日 ' + clock;
  }

  function fmtElapsed(epochSeconds) {
    if (!epochSeconds) return '—';
    var seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (seconds < 60) return Math.round(seconds) + ' 秒';
    if (seconds < 3600) return Math.round(seconds / 60) + ' 分钟';
    if (seconds < 86400) return (seconds / 3600).toFixed(1) + ' 小时';
    return (seconds / 86400).toFixed(1) + ' 天';
  }

  function containerText(row) {
    if (!row.container) return '—';
    return row.container.name || ('容器 ' + row.container.id);
  }

  function statusText(status) {
    return STATUS_TEXT[status] || status || '—';
  }

  function matchesFilter(row) {
    if (!filter) return true;
    return [row.name, String(row.pid), row.user, row.cmd, containerText(row)]
      .some(function (value) {
        return value && String(value).toLowerCase().indexOf(filter) >= 0;
      });
  }

  /* ---------------- 构建 ---------------- */

  function build(host) {
    var card = document.createElement('section');
    card.className = 'card proc-card';
    card.innerHTML =
      '<div class="card-head"><span>进程</span>' +
      '<span class="head-note" id="proc-summary">—</span></div>' +
      '<div class="proc-toolbar">' +
        '<input id="proc-search" class="proc-search" type="search" autocomplete="off" ' +
          'placeholder="搜索名称 / PID / 用户 / 命令行 / 容器">' +
        '<span class="proc-hint" id="proc-hint">点列头排序，点行展开</span>' +
      '</div>' +
      '<div class="proc-table">' +
        '<div class="proc-head" id="proc-head"></div>' +
        '<div class="proc-rows" id="proc-rows"></div>' +
      '</div>';
    host.appendChild(card);

    document.getElementById('proc-head').innerHTML = COLUMNS.map(function (column) {
      return '<span class="' + column.cls + ' sortable" data-sort="' + column.key + '">' +
        column.label + '<i class="sort-mark"></i></span>';
    }).join('');
    var head = document.getElementById('proc-head');
    head.addEventListener('click', function (event) {
      var target = event.target.closest('.sortable');
      if (target) changeSort(target.getAttribute('data-sort'));
    });

    var search = document.getElementById('proc-search');
    search.addEventListener('input', function () {
      filter = this.value.trim().toLowerCase();
      renderRows(true);
    });
  }

  /* ---------------- 排序（锁定：只在换列/再次点击时重排） ---------------- */

  function changeSort(key) {
    if (key === sortKey) {
      sortDir = -sortDir;
    } else {
      sortKey = key;
      sortDir = (key === 'name' || key === 'user') ? 1 : -1;
    }
    reorder();
    renderRows(true);
    updateSortMarks();
  }

  function compare(a, b) {
    var left = a[sortKey];
    var right = b[sortKey];
    if (typeof left === 'string' || typeof right === 'string') {
      return String(left || '').localeCompare(String(right || ''), 'zh') * sortDir;
    }
    return ((left || 0) - (right || 0)) * sortDir;
  }

  function reorder() {
    order = Object.keys(rowsByPid).map(Number).sort(function (pidA, pidB) {
      return compare(rowsByPid[pidA], rowsByPid[pidB]);
    });
  }

  function updateSortMarks() {
    var marks = document.querySelectorAll('#proc-head .sort-mark');
    for (var i = 0; i < marks.length; i++) {
      var key = marks[i].parentElement.getAttribute('data-sort');
      marks[i].textContent = key === sortKey ? (sortDir < 0 ? ' ▼' : ' ▲') : '';
    }
  }

  /* ---------------- 渲染 ---------------- */

  function rowHtml(row) {
    return '<div class="proc-item" data-pid="' + row.pid + '">' +
      '<div class="proc-row">' +
        '<span class="col-name">' + esc(row.name) + '</span>' +
        '<span class="col-pid num">' + row.pid + '</span>' +
        '<span class="col-user">' + esc(row.user || '—') + '</span>' +
        '<span class="col-cpu num">' + row.cpu.toFixed(1) + '%</span>' +
        '<span class="col-mem num">' + esc(fmtMB(row.rss_mb)) + '</span>' +
      '</div>' +
      '<div class="proc-detail" hidden></div>' +
      '</div>';
  }

  function detailHtml(row) {
    return '' +
      '<div class="kv-row"><span>命令行</span><b class="cmd">' +
        esc(row.cmd || '无权限读取') + '</b></div>' +
      '<div class="kv-row"><span>启动时间</span><b>' +
        esc(fmtStarted(row.started)) + '（已运行 ' + esc(fmtElapsed(row.started)) + '）</b></div>' +
      '<div class="kv-row"><span>状态</span><b>' + esc(statusText(row.status)) + '</b></div>' +
      '<div class="kv-row"><span>线程数</span><b>' + row.threads + '</b></div>' +
      '<div class="kv-row"><span>用户</span><b>' + esc(row.user || '—') + '</b></div>' +
      '<div class="kv-row"><span>容器</span><b>' + esc(containerText(row)) + '</b></div>';
  }

  function updateItem(cell, row) {
    cell.querySelector('.col-name').textContent = row.name;
    cell.querySelector('.col-pid').textContent = row.pid;
    cell.querySelector('.col-user').textContent = row.user || '—';
    cell.querySelector('.col-cpu').textContent = row.cpu.toFixed(1) + '%';
    cell.querySelector('.col-mem').textContent = fmtMB(row.rss_mb);
    var detail = cell.querySelector('.proc-detail');
    if (detail && !detail.hidden) detail.innerHTML = detailHtml(row);
  }

  /* rebuild=true 时重建 DOM（换列/搜索/进程增减），否则只更新数值 */
  function renderRows(rebuild) {
    var host = document.getElementById('proc-rows');
    var visible = order.filter(function (pid) { return matchesFilter(rowsByPid[pid]); });

    if (!rebuild) {
      var unchanged = visible.length === host.children.length;
      if (unchanged) {
        for (var i = 0; i < visible.length; i++) {
          if (Number(host.children[i].getAttribute('data-pid')) !== visible[i]) {
            unchanged = false;
            break;
          }
        }
      }
      if (unchanged) {
        visible.forEach(function (pid, index) { updateItem(host.children[index], rowsByPid[pid]); });
        return;
      }
    }

    host.innerHTML = visible.map(function (pid) { return rowHtml(rowsByPid[pid]); }).join('');
    itemsByPid = {};
    for (var j = 0; j < host.children.length; j++) {
      var cell = host.children[j];
      var pid = Number(cell.getAttribute('data-pid'));
      itemsByPid[pid] = cell;
      cell.querySelector('.proc-row').addEventListener('click', function () {
        toggleDetail(Number(this.parentElement.getAttribute('data-pid')));
      });
      if (seenPids[pid] === 'new') cell.classList.add('is-new');
      if (pid === expandedPid) openDetail(cell, rowsByPid[pid]);
    }
    updateSummary(visible.length);
  }

  function openDetail(cell, row) {
    var detail = cell.querySelector('.proc-detail');
    detail.innerHTML = detailHtml(row);
    detail.hidden = false;
    cell.classList.add('open');
  }

  function toggleDetail(pid) {
    var cell = itemsByPid[pid];
    if (!cell) return;
    var detail = cell.querySelector('.proc-detail');
    if (detail.hidden) {
      expandedPid = pid;
      openDetail(cell, rowsByPid[pid]);
    } else {
      detail.hidden = true;
      cell.classList.remove('open');
      if (expandedPid === pid) expandedPid = null;
    }
  }

  function updateSummary(visibleCount) {
    var total = order.length;
    document.getElementById('proc-summary').textContent = filter
      ? '匹配 ' + visibleCount + ' / 共 ' + total + ' 个'
      : '共 ' + total + ' 个 · 每 ' + (REFRESH_MS / 1000) + ' 秒刷新';
  }

  /* ---------------- 数据更新 ---------------- */

  function update(payload) {
    var list = payload.processes || [];
    var previous = Object.keys(rowsByPid).map(Number);
    var fresh = {};
    var appeared = [];
    list.forEach(function (row) {
      fresh[row.pid] = row;
      if (!(row.pid in rowsByPid)) {
        appeared.push(row.pid);
        if (!(row.pid in seenPids)) seenPids[row.pid] = 'new';
      }
    });
    var disappeared = previous.filter(function (pid) { return !(pid in fresh); });
    rowsByPid = fresh;

    var structureChanged = appeared.length > 0 || disappeared.length > 0;
    if (structureChanged) {
      /* 锁定排序：老进程保持原顺序，新进程按服务端顺序（CPU 降序）追加到末尾。
         首次加载时 order 为空，于是整表就是服务端顺序。 */
      order = order.filter(function (pid) { return pid in rowsByPid; });
      var known = {};
      order.forEach(function (pid) { known[pid] = true; });
      list.forEach(function (row) {
        if (!known[row.pid]) {
          known[row.pid] = true;
          order.push(row.pid);
        }
      });
    }
    renderRows(structureChanged);
    updateSummary(order.filter(function (pid) { return matchesFilter(rowsByPid[pid]); }).length);
  }

  function tick() {
    if (window.pollGuard()) return Promise.resolve();
    return fetchJSON('/api/processes').then(function (payload) {
      if (!payload.ready) return null;
      setLive('实时更新中', '');
      update(payload);
      state.tick++;
      /* 新进程高亮 6 秒后取消 */
      setTimeout(function () {
        Object.keys(seenPids).forEach(function (pid) { seenPids[pid] = 'seen'; });
        var marked = document.querySelectorAll('.proc-item.is-new');
        for (var i = 0; i < marked.length; i++) marked[i].classList.remove('is-new');
      }, NEW_HIGHLIGHT_MS);
    }).catch(function () {
      setLive('连接中断，重试中…', 'error');
    });
  }

  window.DashPages = window.DashPages || {};
  DashPages.processes = {
    title: '进程',
    interval: REFRESH_MS,
    mount: function (host) {
      rowsByPid = {};
      order = [];
      itemsByPid = {};
      seenPids = {};
      expandedPid = null;
      filter = '';
      sortKey = 'cpu';
      sortDir = -1;
      build(host);
      updateSortMarks();
    },
    tick: tick,
    /* 公开给探针/测试 */
    render: update,
    changeSort: changeSort,
    state: function () {
      return { order: order.slice(), sortKey: sortKey, sortDir: sortDir,
               expandedPid: expandedPid, total: order.length };
    }
  };
})();
