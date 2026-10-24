"""Build the linked multi-pane viewer with Bokeh.

Every pane is plotted in a common coordinate system: **arcsecond offsets from
the requested field centre**, east-positive and north-positive. That one choice
does most of the work:

* Sharing ``x_range``/``y_range`` between figures then links pan and zoom *on the
  sky*, not in pixels, so surveys with wildly different pixel scales (VLASS at
  ~1"/pix, NVSS at 15"/pix) stay matched with no synchronisation machinery.
* A region drawn once is stored in arcsec offsets too, so rendering the same
  ``ColumnDataSource`` in every figure puts it at the same sky position and the
  same angular size in all of them, automatically.

Pixel grids are never resampled — only the axes are relabelled — so each survey
keeps its native resolution.
"""

from __future__ import annotations

import warnings
from typing import List, Optional

import numpy as np

# Nice round region radii, arcsec.
DEFAULT_REGION_RADIUS = 5.0

# Page palette (static/style.css). Grid lines stay cool blue: they sit on top
# of the images, where saffron would be lost against the afmhot radio maps.
_BG = "#0d0e11"
_PANEL = "#15171c"
_BORDER = "#2c3039"
_TEXT = "#eceae6"
_MUTED = "#9a9fab"
_SAFFRON = "#ff9933"

# Bokeh 3 renders widgets in shadow DOM, so page CSS cannot reach them; each
# widget gets this sheet instead.
_WIDGET_CSS = f"""
:host {{ --font-size: 13px; }}
.bk-btn {{
  background: #1c1f26; color: {_TEXT}; border: 1px solid {_BORDER};
  border-radius: 8px; font-weight: 600;
}}
.bk-btn:hover {{ border-color: {_SAFFRON}; background: #1c1f26; }}
.bk-btn-primary {{ background: {_SAFFRON}; color: #1c0f00; border-color: {_SAFFRON}; }}
.bk-btn-primary:hover {{ background: #ffad5c; border-color: #ffad5c; }}
.bk-btn:focus-visible {{ outline: 2px solid {_SAFFRON}; outline-offset: 2px; }}
.bk-slider-title, .bk-slider-value {{ color: {_MUTED}; }}
.noUi-target {{ background: #1c1f26; border-color: {_BORDER}; box-shadow: none; }}
.noUi-connect {{ background: {_SAFFRON}; }}
.noUi-handle {{ background: {_SAFFRON}; border-color: #b85f0b; box-shadow: none; }}
"""


def _limits(data: np.ndarray, stretch: str):
    """Display limits, matching what the PNG previews use."""
    from astropy.visualization import PercentileInterval, ZScaleInterval

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0, 1.0
    if stretch == "zscale":
        lo, hi = ZScaleInterval().get_limits(finite)
    else:
        lo, hi = PercentileInterval(99.5).get_limits(finite)
    if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
        lo, hi = float(np.nanmin(finite)), float(np.nanmax(finite))
        if lo >= hi:
            hi = lo + 1.0
    return float(lo), float(hi)


def _offsets(wcs, ra0: float, dec0: float, nx: int, ny: int):
    """Arcsec offsets of the image edges from (ra0, dec0).

    Returns ``(x0, dw, y0, dh, flip_x, flip_y)`` where the offsets are
    east-positive / north-positive and the flip flags say whether the array has
    to be reversed so that its column/row index increases with the offset.
    Derived from the WCS rather than assumed, so an image on any orientation
    (SUMSS is served on a SIN/NCP grid) is placed correctly.
    """
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    centre = SkyCoord(ra0 * u.deg, dec0 * u.deg, frame="icrs")
    cos_dec = float(np.cos(np.deg2rad(dec0)))

    def offset_at(px, py):
        """East/north offset in arcsec of a pixel from the field centre.

        Converted to ICRS first — SkyView serves FK5 headers while hips2fits
        serves ICRS — then differenced directly. Over a field of at most a
        degree the small-angle form is accurate to far better than a pixel, and
        it avoids the frame-equivalence rules of ``spherical_offsets_to``.
        """
        sky = wcs.pixel_to_world(px, py).icrs
        d_ra = float(sky.ra.deg) - ra0
        # Keep the difference in (-180, 180] so a field straddling RA 0 works.
        d_ra = (d_ra + 180.0) % 360.0 - 180.0
        return d_ra * cos_dec * 3600.0, (float(sky.dec.deg) - dec0) * 3600.0

    mid_x, mid_y = (nx - 1) / 2.0, (ny - 1) / 2.0
    e_lo, _ = offset_at(-0.5, mid_y)
    e_hi, _ = offset_at(nx - 0.5, mid_y)
    _, n_lo = offset_at(mid_x, -0.5)
    _, n_hi = offset_at(mid_x, ny - 0.5)

    flip_x = e_hi < e_lo          # east normally decreases with pixel column
    flip_y = n_hi < n_lo
    x0, x1 = (e_hi, e_lo) if flip_x else (e_lo, e_hi)
    y0, y1 = (n_hi, n_lo) if flip_y else (n_lo, n_hi)
    return x0, x1 - x0, y0, y1 - y0, flip_x, flip_y


def _palette(name: str, levels: int = 256) -> List[str]:
    """A matplotlib colormap as Bokeh hex colours, so panes match the previews."""
    import matplotlib.pyplot as plt

    try:
        cmap = plt.get_cmap(name)
    except (ValueError, KeyError):
        cmap = plt.get_cmap("gray")
    rgba = cmap(np.linspace(0.0, 1.0, levels))
    return ["#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
            for r, g, b, _a in rgba]


def build_layout(panes: List[dict], ra: float, dec: float, size_arcmin: float,
                 stretch: str = "zscale", cmap_mode: str = "auto",
                 cols: int = 3):
    """Return ``(script, div)`` for a linked grid of cutout panes.

    ``panes`` are dicts with ``name``, ``variant``, ``wavelength``, ``cmap``,
    ``path`` (a cached FITS) and ``obs`` (a date label).
    """
    from bokeh.embed import components
    from bokeh.layouts import column, gridplot, row
    from bokeh.models import (Button, ColumnDataSource, CustomJS, HoverTool,
                              InlineStyleSheet, LinearColorMapper, PointDrawTool,
                              Range1d, Slider)
    from bokeh.plotting import figure

    from .render import flatten_to_2d
    from .service import load_fits

    half = size_arcmin * 60.0 / 2.0
    # East is plotted to the left, the conventional orientation, by running the
    # x axis from +east to -east. Both ranges are shared by every figure, which
    # is what links pan and zoom across panes.
    x_range = Range1d(start=half, end=-half, bounds=(-half * 4, half * 4))
    y_range = Range1d(start=-half, end=half, bounds=(-half * 4, half * 4))

    # One source for every pane: a region added here shows up in all of them.
    regions = ColumnDataSource(data=dict(x=[], y=[], r=[]))

    figures = []
    for pane in panes:
        try:
            with load_fits(pane["path"]) as hdul:
                hdu = flatten_to_2d(hdul)[0]
                data = np.asarray(hdu.data, dtype=float)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from astropy.wcs import WCS
                    wcs = WCS(hdu.header)
        except Exception:  # noqa: BLE001 - a broken cutout should not kill the page
            continue

        ny, nx = data.shape
        x0, dw, y0, dh, flip_x, flip_y = _offsets(wcs, ra, dec, nx, ny)
        if flip_x:
            data = data[:, ::-1]
        if flip_y:
            data = data[::-1, :]

        lo, hi = _limits(data, stretch)
        cmap_name = pane.get("cmap", "gray") if cmap_mode == "auto" else cmap_mode
        mapper = LinearColorMapper(palette=_palette(cmap_name), low=lo, high=hi,
                                   nan_color=_BG)

        title = pane["name"] + (f" · {pane['variant']}" if pane.get("variant") else "")
        fig = figure(
            title=title, width=340, height=340,
            x_range=x_range, y_range=y_range,
            tools="pan,wheel_zoom,box_zoom,reset,save",
            active_scroll="wheel_zoom", active_drag="pan",
            background_fill_color=_BG, border_fill_color=_PANEL,
            outline_line_color=_BORDER, toolbar_location="above",
        )
        fig.image(image=[data], x=x0, y=y0, dw=dw, dh=dh, color_mapper=mapper)
        fig.xaxis.axis_label = "ΔRA (arcsec, E ←)"
        fig.yaxis.axis_label = "ΔDec (arcsec)"
        for axis in (fig.xaxis, fig.yaxis):
            axis.axis_label_text_color = _MUTED
            axis.major_label_text_color = _MUTED
            axis.axis_line_color = _BORDER
        fig.grid.grid_line_color = "#5ab0ff"
        fig.grid.grid_line_alpha = 0.12
        fig.title.text_color = _TEXT
        fig.title.text_font_size = "11px"

        # Built-in $x/$y only — no CustomJSHover, which does not survive
        # embedding and broke an earlier attempt at this.
        fig.add_tools(HoverTool(
            tooltips=[("ΔRA", "$x{0.0}\""), ("ΔDec", "$y{0.0}\""),
                      ("value", "@image{0.000}")],
            renderers=[fig.renderers[0]],
        ))

        glyph = fig.circle(x="x", y="y", radius="r", source=regions,
                           fill_alpha=0.0, line_color="#39ff88", line_width=1.5)
        draw = PointDrawTool(renderers=[glyph], add=True, drag=True,
                             num_objects=200)
        fig.add_tools(draw)
        fig.toolbar.active_tap = None

        sub = pane.get("wavelength", "")
        if pane.get("obs"):
            sub = f"{sub} · {pane['obs']}" if sub else pane["obs"]
        figures.append((fig, sub))

    if not figures:
        from bokeh.models import Div
        return components(Div(text="<p>No cutouts could be displayed.</p>"))

    # --- controls -----------------------------------------------------------
    sheet = InlineStyleSheet(css=_WIDGET_CSS)
    radius = Slider(start=1, end=max(5, int(half)), value=DEFAULT_REGION_RADIUS,
                    step=1, title="Region radius (arcsec)", width=220,
                    stylesheets=[sheet])
    radius.js_on_change("value", CustomJS(args=dict(src=regions), code="""
        const r = src.data.r.map(() => cb_obj.value);
        src.data = {...src.data, r};
        src.change.emit();
    """))

    clear = Button(label="Clear regions", button_type="default", width=110,
                   stylesheets=[sheet])
    clear.js_on_click(CustomJS(args=dict(src=regions), code="""
        src.data = {x: [], y: [], r: []};
        src.change.emit();
    """))

    save = Button(label="Save .reg", button_type="primary", width=110,
                  stylesheets=[sheet])
    save.js_on_click(CustomJS(args=dict(src=regions, ra0=ra, dec0=dec), code="""
        // Offsets back to sky coordinates, then a DS9 region file in ICRS so it
        // opens directly in DS9.
        const d = src.data, lines = ["# Region file format: DS9 version 4.1",
                                     "global color=green", "icrs"];
        const cosd = Math.cos(dec0 * Math.PI / 180);
        for (let i = 0; i < d.x.length; i++) {
            const ra = ra0 + (d.x[i] / 3600.0) / cosd;
            const dec = dec0 + d.y[i] / 3600.0;
            lines.push(`circle(${ra.toFixed(6)},${dec.toFixed(6)},${d.r[i].toFixed(2)}")`);
        }
        const blob = new Blob([lines.join("\\n") + "\\n"], {type: "text/plain"});
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "cutouts.reg";
        a.click();
        URL.revokeObjectURL(a.href);
    """))

    # New points arrive with no radius; give them the slider's value.
    regions.js_on_change("data", CustomJS(args=dict(slider=radius), code="""
        const d = cb_obj.data;
        let changed = false;
        for (let i = 0; i < d.x.length; i++) {
            if (d.r[i] === undefined || d.r[i] === null || isNaN(d.r[i])) {
                d.r[i] = slider.value; changed = true;
            }
        }
        if (changed) cb_obj.change.emit();
    """))

    from bokeh.models import Div
    captions = [Div(text=f'<div style="color:{_MUTED};font-size:11px;'
                         f'margin:-6px 0 6px 6px">{sub}</div>', width=340)
                for _f, sub in figures]
    cells = [column(f, c) for (f, _s), c in zip(figures, captions)]
    # merge_tools=False keeps each pane's own toolbar, so the point-draw tool
    # can be picked per pane; a single merged toolbar would collapse the six
    # separate PointDrawTools into an unusable row of identical buttons.
    grid = gridplot([cells[i:i + cols] for i in range(0, len(cells), cols)],
                    toolbar_location=None, merge_tools=False)
    controls = row(radius, clear, save)
    return components(column(controls, grid))
