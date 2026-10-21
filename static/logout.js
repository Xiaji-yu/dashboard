/* 侧栏「退出登录」：注销当前会话并回到登录页。
   与账号弹窗里的「退出登录」等价，只是把入口放到侧栏，少点一次。 */
(function () {
  'use strict';

  var button = document.getElementById('logout');
  if (!button) return;

  function backToLogin() {
    window.location.replace(basePath('/login'));
  }

  button.addEventListener('click', function () {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = '退出中…';
    postJSON('/api/logout', {})
      .then(backToLogin)
      .catch(backToLogin);
  });
})();
