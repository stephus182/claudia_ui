/**
 * ClaudIA UI — status bar + collapsible code blocks
 * Injects a fixed top bar with live connectivity dots.
 * Polls /api/status every 5s (backend caches in memory — this is cheap).
 * Auto-collapses code blocks longer than COLLAPSE_LINES lines.
 */
(function () {
  'use strict';

  const POLL_MS = 5000;
  const COLLAPSE_LINES = 8;
  const SERVICES = [
    { key: 'gdrive', label: 'GDrive' },
    { key: 'ibkr',   label: 'IBKR' },
    { key: 'tv',     label: 'TradingView' },
  ];

  // ── Status bar ────────────────────────────────────────────────

  const createBar = () => {
    const bar = document.createElement('div');
    bar.id = 'claudia-status-bar';

    const logo = document.createElement('img');
    logo.src = '/cl/claudia-logo.png';
    logo.className = 'cl-logo';
    logo.alt = 'ClaudIA';
    bar.appendChild(logo);

    const svcWrap = document.createElement('div');
    svcWrap.className = 'cl-services';

    SERVICES.forEach(svc => {
      const item = document.createElement('div');
      item.className = 'cl-service';

      const dot = document.createElement('span');
      dot.className = 'cl-dot';
      dot.id = 'cl-dot-' + svc.key;
      dot.title = svc.label + ': checking…';

      const lbl = document.createElement('span');
      lbl.className = 'cl-label';
      lbl.textContent = svc.label;

      item.appendChild(dot);
      item.appendChild(lbl);
      svcWrap.appendChild(item);
    });

    bar.appendChild(svcWrap);
    document.body.prepend(bar);
  };

  const updateDots = status => {
    SERVICES.forEach(svc => {
      const dot = document.getElementById('cl-dot-' + svc.key);
      if (!dot) return;
      const state = (status && status[svc.key]) || 'unknown';
      dot.className = 'cl-dot';
      if (state === 'ok')    dot.classList.add('ok');
      if (state === 'error') dot.classList.add('error');
      dot.title = svc.label + ': ' + state;
    });
  };

  const poll = () => {
    fetch('/api/status', { cache: 'no-store' })
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data) updateDots(data); })
      .catch(() => { /* network error — keep current dots */ });
  };

  // ── Collapsible code blocks ───────────────────────────────────

  const wrapCodeBlock = pre => {
    // Skip if already wrapped
    if (pre.parentNode && pre.parentNode.classList.contains('cl-code-wrap')) return;

    const code = pre.querySelector('code');
    const text = code ? code.textContent : pre.textContent;
    const lines = text.split('\n').length;
    if (lines <= COLLAPSE_LINES) return;

    const wrap = document.createElement('div');
    wrap.className = 'cl-code-wrap collapsed';
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);

    const btn = document.createElement('button');
    btn.className = 'cl-code-toggle';
    btn.textContent = '▼ Show raw (' + lines + ' lines)';
    wrap.appendChild(btn);

    btn.addEventListener('click', () => {
      const collapsed = wrap.classList.toggle('collapsed');
      btn.textContent = collapsed
        ? '▼ Show raw (' + lines + ' lines)'
        : '▲ Hide raw';
    });
  };

  const collapseCodeBlocks = root => {
    root.querySelectorAll('pre').forEach(wrapCodeBlock);
  };

  // ── Init ──────────────────────────────────────────────────────

  const init = () => {
    createBar();
    poll();
    setInterval(poll, POLL_MS);

    // Process any code blocks already in the DOM
    collapseCodeBlocks(document.body);

    // Watch for new messages being added
    const observer = new MutationObserver(mutations => {
      mutations.forEach(m => {
        m.addedNodes.forEach(node => {
          if (node.nodeType !== 1) return;
          collapseCodeBlocks(node);
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
