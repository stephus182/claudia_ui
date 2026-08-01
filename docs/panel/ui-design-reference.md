# Panel UI design & styling reference

**The styling surface available to ClaudIA, what has been proven to work here, and a
recommended direction for the deep restyle.** Living document.

Two rules govern this doc:

1. **Every Panel claim is scraped and cited** (CLAUDE.md "API Docs First"). Official URLs and
   their scrape date are in §9.
2. **§8 is a proposal, not a decision.** The deep restyle is its own project — Track D in
   [`docs/project-status.md`](../project-status.md) Known Gap #14 — to be brainstormed with the
   user first. Nothing here has been agreed.

Companion: `panel-reference.md` (how the Panel layer works today).
Versions described: **panel 1.9.3**, **bokeh 3.9.1**.

---

## 1. Current visual state — the honest baseline

**ClaudIA has no styling.** Verified repo-wide across `claudia/`, `tests/` and `scripts/`:

| | Present? |
|---|---|
| A `.css` file anywhere in the repo | **No** |
| `stylesheets=` on any component | **No** |
| `css_classes=` / `styles=` | **No** |
| `raw_css` / `global_css` | **No** |
| `design=` or `theme=` | **No** |
| `pn.extension(...)` called at all | **No** |
| `pn.config` touched | **No** |
| Any template (`FastListTemplate`, `BootstrapTemplate`, …) | **No** |
| `static_dirs=` on `pn.serve` | **No** |

Every visual parameter in the entire application:

- `sizing_mode="stretch_both"` (layout root, chart pane) and `"stretch_width"` (figure)
- two hardcoded sizes — `height=360` on the chart figure, `width=400` on the echoed screenshot
  image
- Button `color=` semantic names: `success`, `danger`, `light`, `primary`, `warning`
- three hex constants in [`panel_chart.py:40-42`](../../claudia/panel_chart.py#L40-L42) —
  `#26a69a` / `#ef5350` / `#666`

Also: `claudia/assets/claudia-logo.png` (1.1 MB) exists but is **referenced by no code**, and
there is no `static_dirs=` to serve it — Chainlit-era residue, not a live asset.

### What the baseline actually looks like

The reference capture is `docs/panel/screenshots/dots-full.png` — **local only, git-ignored**,
so the description below is written to stand on its own without it. Observable, unstyled Panel
defaults:

- Three status dots flush to the top-left corner. **No label text is visible beside them** —
  from the UI alone you cannot tell which dot is IBKR, GDrive or TradingView (the `label=`
  values are set in code but do not render as adjacent text in this capture).
- Grey circular avatars with a letter/glyph, the author name as plain text above each bubble,
  light-grey rounded message bubbles, and a copy/like icon pair plus an `HH:MM` timestamp
  beneath every message.
- Markdown renders correctly and richly: bold headings, a genuine bordered `<table>` for open
  positions, bulleted live orders, italic footnotes.
- **P&L values are bold but not colored.** `-$1,009.95`, `-$8,892.69` and `+$1,265.10` all
  render in the same black. Nothing distinguishes a loss from a gain at a glance — for a
  trading surface this is the single most visible gap, and §4 shows it is also the easiest to
  close.
- The bottom input row is Panel's stock `Send` / `Rerun` / `Undo` / `Clear` set.
- No header, no sidebar, no branding, no theme.

⚠ **Scope caveat on the screenshots:** all three were captured during the Task 6.2 / TV-offline
smokes, which predate both the screenshot `FileInput` (Task 8.1) and the candlestick chart pane
(Task 10.1). They show the chat column only — the current layout is a two-column
`pn.Row` (see `panel-reference.md` §4). Treat them as a chat-surface baseline, not a
full-window one.

---

## 2. The shadow-DOM constraint — the one finding that dictates everything

This was live-tested with Playwright against a running Panel 1.9.3 `ChatInterface` rather than
assumed. It is reproduced here because the raw record lives only in the git-ignored
`docs/plans/2026-07-22-panel-shadow-dom-live-test.md`.

**Method:** a `ChatInterface` with one message containing inline-styled spans and a markdown
table, served with `panel serve`, inspected by walking `element.getRootNode()` recursively and
reading `getComputedStyle(...)`.

**Chat message content sits 7 shadow-root levels deep.**

| Styling mechanism | Reaches nested message content? |
|---|---|
| Page-level `<style>` with a class or tag selector (the old Chainlit `custom.css` pattern) | **No — confirmed to fail** |
| Inline `style="..."` in the generated message HTML | **Yes — confirmed** |
| `stylesheets=[...]` on the Python component, using `:host` / `:host *` | **Yes — confirmed** |

The negative results are specific: a page-level `.pnl-positive { background: yellow
!important; text-decoration: underline !important }` injected into `document.head` left
`backgroundColor` at `rgba(0, 0, 0, 0)` and `textDecorationLine` at `none` — `!important` did
not help. Setting `document.body.style.fontFamily` likewise did not reach in: the nested span
stayed at `Helvetica, Arial, sans-serif` while `document.body` correctly reported the new
value, i.e. an internal component declares its own font and blocks inheritance.

The positive result is equally specific: `pn.chat.ChatInterface(stylesheets=[":host {
font-family: 'Courier New', monospace !important; } :host * { ... }"])` computed to
`"Courier New", monospace` at the same 7-level depth, across the hosts inside that component's
tree and no others.

**Panel's own documentation says the same thing**, which is why the behavior is a design, not
a bug:

> "Panel components are rendered into the shadow DOM […] it means that each component is
> isolated from all others."

> "Since `styles` only applies to the `<div>` that holds the component we cannot use it to
> directly modify the styling of the **contents** of the component. This is where
> `stylesheets` come in, allowing us to provide CSS rules that affect each part of the
> component."

— `how_to/styling/apply_css.html`

**Consequence:** do not carry a page-level stylesheet forward. Anything inside `ChatInterface`
must be styled through `stylesheets=` with `:host` selectors, or through inline `style=` in the
message HTML we generate ourselves.

---

## 3. Panel's styling surface, scraped

### 3.1 `stylesheets`, `css_classes`, and the deprecation of `raw_css`

`how_to/styling/apply_css.html`. `css_classes` applies a class to the shadow root:

> "The `css_classes` parameter will apply the CSS class to the shadow root (or container)."

```python
pn.widgets.FloatSlider(
    label='Number',
    stylesheets=[stylesheet, color_stylesheet],
    css_classes=['red']
)
```

`:host` targets the shadow root itself:

```css
:host {
  --handle-width: 15px;
  --slider-size: 25px;
}
```

**The global `raw_css` path is deprecated.** Verbatim:

> "Deprecated since version 1.0.0: Before 1.0.0 CSS styling was generally applied globally by
> adding `raw_css` and `css_files` to the global `config` or `extension`. This approach is no
> longer recommended."

Since ClaudIA uses no `raw_css` today, there is nothing to migrate — but a restyle must not
introduce it either. (Note the design-variables page still documents `Template.config.raw_css`
for template-level overrides specifically; the deprecation quoted above is about the *global*
config/extension path.)

### 3.2 Design systems — the `design` parameter

`how_to/styling/design.html`. Panel ships **Bootstrap**, **Material** and **Native** designs.

> "Applying different design systems in Panel can be achieved globally or per component."

> "any component that is rendered will now inherit this design" *(when set globally)*

```python
pn.extension(design='material')          # globally, via extension
```
```python
from panel.theme import Material         # globally, via config
pn.config.design = Material
```
```python
pn.widgets.FloatSlider(label='Slider', design=design)   # per component
```

### 3.3 Themes — light and dark

`how_to/styling/themes.html`. Two built-in themes; light is the default.

> "if you do not explicitly override the theme it will default to a light theme."

> "all `config` options can also be set via the extension, e.g. to set the theme use
> `pn.extension(theme='dark')`."

> "The theme will apply to all components and combines with the design to provide a consistent
> visual language."

Also documented: a `?theme=dark` URL query parameter, and automatic adaptation to global CSS
variables in JupyterLab / pydata-sphinx-theme environments specifically. **No claim is made
that Panel auto-detects a browser's `prefers-color-scheme` in a standalone served app** — do
not assume it does without testing.

### 3.4 Design variables — the token layer

`how_to/styling/design_variables.html`. Panel exposes eight design tokens as CSS custom
properties:

```
--design-primary-color          --design-primary-text-color
--design-secondary-color        --design-secondary-text-color
--design-background-color       --design-background-text-color
--design-surface-color          --design-surface-text-color
```

Three application scopes:

```python
# global — the modern replacement for raw_css

pn.extension(design='material', global_css=[':root { --design-primary-color: purple; }'])
```
```python
# per component

pn.widgets.FloatSlider(stylesheets=[':host { --design-primary-color: red; }'])
```
Template-level: `Template.config.raw_css` / `css_files`.

Documented fallback order: user-defined design variables → editor/notebook variables (e.g.
JupyterLab's `--jp-brand-color0`) → theme variable definitions (e.g. `--panel-primary-color`).
Use `:root` for global consistency, `:host` for component scope.

Note the limitation the shadow-DOM test already ran into: **only color tokens exist** — there
is no documented font custom property, which is why font changes had to go through a
`stylesheets=` rule rather than a token.

### 3.5 Templates

`how_to/templates/index.html`. Built-in templates: **Bootstrap, FastGridTemplate,
FastListTemplate, GoldenLayout, Material, React, Slides, Vanilla, EditableTemplate**.
Sub-guides: `template_set.html`, `template_arrange.html`, `template_modal.html`,
`template_theme.html`, `template_custom.html`.

A template supplies the page chrome ClaudIA currently has none of — header, sidebar, main area,
and a **modal** (`template_modal.html`), the last being directly relevant to destructive
confirmations.

### 3.6 Other styling how-tos

`load_icon.html` (customize the loading indicator — the Load-chart and staging buttons already
use `loading`) and `visibility.html` (control component visibility).

---

## 4. `ChatInterface`'s own styling surface

Verified on the panel 1.9.3 reference page for `ChatInterface`. These change appearance with
**no CSS at all**:

| Parameter | Effect |
|---|---|
| `show_send`, `show_stop`, `show_rerun`, `show_undo`, `show_clear` | Toggle each footer button. ClaudIA currently shows Send/Rerun/Undo/Clear — `Rerun`/`Undo`/`Clear` are questionable on a surface where messages have side effects |
| `show_button_name` | Toggle the text labels on those buttons (icon-only mode) |
| `button_properties` | Customization mapping for the buttons |
| `message_params` | Parameters forwarded to each `ChatMessage` |
| `avatar`, `user` | The avatar and author name |
| `renderers` | Custom rendering for message contents |
| `widgets` | The input widget(s) — **see the `FileInput` caveat** in `panel-reference.md` §6 |
| `adaptive`, `auto_send_types`, `reset_on_send`, `callback` | Behavior, not appearance |

**On the P&L color gap:** the shadow-DOM test proved inline `style="color:…"` computes
correctly at full nesting depth, so the *rendering* half is solved and needs no CSS, no
template and no restyle project. The *sourcing* half is not free, though —
[`claudia/opening_status.py`](../../claudia/opening_status.py) only assembles section headers;
the numbers themselves arrive pre-formatted as text from
`toolkit.execute("get_account_summary"/"get_positions")` and `get_live_pnl_text(toolkit)`,
i.e. from **ibkr_core_mcp**, not from this repo. Coloring them means either post-processing
that text in claudia_ui (a regex over financial strings — contained, but with real correctness
risk) or a change in the other repo. See §8.1.

---

## 5. Actionable-button vocabulary

Already documented — see
[`2026-07-24-pinescript-and-actionable-buttons-research.md`](2026-07-24-pinescript-and-actionable-buttons-research.md)
Part B, which carries the full `Button` parameter table (`label`, `color`, `variant`
solid/outline — plus their deprecated aliases `name`/`button_type`/`button_style`, removed in
Panel 2.0 — `icon`, `icon_size`, `description` tooltip, `disabled`,
`loading`, `css_classes`, sizing), the `pn.state.notifications` toast API **and its caveat that
it is `None` unless `pn.extension(notifications=True)` is called**, and template modals for
destructive confirms.

Not duplicated here.

---

## 6. What is already proven to work in this app

Do not re-litigate these during a restyle:

| Mechanism | Status |
|---|---|
| Inline `style=` reaching message content | Proven live (§2) |
| `stylesheets=[':host …']` reaching message content | Proven live (§2) |
| Page-level CSS reaching message content | Proven **not** to work (§2) |
| `pn.pane.Bokeh` embedding + `pane.object=` refresh | Shipped (`panel_chart.py`) |
| Real client-side clipboard via `js_on_click` | Shipped (`panel_pinescript.py`) |
| Button `loading` spinner | Shipped (chart Load button) |
| `BooleanStatus` as a status dot | Shipped, with the labelling gap noted in §1 |
| `label=` / `color=` over `name=` / `button_type=` | Required — see `panel-reference.md` §6 |

---

## 7. Where the current design has no answer

Stated as questions, not decisions:

1. **Dot labelling** — which dot is which is not discoverable from the UI (§1).
2. **P&L is colorless** on a trading surface (§1, §4).
3. **Chart and chat share no palette.** `#26a69a`/`#ef5350` are orphan constants; nothing else
   in the app knows those colors exist.
4. **No dark theme**, on a tool used against dark charting software.
5. **No page chrome** — no header, no branding, no place to put controls that are not
   conversation.
6. **Chat-vs-chart split is unset** — a bare `pn.Row` with no ratio, and tabs were never
   compared side-by-side.
7. **`Rerun` / `Undo` / `Clear` are exposed** on an interface where a message can stage a live
   order.
8. **`claudia-logo.png` is dead weight** — 1.1 MB, unreferenced, unservable.

---

## 8. Recommended direction for Track D — *a proposal, not a decision*

Ordered by cost. Each carries its open question.

### 8.1 Color P&L inline — separable from the restyle, but not free

Needs no framework decision and no CSS: inline `style=` is proven to reach message content.
**Cost:** more than it looks. The numbers are formatted upstream in **ibkr_core_mcp** (§4), so
this is either a text post-processing pass in claudia_ui or a change in the other repo.
**Open questions:** post-process here or fix at source? A regex over financial strings needs a
real test corpus — misclassifying a sign is worse than no color at all. Red/green only, or a
neutral for flat? And color must not be the *only* signal (accessibility).

### 8.2 Label the status dots

`pn.indicators.BooleanStatus` on 1.9.3 exposes `name` and `label` but **no `description`
parameter** (verified against the installed package), so a hover tooltip is not available on
the indicator itself. The workable options are a `pn.Row(dot, pn.pane.Markdown(label))` pair
per service, or wrapping each dot in a tooltip-capable container.
**Cost:** small. **Open question:** does `label` render in some layout context we are not
using? Worth a five-minute live check before building the Row-pair workaround.

### 8.3 Adopt `pn.extension(design=..., theme='dark')`

ClaudIA calls `pn.extension()` **nowhere**, so this is a new global call — it is also the
documented prerequisite for `notifications=True`, which the buttons research already wants.
Dark suits a trading surface used beside TradingView.
**Cost:** one call, then re-smoke every screen. **Open questions:** which design — Bootstrap,
Material, or Native? Does a global design change the already-shipped Button `color=` semantics?
Note the migration plan flagged a **Panel 2.0 / 3.0 API transition** (2.0 targeted Q2 2026,
3.0 removing legacy in 2027) — a design choice should be checked against that trajectory
before it becomes load-bearing.

### 8.4 Define one token set, shared by chat and chart

Set the eight `--design-*` variables once via `pn.extension(global_css=[':root { … }'])` — the
modern, non-deprecated path (§3.1, §3.4) — and derive the chart's up/down colors from the same
palette instead of leaving them as constants.
**Cost:** moderate. **Open question:** Panel exposes **color tokens only** — fonts and spacing
still need `stylesheets=` rules, so a "token system" here is partial by construction. Accept
that or build a thin layer above it?

### 8.5 Adopt a template for page chrome

A template gives header, sidebar, main and modal. The sidebar is the natural home for the
status dots and the chart controls, pulling non-conversation UI out of the chat column; the
modal is the right place for destructive confirms.
**Cost:** the largest single change — the layout root is rewritten and every smoke re-run.
**Open questions:** which template (`FastListTemplate` and `BootstrapTemplate` are the usual
candidates)? Does moving the chart into a sidebar/main split conflict with wanting it large?
Does a template interact badly with `ChatInterface`'s shadow DOM (untested)?

### 8.6 Resolve split-vs-tabs deliberately

Currently an undecided `pn.Row`. Recommendation: **keep side-by-side with an explicit ratio**
rather than tabs — the chart's value is being visible *while* reading ClaudIA's analysis, which
tabs destroy. **Open question:** what ratio, and what happens on a narrow window? Needs a live
look at both, not a decision on paper.

### 8.7 Style inside `ChatInterface` last

Fonts, bubble treatment, avatars — all via `stylesheets=[':host …', ':host * …']` on the
`ChatInterface` itself. **Deliberately last:** it is the only area where the 7-level shadow DOM
makes iteration slow, and everything above changes what it should look like.
**Open question:** how much of the stock chat chrome (`Rerun`/`Undo`/`Clear`, like buttons,
timestamps) should simply be turned off via §4's parameters instead of styled?

### Not recommended

- Any page-level stylesheet or global `raw_css` — proven not to reach, and deprecated (§2, §3.1).
- Keeping `claudia-logo.png` unless it is actually rendered — delete it or serve it.

---

## 9. Official sources

Scraped **2026-07-24**; each URL was fetched and returned content on that date.

| Topic | URL |
|---|---|
| Panel documentation (root) | https://panel.holoviz.org |
| How-to index | https://panel.holoviz.org/how_to/index.html |
| Styling index | https://panel.holoviz.org/how_to/styling/index.html |
| Apply CSS (`stylesheets`, `css_classes`, `raw_css` deprecation, shadow DOM) | https://panel.holoviz.org/how_to/styling/apply_css.html |
| Apply a Design (`design=`, Bootstrap/Material/Native) | https://panel.holoviz.org/how_to/styling/design.html |
| Toggling themes (`theme='dark'`, `?theme=`) | https://panel.holoviz.org/how_to/styling/themes.html |
| Customize a Design (the eight `--design-*` tokens, `global_css`) | https://panel.holoviz.org/how_to/styling/design_variables.html |
| Customize loading icon | https://panel.holoviz.org/how_to/styling/load_icon.html |
| Control visibility | https://panel.holoviz.org/how_to/styling/visibility.html |
| Templates index (9 built-ins, modal/theme/custom guides) | https://panel.holoviz.org/how_to/templates/index.html |
| `ChatInterface` component reference | https://panel.holoviz.org/reference/chat/ChatInterface.html |
| Bokeh (candlestick glyphs) | https://docs.bokeh.org |
| MDN — `navigator.clipboard.writeText` (secure context) | https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText |
| MDN — Using shadow DOM | https://developer.mozilla.org/en-US/docs/Web/Web_Components/Using_shadow_DOM |

[`docs/api-reference.md`](../api-reference.md) points here rather than duplicating this table.

---

## 10. Deferred / out of scope

Carried forward from
[`2026-07-24-candlestick-chart-pane-research.md`](2026-07-24-candlestick-chart-pane-research.md)
§5 — chart features **not** built, several of which are design decisions rather than
engineering ones:

volume subplot · MA / indicator overlays · crosshair & hover tooltips · zoom synchronization ·
multi-symbol comparison · non-STK instruments (FUT/OPT/CASH) · theme-matched candle colors
(→ §8.4) · freeform period entry.

Plus, from this document: text-to-speech audio delivery in Panel (`docs/project-status.md`
Planned Features), and any restyle of the order-proposal message itself — that surface is
safety-critical and changing it needs the order-flow review in
[`docs/order-api-reference.md`](../order-api-reference.md), not just a design pass.
