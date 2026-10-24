"""Render a FITS image array to a PNG preview with celestial coordinates."""

from __future__ import annotations

import io
import re
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
_BG = "#0d0e11"
_FG = "#d9d6d0"
# In-image overlays deliberately stay cool blue rather than the UI's saffron:
# radio previews use afmhot, and a saffron beam would vanish into the map.
_GRID = "#5ab0ff"
_BEAM = "#5ab0ff"
_MUTED = "#9a9fab"   # for the survey-epoch fallback, to set it apart


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
    obs_period: str = "",
) -> bytes:
    """Return PNG bytes for the cutout, with RA/Dec coordinate axes.

    ``stretch`` is one of: zscale, linear, sqrt, log, asinh. ``zscale`` uses the
    classic IRAF z-scale interval; the others use a 99.5% percentile interval.

    ``obs_period`` is the survey's observing campaign (e.g. ``"1993-1997"``),
    drawn in the corner only when the FITS itself has no observation date. See
    :func:`_obs_date_from_header` for why most surveys need it.
    """
    hdu = _image_hdu(hdul)
    data = _image_data(hdu)
    wcs = _celestial_wcs(hdu)
    beam = _beam_from_header(hdu.header)
    obs_date = _obs_date_from_header(hdu.header)

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
        ax = fig.add_axes([0.21, 0.13, 0.76, 0.83], projection=wcs)
        _style_wcs_axes(ax)
    else:
        ax = fig.add_axes([0.21, 0.13, 0.76, 0.83])
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
    _add_obs_date(ax, obs_date, obs_period)

    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", facecolor=_BG)
    plt.close(fig)
    return buf.getvalue()


def flatten_to_2d(hdul: HDUList) -> HDUList:
    """Return a single-HDU copy carrying just the 2-D plane and its celestial WCS.

    VLASS cutouts arrive as NAXIS=4 ``(1, 1, ny, nx)`` cubes with a 4-D WCS, and
    several hips2fits products carry degenerate axes too. Browser viewers cope
    poorly with that: the extra axes either break the WCS or are read as a cube,
    and a viewer whose WCS is wrong cannot align panes on the sky.

    The pixel grid is untouched — only length-1 axes are dropped — so each survey
    keeps its native resolution. Reuses the same helpers as the PNG path so both
    renderers agree on which HDU, plane and WCS a cutout means.
    """
    from astropy.io import fits

    hdu = _image_hdu(hdul)
    data = _image_data(hdu)
    wcs = _celestial_wcs(hdu)
    header = wcs.to_header() if wcs is not None else fits.Header()
    # Carry over the cards a viewer (or a DS9 round-trip) still wants.
    for key in ("BMAJ", "BMIN", "BPA", "BUNIT", "DATE-OBS", "DATE-END",
                "MJD-OBS", "TELESCOP", "INSTRUME", "SURVEY", "OBJECT"):
        if key in hdu.header and key not in header:
            header[key] = (hdu.header[key], hdu.header.comments[key])
    return fits.HDUList([fits.PrimaryHDU(data=data, header=header)])


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
        facecolor=_BEAM,
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


def _obs_date_from_header(header) -> str | None:
    """Return this image's observation date as ``YYYY-MM-DD``, or ``None``.

    Only some cutouts know when they were taken. VLASS carries a real
    ``DATE-OBS`` from the archive and Pan-STARRS an ``MJD-OBS``; SkyView strips
    the date during mosaicking (as it strips the beam) and hips2fits does not
    propagate one, so for those the caller falls back to the survey's campaign
    span. GB6 uses the pre-1999 ``DD/MM/YY`` form, which astropy will not parse.
    """
    raw = header.get("DATE-OBS") or header.get("DATEOBS")
    if raw:
        text = str(raw).strip()
        iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
        if iso:
            return "-".join(iso.groups())
        old = re.match(r"^(\d{2})/(\d{2})/(\d{2})$", text)
        if old:  # old-style FITS: DD/MM/YY, windowed on 1950
            dd, mm, yy = (int(v) for v in old.groups())
            year = 1900 + yy if yy >= 50 else 2000 + yy
            return f"{year:04d}-{mm:02d}-{dd:02d}"

    mjd = header.get("MJD-OBS", header.get("MJDOBS"))
    if mjd is not None:
        try:
            value = float(mjd)
        except (TypeError, ValueError):
            return None
        if value > 0 and np.isfinite(value):
            from astropy.time import Time

            return Time(value, format="mjd").iso[:10]
    return None


def _add_obs_date(ax, obs_date: str | None, obs_period: str) -> None:
    """Label the cutout with when it was observed, top-left inside the frame.

    An exact per-image date is shown plainly; a survey campaign span is muted
    and tagged "(survey)" so it is never mistaken for this image's own date.
    """
    if obs_date:
        text, color = f"obs. {obs_date}", _FG
    elif obs_period:
        text, color = f"obs. {obs_period} (survey)", _MUTED
    else:
        return
    ax.text(
        0.025,
        0.975,
        text,
        transform=ax.transAxes,
        color=color,
        fontsize=8,
        ha="left",
        va="top",
        # The backing needs to be fairly opaque: these labels sit over whatever
        # the cutout happens to show, often a saturated bright field.
        bbox=dict(boxstyle="round,pad=0.25", facecolor=_BG, edgecolor="none",
                  alpha=0.8),
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
    # Full signed label on every Dec tick. WCSAxes otherwise abbreviates all
    # but one to bare arcminutes, so a southern field reads 48', 47', -45°46'
    # going up and looks as if Dec decreases northwards (the sign is hidden).
    # astropy 6.1 has no switch for this, so keep only the sort step of
    # simplify_labels and skip the prefix stripping.
    dec.set_ticklabel(color=_FG, size=8)
    dec.ticklabels.simplify_labels = dec.ticklabels.sort
    ra.set_ticks(color=_FG)
    dec.set_ticks(color=_FG)
    ra.set_major_formatter("hh:mm:ss")
    dec.set_major_formatter("dd:mm")
    ra.display_minor_ticks(True)
    dec.display_minor_ticks(True)
    ax.coords.grid(color=_GRID, alpha=0.25, linestyle=":")
    ax.coords.frame.set_color(_FG)
