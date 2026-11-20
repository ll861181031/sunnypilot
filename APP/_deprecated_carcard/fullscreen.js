(function () {
  'use strict';
  var PATH = 'fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"';
  var ICON = {
    full: '<svg viewBox="0 0 24 24"><path d="M4 9.5V4h5.5M14.5 4H20v5.5M20 14.5V20h-5.5M9.5 20H4v-5.5" ' + PATH + '/></svg>',
    small: '<svg viewBox="0 0 24 24"><path d="M9.5 4v5.5H4M20 9.5h-5.5V4M14.5 20v-5.5H20M4 14.5h5.5V20" ' + PATH + '/></svg>'
  };
  function text(code, mode) {
    if (code === 'UNSUPPORTED') return mode === 'small' ? '现在不是全屏，不用恢复' : '这台车机还不能全屏打开卡片';
    if (code === 'TIMEOUT' || code === 'SESSION_CLOSED') return '车机没回应，过一会儿再点';
    if (code === 'PERMISSION_DENIED') return '车机不让切换窗口';
    return (mode === 'small' ? '没能恢复小屏' : '没能全屏打开') + (code ? '（' + code + '）' : '');
  }
  function bind(btn, opt) {
    opt = opt || {};
    var u = window.uiplus, w = u && u.windows, mode = '', at = 0;
    if (!btn || !w || typeof w.openFullscreen !== 'function') return null;
    var info = u.getInfo ? Promise.resolve().then(function () { return u.getInfo(); }).then(null, function () { return null; }) : Promise.resolve(null);
    info.then(function (i) {
      mode = i && i.hostType === 'fullscreen' ? 'small' : 'full';
      if (mode === 'small' && typeof w.restoreSmall !== 'function' && typeof w.toggleFullscreen !== 'function') { mode = ''; return; }
      btn.innerHTML = ICON[mode];
      btn.setAttribute('aria-label', mode === 'small' ? '恢复小屏' : '全屏');
      btn.classList.toggle('fs-small', mode === 'small');
      btn.hidden = false;
      if (opt.ready) opt.ready(mode);
    });
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!mode || Date.now() - at < 2500) return;
      at = Date.now();
      var m = mode;
      Promise.resolve().then(function () {
        return typeof w.toggleFullscreen === 'function' ? w.toggleFullscreen() : m === 'small' ? w.restoreSmall() : w.openFullscreen();
      }).then(null, function (er) {
        at = 0;
        var c = er && er.code;
        if (opt.fail) opt.fail(c, m, text(c, m));
      });
    });
    return { mode: function () { return mode; } };
  }
  function bar(visible) {
    var u = window.uiplus, w = u && u.windows;
    if (typeof visible !== 'boolean' || !w || typeof w.setStatusBarVisible !== 'function') return null;
    return Promise.resolve().then(function () { return w.setStatusBarVisible(visible); });
  }
  window.CardFullscreen = { bind: bind, text: text, bar: bar };
})();
