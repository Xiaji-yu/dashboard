/* 手绘 SVG 迷你折线图：无坐标轴、渐变填充、随容器拉伸铺满。（无第三方依赖） */
(function (global) {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    if (attrs) {
      for (var key in attrs) node.setAttribute(key, attrs[key]);
    }
    return node;
  }

  /* 点列 -> 路径字符串；null 处断开成多段。坐标系固定 0-100 x 0-100。 */
  function buildPaths(points, t0, t1, yMin, yMax) {
    var span = Math.max(0.001, t1 - t0);
    var ySpan = Math.max(0.0001, yMax - yMin);
    var segments = [];
    var current = [];

    for (var i = 0; i < points.length; i++) {
      var ts = points[i][0];
      var value = points[i][1];
      if (value === null || value === undefined || !isFinite(value)) {
        if (current.length) { segments.push(current); current = []; }
        continue;
      }
      var x = ((ts - t0) / span) * 100;
      var y = 100 - ((value - yMin) / ySpan) * 96 - 2;  /* 上下各留 2 的余量给线宽 */
      current.push([
        Math.max(0, Math.min(100, x)),
        Math.max(2, Math.min(98, y))
      ]);
    }
    if (current.length) segments.push(current);

    var line = '', area = '';
    for (var s = 0; s < segments.length; s++) {
      var seg = segments[s];
      if (seg.length === 1) {
        /* 单点：画一小段横线，否则什么都看不见 */
        var px = seg[0][0], py = seg[0][1];
        line += 'M' + (px - 0.4).toFixed(2) + ',' + py.toFixed(2) +
                'L' + (px + 0.4).toFixed(2) + ',' + py.toFixed(2);
        continue;
      }
      var d = 'M' + seg[0][0].toFixed(2) + ',' + seg[0][1].toFixed(2);
      for (var k = 1; k < seg.length; k++) {
        d += 'L' + seg[k][0].toFixed(2) + ',' + seg[k][1].toFixed(2);
      }
      line += d;
      area += d + 'L' + seg[seg.length - 1][0].toFixed(2) + ',100L' +
              seg[0][0].toFixed(2) + ',100Z';
    }
    return [line, area];
  }

  /**
   * 在 host 元素内创建一张曲线图。
   * options:
   *   series: [{key, color, fill, width}]  多条曲线叠加
   *   fixed:  [min, max]                   固定量程；省略则按窗口内峰值自适应
   *   autoMinTop: 自适量程的最小上限（避免小数值把噪声放大）
   *   window: 时间窗口秒数
   */
  function createChart(host, options) {
    var opts = Object.assign(
      { series: [], fixed: null, autoMinTop: 1, window: 120 }, options || {}
    );
    var uid = 'spark' + Math.random().toString(36).slice(2, 9);
    var svg = el('svg', { viewBox: '0 0 100 100', preserveAspectRatio: 'none' });
    var defs = el('defs');
    svg.appendChild(defs);

    var tracks = {};
    var order = [];

    opts.series.forEach(function (spec, index) {
      var hasFill = spec.fill !== false;
      if (hasFill) {
        var grad = el('linearGradient', {
          id: uid + '-' + index, x1: '0', y1: '0', x2: '0', y2: '1'
        });
        grad.appendChild(el('stop', { offset: '0%', 'stop-color': spec.color, 'stop-opacity': '0.22' }));
        grad.appendChild(el('stop', { offset: '100%', 'stop-color': spec.color, 'stop-opacity': '0' }));
        defs.appendChild(grad);
      }
      var area = el('path', {
        d: '', stroke: 'none', fill: hasFill ? 'url(#' + uid + '-' + index + ')' : 'none'
      });
      var line = el('path', {
        d: '', fill: 'none', stroke: spec.color,
        'stroke-width': spec.width || 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        'vector-effect': 'non-scaling-stroke'
      });
      svg.appendChild(area);
      svg.appendChild(line);
      tracks[spec.key] = { points: [], line: line, area: area, filled: hasFill };
      order.push(spec.key);
    });

    host.appendChild(svg);

    var fixed = opts.fixed || null;
    var lastTs = 0;
    var na = null;

    function prune(nowTs) {
      var cutoff = nowTs - opts.window;
      order.forEach(function (key) {
        var pts = tracks[key].points;
        while (pts.length && pts[0][0] < cutoff) pts.shift();
      });
    }

    function peak() {
      var max = 0;
      order.forEach(function (key) {
        tracks[key].points.forEach(function (p) {
          if (p[1] !== null && p[1] > max) max = p[1];
        });
      });
      return max;
    }

    return {
      /** 追加增量点；pts 为 [[ts, value], ...] */
      append: function (key, pts) {
        var track = tracks[key];
        if (!track || !pts || !pts.length) return;
        for (var i = 0; i < pts.length; i++) track.points.push(pts[i]);
      },

      setFixed: function (range) { fixed = range || null; },

      setWindow: function (seconds) { opts.window = seconds; },

      /** 隐藏曲线并显示「不可用」提示 */
      setUnavailable: function (reason) {
        if (na) na.remove();
        svg.style.display = 'none';
        na = document.createElement('div');
        na.className = 'chart-na';
        na.textContent = reason || '不可用';
        host.appendChild(na);
      },

      /** 重绘，返回最近一次绘制的峰值（供量程标注使用） */
      redraw: function (serverTs) {
        if (serverTs) lastTs = serverTs;
        var nowTs = lastTs || (Date.now() / 1000);
        prune(nowTs);

        var yMin, yMax;
        if (fixed) {
          yMin = fixed[0];
          yMax = fixed[1];
        } else {
          yMin = 0;
          yMax = Math.max(peak() * 1.15, opts.autoMinTop || 1);
        }

        var t0 = nowTs - opts.window;
        order.forEach(function (key) {
          var track = tracks[key];
          var paths = buildPaths(track.points, t0, nowTs, yMin, yMax);
          track.line.setAttribute('d', paths[0]);
          if (track.filled) track.area.setAttribute('d', paths[1]);
        });
        return peak();
      }
    };
  }

  global.createChart = createChart;
})(window);
