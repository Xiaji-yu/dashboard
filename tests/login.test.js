/* login.js 回归测试：node tests/login.test.js
 *
 * 守住登录页最容易出错的三类行为：
 *   1. 登录请求「结果」与「真实状态」不一致——响应被浏览器拦截/中断时，
 *      服务端其实已经登录成功，必须复查 /api/auth 并跳转，而不是报「无法连接服务」；
 *   2. 凭据错误时要展示服务端 message、清空密码框、允许重试；
 *   3. 显示/隐藏密码与大写锁定提示。
 *
 * login.js 是给浏览器写的，这里用最小 DOM 桩把它跑起来（同 chart.test.js 的做法）。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const LOGIN_SRC = fs.readFileSync(path.join(__dirname, '../static/login.js'), 'utf8');

let pass = 0;
let failCount = 0;
const fails = [];
function assert(cond, msg) {
  if (cond) { pass += 1; console.log('ok   -', msg); }
  else { failCount += 1; fails.push(msg); console.log('FAIL -', msg); }
}

let els = {};

function makeEl(id) {
  return {
    id,
    textContent: '',
    value: '',
    type: 'text',
    hidden: false,
    disabled: false,
    classes: new Set(),
    handlers: {},
    classList: {
      add(c) { els[id].classes.add(c); },
      remove(c) { els[id].classes.delete(c); },
      toggle(c, on) { on ? els[id].classes.add(c) : els[id].classes.delete(c); },
    },
    addEventListener(type, fn) { els[id].handlers[type] = fn; },
    focus() {},
    offsetWidth: 0,
  };
}

/* 每个用例独立一套 DOM / fetch 桩。authQueue：GET /api/auth 依次返回的应答（函数或对象） */
function setup(authQueue, loginReply) {
  els = {};
  const el = (id) => (els[id] || (els[id] = makeEl(id)));
  el('login-pass').type = 'password';   /* 与 login.html 一致：默认密文 */
  const calls = [];
  const replaced = [];
  global.window = { DASH_BASE: '', location: { replace: (u) => replaced.push(u) } };
  global.document = { getElementById: el };
  /* 回答可以是：Response 形状（带 json 方法）、Promise、或直接当响应体 */
  const wrap = (reply) => {
    if (typeof reply === 'object' && reply !== null) {
      if (typeof reply.then === 'function') return reply;
      if (typeof reply.json === 'function') return Promise.resolve(reply);
      return Promise.resolve({ json: () => Promise.resolve(reply) });
    }
    return Promise.resolve({ json: () => Promise.resolve(reply) });
  };
  global.fetch = (url, init) => {
    calls.push({ url, method: (init && init.method) || 'GET' });
    if (url.endsWith('/api/login')) {
      const reply = typeof loginReply === 'function' ? loginReply() : loginReply;
      return wrap(reply);
    }
    const next = authQueue.length > 1 ? authQueue.shift() : authQueue[0];
    const reply = typeof next === 'function' ? next() : next;
    return wrap(reply);
  };
  els['login-pass'] && (els['login-pass'].type = 'password');
  new Function(LOGIN_SRC)();
  return {
    els,
    el,
    calls,
    replaced,
    submit() { els['login-form'].handlers.submit({ preventDefault() {} }); },
    click(id) { els[id].handlers.click(); },
    key(id, event) { els[id].handlers.keydown(event); },
    blur(id) { els[id].handlers.blur(); },
    settle: () => new Promise((r) => setTimeout(r, 20)),
  };
}

async function main() {
  const json = (body, status = 200) => ({ status, json: () => Promise.resolve(body) });
  const reject = () => Promise.reject(new TypeError('Failed to fetch'));

  // 1. 初始已登录：进页面就跳看板
  {
    const s = setup([json({ authenticated: true, host: 'h' })], json({ ok: true }));
    await s.settle();
    assert(s.replaced.length === 1 && s.replaced[0] === '/', '初始已登录 → 自动跳看板');
  }

  // 2. 正常登录：POST 成功 → 跳看板
  {
    const s = setup([json({ authenticated: false })], json({ ok: true, username: 'a' }));
    await s.settle();
    s.els['login-user'].value = 'a';
    s.els['login-pass'].value = 'p';
    s.submit();
    await s.settle();
    assert(s.replaced[s.replaced.length - 1] === '/', '登录成功 → 跳看板');
    assert(s.els['login-error'].textContent === '', '登录成功不显示错误');
  }

  // 3. 登录响应被拦截，但服务端已登录 → 复查 /api/auth 后跳看板
  {
    const s = setup([json({ authenticated: false }), json({ authenticated: true })], reject);
    await s.settle();
    s.submit();
    await s.settle();
    const gotAuth = s.calls.some((c) => c.method === 'GET' && c.url.endsWith('/api/auth'));
    assert(gotAuth, 'POST 失败后会复查 /api/auth');
    assert(s.replaced[s.replaced.length - 1] === '/', '响应丢失但已登录 → 仍跳看板');
    assert(s.els['login-error'].textContent === '', '已登录时不显示「无法连接服务」');
  }

  // 4. 200 但响应体读不出来，且会话已建立 → 跳看板
  {
    const s = setup(
      [json({ authenticated: false }), json({ authenticated: true })],
      { status: 200, json: () => Promise.reject(new Error('bad body')) }
    );
    await s.settle();
    s.submit();
    await s.settle();
    assert(s.replaced[s.replaced.length - 1] === '/', '响应体损坏但已登录 → 跳看板');
  }

  // 5. 密码错误：展示服务端 message、清空密码框、允许重试
  {
    const s = setup([json({ authenticated: false })], json(
      { error: 'invalid_credentials', message: '账号或密码不正确' }, 401
    ));
    await s.settle();
    s.els['login-user'].value = 'a';
    s.els['login-pass'].value = 'wrong';
    s.submit();
    await s.settle();
    assert(s.els['login-error'].textContent === '账号或密码不正确', '密码错误展示 message');
    assert(s.els['login-pass'].value === '', '密码错误后清空密码框');
    assert(s.els['login-submit'].disabled === false, '密码错误后按钮恢复可用');
    assert(s.replaced.length === 0, '密码错误不跳转');
  }

  // 6. 真的连不上：复查也失败 → 显示连接错误
  {
    const s = setup([reject, reject], reject);
    await s.settle();
    s.submit();
    await s.settle();
    assert(
      s.els['login-error'].textContent === '无法连接服务，请确认看板还在运行',
      '复查仍失败才显示「无法连接服务」'
    );
    assert(s.els['login-submit'].textContent === '进入总控台', '失败后按钮文案复位');
  }

  // 7. 显示 / 隐藏密码
  {
    const s = setup([json({ authenticated: false })], json({ ok: true }));
    await s.settle();
    assert(s.els['login-pass'].type === 'password', '密码框默认是密文');
    s.click('login-show');
    assert(s.els['login-pass'].type === 'text', '点「显示」后变明文');
    s.click('login-show');
    assert(s.els['login-pass'].type === 'password', '再点一次变回密文');
  }

  // 8. 大写锁定提示
  {
    const s = setup([json({ authenticated: false })], json({ ok: true }));
    await s.settle();
    s.key('login-pass', { getModifierState: () => true });
    assert(s.els['login-caps'].hidden === false, 'CapsLock 开 → 提示显示');
    s.blur('login-pass');
    assert(s.els['login-caps'].hidden === true, '失焦 → 提示隐藏');
  }

  console.log('\nlogin.test.js：%d 通过，%d 失败', pass, failCount);
  if (failCount) { fails.forEach((f) => console.error('  失败点：' + f)); process.exit(1); }
}

main().catch((e) => { console.error(e); process.exit(1); });
