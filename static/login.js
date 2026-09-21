/* 登录页：提交账号密码，成功后回到看板。独立于 SPA，不依赖 app.js。 */
'use strict';

(function () {
  var form = document.getElementById('login-form');
  var userInput = document.getElementById('login-user');
  var passInput = document.getElementById('login-pass');
  var errorBox = document.getElementById('login-error');
  var submit = document.getElementById('login-submit');

  function showError(text) {
    errorBox.textContent = text || '';
    errorBox.classList.toggle('show', Boolean(text));
  }

  function showHost(host) {
    if (host) document.getElementById('login-host').textContent = host;
  }

  /* 已经登录过就直接进看板；顺便把主机名显示出来 */
  fetch('/api/auth', { cache: 'no-store' })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      showHost(data.host);
      if (data.authenticated) window.location.replace('/');
    })
    .catch(function () { /* 服务没起来时保持登录页 */ });

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    showError('');
    submit.disabled = true;
    submit.textContent = '登录中…';
    fetch('/api/login', {
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
        window.location.replace('/');
        return;
      }
      showError(result.data.message || '登录失败，请重试');
      passInput.value = '';
      passInput.focus();
      submit.disabled = false;
      submit.textContent = '登录';
    }).catch(function () {
      showError('无法连接服务，请确认看板还在运行');
      submit.disabled = false;
      submit.textContent = '登录';
    });
  });

  userInput.focus();
})();
