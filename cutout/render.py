"""Render a FITS image array to a PNG preview with celestial coordinates."""

from __future__ import annotations

import io
import warnings

import matplotlib

matplotlib.use("Agg")  # headless: render to buffers, never to a display

import matplotlib.pyplot as plt
import numpy as np
from astropy.io.fits import HDUList
from astropy.wcs.utils import proj_plane_pixel_scales
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Ellipse
from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar
from astropy.visualization import (
    AsinhStretch,
    ImageNormalize,
    LinearStretch,
    LogStretch,
    PercentileInterval,
    SqrtStretch,
    ZScaleInterval,
)
from astropy.wcs import WCS
from matplotlib.colors import Normalize

_STRETCHES = {
    "linear": LinearStretch,
    "sqrt": SqrtStretch,
    "log": LogStretch,
    "asinh": AsinhStretch,
}

# Dark-theme palette to match the UI.
_BG = "#05070c"
_FG = "#cfd6e4"
_GRID = "#5ab0ff"


def _image_hdu(hdul: HDUList):
    """Return the first HDU that carries 2-D (or higher) image data."""
    for hdu in hdul:
        if getattr(hdu, "data", None) is not None and hdu.data.ndim >= 2:
            return hdu
    raise ValueError("No 2-D image data found in FITS.")


def _image_data(hdu) -> np.ndarray:
    data = np.asarray(hdu.data, dtype=float)
    # Collapse any leading degenerate axes (e.g. radio cubes 1x1xNxN).
    while data.ndim > 2:
        data = data[0]
    return data


def _celestial_wcs(hdu):
    """Return a 2-D celestial WCS for the HDU, or ``None`` if unavailable."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wcs = WCS(hdu.header)
            if wcs.has_celestial:
                return wcs.celestial
    except Exception:  # noqa: BLE001 - bad/missing WCS: fall back to pixels
        pass
    return None


def render_png(
    hdul: HDUList,
    *,
    cmap: str = "gray",
    stretch: str = "zscale",
    invert: bool = False,
) -> bytes:
    """Return PNG bytes for the cutout, with RA/Dec coordinate axes.

    ``stretch`` is one of: zscale, linear, sqrt, log, asinh. ``zscale`` uses the
    classic IRAF z-scale interval; the others use a 99.5% percentile interval.
    """
    hdu = _image_hdu(hdul)
    data = _image_data(hdu)
    wcs = _celestial_wcs(hdu)
    beam = _beam_from_header(hdu.header)

    finite = np.isfinite(data)
    if not finite.any():
        data = np.zeros_like(data)
        finite = np.ones_like(data, dtype=bool)

    if stretch == "zscale":
        vmin, vmax = ZScaleInterval().get_limits(data[finite])
        norm = Normalize(vmin=vmin, vmax=vmax, clip=True)
    else:
        stretch_cls = _STRETCHES.get(stretch, LinearStretch)
        norm = ImageNormalize(
            data[finite],
            interval=PercentileInterval(99.5),
            stretch=stretch_cls(),
            clip=True,
        )

    colormap = plt.get_cmap(cmap)
    if invert:
        colormap = colormap.reversed()

    display = np.nan_to_num(data, nan=float(np.nanmin(data[finite])))

    fig = plt.figure(figsize=(4.2, 4.2), dpi=100)
    fig.patch.set_facecolor(_BG)
    # origin="lower" + WCS gives the conventional north-up / east-left view
    # without manually flipping the array.
    if wcs is not None:
        ax = fig.add_axes([0.17, 0.13, 0.80, 0.83], projection=wcs)
        _style_wcs_axes(ax)
    else:
        ax = fig.add_axes([0.17, 0.13, 0.80, 0.83])
        ax.set_facecolor(_BG)
        ax.tick_params(colors=_FG, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(_FG)
        ax.set_xlabel("x (pixels)", color=_FG, fontsize=9)
        ax.set_ylabel("y (pixels)", color=_FG, fontsize=9)

    ax.imshow(display, origin="lower", cmap=colormap, norm=norm, interpolation="nearest")
    if wcs is not None:
        # Radio cutouts carry a synthesised beam (BMAJ/BMIN/BPA); show that
        # instead of a scale bar. Everything else gets an angular scale bar.
        if beam is not None:
            _add_beam(ax, data.shape, wcs, beam)
        else:
            _add_scale_bar(ax, data.shape, wcs)

    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", facecolor=_BG)
    plt.close(fig)
    return buf.getvalue()


def _beam_from_header(header):
    """Return the synthesised beam ``(bmaj_deg, bmin_deg, bpa_deg)`` or ``None``.

    Reads the FITS-standard ``BMAJ``/``BMIN``/``BPA`` keywords (degrees / degrees
    / degrees east-of-north). ``BPA`` defaults to 0 if absent.
    """
    try:
        bmaj, bmin = header.get("BMAJ"), header.get("BMIN")
        if bmaj is None or bmin is None:
            return None
        bmaj, bmin = float(bmaj), float(bmin)
        bpa = float(header.get("BPA", 0.0) or 0.0)
        if not (np.isfinite(bmaj) and np.isfinite(bmin)) or bmaj <= 0 or bmin <= 0:
            return None
        return bmaj, bmin, bpa
    except (TypeError, ValueError):
        return None


def _add_beam(ax, shape, wcs, beam) -> None:
    """Draw the synthesised beam as an ellipse in the lower-left corner."""
    bmaj_deg, bmin_deg, bpa_deg = beam
    try:
        scale_deg = float(np.mean(proj_plane_pixel_scales(wcs)))
    except Exception:  # noqa: BLE001 - degenerate WCS: skip the beam
        return
    if not np.isfinite(scale_deg) or scale_deg <= 0:
        return

    bmaj_pix = bmaj_deg / scale_deg
    bmin_pix = bmin_deg / scale_deg
    ny, nx = shape[-2], shape[-1]
    # Inset from the lower-left corner by half the major axis plus a margin so
    # the rotated ellipse always sits fully inside the frame.
    margin = 0.05 * nx + bmaj_pix / 2.0
    cx = cy = margin

    # BPA is measured from north through east. In the north-up / east-left
    # pixel frame this WCS produces, matplotlib's CCW-from-+x angle is 90 + BPA.
    ell = Ellipse(
        (cx, cy),
        width=bmaj_pix,
        height=bmin_pix,
        angle=90.0 + bpa_deg,
        facecolor=_GRID,
        edgecolor="white",
        linewidth=0.8,
        alpha=0.9,
    )
    ax.add_patch(ell)
    ax.text(
        cx,
        cy + bmaj_pix / 2.0 + 0.025 * ny,
        "beam",
        color=_FG,
        fontsize=8,
        ha="center",
        va="bottom",
    )


# Nice round bar lengths, in arcseconds.
_BAR_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 1800)


def _bar_label(arcsec: float) -> str:
    if arcsec < 60:
        return f"{arcsec:g}″"  # arcseconds
    return f"{arcsec / 60:g}′"  # arcminutes


def _add_scale_bar(ax, shape, wcs) -> None:
    """Draw an angular scale bar sized to a round fraction of the field."""
    try:
        scale_deg = float(np.mean(proj_plane_pixel_scales(wcs)))
    except Exception:  # noqa: BLE001 - degenerate WCS: skip the bar
        return
    arcsec_per_pix = scale_deg * 3600.0
    if not np.isfinite(arcsec_per_pix) or arcsec_per_pix <= 0:
        return

    nx = shape[-1]
    field_arcsec = nx * arcsec_per_pix
    target = field_arcsec / 3.0  # aim for ~1/3 of the field width
    bar_arcsec = min(_BAR_STEPS, key=lambda s: abs(s - target))
    bar_pix = bar_arcsec / arcsec_per_pix

    bar = AnchoredSizeBar(
        ax.transData,
        bar_pix,
        _bar_label(bar_arcsec),
        loc="lower right",
        pad=0.4,
        borderpad=0.5,
        sep=4,
        color=_FG,
        frameon=False,
        size_vertical=max(1.0, nx * 0.006),
        fontproperties=FontProperties(size=9),
    )
    ax.add_artist(bar)


def _style_wcs_axes(ax) -> None:
    """Apply the dark-theme styling and RA/Dec labels to a WCSAxes."""
    ax.set_facecolor(_BG)
    ra, dec = ax.coords[0], ax.coords[1]
    ra.set_axislabel("RA (J2000)", color=_FG, fontsize=9)
    dec.set_axislabel("Dec (J2000)", color=_FG, fontsize=9)
    ra.set_ticklabel(color=_FG, size=8)
    dec.set_ticklabel(color=_FG, size=8)
    ra.set_ticks(color=_FG)
    dec.set_ticks(color=_FG)
    ra.set_major_formatter("hh:mm:ss")
    dec.set_major_formatter("dd:mm")
    ra.display_minor_ticks(True)
    dec.display_minor_ticks(True)
    ax.coords.grid(color=_GRID, alpha=0.25, linestyle=":")
    ax.coords.frame.set_color(_FG)
