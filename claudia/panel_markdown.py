"""XSS-safe Markdown rendering for the Panel UI — the single control for H-1.

Panel's Markdown pane renders raw HTML **and re-executes `<script>` tags** by default.
Verified end-to-end against panel 1.9.3 / bokeh 3.9.1 (security-audit-2026-07-25.md, H-1):

1. ``MarkdownIt('gfm-like').options['html']`` is ``True`` — raw HTML passes the parser.
2. The bokeh ``HTML`` model carries ``run_scripts = True`` (Panel's default). Its ``text``
   is HTML-escaped, but only as *transport* encoding.
3. Client ``process_tex()`` calls ``html_decode(this.model.text)`` — undoing that escaping.
4. Client ``set_html()`` assigns ``this.container.innerHTML = html``.
5. ``run_scripts()`` then re-creates every ``<script>`` node via
   ``document.createElement("script")``, which makes it *execute* — plain ``innerHTML``
   would not.

Untrusted text reaches those panes from the LLM, from raw tool results (a page fetched by
``fetch_web_page``/``firecrawl_*`` needs no LLM cooperation at all), from IBKR and
TradingView responses, and from exception strings.

Two helpers, because the UI has two distinct rendering paths:

- :func:`safe_markdown` — for panes we construct, and as the ``renderers`` hook on the
  ChatInterface so every ``chat.send()`` string is covered.
- :func:`escape_markup` — for text streamed into a ``pn.chat.ChatStep``. ``ChatStep`` has
  no ``renderers`` parameter and builds its own Markdown panes internally, so the feed-level
  renderer cannot reach it; the text must be escaped before it is handed over.

**Fencing is not a substitute.** A ```` ``` ```` fence around untrusted text was tested and
is bypassable — content containing its own closing fence escapes it and renders as markup.
``run_scripts=False`` is also insufficient on its own: it stops ``<script>`` but not
``onerror=``/``onload=`` attributes. Escaping is the load-bearing control.

Source (Markdown pane / renderer_options):
https://panel.holoviz.org/reference/panes/Markdown.html
"""

from __future__ import annotations

import html
from typing import Any

import panel as pn

# markdown-it's `html` option. False makes the parser emit raw HTML as escaped text instead
# of passing it through, so the payload survives the client's single html_decode as literal
# characters rather than markup.
_SAFE_RENDERER_OPTIONS = {"html": False}


def safe_markdown(obj: Any, **params: Any) -> pn.pane.Markdown:
    """Build a Markdown pane that renders HTML in ``obj`` as visible text, not as markup.

    Doubles as the ``renderers`` hook for ``pn.chat.ChatInterface``. Panel applies renderers
    only to plain values — Panel objects sent through the feed (``ChatStep``, ``Column``,
    image panes) bypass it and are returned unchanged, so installing this feed-wide is safe
    (verified for str/ChatStep/Column/Markdown/PNG).

    Args:
        obj: Content to render. Untrusted input is expected and safe to pass.
        **params: Extra Markdown pane parameters. ``renderer_options`` is set here and
            callers should not override it — doing so re-opens the injection.

    Returns:
        A ``pn.pane.Markdown`` whose model text stays double-escaped, so the browser's
        single ``html_decode`` yields text rather than executable markup.
    """
    return pn.pane.Markdown(obj, renderer_options=_SAFE_RENDERER_OPTIONS, **params)


def escape_markup(text: str) -> str:
    """Escape HTML so ``text`` is displayed literally when streamed into a ChatStep.

    For the ``pn.chat.ChatStep`` path only. The escaped entities pass through markdown-it
    untouched and are decoded exactly once by the client, so the user sees the original
    characters (``<img src=x>``) while the browser parses them as a text node. Markdown
    syntax such as ``*emphasis*`` still renders — only the HTML/script vector is closed.

    ``quote=False`` is deliberate. Escaping ``<`` and ``>`` alone makes it impossible to
    open a tag, so no attribute context can ever be entered and quotes cannot contribute to
    an injection. Keeping them literal leaves JSON tool arguments readable
    (``{"foo": "bar"}`` rather than ``{&quot;foo&quot;: &quot;bar&quot;}``).

    Not covered here because markdown-it already handles it: dangerous link schemes.
    ``[x](javascript:…)``, ``data:`` and ``vbscript:`` are rejected by markdown-it's
    ``validateLink`` and emitted as plain text, while ``https:`` links still render
    (verified 2026-07-25).

    Args:
        text: Untrusted text, typically a tool argument blob or a raw tool result.

    Returns:
        The text with ``&``, ``<`` and ``>`` replaced by entities.
    """
    return html.escape(text, quote=False)
