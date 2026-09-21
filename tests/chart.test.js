/* chart.js 回归测试：node tests/chart.test.js
 *
 * 守住两类会让曲线「画错」的点（历史上真出过 bug）：
 *   1. 重复 / 回退的时间戳 —— 并发轮询把同一批点追加两次，曲线会倒退画出一条横穿画面的直线；
 *   2. 采样中断造成的时间断层 —— 服务重启、机器休眠后，SVG 不能把断开的两段连成直线。
 */

'use strict';

/* chart.js 是给浏览器写的，这里用最小 DOM 桩把它跑起来 */
function makeElement(name) {
  return {
    tagName: name,
    attrs: {},
    children: [],
    style: {},
    setAttribute: function (key, value) { this.attrs[key] = String(value); },
    getAttribute: function (key) { return this.attrs[key]; },
    appendChild: function (child) { this.children.push(child); return child; },
    remove: function () {}
  };
}

global.window = global;
global.document = {
  createElementNS: function (_ns, name) { return makeElement(name); },
  createElement: function (name) { return makeElement(name); }
};

require('../static/chart.js');

/* createChart 把 svg 挂在 host 下：svg.children = [defs, area1, line1, area2, line2, ...] */
function mount(options) {
  var host = makeElement('div');
  var chart = global.createChart(host, options || {});
  return { host: host, chart: chart, svg: host.children[0] };
}

function lineD(svg, seriesIndex) {
  return svg.children[1 + seriesIndex * 2 + 1].getAttribute('d') || '';
}

/* 路径里 x 倒退的次数：>0 就说明画出了往回走的线 */
function backwardJumps(d) {
  var nums = d.match(/-?\d+(\.\d+)?/g) || [];
  var xs = [];
  for (var i = 0; i + 1 < nums.length; i += 2) xs.push(parseFloat(nums[i]));
  var jumps = 0;
  for (var k = 1; k < xs.length; k++) {
    if (xs[k] < xs[k - 1] - 0.01) jumps++;
  }
  return jumps;
}

function subpaths(d) {
  return (d.match(/M/g) || []).length;
}

var checks = 0;
var failures = 0;

function test(name, fn) {
  checks++;
  try {
    fn();
    console.log('  ✓ ' + name);
  } catch (err) {
    failures++;
    console.log('  ✗ ' + name + '\n      ' + err.message);
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message || '断言失败');
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error((label || '') + '期望 ' + expected + '，实际 ' + actual);
  }
}

console.log('chart.js 曲线绘制：');

test('同一批点追加两次：第二次被丢弃，路径不变且无倒退', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }] });
  var batch = [[100, 10], [101, 20], [102, 30]];
  m.chart.append('a', batch);
  m.chart.redraw(102);
  var before = lineD(m.svg, 0);
  m.chart.append('a', batch);  /* 模拟并发 tick 用旧 since 拉到同一批点 */
  m.chart.redraw(102);
  assertEqual(lineD(m.svg, 0), before, '重复批次不应改变路径：');
  assertEqual(backwardJumps(lineD(m.svg, 0)), 0, 'x 倒退次数：');
});

test('比已有点更旧的点被丢弃', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }] });
  m.chart.append('a', [[100, 10], [105, 50]]);
  m.chart.append('a', [[103, 30], [106, 60]]);  /* 103 是回退点，106 应当保留 */
  var peak = m.chart.redraw(106);
  assertEqual(backwardJumps(lineD(m.svg, 0)), 0, 'x 倒退次数：');
  assertEqual(peak, 60, '窗口内峰值：');
});

test('连续采样连成一条曲线', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }] });
  m.chart.append('a', [[100, 10], [101, 20], [102, 30], [103, 40]]);
  m.chart.redraw(103);
  assertEqual(subpaths(lineD(m.svg, 0)), 1, '子路径数：');
});

test('时间断层处断线，不再横穿整张图', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }], gap: 5 });
  m.chart.append('a', [[100, 10], [101, 20], [160, 30], [161, 40]]);  /* 中间缺了 59 秒 */
  m.chart.redraw(161);
  assertEqual(subpaths(lineD(m.svg, 0)), 2, '子路径数：');
  assertEqual(backwardJumps(lineD(m.svg, 0)), 0, 'x 倒退次数：');
});

test('setGap 可调：阈值放大后同一批数据改为相连', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }], gap: 5 });
  m.chart.setGap(120);
  m.chart.append('a', [[100, 10], [160, 30]]);
  m.chart.redraw(160);
  assertEqual(subpaths(lineD(m.svg, 0)), 1, '子路径数：');
});

test('null 值处断线（指标采不到时不连直线）', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }] });
  m.chart.append('a', [[100, 10], [101, null], [102, 30]]);
  m.chart.redraw(102);
  assertEqual(subpaths(lineD(m.svg, 0)), 2, '子路径数：');
});

test('窗口外的旧点被裁掉', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff' }], window: 10 });
  m.chart.append('a', [[100, 99], [120, 20], [121, 30]]);
  var peak = m.chart.redraw(121);  /* 121-10=111，100 那个点应当被裁掉 */
  assertEqual(peak, 30, '裁掉旧点后的峰值：');
});

test('填充面积与线条路径一致（单条序列两条 path）', function () {
  var m = mount({ series: [{ key: 'a', color: '#fff', fill: true }] });
  m.chart.append('a', [[100, 10], [101, 20]]);
  m.chart.redraw(101);
  var area = m.svg.children[1].getAttribute('d') || '';
  assert(area.indexOf('Z') > 0, '填充路径应当闭合');
  assert(subpaths(area) === 1, '填充路径子路径数应为 1');
});

console.log('');
if (failures) {
  console.log(failures + ' / ' + checks + ' 项未通过 ❌');
  process.exit(1);
}
console.log(checks + ' 项全部通过 ✅');
