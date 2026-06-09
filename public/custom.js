/**
 * ClaudIA UI — status bar injector
 * Injects a fixed top bar with live connectivity dots.
 * Polls /api/status every 60s (matches backend poll interval).
 */
(function () {
  'use strict';

  var POLL_MS = 60000;
  var SERVICES = [
    { key: 'gdrive', label: 'GDrive' },
    { key: 'ibkr',   label: 'IBKR' },
    { key: 'tv',     label: 'TradingView' },
  ];

  function createBar() {
    var bar = document.createElement('div');
    bar.id = 'claudia-status-bar';

    // Logo
    var logo = document.createElement('img');
    logo.src = '/public/claudia-logo.png';
    logo.className = 'cl-logo';
    logo.alt = 'ClaudIA';
    bar.appendChild(logo);

    // Services
    var svcWrap = document.createElement('div');
    svcWrap.className = 'cl-services';

    SERVICES.forEach(function (svc) {
      var item = document.createElement('div');
      item.className = 'cl-service';

      var dot = document.createElement('span');
      dot.className = 'cl-dot';
      dot.id = 'cl-dot-' + svc.key;
      dot.title = svc.label + ': checking…';

      var lbl = document.createElement('span');
      lbl.className = 'cl-label';
      lbl.textContent = svc.label;

      item.appendChild(dot);
      item.appendChild(lbl);
      svcWrap.appendChild(item);
    });

    bar.appendChild(svcWrap);
    document.body.prepend(bar);
  }

  function updateDots(status) {
    SERVICES.forEach(function (svc) {
      var dot = document.getElementById('cl-dot-' + svc.key);
      if (!dot) return;
      var state = (status && status[svc.key]) || 'unknown';
      dot.className = 'cl-dot';                     // reset
      if (state === 'ok')    dot.classList.add('ok');
      if (state === 'error') dot.classList.add('error');
      dot.title = svc.label + ': ' + state;
    });
  }

  function poll() {
    fetch('/api/status', { cache: 'no-store' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) { if (data) updateDots(data); })
      .catch(function () { /* network error — keep current dots */ });
  }

  function init() {
    createBar();
    poll();                            // immediate first check
    setInterval(poll, POLL_MS);        // then every 60s
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
