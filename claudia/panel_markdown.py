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

Four helpers, because the UI has four distinct rendering paths:

- :func:`safe_markdown` — for panes we construct, and as the ``renderers`` hook on the
  ChatInterface so every ``chat.send()`` string is covered.
- :func:`safe_text` — for the System log's monospace ``Str`` lines.
- :func:`escape_markup` — for text streamed into a ``pn.chat.ChatStep``. ``ChatStep`` has
  no ``renderers`` parameter and builds its own Markdown panes internally, so the feed-level
  renderer cannot reach it; the text must be escaped before it is handed over.
- :func:`safe_toast` — for ``pn.state.notifications``, whose body notyf assigns with
  ``innerHTML``.

**How many escapes each path needs is not the same, and that is the whole trap.** A pane
path (Markdown, Str) escapes once for transport and the client decodes once before
``innerHTML``, so the text must arrive already escaped to survive as text — two escapes
total, one of which Panel adds. The toast path has no decode step, so exactly one escape is
the control. Asserting the pane's single transport escape as if it were a control is how
``safe_text`` shipped vulnerable from 2026-09-04 to 2026-09-14 with a green test
(audit 2026-09-13, finding A-1).

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


def safe_text(text: str, **params: Any) -> pn.pane.Str:
    """Build a ``Str`` pane: the text is shown as a raw string, every character escaped.

    ``pn.pane.Str`` is Panel's literal-text pane — no Markdown, no HTML; its
    ``_transform_object`` emits ``escape('<pre>' + str(obj) + '</pre>')`` — but that escape
    is **transport encoding, not a control**: ``Str`` carries the same bokeh ``HTML`` model
    as ``Markdown``, ``run_scripts=True`` included, and the client's ``html_decode`` undoes
    it before ``innerHTML``. So the text is escaped *here* first, and Panel's own escape
    makes it double — which is what survives the decode as characters rather than markup.

    Corrected 2026-09-14 (audit 2026-09-13, finding A-1). The 2026-09-04 version passed the
    object through unescaped and its test asserted the single-escaped form as success; the
    System log carries IBKR fill strings, gateway detail, tool output and exception text, so
    that pane executed markup from those sources. ``test_unsafe_str_pane_would_be_vulnerable``
    now guards the premise.

    It is constructed here, and only here, so the structural test that bans direct
    ``pn.pane.*`` markup constructors elsewhere in the package keeps one sanctioned site for
    every markup pane. Used by the System log's terminal-style lines.

    Args:
        text: The line to show. Untrusted input is expected and safe to pass.
        **params: Extra pane parameters (``margin``, sizing).
    """
    return pn.pane.Str(escape_markup(text), **params)


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


def safe_toast(notifications: Any, level: str, text: str, *, duration: int) -> None:
    """Raise one toast whose body cannot become markup.

    The single route for ``pn.state.notifications``. Panel hands ``message`` to notyf
    unchanged and notyf assigns it with ``innerHTML`` (no ``html_decode`` on this path, so
    one escape is exactly the control — two would show entities to the user).

    A function rather than escaping at each call site because the sites drift: the System
    log and the dashboard's stale-data toast both interpolate IBKR and exception text, and
    a third site would be written by copying whichever one was found first.

    Args:
        notifications: A live ``pn.state.notifications``; callers check it is not None
            first, since it is None outside a served session.
        level: ``"info"``, ``"warning"``, ``"error"`` or ``"success"`` — the notyf method.
        text: The message. Untrusted input is expected and safe to pass.
        duration: Milliseconds; ``0`` is sticky.
    """
    getattr(notifications, level)(escape_markup(text), duration=duration)
