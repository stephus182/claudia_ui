# Panel component model reference — how components actually work

**The mechanics underneath every Panel component: the object taxonomy, the real class
hierarchy, the Param foundation, the four interactivity APIs and how they rank, and the four
routes to building a component of our own.** Living document — started 2026-08-01.

This doc answers a different question from its neighbours. They cover **which** components
exist and **how they look**; this one covers **how a component is built, wired and updated**,
because that is what an architecture choice is actually made of.

Companion docs in this folder:
- [`panel-reference.md`](panel-reference.md) — how ClaudIA uses Panel **today**. Read first.
- [`data-surfaces-reference.md`](data-surfaces-reference.md) — the **inventory**: which panes,
  widgets, indicators and layouts exist and what they cost. This doc does not repeat it.
- [`ui-design-reference.md`](ui-design-reference.md) — styling, the shadow-DOM constraint, the
  restyle proposal. Anything visual belongs there.

Versions described: **panel 1.9.3**, **param 2.4.1**, **bokeh 3.9.2**, Python 3.11.

---

## 0. Evidence key

Same convention as [`data-surfaces-reference.md`](data-surfaces-reference.md) §0.

| Tag | Meaning |
|---|---|
| **[S]** | **Scraped** from the official Panel docs on **2026-08-01**; URLs in §14 |
| **[P]** | **Probed** against the *installed* panel 1.9.3 in `.venv` — the source of truth for our runtime behavior |
| **[C]** | **Code** in this repo, cited `file:line` |
| **[?]** | **Unverified** — an open question or something needing a live check before it is relied on |

Where the docs describe something in prose and the installed package tells a more precise
story, **the package wins and the difference is stated**. That happens three times below
(§2, §5, §9) and each one changes an architecture decision.

---

## 1. The taxonomy — six kinds of object [S]

> "Panel is a library that provides a lot of object types and while building an app, even a
> simple one, you will create and interact with many of them."

| Kind | What it is | Holds state in |
|---|---|---|
| **Widget** | User **input**. ~50+ in `pn.widgets` | `value`, bi-directionally synced |
| **Pane** | A **wrapper around data** that knows how to render it | `object` |
| **Indicator** | Displays state; **programmatically controlled only** | `value`, one-way |
| **Layout** | Arranges other components (`Row`, `Column`, `Tabs`, `GridSpec`, …) | `objects` |
| **Template** | The **HTML document** — header / sidebar / main / modal | fixed areas |
| **Notification** | Transient "toast"; never instantiated directly | via `pn.state.notifications` |

Two API contracts are worth memorising because they hold across the whole library **[S]**:

- **Widgets *all* have `value`**, and it is bi-directionally synced (browser ↔ Python).
- **Panes *all* store their content on `object`**, and every existing view updates when it is
  reassigned.

That is why [`panel_chart.py`](../../claudia/panel_chart.py) refreshes with `chart.object = fig`
and why `Tabulator` is a *widget* rather than a pane — its DataFrame round-trips to Python
([`data-surfaces-reference.md`](data-surfaces-reference.md) §2.2) **[C]/[S]**.

---

## 2. The real class hierarchy [P]

The prose taxonomy is a teaching device. The MRO is what actually governs behavior, and it
disagrees with the prose in ways that matter. Probed against the installed package:

| Component | MRO (truncated) — the load-bearing base in **bold** |
|---|---|
| `Button` | Button → \_ButtonBase → \_ClickButton → IconMixin → TooltipMixin → **Widget** → Reactive → Syncable → Viewable |
| `Markdown` | Markdown → HTMLBasePane → ModelPane → Pane → **PaneBase** → Reactive → Syncable → Viewable → Renderable |
| `Column` | Column → ListPanel → **ListLike** → Panel → Reactive → Syncable → Viewable → Renderable → Layoutable |
| `BooleanStatus` | BooleanStatus → BooleanIndicator → Indicator → **Widget** → Reactive → Syncable → Viewable → Renderable → Layoutable |
| `Tabulator` | Tabulator → BaseTable → **ReactiveData → SyncableData** → Widget → Reactive → Syncable → Viewable |
| `ChatInterface` | ChatInterface → ChatFeed → ListPanel → **ListLike** → Panel → Reactive → Syncable → Viewable |
| `ChatMessage` | ChatMessage → Pane → **PaneBase** → Reactive → Syncable → Viewable |
| `ChatStep` | ChatStep → **Card → Column → ListPanel → ListLike** → Panel |

Four consequences, none of them visible from the taxonomy prose:

1. **An Indicator *is* a Widget.** The docs say indicators "sit in between widgets and panes"
   **[S]**; in code `BooleanStatus` inherits `Widget` directly. The difference is behavioral
   (no browser→Python edit path), not structural. Anything true of a widget's parameters is
   true of our status dots.
2. **`ChatInterface` is a *layout*, not a widget.** See §12 — this is the single most
   consequential finding in this document for ClaudIA.
3. **`ChatStep` is a `Card`, which is a `Column`.** The tool-step objects
   [`panel_sink.py`](../../claudia/panel_sink.py) creates are mutable list-like containers, not
   opaque log lines **[C]**.
4. **`Syncable` / `Reactive` is the sync machinery; `Viewable` / `Layoutable` is the display
   contract.** `SyncableData`/`ReactiveData` (the `Tabulator` layer) is what adds
   `.stream()`/`.patch()`. A component that lacks that layer has no incremental-update story —
   which is why the chart pane must reassign `object` wholesale
   ([`data-surfaces-reference.md`](data-surfaces-reference.md) §5.1) **[C]**.

---

## 3. Everything is Parameterized [S]/[P]

> "All components in Panel are built on the Param library. Each component declares a set of
> *Parameters* that control the behavior and output of the component."

Re-executed here rather than taken from the page: `pn.widgets.FloatSlider`, `pn.pane.Matplotlib`,
`pn.Column` and `pn.template.BootstrapTemplate` are all `param.Parameterized` subclasses — four
different families, all `True` **[P]**.

Param buys two things, and both are load-bearing for a trading UI:

1. **Runtime type validation** — of *type* and of *bounds*, at assignment, with a named error.
   Both executed against param 2.4.1 **[P]**:

   ```text
   param.Integer(default=10, bounds=(5, 15))
   i='bad data' → ValueError: Integer parameter 'B.i' must be an integer, not <class 'str'>.
   i=99         → ValueError: Integer parameter 'B.i' must be at most 15, not 99.
   ```

   For a trading surface this is the interesting half: a quantity or price parameter carries its
   own legal range, and the check lives with the state rather than in the widget.
2. **Watchability.** Every parameter can be watched; that is what all reactivity is built on.

### The `value` / `object` distinction that trips everyone

`text.value` is the **value** (resolved now). `text.param.value` is the **Parameter object** —
a live reference you can bind. Binding requires the object, not the value **[S]**. Everything
in §5 hinges on this.

### A pane's `object` can hold a *specification*, not a rendering [P]

Panes take `object`, widgets take `value` (§1). But what a pane's `object` *is* varies more
than the taxonomy suggests, and it changes how testable the component is.

`pn.pane.Bokeh.object` is a `bokeh.plotting.figure` — an already-rendered artifact. Asking it
what it draws means walking `figure.renderers`, matching on glyph classes and reading
`data_source` dictionaries.

`pn.pane.HoloViews.object` is a `holoviews.Layout` — a **declarative spec** that has not been
rendered yet. It answers questions about the data directly:

```python
[type(e).__name__ for e in pane.object]   # ['Overlay', 'Bars']
layout.Overlay.I.Rectangles.I.data        # the actual candle bodies
```

ClaudIA's chart pane moved from the first to the second on 2026-08-03, and the test suite got
*shorter and more direct* as a result — assertions on the data being drawn replaced assertions
on Bokeh glyph internals ([`panel-reference.md`](panel-reference.md) §10). Worth knowing when
choosing a pane for a new surface: a spec-holding pane is cheaper to test than a
figure-holding one, independent of what it renders.

⚠ **The trap that comes with it:** HoloViews containers use dynamic attribute access, so
`hasattr(overlay, "Overlay")` returns **`True`** on a bare `Overlay` and yields an *empty*
`:Overlay` rather than raising. Type-test with `isinstance(obj, hv.Layout)`; a `hasattr` check
silently resolves to an element with no data **[P]**.

### Parameters can be set at class level *and* instance level [S]

```python
pn.widgets.IntRangeSlider.width = 350     # every future instance, process-wide
pn.widgets.IntRangeSlider(width=100)      # this one only
```

⚠ The class-level form is process-wide and would leak across every browser session in our
`pn.serve` model ([`panel-reference.md`](panel-reference.md) §1). Treat it as a global mutation,
not a styling shortcut **[?]** — untested here, and there is no reason to reach for it.

### The class-variable trap [S]

```python
class P(param.Parameterized):
    x  = param.Number()                                    # Good
    w1 = pn.widgets.FloatSlider()                          # shared by ALL instances
    w2 = param.ClassSelector(class_=pn.widgets.FloatSlider) # Much better
```

For an object attribute to be Param-powered it must be declared as a *Parameter*; otherwise it
is a plain class variable **shared across every instance**. In a per-session app that is a
cross-session state leak.

---

## 4. The shared-parameter contract — the exact split [P]

The docs say widgets, indicators, panes and layouts "all share a set of Parameters" **[S]**.
Probed, the set is precisely 20, split across two base classes:

| Base class | Parameters |
|---|---|
| **`Viewable`** | `loading` — **and only `loading`** |
| **`Layoutable`** (19) | `align`, `aspect_ratio`, `css_classes`, `design`, `height`, `height_policy`, `margin`, `max_height`, `max_width`, `min_height`, `min_width`, `name`, `sizing_mode`, `styles`, `stylesheets`, `tags`, `visible`, `width`, `width_policy` |

Confirmed present in full on `Button`, `Markdown`, `Column`, `BooleanStatus` **and**
`ChatInterface` **[P]**.

**Why the split matters:** it is the test for whether a custom component behaves like a native
one. `Viewer` has *neither* set; `PyComponent` has *both* — see §9, where that difference is
proven with a `TypeError`.

Sizing semantics (`sizing_mode`: `fixed` / `stretch_width` / `stretch_height` / `stretch_both`
/ `scale_width` / `scale_height` / `scale_both`) are documented in **[S]**
`how_to/layout/size.html`. One caveat straight from that page and directly relevant to our
chart pane:

> "Unlike other components, the size of a plot component is usually determined by the
> underlying plotting library, so it may be necessary to ensure that you set the size and
> aspect when declaring the plot."

---

## 5. State syncing, and the one trap in it

Every view of a component stays in sync — set `w.value` in Python and all rendered views
update; type in the browser and `w.value` updates **[S]**.

### The mutable-value trap — reproduced [P]

> "If you programmatically update that list directly, with for example `append` or `extend`,
> Panel will not be able to detect that change." **[S]**

Executed against 1.9.3, watcher fire count in brackets:

```python
ms = pn.widgets.MultiSelect(options=list("abc"))
ms.value = ["a"]            # watcher fired  [1]  ← rebinding is detected
ms.value.append("b")        # watcher fired  [1]  ← in-place mutation is NOT
ms.param.trigger("value")   # watcher fired  [2]  ← the escape hatch
```

**This is a general rule, not a `MultiSelect` quirk.** It applies to any parameter holding a
mutable structure — most importantly `Tabulator.value`, a DataFrame that can be mutated with
`df.loc[0,'A'] = x` **[S]**. `data-surfaces-reference.md` §5.1 records the sibling case
(`Tabulator.patch()` not firing a `value` update); this is the same failure mode arriving from
the other direction, and the same fix: `param.trigger('value')`.

### Throttling — which widgets have it [P]

| Widget | Extra parameter | Semantics **[S]** |
|---|---|---|
| Sliders (`IntSlider`, `FloatSlider`, `RangeSlider`) | `value_throttled` ✅ | `value` updates continuously while dragging; `value_throttled` only on release |
| Text inputs (`TextInput`, `AutocompleteInput`) | `value_input` ✅ | `value` updates on Enter/blur; `value_input` on every keypress |
| `Select` | **none** ❌ | Discrete — nothing to throttle |

`pn.config.throttled = True` flips bound functions to the throttled parameter globally; default
is `False` **[P]**.

---

## 6. The four interactivity APIs, ranked [S]

Panel deliberately offers four, in descending order of preference:

| # | API | Form | When |
|---|---|---|---|
| 1 | **Component-level binding** | `pn.pane.Markdown(object=text.param.value)` | Default. Most efficient — updates one parameter on the wire |
| 2 | **Function-level binding** | `pn.bind(fn, w1, w2)` | When the output needs real Python logic |
| 3 | **Declarative dependency** | `@param.depends('x','y')` on a `Parameterized` | Class-based apps; derivations from owned state |
| 4 | **Watchers / callbacks** | `w.param.watch(cb,'value')`, `btn.on_click(cb)` | Lowest level. Side effects, transient events, fine-grained perf |

The efficiency claim is explicit and specific **[S]**:

> "in this simple setup with function-level binding, Panel has to re-render the corresponding
> model every time the inputs change instead of simply updating the pane `object` […] For
> complex panes and output this approach can lead to undesirable flicker."

The escape hatch is `inplace=True` — **verified present** on `ParamFunction` in 1.9.3 **[P]**
(its non-`Layoutable` params are `default_layout`, `defer_load`, `generator_mode`, `inplace`,
`lazy`, `loading`, `loading_indicator`, `object`, `_pane`):

```python
pn.param.ParamFunction(pn.bind(add, a, b), inplace=True)
```

Better still, the docs show the refactor that avoids the problem entirely — return **data** from
the bound function and use it as a *component-level* reference, rather than returning a
component **[S]**:

```python
# function-level: rebuilds the pane on every change
pn.bind(lambda a, b: pn.pane.Str(f"{a+b}"), a, b)

# component-level: updates one parameter
pn.pane.Str(object=pn.bind(lambda a, b: f"{a+b}", a, b))
```

**Five things can act as a reference** **[S]**: a `Parameter` object, a `Widget` (a proxy for
its own `value`), a `pn.bind`-bound function, a reactive expression (`pn.rx`), and an
(async) generator function — the last driving streaming output.

### Reactive expressions — present and complete [P]

`pn.rx` is `param.reactive.rx`. The `.rx` namespace on 1.9.3 exposes:
`and_`, `bool`, `buffer`, `in_`, `is_`, `is_not`, `len`, `map`, `not_`, `or_`, `pipe`,
`resolve`, `set`, `updating`, `value`, `watch`, `when`, `where`.

### The other direction — widgets *from* parameters [P]

Binding usually goes widget → function. It also runs the other way: a `Parameterized` object can
**generate its own controls**, which is what makes the §8 class-based route cheap rather than
verbose.

```python
class M(param.Parameterized):
    x = param.Integer(default=3, bounds=(0, 10))

m = M()
w = pn.widgets.IntSlider.from_param(m.param.x)   # bounds + default + label inherited
w.value = 7   # → m.x == 7        (verified)
m.x   = 2     # → w.value == 2    (verified)

pn.Param(m.param)   # → auto-generated ['StaticText', 'IntSlider']
```

Both directions of the sync were executed against 1.9.3 **[P]**. `from_param` picks up
`bounds`, `default` and the label from the Parameter declaration, so the widget cannot drift out
of range — the validation lives with the state, not with the GUI. This is the concrete mechanism
behind the docs' claim that GUI code "rarely needs to encode detailed information about the core
code" **[S]**.

---

## 7. Transient events — the documented exception ClaudIA lives in [S]

Button clicks are the awkward case for reactive code, and the docs say so plainly:

> "Certain user interactions are, by their very nature more amenable to an event-driven
> approach because they are transient, i.e. they reflect some event in time rather than a
> permanent update to some value. One very common such example are button clicks."

And the rule that separates the two worlds:

> "The key distinction is between *derivations* (outputs that are a pure function of current
> state) and *effects* (things that happen as a consequence of state changes). Declarative code
> handles derivations well; imperative code handles effects well. **Mixing them** […] leads to
> code that's hard to follow as the app grows."

### Where ClaudIA actually stands [C]

Surveyed across `claudia/`:

| API | Count |
|---|---|
| `Button.on_click(...)` | **11** — order flow ×6, action buttons ×3, chart Load, PineScript inject |
| `param.watch(...)` | **1** — the screenshot `FileInput` ([`panel_app.py:820`](../../claudia/panel_app.py#L820)) |
| `js_on_click(...)` | **1** — the clipboard write ([`panel_pinescript.py:107`](../../claudia/panel_pinescript.py#L107)) |
| `pn.bind` / `pn.depends` / `pn.rx` / `@param.depends` | **0** |
| `Viewer` / `PyComponent` / `param.Parameterized` subclass | **0** |
| `pn.extension(...)` | **0** |

**ClaudIA is 100% on API #4, the lowest level.** That is not an accident and it is not
obviously wrong: every one of those 13 call sites is a *transient event* or an *effect* —
stage an order, launch a gateway, load a chart, upload a file, write the clipboard. That is
precisely the case the docs reserve for callbacks.

**The honest reading:** the current style is defensible for what exists, and it is *not* a
template for what comes next. The moment a surface has to **derive** something from state —
a table filtered by a symbol selector, a P&L tile tracking a position, a chart following the
conversation — that is a derivation, and reaching for `on_click` + manual mutation there is the
mistake the docs warn about. The two styles are meant to coexist; the line between them is
derivation vs. effect, not old code vs. new code.

The one place callbacks are *required* rather than merely tolerated is our test suite: it drives
buttons headlessly by reading `button.param.watchers["clicks"]["value"]`
([`panel-reference.md`](panel-reference.md) §10) **[C]**. A move to `pn.bind` for any surface
would need a new headless-testing idiom **[?]** — an unmeasured cost that belongs in any such
decision.

---

## 8. Functions vs. classes — the actual architecture tradeoff [S]

Panel ships a whole page on this. Condensed, with nothing added:

**Where functions struggle — the state problem:**

> "If a table and a chart both need to reflect the current filter state, you bind both to the
> same widgets […] But each new view means repeating the same widget references. If the filter
> logic changes, you update it in every bound function separately. And because the widgets have
> no single owner, any part of the app that needs to know 'what's currently selected?' has to
> reach into the layout to find out. **State is scattered across the codebase rather than living
> in one place.**"

**What classes give:** a single source of truth — state as `param.Parameter` attributes on a
dedicated object, with `@param.depends` declaring what recomputes.

**Coupling, and why it is subtler than it looks:**

> "There is a genuine tradeoff with `pn.bind`: the GUI code knows about the domain code, but the
> domain code knows nothing about Panel." … "Classes that hold state or handle data
> transformation can be written as pure `param.Parameterized` subclasses **with no Panel
> dependency whatsoever** […] The Panel coupling is confined to the presentation layer."

**Testability** is the practical consequence — set parameters directly, assert on output, "no
widgets, no layout, no browser."

**The rule of thumb, verbatim:**

> "If you're wrapping an existing function for a notebook or a small exploratory app, `pn.bind`
> is almost certainly the right choice […] If you're building something with **multiple views
> sharing a single data source**, logic that needs to be tested in isolation, or components that
> will be reused across layouts, the class-based approach will pay off quickly."

**Why this lands squarely on ClaudIA:** "multiple views sharing a single data source" is the
exact shape of every candidate surface in
[`data-surfaces-reference.md`](data-surfaces-reference.md) §7 — a positions table, P&L tiles and
the candlestick pane all reading the same account/position state. And the no-Panel-dependency
property is the same architectural instinct that already produced the `MessageSink` seam
([`panel-reference.md`](panel-reference.md) §5) **[C]**: a `param.Parameterized` state object
would be testable in exactly the way `message_sink.py` is.

Recorded as a finding, **not a decision** — see §13.

---

## 9. Four routes to a custom component

Ordered by cost. The choice is not cosmetic: routes 1 and 2 produce something that only *looks*
like a component, routes 3 and 4 produce something that *is* one.

### Route 1 — plain functions *(what ClaudIA does today)* [C]

ClaudIA has **two distinct function idioms here, and they are not interchangeable** — worth
separating, because only the first is a candidate for the upgrades below:

| Idiom | Example | Signature | Composable into a layout tree? |
|---|---|---|---|
| **Returns a layout** | [`build_chart_pane()`](../../claudia/panel_chart.py#L110) | `-> pn.Column` | **Yes** — `panel_app` drops it straight into the session root |
| **Pushes into a feed** | [`render_order_proposal()`](../../claudia/panel_order_flow.py#L45) | `-> None`, takes `chat` and calls `chat.send(...)` | **No** — it is a side effect on an existing `ChatInterface` |

Zero ceremony, zero new concepts, and for the second idiom the imperative form is arguably
correct (it *is* an effect — see §7).

**Limits of the first idiom** — and only the first one is what routes 2–4 replace: no
parameters, no reuse contract, no `sizing_mode`/`loading`/`visible` of its own, and the internal
widgets are reachable only through closures. `build_chart_pane()` returns a `pn.Column`, so it
inherits *that* `Column`'s Layoutable parameters — but the pane as a concept has none of its
own, and callers cannot configure it.

### Route 2 — `Viewer` [S]/[P]

> "The simplest way to extend Panel is to implement a so called `Viewer` component that can wrap
> multiple existing Panel components into an easily reusable unit **that behaves like a native
> Panel component**."

Subclass `panel.viewable.Viewer`, declare `param.Parameter`s, implement `__panel__()`.

⚠ **The doc's "behaves like a native Panel component" is an overstatement, and the package
proves it** **[P]**:

```text
Viewer.__mro__       = [Viewer, Parameterized, object]
Viewer.param         = ['name']          ← that is the complete list
V(width=300)         → TypeError: V.__init__() got an unexpected keyword argument 'width'
pn.panel(viewer)     → Column            ← unwrapped; the Viewer itself is not in the tree
```

A `Viewer` has **no `width`, no `sizing_mode`, no `loading`, no `visible`, no `stylesheets`** —
none of §4's 20 shared parameters. It is a Parameterized object with a render method.

### Route 3 — `PyComponent` *(the one that actually is native)* [P]

```text
PyComponent.__mro__  = [PyComponent, Viewable, Renderable, Layoutable, Parameterized, MimeRenderMixin]
PyComponent.param    = all 19 Layoutable params + loading   ← the full §4 contract
P(width=300, loading=True)  → OK
pn.panel(pycomponent)       → P          ← stays itself in the layout tree
```

Still pure Python — same `__panel__()` pattern, no JS. Pair it with `WidgetBase` to build a
custom *widget* **[S]**.

**This Viewer-vs-PyComponent difference is not in the prose anywhere**; it was found by probing.
If a future ClaudIA component needs to be dropped into a `Row` with a `sizing_mode`, or show a
`loading` spinner, or be styled with `stylesheets=`, **`PyComponent` is the base class and
`Viewer` is not.**

### Route 4 — JS-backed components

Two generations, and the recommendation flipped **[S]**:

> "`ReactiveHTML` was the recommended approach for building custom components before so-called
> **ESM components** were added to Panel." … "**We recommend using `JSComponent` over
> `ReactiveHTML` for new custom components.**"

All present in `panel.custom` on 1.9.3 **[P]**: `PyComponent`, `JSComponent`, `ReactComponent`,
`AnyWidgetComponent`, `ReactiveHTML`, `ReactiveESM`, `WidgetBase`, `Child`, `Children`,
`DOMEvent`, `ESMEvent`.

`ReactiveHTML` in one paragraph, since it is still supported and fully documented **[S]**:
`_template` (HTML with `${...}` JS template variables — dynamically linked — and `{{...}}`
Jinja2 — literal, render-time only), `_scripts` (JS callbacks keyed by parameter name plus the
reserved lifecycle keys `render` / `after_layout` / `remove`), `_dom_events`, `_child_config`
(`model` / `literal` / `template`), `_extension_name`, and `__javascript__` / `__css__` for
external deps.

Two warnings from that page worth carrying, both about the shadow DOM
([`ui-design-reference.md`](ui-design-reference.md) §2 is the same constraint from the styling
side) **[S]**:

> "**We recommend not using Bootstrap with Panel.** You can use its CSS to style your
> components, but in our experience its javascript does not work well with Panel. It simply
> cannot select and update HTML elements inside the *shadowroot*."

Web Components (Shoelace, Fast, Material) *do* work well, as do React/Preact/Vue.

**For ClaudIA this route is almost certainly out of scope** — it adds a JS build story to a repo
that deliberately has none. Recorded for completeness and for the one case that could justify it
**[?]**: wrapping a third-party charting widget that has no Python binding.

---

## 10. `pn.panel()` and pane resolution [S]/[P]

> "`pn.panel` […] resolves the appropriate representation for an object by checking all Pane
> object types available and then ranking them by **priority**."

Every layout does this implicitly: non-Panel objects passed to a `Row`/`Column` go through
`pn.panel` **[S]**. That is why `pn.Column('# A title', slider)` works.

Resolution mechanics, probed **[P]**:

| Pane | `priority` | `applies('# t')` |
|---|---|---|
| `Markdown` | `None` | `0.1` |
| `HTML` | `None` | `None` |
| `Str` | `0` | `True` |
| `PNG` | `0.5` | `True` for `…/a.png` |

`priority = None` means **the class computes its own priority** — `applies()` returns a float
instead of a bool, and the highest wins. That is how `'# Title'` becomes a `Markdown` (0.1)
rather than a `Str` (0), and how `'https://…/x.png'` becomes a `PNG`.

Verified resolutions **[P]**: `'# Title'` → `Markdown`, `'…/x.png'` → `PNG`,
`pd.DataFrame` → `DataFrame`, `pn.bind(f, w)` → `ParamFunction`.

⚠ A bare zero-argument callable does **not** resolve to `ParamFunction`:
`pn.panel(lambda: 'x')` returns a `Column` wrapping `[Column, Row[Markdown]]`, and
`ParamFunction.applies(lambda: 'x')` returns `None` **[P]**. If a `ParamFunction` is what you
want, bind arguments to it or construct it explicitly.

**`print(component)` dumps the layout tree** with indices — the cheapest debugging tool in the
library, and one nothing in this repo currently uses. Executed here, reproducing the docs'
example exactly **[P]**:

```text
Column
    [0] Markdown(str)
    [1] FloatSlider()
    [2] Markdown(str)
    [3] TextInput()
    [4] Button(label='Click here', name='Click here')
```

Note the last line as a free confirmation of [`panel-reference.md`](panel-reference.md) §6:
`Button(label='Click here')` reports **both** `label` and `name`, because `name` is the alias
that `label` now drives — not the other way round.

---

## 11. Layout semantics — list-like vs. grid-like [S]/[P]

Two families with genuinely different APIs.

**List-like** — full Python list semantics. Probed: **all 9 classes implement all 7 methods**
(`append`, `extend`, `clear`, `insert`, `pop`, `remove`, `__setitem__`) **[P]**:

`Row` · `Column` · `Tabs` · `GridBox` · `FlexBox` · `Accordion` · `FloatPanel` · `Swipe` · `Feed`

`Tabs` and `Accordion` additionally accept `(title, component)` tuples on add/replace **[S]**.

**Grid-like** — `GridSpec` and `GridStack`: `__setitem__` yes, `append` **no** **[P]**. They
behave like a 2D array that auto-expands, addressed `[row, col]` with slice spans:

```python
gspec[0, :3]   = pn.Spacer(styles=dict(background='#FF0000'))
gspec[1:3, 1:3] = some_plot
```

**Templates are a third thing** and the difference is a real trap **[S]**:

> "These four areas behave very similarly to layouts that have list-like semantics […] Unlike
> other layout components however, **the contents of the areas is fixed once rendered.** If you
> need a dynamic layout you should therefore insert a regular layout (e.g. a `Column` or `Row`)
> and modify it in place once added to one of the content areas."

Already recorded in [`data-surfaces-reference.md`](data-surfaces-reference.md) §4.2 gotcha 11 —
repeated here because it is a *component-model* constraint, and it is the thing that would break
a naive template adoption in Track D ([`ui-design-reference.md`](ui-design-reference.md) §8.5).

---

## 12. `ChatInterface` is a mutable list — and nothing in this repo uses that [P]

From §2: ChatInterface → ChatFeed → ListPanel → **ListLike** → Panel. Probed, `ChatFeed`
implements `append`, `extend`, `clear`, `insert`, `pop`, `remove`, `__setitem__`, `__getitem__`,
`__len__`.

Executed live against 1.9.3:

```python
ci = pn.chat.ChatInterface()
ci.send("hello", user="X", respond=False)
len(ci)                                     # 1
type(ci[0])                                 # ChatMessage
ci[0] = pn.chat.ChatMessage("replaced", …)  # ci[0].object == 'replaced'
ci.pop(0)                                   # len(ci) == 0
```

**Today ClaudIA treats the chat as append-only, repo-wide** — 44 `chat.send(...)` call sites
across `claudia/` and **zero** uses of `chat[...]`, `.pop`, `.insert`, `.remove`, `.clear`,
`.extend`, `.append` or `.objects` on the feed **[C]**. The transcript is in fact an indexable,
mutable sequence of `ChatMessage` panes, each with an `object` that can be reassigned in place.

⚠ **Python-side mutation is verified [P]; that the browser re-renders correctly on
`__setitem__`/`pop` in a live session is NOT** **[?]**. It needs a live check before anything
depends on it.

Why it is worth knowing anyway: several open items in this folder are shaped like "replace what
is already on screen" rather than "append something new" — a rendered order proposal that should
show its outcome, a tool step that should collapse when superseded, a stale surface that should
be refreshed. `pn.pane.Placeholder` is already identified as the clean primitive for a
swappable slot ([`data-surfaces-reference.md`](data-surfaces-reference.md) §4.1); this says the
chat feed itself has the same capability natively.

⚠ **Safety boundary:** rewriting a message that carried an order proposal touches a
safety-critical surface. Hard Rule 1 and the order-flow review in
[`docs/order-api-reference.md`](../order-api-reference.md) govern that path — the capability
existing is not permission to use it there.

---

## 13. What this changes for ClaudIA — findings, not decisions

Nothing here has been agreed. Each item is stated with what would settle it.

1. **`PyComponent`, not `Viewer`, is the base class for any composite component we build** (§9).
   This is the clearest actionable finding: `Viewer` cannot take `width`, `sizing_mode`,
   `loading` or `stylesheets`, and the cost of discovering that after building on it is a
   rewrite. **Settled by evidence, not preference** — the `TypeError` is reproducible.
2. **The next surface is the first *derivation*, and the current callback style does not fit
   it** (§7, §8). A table or tile that reflects account state is a derivation from shared state;
   the 11 existing `on_click` handlers are all effects. **What would settle it:** deciding
   whether the state behind those surfaces gets a `param.Parameterized` home — which is also the
   §8 answer to "multiple views sharing a single data source".
3. **A `param.Parameterized` state object would extend the `MessageSink` instinct, not fight
   it** (§8). Pure-param classes carry no Panel dependency and are testable without a browser —
   the same property that makes [`message_sink.py`](../../claudia/message_sink.py) work **[C]**.
4. **`pn.bind` would break the headless button-click test idiom** (§7) **[?]**. The suite drives
   buttons through `param.watchers["clicks"]`; bound functions have no equivalent hook
   established here. Cost is real and unmeasured — it belongs in the decision, not after it.
5. **The chat transcript is editable** (§12) — with a live re-render check outstanding **[?]**
   and a safety boundary around order messages.
6. **`pn.config.notifications = True` is sufficient for toasts** — see the §15 note. A refinement,
   not a correction, to what the buttons research and
   [`ui-design-reference.md`](ui-design-reference.md) §5 already record.
7. **A spec-holding pane is cheaper to test than a figure-holding one** (§3). Demonstrated
   rather than argued: the chart pane moved `pn.pane.Bokeh` → `pn.pane.HoloViews` on
   2026-08-03 and its tests got shorter *and* stricter, because `pane.object` became the
   declarative `Layout` instead of a rendered `figure`. **Settled by evidence** for that pane;
   what it does *not* settle is whether the same holds for a `Tabulator` or `Trend`, whose
   `value`/`data` already are the data. Worth weighing when a new surface picks a component.
8. **The survey in §7 survived the chart rewrite unchanged** — re-counted 2026-08-03:
   `on_click` 11, `js_on_click` 1, `param.watch` 1, `pn.bind`/`pn.rx`/`@param.depends` 0,
   `pn.extension(...)` **0** (the one grep hit in `claudia/` is a comment, not a call). Swapping
   the chart's rendering layer changed nothing about *how* ClaudIA drives components. That is
   the point finding 2 makes: the callback style is still untested against a derivation,
   because no derivation has been built yet.

**What this document does *not* settle:** which surfaces get built, in what order, or whether
any of the class-based machinery is adopted at all. Those are
[`data-surfaces-reference.md`](data-surfaces-reference.md) §7/§9 questions, and they need the
data-source answer first.

---

## 14. Gotchas index

| # | Gotcha | Tag |
|---|---|---|
| 1 | In-place mutation of a mutable parameter value is **not detected** — `param.trigger('value')` | **[S]/[P]** |
| 2 | `Viewer` has **only** the `name` parameter — `Viewer(width=…)` raises `TypeError`. Use `PyComponent` for a native component | **[P]** |
| 3 | Non-Parameter class attributes on a `Parameterized` are **shared across all instances** — a cross-session leak in a served app | **[S]** |
| 4 | Class-level parameter assignment (`Slider.width = 350`) is **process-wide** | **[S]** |
| 5 | Function-level binding **re-renders the whole model**; prefer component-level binding, or `ParamFunction(..., inplace=True)` | **[S]/[P]** |
| 6 | `pn.panel(lambda: 'x')` does **not** produce a `ParamFunction` — bind arguments or construct it explicitly | **[P]** |
| 7 | `pane.priority = None` means `applies()` returns a **float**, not a bool | **[P]** |
| 8 | `GridSpec`/`GridStack` have **no** `append` — 2D assignment only | **[P]** |
| 9 | Template areas are **fixed once rendered** — nest a mutable layout (also `data-surfaces` §4.2) | **[S]** |
| 10 | `Select` has **no** `value_throttled`; only sliders and text inputs have throttling params | **[P]** |
| 11 | Bootstrap's **JavaScript** does not work with Panel — it cannot reach into the shadow root. Its CSS is fine | **[S]** |
| 12 | `ChatFeed` list mutation is verified **Python-side only**; live browser re-render untested | **[P]/[?]** |
| 13 | An **Indicator is a Widget** in the MRO — widget parameter facts apply to our status dots | **[P]** |
| 14 | `hasattr(overlay, "Overlay")` is **`True`** on a bare HoloViews `Overlay` and returns an *empty* element — type-test with `isinstance(obj, hv.Layout)`, never `hasattr` (§3) | **[P]** |
| 15 | Bokeh's `Model.select()` returns a **generator** (annotated `Iterable[Model]`) — `len()` on it raises `TypeError`; wrap in `list(...)` | **[P]** |
| 16 | `pn.pane.HoloViews.linked_axes` does **not** link axes within one `Layout` — that is holoviews' own `Layout.shared_axes` (default `True`), which applies with no Panel pane involved. `linked_axes` links across *separate panes* in a Panel layout | **[P]** |
| 17 | A served Panel app delivers its document over **WebSocket** — the initial `GET` returns a shell with no `docs_json`, so grepping the HTML proves nothing. Pull the session (`bokeh.client.pull_session`) or query `window.Bokeh.documents` | **[P]** |

---

## 15. Sources

Scraped **2026-08-01** via Firecrawl (keyless tier); each URL returned content on that date.
Complements — does not duplicate — the URL indexes in
[`ui-design-reference.md`](ui-design-reference.md) §9 (styling/templates) and
[`data-surfaces-reference.md`](data-surfaces-reference.md) §10 (component gallery, streaming,
server).

| Topic | URL |
|---|---|
| **Components Overview** — the taxonomy, `value`/`object`, throttling, shared params, templates, notifications | https://panel.holoviz.org/explanation/components/components_overview.html |
| Reactivity in Panel — reactive vs event-driven, component- vs function-level binding, references, `Skip`, `.rx.when` | https://panel.holoviz.org/explanation/api/reactivity.html |
| Classes vs functions — the state problem, coupling, derivations vs effects | https://panel.holoviz.org/explanation/api/functions_vs_classes.html |
| Reactive API (`pn.bind`, `@pn.depends`) — pros/cons | https://panel.holoviz.org/explanation/api/reactive.html |
| Declarative API (`param.Parameterized`, `@param.depends`) — pros/cons | https://panel.holoviz.org/explanation/api/parameterized.html |
| Callback API — pros/cons | https://panel.holoviz.org/explanation/api/callbacks.html |
| Panel and Param — validation, watchers, the class-variable trap | https://panel.holoviz.org/explanation/dependencies/param.html |
| Build Custom Components (index) — Viewer/PyComponent, ESM, ReactiveHTML | https://panel.holoviz.org/how_to/custom_components/index.html |
| Combine Existing Components (`Viewer`, `__panel__`) | https://panel.holoviz.org/how_to/custom_components/custom_viewer.html |
| Building `ReactiveHTML` Components — `_template`, `_scripts`, `_dom_events`, Bootstrap warning | https://panel.holoviz.org/explanation/components/reactive_html_components.html |
| Designs and Theming — `Design`, modifiers, `Theme` | https://panel.holoviz.org/explanation/styling/design.html |
| Control Size — `sizing_mode`, absolute vs responsive | https://panel.holoviz.org/how_to/layout/size.html |
| Improve the Performance — reuse sessions, throttling, `hold` | https://panel.holoviz.org/how_to/performance/index.html |
| Communication Channels — which comm layer serves which context | https://panel.holoviz.org/explanation/architecture/comms.html |
| Param (upstream) | https://param.holoviz.org/ |

**Note on the notifications gate** — reading the installed source of
`panel.io.state.state.notifications` **[P]**: inside a session the property returns `None` unless
`config.notifications` is truthy, and `pn.config.notifications = True` alone is enough to get a
`NotificationArea` (`pn.extension(notifications=True)` sets that same config flag). Calling a
method on the `None` gives `AttributeError: 'NoneType' object has no attribute 'success'` **[P]**.
The existing wording in
[`2026-07-24-pinescript-and-actionable-buttons-research.md`](2026-07-24-pinescript-and-actionable-buttons-research.md)
§B2 and [`ui-design-reference.md`](ui-design-reference.md) §5 is consistent with this.

**Installed-package probes [P]** were run against `.venv` (panel 1.9.3, param 2.4.1) in this
session and cover: the MRO of six component families; `Viewable`/`Layoutable` parameter sets;
`pn.bind`/`pn.rx`/`pn.depends`/`pn.param.Skip`/`ParamFunction.inplace`; the `.rx` namespace;
`panel.custom` exports and the `Viewer` vs `PyComponent` parameter difference (including the
`TypeError`); throttling parameters and `pn.config.throttled`; list-like vs grid-like layout
methods across 11 classes; `pn.panel` resolution and pane priorities; `ChatFeed` list mutation;
the mutable-value watcher trap; `Widget.from_param` bidirectional sync; and the
`pn.state.notifications` source. They are ad-hoc session probes, **not** committed under
`docs/probes/` — anything that becomes load-bearing should be promoted there per that folder's
convention.
