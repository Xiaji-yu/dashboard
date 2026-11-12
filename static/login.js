/* 登录页：提交账号密码，成功后回到看板。独立于 SPA，不依赖 app.js。
   版式对齐 xiaji-home 内容后台：底线式输入条 + 错误抖动 + 大写锁定提示。 */
'use strict';

(function () {
  var BASE = window.DASH_BASE || '';
  var form = document.getElementById('login-form');
  var userInput = document.getElementById('login-user');
  var passInput = document.getElementById('login-pass');
  var errorBox = document.getElementById('login-error');
  var submit = document.getElementById('login-submit');
  var showBtn = document.getElementById('login-show');
  var capsBox = document.getElementById('login-caps');
  var userField = document.getElementById('login-user-field');
  var passField = document.getElementById('login-pass-field');

  function showError(text) {
    errorBox.textContent = text || '';
    errorBox.classList.toggle('show', Boolean(text));
  }

  /* 口令不对：底线变红 + 整条抖动（动画结束后由 CSS animationend 复位） */
  function shakeFields() {
    [userField, passField].forEach(function (field) {
      if (!field) return;
      field.classList.remove('is-error');
      /* 强制重排，让连续两次失败也能重新触发抖动动画 */
      void field.offsetWidth;
      field.classList.add('is-error');
    });
  }

  [userField, passField].forEach(function (field) {
    if (field) field.addEventListener('animationend', function () {
      field.classList.remove('is-error');
    });
  });

  /* 大写锁定检测：只在密码框里提示 */
  function checkCaps(event) {
    if (!capsBox) return;
    var on = typeof event.getModifierState === 'function'
      && event.getModifierState('CapsLock');
    capsBox.hidden = !on;
  }
  if (passInput) {
    passInput.addEventListener('keydown', checkCaps);
    passInput.addEventListener('keyup', checkCaps);
    passInput.addEventListener('blur', function () { capsBox.hidden = true; });
  }

  /* 显示 / 隐藏密码 */
  if (showBtn) {
    showBtn.addEventListener('click', function () {
      var hidden = passInput.type === 'password';
      passInput.type = hidden ? 'text' : 'password';
      showBtn.textContent = hidden ? '隐藏' : '显示';
      passInput.focus();
    });
  }

  function showHost(host) {
    if (host) document.getElementById('login-host').textContent = host;
  }

  var AUTH_URL = BASE + '/api/auth';

  function enterDashboard() {
    window.location.replace(BASE + '/');
  }

  function resetSubmit() {
    submit.disabled = false;
    submit.textContent = '进入总控台';
  }

  /* 会话是否已经建立。
     不能只看登录请求的返回值：请求可能已经到达服务端、Cookie 也已经下发，
     但响应在浏览器侧被拦截/中断（Vivaldi 的内置拦截器、扩展、代理抖动都出现过），
     那样 fetch 会 reject，而实际已经登录成功。 */
  function checkSignedIn() {
    return fetch(AUTH_URL, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (data) { return Boolean(data && data.authenticated); })
      .catch(function () { return false; });
  }

  /* 登录请求没拿到可用的响应：先复查会话，真的没登上才报错 */
  function recover(message) {
    return checkSignedIn().then(function (signedIn) {
      if (signedIn) {
        enterDashboard();
        return;
      }
      showError(message || '无法连接服务，请确认看板还在运行');
      resetSubmit();
    });
  }

  /* 已经登录过就直接进看板；顺便把主机名显示出来 */
  fetch(AUTH_URL, { cache: 'no-store' })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      showHost(data.host);
      if (data.authenticated) enterDashboard();
    })
    .catch(function () { /* 服务没起来时保持登录页 */ });

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    showError('');
    submit.disabled = true;
    submit.textContent = '验证中…';
    fetch(BASE + '/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ username: userInput.value, password: passInput.value })
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        return { status: res.status, data: data };
      });
    }).then(function (result) {
      var data = result.data;
      if (data && data.ok) {
        enterDashboard();
        return;
      }
      if (data && data.message) {
        showError(data.message);
        shakeFields();
        passInput.value = '';
        passInput.focus();
        resetSubmit();
        return;
      }
      /* 状态码拿到了但响应体读不出来：可能是代理返回了错误页，复查一次会话 */
      return recover(result.status >= 500
        ? '服务暂时不可用，请稍后重试'
        : '登录失败，请重试');
    }).catch(function () {
      /* 连状态码都没拿到（响应被拦截或连接中断）：同样以会话状态为准 */
      return recover();
    });
  });

  userInput.focus();
})();
