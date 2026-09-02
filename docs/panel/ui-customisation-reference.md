# ClaudIA UI customisation reference

**What the UI's adjustable settings are, where each one lives, and how to change it.** Living
document — one job: the record of the customisation track (Track D,
[`docs/project-status.md`](../project-status.md) gap #23). Phase 1 shipped 2026-09-02.

Companions: [`ui-design-reference.md`](ui-design-reference.md) is the *styling surface* Panel
offers (shadow DOM, designs, tokens, templates) and the long-form proposal; this document is
what was actually chosen and how to operate it. `panel-reference.md` is how the Panel layer
works today.

Two rules govern this doc (same as its companion):

1. **Every Panel claim is scraped and cited.** The official pages in §6 were fetched with
   Firecrawl on **2026-09-02** (site docs at that date: **panel v1.9.4**; installed: **1.9.3**,
   `pip show panel`). The 1.9.4 delta is Tabulator/ESM/OAuth fixes — nothing on chat components
   or themes (§6, releases page). Where the docs are silent, the installed source is cited by
   path and line, and the finding was executed, not read.
2. **Simple first.** Phase 1 uses Panel parameters only — no CSS, no template, no custom
   component. Anything needing more is listed in §4/§5 with its real cost, not built.

---

## 1. What phase 1 changed

The user's brief (2026-09-02): dark mode, ClaudIA's own avatar, a wider input box, no reaction
hearts, and a menu of other easy changes. Decisions taken in the same session:

| Setting | Value | Where | Env var | Revert |
|---|---|---|---|---|
| Theme default | light unless `CLAUDIA_THEME=dark` | [`panel_theme.py:79`](../../claudia/panel_theme.py#L79) `apply_session_theme`, called first thing in [`panel_app.py:1176`](../../claudia/panel_app.py#L1176) | `CLAUDIA_THEME` | unset the var |
| Theme per tab | `?theme=dark` / `?theme=default` on the URL wins over the env var | [`panel_theme.py:48`](../../claudia/panel_theme.py#L48) `resolve_theme` | — | drop the query parameter |
| ClaudIA's avatar | `claudia/assets/claudia-avatar.png`, registered in `ChatMessage.default_avatars` | [`panel_theme.py:104`](../../claudia/panel_theme.py#L104), called at import in [`panel_app.py:99`](../../claudia/panel_app.py#L99) | — | delete the file (letter fallback + one warning) |
| Human's author label | `CLAUDIA_USER_NAME`, else `User` | [`panel_theme.py:98`](../../claudia/panel_theme.py#L98); applied at [`panel_app.py:758`](../../claudia/panel_app.py#L758) | `CLAUDIA_USER_NAME` | unset the var |
| Footer buttons | Send only (Stop replaces it while a reply streams) | [`panel_app.py:760-762`](../../claudia/panel_app.py#L760-L762) `show_rerun/undo/clear=False` | — | delete the three lines |
| Input box | `ChatAreaInput`, 3 rows growing to 10, placeholder "Ask ClaudIA…", Enter sends | [`panel_app.py:759`](../../claudia/panel_app.py#L759) | — | delete `widgets=` |
| Reaction icons | off on every message | [`panel_app.py:763`](../../claudia/panel_app.py#L763) `message_params={"show_reaction_icons": False}` | — | delete the line |

### 1.1 Live smoke, 2026-09-02 (IBKR offline, TradingView not running)

Served with `CLAUDIA_THEME=dark`, opened with Playwright, captures in
`docs/panel/screenshots/2026-09-02-phase1-*.png` (git-ignored, no account data — the KPI
tiles read `—`):

| Check | Result |
|---|---|
| No URL parameter → dark (env default) | ✅ page background, bubbles, tiles, tabs all dark |
| `?theme=default` on the same process → light | ✅ |
| `?theme=dark` explicit | ✅ |
| `?theme=bogus` | ✅ one WARNING `Ignoring ?theme='bogus' — not one of default/dark`, rendered **dark** (fell back to the env default, not to light) |
| Image avatar on every `ClaudIA` message, gear kept on `System` | ✅ (smoked with a 128 px copy of the logo as the placeholder file) |
| Footer: Send only; placeholder "Ask ClaudIA…"; 3-row box | ✅ |
| No reaction icon under any bubble, incl. the action-button row | ✅ (copy icon + timestamp remain, by decision) |
| KPI `Number` tiles inherit the theme colour | ✅ |
| `Tabulator` under dark | ⚠ **light skin on a dark page** — a white block; header text legible, body empty (offline). Not changed in phase 1; `theme="midnight"` is the one-line fix in §4, to be judged with data in the table |
| Chart pane under dark | **not verified** — cache miss (`AAPL_1D_6M_2026-09-02`, IBKR offline). Source says `pn.pane.HoloViews` applies Bokeh's dark theme (§3.1); confirm at the next connected session |
| Transient: an empty bubble with a blank avatar ~1s after load | Expected — the opening-status message streaming before its text arrives; it fills in (`…-dark-initialising.png` vs `…-dark.png`) |

**Deliberately not on `pn.extension(...)`:** `theme=`. See §3.1 — a global theme would
silence the URL override. The guard is
`tests/test_panel_app.py::test_extension_call_does_not_pre_empt_the_restyle_track`.

Tests: `tests/test_panel_theme.py` (resolver precedence, invalid values, avatar registration
and fallback, oversized-file warning) and the "UI customisation phase 1" block in
`tests/test_panel_app.py` (the composed chat surface, the per-session theme call, the
import-time avatar hook). All three chat-surface tests were mutation-checked before shipping:
flipping `show_rerun` back, dropping the `apply_session_theme()` call, and moving the avatar
call above `pn.extension` each fail exactly one test.

---

## 2. How to

### 2.1 Switch the theme

```bash
# default for every session
CLAUDIA_THEME=dark          # in .env; "default" (light) when unset

# one tab, regardless of the default
open "http://localhost:8001/?theme=dark"
open "http://localhost:8001/?theme=default"
```

A bad value at either level (`?theme=bogus`, `CLAUDIA_THEME=Bogus`) is logged once and skipped,
falling through to the next level — so a typo in the URL lands on the configured default, not
on light. Values are case- and whitespace-insensitive.

**Why there is no switch in the UI.** Panel's only built-in switch is
`FastListTemplate.theme_toggle`, and it is a page reload:

```js
// panel/template/fast/js/fast_template.js:39-49 (installed 1.9.3)
function toggleLightDarkTheme(theme) { … href = updateURLParameter(href, 'theme', theme); window.location.href = href }
```

In ClaudIA a reload is a **new session**: the on-screen conversation is gone, the destroy hook
runs the session-end cleanup (report, Drive upload — `panel_app.py` `_run_session_cleanup`),
and the new session re-downloads `claudia.db` and re-initialises (`_init_session`). Offering
that as a "toggle" would be a trap. Choose the theme before starting a conversation; the URL
parameter is the per-tab override.

### 2.2 Replace ClaudIA's avatar

Drop a **small** PNG at `claudia/assets/claudia-avatar.png`. The committed file is the
user's chatbot portrait (source 1254×1254, resized to 160 px on 2026-09-02 — the smoke
earlier that day ran on a temporary copy of the logo). Panel embeds a local path as
base64 in every message model (`panel/chat/utils.py` `build_avatar_pane` →
`pn.pane.Image`), so a 1 MB file is a 1 MB message. `register_claudia_avatar` warns above
200 KB. To shrink a source image on macOS:

```bash
sips -Z 128 path/to/source.png --out claudia/assets/claudia-avatar.png
```

The registration is Panel's documented in-place update of `ChatMessage.default_avatars`
(ChatMessage reference: "You can modify, but not replace the dictionary"). Keys are matched
after `to_alpha_numeric` (`panel/chat/utils.py:23-29` — non-alphanumerics stripped,
lower-cased), so the key `"claudia"` covers `user="ClaudIA"`. This is why no send site passes
`avatar=`: [`panel_sink.py:154`](../../claudia/panel_sink.py#L154), the opening messages and the
order-proposal renders all say `user="ClaudIA"` and inherit it.

The 1.4 MB `claudia/assets/claudia-logo.png` (1254×1254, replaced 2026-09-02 with the user's
new logo) is **not** the avatar; it is reserved for a phase-2 header (§5) and must be
resized before a template embeds it.

### 2.3 Rename the human

```bash
CLAUDIA_USER_NAME=Steph     # author label on your messages; avatar is the first letter
```

Blank or unset → `User`. The value is a label only; nothing downstream (store, agent) reads it.

### 2.4 Change the input box

`ChatAreaInput` parameters (ChatAreaInput reference, §6): `rows` (default 2), `max_rows`
(default 10, with `auto_grow=True`), `placeholder`, `enter_sends` (`False` → Ctrl-Enter
sends), `resizable`. Edit the `widgets=` line at
[`panel_app.py:759`](../../claudia/panel_app.py#L759). Passing our own instance keeps
Enter-to-send because `ChatInterface` wires auto-send by widget type
(`panel/chat/interface.py:296-304`).

### 2.5 Bubble chrome and footer buttons — each one parameter

| Piece | Parameter | Where it goes |
|---|---|---|
| Reaction icons | `show_reaction_icons` | `message_params={...}` on the `ChatInterface` — forwarded to every message the feed builds (ChatFeed reference), including `ChatStep`s and proposal `Column`s |
| Copy icon | `show_copy_icon` | `message_params` |
| Timestamp | `show_timestamp`, `timestamp_format` | `message_params` |
| Author name / avatar | `show_user`, `show_avatar` | `message_params` |
| Streaming dot | `show_activity_dot` | `ChatInterface` |
| Send / Stop / Rerun / Undo / Clear | `show_send`, `show_stop`, `show_rerun`, `show_undo`, `show_clear` | `ChatInterface` |
| Button text vs icon-only | `show_button_name` (icon-only shrinks each to 45 px) | `ChatInterface` |
| Button tooltips | `show_button_tooltips` | `ChatInterface` |

`renderers` must stay a **top-level** argument — inside `message_params` it raises
(`panel_app.py`, comment above the constructor).

---

## 3. Research findings the implementation rests on

Each was executed against the installed package, not inferred from prose.

### 3.1 The theme is session-scoped when set inside the session factory

`panel/config.py` (1.9.3): `theme` is **not** in `_config._globals`, so
`pn.config.theme = x` while `state.curdoc` is set writes `_session_config[curdoc]`
(`__setattr__`, lines 436-462), and the `theme` getter (line 677) reads that slot first. The
getter's order is: session slot → global slot (`_theme_`, set by `pn.extension(theme=…)` /
`pn.config.theme` at import) → `?theme=` in `session_args` → `"default"`. **A global default
therefore beats the URL parameter**, which is why ClaudIA resolves the precedence itself and
sets the session slot. The themes how-to documents both `pn.config.theme = 'dark'` and the
`?theme=dark` URL parameter but is silent on their precedence, and makes **no claim** that a
standalone served app follows the browser's `prefers-color-scheme` — it does not.

What follows the theme automatically: the page background (`panel/_templates/base.html:32`,
`html { background-color: #121212 }` when dark), Panel widgets and the chat, the KPI `Number`
tiles (`default_color="inherit"`, `panel_dashboard.py`), and `pn.pane.HoloViews`, which applies
Bokeh's dark theme from the design (`panel/pane/holoviews.py:574-575`). What does **not**:
`Tabulator` — its themes are its own list (`panel/models/tabulator.py:26-30`; the dark skin is
`"midnight"`) and must be passed explicitly. Whether the positions/orders tables need it is
settled by looking, not by assuming (see the phase-1 smoke note in §1's status line once run).

### 3.2 Avatars

ChatMessage reference: `avatar` accepts "a single character text, an emoji, or anything
supported by `pn.pane.Image`"; `default_avatars` is a class-level dict, modify-in-place.
Installed: `build_avatar_pane` (`panel/chat/utils.py:59-81`) tries `Image(avatar)` for anything
longer than one character and falls back to `HTML` (emoji). `ChatInterface(avatar=…)` is the
**human's** avatar, and `callback_avatar` applies only to values *returned* by the callback —
ClaudIA's replies come through the sink's `chat.send(..., user="ClaudIA")`, so neither reaches
them; `default_avatars` does.

### 3.3 Footer buttons and the input row

`ChatInterface` composes the row as `Row(widget, *buttons)`
(`panel/chat/interface.py:354-361`) — there is no parameter that stacks the buttons under the
box. The row's `stylesheets` are Panel's own CDN sheet (`_stylesheets`, line 169), not the
instance's, and it is a nested shadow root; the 2026-07-22 live test proved only *inherited*
properties (font) reach that deep from a `ChatInterface` stylesheet, and `flex-direction` is
not inherited. "Buttons under the box" is therefore a probe, not a parameter (§5). Hiding
Rerun/Undo/Clear leaves the box `stretch_width` beside one 90 px Send button — the requested
width without CSS.

### 3.4 Reaction icons

`reaction_icons` defaults to `{"favorite": "heart"}` and `show_reaction_icons` toggles
rendering (ChatMessage reference). `ChatFeed.message_params` "Parameters to pass to each
ChatMessage" — verified in `test_chat_interface_phase1_surface`: messages already in the feed
carry `show_reaction_icons is False`.

### 3.5 Input widget

The default input is already `ChatAreaInput` (`panel/chat/interface.py:170,175`, placeholder
"Send a message"); the docs' statement that `widgets` "defaults to `[TextInput]`" is stale
against the installed source. `ChatAreaInput` inherits `TextAreaInput`; `value` syncs only on
send, `value_input` holds the draft (ChatAreaInput reference).

### 3.6 Templates, for phase 2

`FastListTemplate` (reference): areas `header`/`sidebar`/`main`/`right_sidebar`/`modal`;
`logo` and `favicon` accept a local file (**base64-encoded into the page** — another reason to
keep image files small); `theme` accepts `"default"`/`"dark"` and `?theme=` overrides it
"unless explicitly declared"; `theme_toggle` default `True` (the reload, §2.1);
`main_layout="card"` by default wraps `main` in a card (`""` to disable); `sidebar_width` 330;
`collapsed_sidebar`. Serving: return the template from the `pn.serve` factory exactly like a
layout (`panel/io/application.py:79` special-cases `BaseTemplate`). Static files, if the logo
is ever served rather than embedded: `pn.serve(..., static_dirs={'assets': './assets'})`; the
`/static` route is reserved (static-files how-to).

---

## 4. Menu of other easy changes — not built, costed

Cost: **S** = one parameter or line, **M** = one function plus a test, **L** = its own design.

| Change | Cost | How | Note |
|---|---|---|---|
| Label the three status dots (which is IBKR / GDrive / TV) | M | `pn.Row(dot, pn.pane.Markdown(label))` per service — `BooleanStatus` has no `description` on 1.9.3 | `ui-design-reference.md` §8.2 |
| Hide timestamp / copy icon | S | `message_params` (§2.5) | copy icon is arguably useful; decide by use |
| Icon-only footer, tooltips | S | `show_button_name=False`, `show_button_tooltips=True` | moot with Send only |
| Ctrl-Enter to send | S | `enter_sends=False` on the `ChatAreaInput` | multi-line drafts without accidental sends |
| Dark skin on the tables | S | `Tabulator(theme="midnight")` when the resolved theme is dark | thread the theme into `build_dashboard(...)`; only if the light skin is unreadable on dark |
| KPI tile sizes | S | `font_size="19pt"`, `title_size="10pt"` in `panel_dashboard.py` `Number(...)` | |
| One palette for chart + dashboard | M | `#26a69a` / `#ef5350` are duplicated in `panel_chart.py` and `panel_dashboard.py`; move to one module | prerequisite for theme-matched candles |
| Chat vs dashboard width | M | the `pn.Row` at the root has no ratio; `width`/`sizing_mode` per column | `ui-design-reference.md` §8.6 — needs a live look, not a number on paper |
| Page title | S | `pn.serve(title=...)` — already `"ClaudIA"` | favicon needs a template (§3.6) |
| A design system (Bootstrap / Material) | S to set, M to re-smoke | `pn.extension(design="material")` | changes every widget's look at once; `Button(color=)` names are design-level, verify |
| Fonts, bubble colours | L | `stylesheets=[":host …"]` on `ChatInterface` | the one area where the 7-level shadow DOM makes iteration slow (`ui-design-reference.md` §2, §8.7) |

---

## 5. Phase 2 candidates, with their real cost

1. **A page template** (`FastListTemplate`): header with logo + title, a sidebar for the status
   dots and the screenshot upload (pulling non-conversation UI out of the chat column), a
   modal for destructive confirms. Cost: the layout root becomes a template, the positional
   root tests and `_find_chat` (walks `.objects`; start it from `template.main`) are updated,
   every screen is re-smoked, and `main_layout=""` is set so the app is not wrapped in a
   card. **Its theme switch reloads the page** (§2.1) — either hide it (`theme_toggle=False`)
   or accept that a flip ends the session and say so in the UI.
2. **Buttons under the box** — a live CSS probe against the nested input row (§3.3). If it
   fails, the Panel-native alternative is our own `pn.Row` of buttons *below* the
   `ChatInterface` with `show_send=False` (Enter still sends).
3. **Saved presets** ("templates" in the user's words): a small settings file (`TOML`) the env
   vars in §1 read from, so a look can be named and switched. Only worth it once there are
   more than a handful of knobs — today there are two env vars.
4. **Screenshot gallery** — before/after captures per phase in `docs/panel/screenshots/`
   (git-ignored; register each in its README with the account-data column filled honestly).
5. **Status dot labels** (§4, first row) — the most-asked "which dot is which" gap.

---

## 6. Sources

Fetched with Firecrawl on **2026-09-02**; local copies in `.firecrawl/panel/` (git-ignored).
Site version at fetch time: panel v1.9.4.

| Topic | URL |
|---|---|
| `ChatInterface` (buttons, `widgets`, `avatar`, `button_properties`) | https://panel.holoviz.org/reference/chat/ChatInterface.html |
| `ChatMessage` (avatars, `default_avatars`, reactions, CSS class targets) | https://panel.holoviz.org/reference/chat/ChatMessage.html |
| `ChatFeed` (`message_params`, `placeholder_params`) | https://panel.holoviz.org/reference/chat/ChatFeed.html |
| `ChatAreaInput` (`rows`, `max_rows`, `auto_grow`, `enter_sends`) | https://panel.holoviz.org/reference/chat/ChatAreaInput.html |
| Toggling themes (`pn.config.theme`, `?theme=`) | https://panel.holoviz.org/how_to/styling/themes.html |
| Apply a design | https://panel.holoviz.org/how_to/styling/design.html |
| Design variables (`--design-*`, `global_css`) | https://panel.holoviz.org/how_to/styling/design_variables.html |
| Apply CSS (`stylesheets`, shadow DOM) | https://panel.holoviz.org/how_to/styling/apply_css.html |
| Customize template theme | https://panel.holoviz.org/how_to/templates/template_theme.html |
| Set a template | https://panel.holoviz.org/how_to/templates/template_set.html |
| `FastListTemplate` reference (`theme_toggle`, `logo`, `main_layout`) | https://panel.holoviz.org/reference/templates/FastListTemplate.html |
| Static files (`static_dirs`) | https://panel.holoviz.org/how_to/server/static_files.html |
| Releases (1.9.4 delta vs installed 1.9.3) | https://panel.holoviz.org/about/releases.html |

Installed-source citations (`.venv/lib/python3.11/site-packages/panel/`, 1.9.3): `config.py`
`_config.__setattr__` / `theme`; `chat/interface.py` `_init_widgets`; `chat/utils.py`
`to_alpha_numeric`, `build_avatar_pane`; `template/fast/js/fast_template.js`
`toggleLightDarkTheme`; `models/tabulator.py` `TABULATOR_THEMES`; `pane/holoviews.py`
theme application; `io/application.py` template handling; `_templates/base.html` dark
background.
