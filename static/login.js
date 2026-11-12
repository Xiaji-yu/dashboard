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

  /* 已经登录过就直接进看板；顺便把主机名显示出来 */
  fetch(BASE + '/api/auth', { cache: 'no-store' })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      showHost(data.host);
      if (data.authenticated) window.location.replace(BASE + '/');
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
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { status: res.status, data: data };
      });
    }).then(function (result) {
      if (result.status === 200 && result.data.ok) {
        window.location.replace(BASE + '/');
        return;
      }
      showError(result.data.message || '登录失败，请重试');
      shakeFields();
      passInput.value = '';
      passInput.focus();
      submit.disabled = false;
      submit.textContent = '进入总控台';
    }).catch(function () {
      showError('无法连接服务，请确认看板还在运行');
      submit.disabled = false;
      submit.textContent = '进入总控台';
    });
  });

  userInput.focus();
})();
