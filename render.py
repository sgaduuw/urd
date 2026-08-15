"""SVG primitives for urd's report.

This module knows nothing about Jira or DuckDB: every function takes rows as
a plain list[dict] and column names as strings, so a chart's SQL column
names flow straight through with no adapter. That isolation is the point:
this is the one file in the project a reader can understand without knowing
anything about the domain it happens to be charting.

Charts follow the `dataviz` skill: a fixed-order categorical palette (never
cycled, never picked by rank), thin marks, recessive gridlines, a legend for
two or more series, and both a light and a dark theme defined explicitly
(never inherited) since the report is a static file opened directly in a
browser.
"""

import decimal
import html
import math

# Reference categorical palette (see the `dataviz` skill, references/palette.md).
# Fixed hue order is the CVD-safety mechanism: within one chart, slot N always
# means the same series. `_slot` below does wrap past 8 (see its own docstring
# for why that's an accepted ceiling here, not a claim that it never happens).
# This list also drives CSS's --sN tokens (see CSS below), so it is the one
# place a colour is spelled out; nothing else hand-copies these hex values.
PALETTE = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

# Dark-mode steps for the same 8 hue families (see the dataviz skill's
# palette.md). Kept as a private constant rather than derived from PALETTE:
# the two modes are genuinely different hex values (stepped for the dark
# surface), not a computed transform of the light ones.
_PALETTE_DARK = [
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
]

# Roles used for chart chrome (gridlines, axis rules, tick/label text), as
# opposed to data marks. Same roles as PALETTE but never used for identity.
PALETTE_MUTED = {
    "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
    "text": "#898781",
}

# One CSS custom property per PALETTE slot, plus the chart chrome tokens.
# Light values on bare :root; only what changes in dark mode is redefined
# under the media query, per the dataviz skill's theme guidance. Marks are
# painted with var(--sN) rather than a literal hex so a saved report keeps
# switching between light and dark on its own, with no JS involved.
#
# The --sN blocks are generated from PALETTE / _PALETTE_DARK rather than
# hand-copied a second time: a hand-maintained duplicate is exactly how a
# palette fix can land in the list that's tested while the CSS that actually
# paints every mark keeps the old (or a duplicated) value.
def _token_block(colors, indent):
    return "\n".join(f"{indent}--s{i + 1}: {c};" for i, c in enumerate(colors))


# Template with two placeholders rather than an f-string: the rest of this
# block is plain CSS full of literal `{`/`}`, and a placeholder + .replace()
# avoids having to double every one of them.
_CSS_TEMPLATE = """
:root {
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10);
__LIGHT_TOKENS__
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page: #0d0d0d;
    --surface: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255, 255, 255, 0.10);
__DARK_TOKENS__
  }
}

body {
  background: var(--page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}

svg.chart {
  width: 100%;
  max-width: 480px;
  height: auto;
  display: block;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
}

.grid { stroke: var(--grid); stroke-width: 1; }
.axis-line { stroke: var(--baseline); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; }
.value-label { fill: var(--text-secondary); font-size: 11px; }
.legend-label { fill: var(--text-secondary); font-size: 11px; }
.facet-title { fill: var(--text-secondary); font-size: 10px; }
.guide-label { fill: var(--muted); font-size: 11px; }
.guide-line { stroke-width: 1; }

table.urd {
  border-collapse: collapse;
  background: var(--surface);
  color: var(--text-primary);
  font-size: 13px;
}
table.urd th, table.urd td {
  border: 1px solid var(--border);
  padding: 4px 8px;
  text-align: left;
  font-variant-numeric: tabular-nums;
}
table.urd th {
  color: var(--text-secondary);
  font-weight: 600;
}
td.shaded { position: relative; }
td.shaded .cell-shade { position: absolute; inset: 0; width: 100%; height: 100%; }
td.shaded span { position: relative; }

p.empty { color: var(--muted); font-style: italic; }
"""

CSS = (
    _CSS_TEMPLATE
    .replace("__LIGHT_TOKENS__", _token_block(PALETTE, "  "))
    .replace("__DARK_TOKENS__", _token_block(_PALETTE_DARK, "    "))
    .strip()
)


def esc(text):
    """Escape a value for interpolation into SVG/HTML markup and attributes.

    Row values, column names and labels come from outside this module and
    routinely contain &, <, >, and quotes. Every primitive here runs every
    interpolated string through this before it lands in markup; skipping it
    for even one field turns a chart into a broken XML document rather than
    a slightly wrong one.
    """
    return html.escape(str(text), quote=True)


def svg(width, height, body):
    """Wrap body in a viewBox'd <svg>.

    No fixed width/height attribute: sizing comes from the viewBox plus the
    CSS on svg.chart, so the chart scales down (never crops) in a narrow
    window. role="img" plus a <title> as the first element of `body` (each
    primitive supplies one) gives the element its accessible name; there is
    no separate aria-label parameter to keep this signature exactly the one
    the brief specifies.
    """
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" '
        f'role="img" class="chart">{body}</svg>'
    )


def _num(v):
    """Coerce a row value to float for chart math, or None if it isn't
    usable as a number.

    This is the one place numeric-ness is decided; every primitive filters
    and reads values through it so they all agree. DuckDB returns
    decimal.Decimal for ROUND()/SUM() over a decimal column, and treating
    only int/float as numeric (an earlier version of this module did) made
    `bars` raise TypeError on a Decimal, `lines` silently drop every point
    (which looks like an empty chart, not an error), and `scatter` fall back
    to treating a numeric column as categorical labels. A NaN/inf float is
    treated as missing too, the same as None, rather than reaching a path
    or rect coordinate. bool is a subclass of int in Python but is excluded:
    a True/False column is categorical, not a number.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def _fmt_num(v):
    if isinstance(v, float) and not math.isfinite(v):
        return "0"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def _nice_ticks(lo, hi, count=4):
    """A handful of evenly spaced values between lo and hi, inclusive.

    ponytail: even spacing rather than a "nice round number" algorithm
    (multiples of 1/2/5/10, as d3's tick generator does). Fine for a
    dashboard of small aggregates; upgrade path is a proper nice-number
    step if the report ever needs presentation-grade axis ticks.
    """
    if hi <= lo:
        return [lo]
    step = (hi - lo) / count
    return [lo + step * i for i in range(count + 1)]


def _slot(index):
    """The CSS var for categorical slot `index` (0-based), cycling past 8.

    ponytail: real dashboards here plausibly plot at most a handful of
    statuses or assignees, so this doesn't implement the dataviz skill's
    "fold the 9th series into Other" rule; a caller with more than 8 series
    should collapse the tail itself before calling. Cycling silently past 8
    is exactly the anti-pattern the skill warns about, so keep it out of
    real reports rather than fixing it here.
    """
    return f"var(--s{index % len(PALETTE) + 1})"


def _y_gridlines(values, left, right, top, bottom, plot_h, zero_floor):
    """Shared y-axis: hairline gridlines, tick labels, and the sy scale.

    Used by `axes` (for line/scatter charts) and directly by the categorical
    band charts (bars, stacked), which lay out x differently but need the
    same honest y-scale. A zero-width domain, be that a single value or
    every value equal, is widened symmetrically around that value so sy
    never divides by zero and a flat series doesn't render pinned to the
    baseline.
    """
    y_lo = min(values) if values else 0
    y_hi = max(values) if values else 1
    if zero_floor:
        # Bars/stacked bars must grow from zero or their length stops being
        # an honest read of magnitude, even when every value is positive.
        y_lo = min(y_lo, 0)
        y_hi = max(y_hi, 0)
    if y_hi == y_lo:
        y_lo -= 0.5
        y_hi += 0.5
    y_span = y_hi - y_lo

    def sy(v, _lo=y_lo, _span=y_span):
        n = _num(v)
        return bottom if n is None else bottom - (n - _lo) / _span * plot_h

    parts = []
    for t in _nice_ticks(y_lo, y_hi):
        yp = sy(t)
        parts.append(f'<line x1="{left}" y1="{yp:.1f}" x2="{right}" y2="{yp:.1f}" class="grid" />')
        parts.append(
            f'<text x="{left - 6}" y="{yp + 3:.1f}" class="tick" text-anchor="end">'
            f"{esc(_fmt_num(t))}</text>"
        )
    parts.append(f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis-line" />')
    return "".join(parts), sy


def axes(rows, x, y, width, height, zero_floor=False):
    """Plot frame for a point/linear chart (used by `lines` and `scatter`).

    Returns (markup, sx, sy): markup is the gridlines/axis-line/tick-label
    SVG fragment, sx and sy map a value from columns `x` and `y` to pixel
    coordinates. `y` may be one column name or a list of them, so a
    multi-series line chart can share one y-scale across every series
    instead of each line inventing its own.

    x is a continuous linear scale when every value under `x` is numeric,
    otherwise an ordinal scale that places each distinct value (in
    first-seen order) at an even interval, which is what lets a week-start
    date string or a status name serve as x with no adapter. Either scale
    widens a zero-width domain so it never divides by zero (see
    `_y_gridlines` for the y side).
    """
    pad_l, pad_r, pad_t, pad_b = 46, 14, 12, 26
    plot_w = max(width - pad_l - pad_r, 1)
    plot_h = max(height - pad_t - pad_b, 1)
    left, right = pad_l, pad_l + plot_w
    top, bottom = pad_t, pad_t + plot_h

    if not rows:
        return "", (lambda v: left), (lambda v: bottom)

    y_cols = y if isinstance(y, (list, tuple)) else [y]
    xs = [r.get(x) for r in rows if r.get(x) is not None]
    ys = [n for r in rows for c in y_cols for n in (_num(r.get(c)),) if n is not None]

    y_markup, sy = _y_gridlines(ys, left, right, top, bottom, plot_h, zero_floor)

    x_nums = [_num(v) for v in xs]
    numeric_x = bool(xs) and all(n is not None for n in x_nums)
    parts = [y_markup]

    if numeric_x:
        x_lo, x_hi = min(x_nums), max(x_nums)
        if x_hi == x_lo:
            x_lo -= 0.5
            x_hi += 0.5
        x_span = x_hi - x_lo

        def sx(v, _lo=x_lo, _span=x_span):
            n = _num(v)
            return left if n is None else left + (n - _lo) / _span * plot_w

        tick_items = [(sx(t), _fmt_num(t)) for t in _nice_ticks(x_lo, x_hi)]
    else:
        cats = list(dict.fromkeys(xs))
        n = len(cats)

        def sx(v, _cats=cats, _n=n):
            if v not in _cats:
                return left
            if _n <= 1:
                return left + plot_w / 2
            return left + _cats.index(v) / (_n - 1) * plot_w

        tick_items = [(sx(c), str(c)) for c in cats]

    for xp, label in tick_items:
        parts.append(
            f'<text x="{xp:.1f}" y="{bottom + 16}" class="tick" text-anchor="middle">'
            f"{esc(label)}</text>"
        )

    return "".join(parts), sx, sy


def _legend(names, y0, width, x0=46, row_h=18):
    """A colour-key + label per series, wrapped onto additional rows so it
    never runs past `width` and gets cropped by the viewBox with nothing to
    show it happened. Returns (markup, height): height is how many pixels
    the (possibly multi-row) legend occupies, so the caller can grow the
    chart to fit it rather than guess one fixed row of space.

    dataviz: a legend is always present for two or more series (the
    dependable identity channel, worst to lose for `stacked`, whose bands
    have no other one); a single series needs no legend box since the
    chart's own title already says what's plotted, so callers only invoke
    this when len(names) > 1.
    """
    rows, current, cx = [], [], x0
    for i, name in enumerate(names):
        # ponytail: advances the cursor by a rough characters-per-label
        # estimate instead of measured text width (no renderer available
        # offline). At this size the only consequence is wrapping a little
        # earlier or later than a real font would; entries themselves
        # always render (that's the wrap's job), never run off the edge.
        w = 18 + 7 * len(str(name)) + 16
        if current and cx + w > width - 10:
            rows.append(current)
            current, cx = [], x0
        current.append((i, name, cx))
        cx += w
    if current:
        rows.append(current)

    parts = []
    for ri, row in enumerate(rows):
        y = y0 + ri * row_h
        for i, name, cx in row:
            color = _slot(i)
            parts.append(
                f'<line x1="{cx}" y1="{y}" x2="{cx + 14}" y2="{y}" stroke="{color}" '
                f'stroke-width="3" stroke-linecap="round" />'
            )
            parts.append(f'<text x="{cx + 18}" y="{y + 4}" class="legend-label">{esc(name)}</text>')
    return "".join(parts), len(rows) * row_h + 4


def bars(rows, labels, series):
    """Grouped bar chart: one categorical band per row (named by `labels`),
    one bar per name in `series` within that band, each series in its own
    PALETTE slot shared with every other chart. Bars always grow from a
    zero baseline (`_y_gridlines(..., zero_floor=True)`), which is what
    keeps bar length an honest read of magnitude even when every value in
    the data happens to be positive.
    """
    if not rows:
        return '<p class="empty">no data</p>'

    base_h = 220
    width = 480
    legend, legend_h = _legend(series, base_h + 4, width) if len(series) > 1 else ("", 0)
    height = base_h + legend_h
    pad_l, pad_r, pad_t, pad_b = 46, 14, 12, 26
    plot_w = max(width - pad_l - pad_r, 1)
    plot_h = max(base_h - pad_t - pad_b, 1)
    left, bottom = pad_l, pad_t + plot_h

    values = [n for row in rows for s in series for n in (_num(row.get(s)),) if n is not None]
    y_markup, sy = _y_gridlines(values, left, left + plot_w, pad_t, bottom, plot_h, zero_floor=True)

    n = len(rows)
    band_w = plot_w / n
    gap = 2  # dataviz: 2px surface gap between adjacent bars
    m = len(series)
    bar_w = max(min((band_w - gap * (m + 1)) / m, 24), 1)  # 24px mark-spec cap

    parts = [y_markup]
    for i, row in enumerate(rows):
        band_x = left + i * band_w
        for j, s in enumerate(series):
            v = _num(row.get(s))
            v = 0 if v is None else v
            y0, y1 = sy(0), sy(v)
            top_y, h = (y1, y0 - y1) if v >= 0 else (y0, y1 - y0)
            bx = band_x + gap + j * (bar_w + gap)
            color = _slot(j)
            # ponytail: rx rounds all four corners rather than just the data
            # end (mark spec: "4px rounded data-end, square at the baseline").
            # A few hundred small bars don't earn a custom rounded-top path;
            # upgrade to one if pixel-exact corners are ever required.
            parts.append(
                f'<rect x="{bx:.1f}" y="{top_y:.1f}" width="{bar_w:.1f}" '
                f'height="{max(h, 0):.1f}" rx="4" fill="{color}">'
                f"<title>{esc(s)}: {esc(_fmt_num(v))}</title></rect>"
            )
            if h > 12:  # only label a bar tall enough for the text to fit
                parts.append(
                    f'<text x="{bx + bar_w / 2:.1f}" y="{top_y - 4:.1f}" class="value-label" '
                    f'text-anchor="middle">{esc(_fmt_num(v))}</text>'
                )
        label = row.get(labels)
        parts.append(
            f'<text x="{band_x + band_w / 2:.1f}" y="{bottom + 16}" class="tick" '
            f'text-anchor="middle">{esc(label)}</text>'
        )

    title = f'<title>{esc(", ".join(series))} by {esc(labels)}</title>'
    return svg(width, height, title + "".join(parts) + legend)


def lines(rows, x, series):
    """Multi-series line chart sharing one x axis and one y-scale across
    every series in `series`, so their magnitudes compare honestly. Each
    series gets the next PALETTE slot and an end-marker; a legend appears
    only once there are two or more series to tell apart.
    """
    if not rows:
        return '<p class="empty">no data</p>'

    base_h = 220
    width = 480
    legend, legend_h = _legend(series, base_h + 4, width) if len(series) > 1 else ("", 0)
    height = base_h + legend_h
    grid_markup, sx, sy = axes(rows, x, series, width, base_h)

    parts = [grid_markup]
    for j, s in enumerate(series):
        pts, last_v = [], None
        for r in rows:
            n = _num(r.get(s))
            if n is None:
                continue
            pts.append((sx(r.get(x)), sy(n)))
            last_v = n
        if not pts:
            continue
        color = _slot(j)
        d = " ".join(f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}' for i, (px, py) in enumerate(pts))
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">'
            f"<title>{esc(s)}: {esc(_fmt_num(last_v))}</title></path>"
        )
        lx, ly = pts[-1]
        parts.append(
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="{color}" '
            f'stroke="var(--surface)" stroke-width="2" />'
        )
        # dataviz relief rule: a contrast WARN on a categorical slot obliges
        # visible direct labels or the table view. Every series gets its
        # endpoint value labelled (not just the line drawn), so a low-
        # contrast slot at 3+ series still has its magnitude readable
        # without relying on colour or a hover state.
        parts.append(
            f'<text x="{lx + 6:.1f}" y="{ly + 4:.1f}" class="value-label">'
            f"{esc(_fmt_num(last_v))}</text>"
        )

    title = f'<title>{esc(", ".join(series))} over {esc(x)}</title>'
    return svg(width, height, title + "".join(parts) + legend)


def stacked(rows, x, band, value):
    """Stacked bar chart from long-format rows: one bar per distinct `x`,
    split into segments named by `band`, each segment's height proportional
    to `value`. A band's colour comes from its first-seen order in *this*
    dataset (dataviz: colour must follow the entity, never its position in
    a given stack); if a later call filters the same band set differently,
    the caller is responsible for keeping bands in the same relative order,
    since a band absent from a filtered dataset can't hold a slot for it.
    """
    if not rows:
        return '<p class="empty">no data</p>'

    band_order = list(dict.fromkeys(r.get(band) for r in rows))
    x_order = list(dict.fromkeys(r.get(x) for r in rows))
    color_of = {b: _slot(i) for i, b in enumerate(band_order)}

    totals = {}
    for r in rows:
        n = _num(r.get(value)) or 0
        totals[r.get(x)] = totals.get(r.get(x), 0) + n
    v_max = max(totals.values()) if totals else 0

    base_h = 220
    width = 480
    legend, legend_h = _legend(band_order, base_h + 4, width) if len(band_order) > 1 else ("", 0)
    height = base_h + legend_h
    pad_l, pad_r, pad_t, pad_b = 46, 14, 12, 26
    plot_w = max(width - pad_l - pad_r, 1)
    plot_h = max(base_h - pad_t - pad_b, 1)
    left, bottom = pad_l, pad_t + plot_h

    y_markup, sy = _y_gridlines(
        [0, v_max], left, left + plot_w, pad_t, bottom, plot_h, zero_floor=True
    )

    n = len(x_order)
    band_w = plot_w / n
    gap = 2  # dataviz: 2px surface gap, both between bars and between segments
    bar_w = max(band_w - gap * 2, 1)

    by_x = {}
    for r in rows:
        by_x.setdefault(r.get(x), []).append(r)

    parts = [y_markup]
    for i, xv in enumerate(x_order):
        band_x = left + i * band_w + gap
        running = 0
        segs = {r.get(band): (_num(r.get(value)) or 0) for r in by_x[xv]}
        seg_idx = 0
        for b in band_order:
            v = segs.get(b, 0)
            if v <= 0:
                continue
            y0, y1 = sy(running), sy(running + v)
            if seg_idx > 0:
                y0 -= gap  # open a surface gap below every segment but the first
            parts.append(
                f'<rect x="{band_x:.1f}" y="{y1:.1f}" width="{bar_w:.1f}" '
                f'height="{max(y0 - y1, 0):.1f}" fill="{color_of[b]}">'
                f"<title>{esc(b)}: {esc(_fmt_num(v))}</title></rect>"
            )
            running += v
            seg_idx += 1
        if running > 0:
            # dataviz relief rule: a stacked segment has no free end to
            # label (an interior segment's value belongs in the legend or
            # tooltip, not squeezed inside it), but the bar's total does
            # have one, so label that directly rather than leaving every
            # bar's magnitude readable only via colour or a hover state.
            parts.append(
                f'<text x="{band_x + bar_w / 2:.1f}" y="{sy(running) - 4:.1f}" '
                f'class="value-label" text-anchor="middle">{esc(_fmt_num(running))}</text>'
            )
        parts.append(
            f'<text x="{band_x + bar_w / 2:.1f}" y="{bottom + 16}" class="tick" '
            f'text-anchor="middle">{esc(xv)}</text>'
        )

    title = f'<title>{esc(value)} by {esc(band)}, over {esc(x)}</title>'
    return svg(width, height, title + "".join(parts) + legend)


def scatter(rows, x, y, guides=()):
    """Scatter plot of two continuous columns, with optional horizontal
    reference lines. `guides` is a sequence of (label, value) pairs; each
    draws a dashed line at that y-value with its label at the right edge of
    the plot, e.g. a p50/p85 cycle-time marker.

    One PALETTE slot for every point: a scatter with per-point category
    colours would run into the dataviz skill's all-pairs colour cap (three
    series, since any two points can end up adjacent), which this
    signature has no series argument to support.
    """
    if not rows:
        return '<p class="empty">no data</p>'

    width, height = 480, 220
    grid_markup, sx, sy = axes(rows, x, y, width, height)

    parts = [grid_markup]
    for r in rows:
        vx, vy = _num(r.get(x)), _num(r.get(y))
        if vx is None or vy is None:
            continue
        cx, cy = sx(vx), sy(vy)
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="var(--s1)" '
            f'stroke="var(--surface)" stroke-width="2">'
            f"<title>{esc(x)} {esc(_fmt_num(vx))}, {esc(y)} {esc(_fmt_num(vy))}</title></circle>"
        )

    pad_r = 14
    for label, value in guides:
        gy = sy(value)
        parts.append(
            f'<line x1="46" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
            f'class="guide-line" stroke="var(--baseline)" stroke-dasharray="4 3" />'
        )
        parts.append(
            f'<text x="{width - pad_r - 4}" y="{gy - 4:.1f}" class="guide-label" '
            f'text-anchor="end">{esc(label)}</text>'
        )

    title = f'<title>{esc(y)} versus {esc(x)}</title>'
    return svg(width, height, title + "".join(parts))


def table(rows, headers, shade=None):
    """An HTML table. When `shade` names a numeric column, that column's
    cells get a background wash (an inline SVG rect, so its opacity is
    independent of the text sitting in front of it) whose opacity is
    proportional to value / max(values): the largest cell in the column
    renders at full strength, the rest scaled down, zero renders unshaded.
    """
    if not rows:
        return '<p class="empty">no data</p>'

    shade_max = 0
    if shade:
        shade_values = [n for r in rows for n in (_num(r.get(shade)),) if n is not None]
        shade_max = max(shade_values) if shade_values else 0

    thead = "".join(f"<th>{esc(h)}</th>" for h in headers)

    body_rows = []
    for r in rows:
        cells = []
        for h in headers:
            v = r.get(h)
            cell_text = esc("" if v is None else v)
            shade_v = _num(v) if shade and h == shade else None
            if shade_v is not None and shade_max:
                opacity = max(shade_v, 0) / shade_max
                cells.append(
                    '<td class="shaded"><svg class="cell-shade" viewBox="0 0 1 1" '
                    f'preserveAspectRatio="none" aria-hidden="true">'
                    f'<rect width="1" height="1" fill="var(--s1)" fill-opacity="{opacity:.2f}" />'
                    f"</svg><span>{cell_text}</span></td>"
                )
            else:
                cells.append(f"<td>{cell_text}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    return (
        f'<table class="urd"><thead><tr>{thead}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table>'
    )


def small_multiples(groups, x, y):
    """Facet `y` over `x` into one small line chart per group, all sharing a
    single y-scale AND a single x-category list, so both position and
    magnitude compare honestly across facets. `groups` is a mapping, or a
    sequence of (title, rows) pairs, of facet title to that facet's rows.

    Sharing only the y-scale (an earlier version of this function did) still
    misleads when facets don't all have rows for the same categories, e.g.
    one person per week with no gap-filling: an independent x per facet
    puts a different week under "column 1" in every panel, so the columns
    can't be compared even though the y-axis now agrees. Categories are
    collected across every facet up front (same first-seen-order approach as
    the shared y-max), and a facet missing a category breaks its line there
    rather than joining straight across the gap, which would draw a silent
    stretch as though output had been steady through it.
    """
    items = list(groups.items()) if hasattr(groups, "items") else list(groups)
    if not items:
        return '<p class="empty">no data</p>'

    values = [
        n for _, facet_rows in items for r in facet_rows
        for n in (_num(r.get(y)),) if n is not None
    ]
    shared_max = max(values) if values else 0
    if shared_max <= 0:
        shared_max = 1

    # Sorted, not first-seen: first-seen across concatenated facets is just
    # facet iteration order (whichever facet happens to come first contributes
    # its categories first), not a real timeline. A facet missing a middle
    # category (the whole point of this function) would then jumble the
    # shared axis, e.g. w1, w3, w2 instead of w1, w2, w3. x here is always a
    # naturally-ordered value in a small-multiples facet grid (a date or week
    # string), so sorting is the axis callers actually want.
    shared_cats = sorted({r.get(x) for _, facet_rows in items for r in facet_rows})
    n_cats = len(shared_cats)
    cat_index = {c: i for i, c in enumerate(shared_cats)}

    cols = 3
    fw, fh = 150, 90
    pad = 10
    plot_l, plot_r = pad, fw - pad
    plot_t, plot_b = 18, fh - pad
    rows_of_facets = math.ceil(len(items) / cols)
    width, height = cols * fw, rows_of_facets * fh

    def fx(cat):
        i = cat_index.get(cat)
        if i is None:
            return None
        if n_cats <= 1:
            return plot_l + (plot_r - plot_l) / 2
        return plot_l + i / (n_cats - 1) * (plot_r - plot_l)

    def fy(v):
        return plot_b - (max(v, 0) / shared_max) * (plot_b - plot_t)

    parts = []
    for idx, (title, facet_rows) in enumerate(items):
        col, row = idx % cols, idx // cols
        ox, oy = col * fw, row * fh
        g = [f'<g transform="translate({ox},{oy})">']
        g.append(
            f'<text x="{fw / 2}" y="12" class="facet-title" text-anchor="middle">'
            f"{esc(title)}</text>"
        )

        if not facet_rows:
            g.append(
                f'<text x="{fw / 2}" y="{fh / 2}" class="tick" text-anchor="middle">'
                f"no data</text>"
            )
        else:
            facet_vals = {}
            for r in facet_rows:
                n = _num(r.get(y))
                if n is not None:
                    facet_vals[r.get(x)] = n

            segments, current = [], []
            for cat in shared_cats:
                if cat in facet_vals:
                    current.append((fx(cat), fy(facet_vals[cat])))
                elif current:
                    segments.append(current)
                    current = []
            if current:
                segments.append(current)

            for seg in segments:
                if len(seg) >= 2:
                    d = " ".join(
                        f'{"M" if i == 0 else "L"}{px:.1f},{py:.1f}'
                        for i, (px, py) in enumerate(seg)
                    )
                    g.append(
                        f'<path d="{d}" fill="none" stroke="var(--s1)" stroke-width="2" '
                        f'stroke-linecap="round" stroke-linejoin="round" />'
                    )
                elif seg is not segments[-1]:
                    # a single point surrounded by gaps has no line to draw,
                    # but it's still real activity and must stay visible; the
                    # last segment's own point gets the end-marker below
                    # instead, so this isn't drawn twice at the same spot.
                    px, py = seg[0]
                    g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2" fill="var(--s1)" />')
            if segments:
                lx, ly = segments[-1][-1]
                g.append(
                    f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="var(--s1)" '
                    f'stroke="var(--surface)" stroke-width="1.5" />'
                )
        g.append("</g>")
        parts.append("".join(g))

    title = f'<title>{esc(y)} by {esc(x)}, one panel per group</title>'
    return svg(width, height, title + "".join(parts))
