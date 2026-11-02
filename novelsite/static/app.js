/* novelsite —— 阅读设置、进度同步、键盘翻章。无依赖，ES5 语法以兼容老旧阅读器浏览器。 */
(function () {
  'use strict';
  var KEY = 'novelsite.reader';
  var DEFAULTS = { theme: '', fs: 19, lh: 1.8, w: 40, font: 'serif' };
  var THEMES = ['', 'paper', 'green', 'dark'];
  var root = document.documentElement;

  function load() {
    var s = {}, k;
    try { s = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { s = {}; }
    for (k in DEFAULTS) if (!(k in s)) s[k] = DEFAULTS[k];
    return s;
  }
  function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* 隐私模式 */ } }
  function apply(s) {
    if (s.theme) root.setAttribute('data-theme', s.theme); else root.removeAttribute('data-theme');
    root.setAttribute('data-font', s.font);
    root.style.setProperty('--fs', s.fs + 'px');
    root.style.setProperty('--lh', String(s.lh));
    root.style.setProperty('--w', s.w + 'em');
  }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  var S = load();
  apply(S); // 在 <head> 中同步执行，避免夜间模式闪白

  function initTheme() {
    var b = document.getElementById('theme');
    if (!b) return;
    b.addEventListener('click', function () {
      S.theme = THEMES[(THEMES.indexOf(S.theme) + 1) % THEMES.length];
      apply(S); save(S);
    });
  }

  function initPanel() {
    var btn = document.getElementById('set'), panel = document.getElementById('panel');
    if (!btn || !panel) return;
    btn.addEventListener('click', function () {
      panel.hidden = !panel.hidden;
      btn.setAttribute('aria-expanded', String(!panel.hidden));
    });
    panel.addEventListener('click', function (ev) {
      var t = ev.target;
      while (t && t !== panel && !(t.getAttribute && t.getAttribute('data-k'))) t = t.parentNode;
      if (!t || t === panel) return;
      var k = t.getAttribute('data-k'), v = t.getAttribute('data-v');
      if (k === 'theme' || k === 'font') S[k] = v;
      else if (k === 'fs') S.fs = clamp(S.fs + Number(v), 12, 36);
      else if (k === 'lh') S.lh = clamp(Math.round((S.lh + Number(v)) * 10) / 10, 1.2, 3);
      else if (k === 'w') S.w = clamp(S.w + Number(v), 20, 80);
      else if (k === 'reset') { var th = S.theme; S = load(); for (var d in DEFAULTS) S[d] = DEFAULTS[d]; S.theme = th; }
      apply(S); save(S);
    });
  }

  function initReader() {
    var el = document.getElementById('text');
    if (!el) return;
    var book = Number(el.getAttribute('data-book')), ch = Number(el.getAttribute('data-ch'));
    var prev = el.getAttribute('data-prev'), next = el.getAttribute('data-next');

    function frac() {
      var h = root.scrollHeight - window.innerHeight;
      return h > 0 ? clamp((window.pageYOffset || root.scrollTop) / h, 0, 1) : 0;
    }
    function send(beacon) {
      var body = JSON.stringify({ b: book, c: ch, p: Math.round(frac() * 1000) / 1000 });
      if (beacon && navigator.sendBeacon) {
        try { if (navigator.sendBeacon('/api/progress', new Blob([body], { type: 'application/json' }))) return; } catch (e) { /* 回退到 XHR */ }
      }
      try {
        var x = new XMLHttpRequest();
        x.open('POST', '/api/progress', true);
        x.setRequestHeader('Content-Type', 'application/json');
        x.send(body);
      } catch (e) { /* 离线时忽略 */ }
    }

    var mark = el.querySelector('mark');
    var m = /pos=([0-9.]+)/.exec(location.hash);
    if (mark && mark.scrollIntoView) {
      mark.scrollIntoView({ block: 'center' });
    } else if (m) {
      var p = parseFloat(m[1]);
      window.scrollTo(0, p * (root.scrollHeight - window.innerHeight));
    }

    var timer = null;
    window.addEventListener('scroll', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { send(false); }, 1500);
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') send(true);
    });
    window.addEventListener('pagehide', function () { send(true); });
    setTimeout(function () { send(false); }, 600); // 打开即记录，等恢复滚动位置后再发

    document.addEventListener('keydown', function (e) {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if ((e.key === 'ArrowLeft' || e.keyCode === 37) && prev) location.href = prev;
      else if ((e.key === 'ArrowRight' || e.keyCode === 39) && next) location.href = next;
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initTheme();
    initPanel();
    initReader();
  });
})();
