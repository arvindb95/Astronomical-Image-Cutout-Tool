"""Survey providers — pluggable sources of image cutouts.

Each provider knows how to fetch a FITS cutout for a given sky position and
angular size. New surveys are added by appending a provider to ``PROVIDERS``.

Four fetch backends are implemented:

``SkyViewProvider``   NASA SkyView (most classic radio/IR/optical surveys)
``CadcVlassProvider`` CADC SODA server-side cutouts (VLASS)
``HipsProvider``      CDS hips2fits (ASKAP RACS, LOFAR LoLSS, THOR, MeerKAT …)
``LotssProvider``     the LOFAR surveys DR2 cutout server (native mosaics)
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.io.fits import HDUList

# Survey groups ordered by wavelength (longest first): Radio, Infrared, Optical.
GROUP_ORDER = ("Radio", "Infrared", "Optical")

# Default colormap per group, running warm (long wavelength / radio) to cool
# (short wavelength / optical) so previews visually echo the wavelength.
GROUP_CMAP = {
    "Radio": "afmhot",      # warm red/orange
    "Infrared": "magma",    # purple/pink — between warm and cool
    "Optical": "bone",      # cool blue
}

# A restoring beam is either fixed — (bmaj", bmin", bpa°) — or, for surveys
# imaged with an E-W array, a function of declination (the synthesised beam
# stretches N-S away from the pole). See ``_dec_beam``.
BeamSpec = Union[
    Tuple[float, float, float],
    Callable[[float], Tuple[float, float, float]],
]


def _dec_beam(minor_arcsec: float, law: str, ref_dec: float = 0.0):
    """Build a declination-dependent circular-in-RA beam.

    Surveys made with an E-W interferometer (MOST/SUMSS, WSRT/WENSS) or with a
    fixed pointing geometry (GMRT/TGSS) have a beam that is ``minor`` across RA
    and stretched along declination. ``law`` picks the stretch factor:

    ``"cosec"``  minor / sin|dec|        (SUMSS, WENSS)
    ``"sec"``    minor / cos(dec - ref)  (TGSS south of ``ref_dec``)

    The major axis lies N-S, i.e. position angle 0.
    """
    import math

    def beam(dec: float) -> Tuple[float, float, float]:
        if law == "cosec":
            s = math.sin(math.radians(abs(dec)))
            factor = 1.0 / s if s > 0.05 else 20.0  # clamp near the equator
        else:  # "sec"
            if dec >= ref_dec:
                factor = 1.0
            else:
                c = math.cos(math.radians(dec - ref_dec))
                factor = 1.0 / c if c > 0.05 else 20.0
        return (minor_arcsec * factor, minor_arcsec, 0.0)

    return beam


@dataclass
class Select:
    """A per-survey choice axis shown as a dropdown in the UI.

    One generic mechanism serves the VLASS observing *epoch*, the SDSS/PanSTARRS
    optical *filter* and the GLEAM/THOR *sub-band*: pick one option, or "All" to
    fan out into one cutout per option. The chosen value is passed to
    :meth:`fetch` as ``option``; ``None`` means the survey's default/auto choice.
    """

    label: str                       # UI noun, e.g. "Epoch" or "Filter"
    options: Tuple[str, ...]         # ("1","2","3") / ("u","g","r","i","z")
    default: str = ""                # preselected value; "" picks the auto entry
    auto: str = ""                   # label for the value="" entry (e.g. "Latest")
    allow_all: bool = True           # offer an "All <label>s" entry (value "all")
    fmt: str = "{label} {v}"         # render a value, e.g. "Epoch 1" / "{v} band"

    def render(self, value: str) -> str:
        return self.fmt.format(v=value, label=self.label)

    def option_list(self) -> List[dict]:
        """Full dropdown entries (value + display text) for the template."""
        opts: List[dict] = []
        if self.auto:
            opts.append({"value": "", "text": self.auto})
        if self.allow_all:
            opts.append({"value": "all", "text": f"All {self.label.lower()}s"})
        for v in self.options:
            opts.append({"value": v, "text": self.render(v)})
        return opts

    def info(self) -> dict:
        return {
            "label": self.label,
            "default": self.default,
            "values": list(self.options),
            "options": self.option_list(),
        }


@dataclass
class CutoutProvider:
    """Base provider. Subclasses implement :meth:`fetch`."""

    key: str                      # stable id used in URLs (e.g. "first")
    name: str                     # display name (e.g. "VLA FIRST")
    group: str                    # grouping for the UI (e.g. "Radio")
    wavelength: str = ""          # human label (e.g. "1.4 GHz", "3.4 um")
    default: bool = False         # selected by default in the UI
    # Sub-section within the group, e.g. the radio frequency band. Empty means
    # the survey sits directly under its group heading.
    subgroup: str = ""
    resolution: str = ""          # angular resolution label, e.g. '5.4"'
    coverage: str = ""            # sky coverage label, e.g. "Dec > -40"
    # When the survey was observed, e.g. "1993-1997". This is the *campaign*
    # span from the survey documentation, used only as a fallback: a cutout
    # whose FITS carries a real DATE-OBS (VLASS, Pan-STARRS) shows that instead.
    obs_period: str = ""
    # Synthesised restoring beam for radio surveys: either a fixed
    # (bmaj_arcsec, bmin_arcsec, bpa_deg) or a callable of declination.
    # ``None`` for non-radio surveys (which get an angular scale bar instead).
    beam: Optional[BeamSpec] = None
    # Optional choice axis (epoch / filter / sub-band). ``None`` => no selector.
    select: Optional[Select] = None

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        raise NotImplementedError

    def info(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "group": self.group,
            "subgroup": self.subgroup,
            "wavelength": self.wavelength,
            "resolution": self.resolution,
            "coverage": self.coverage,
            "obs_period": self.obs_period,
            "default": self.default,
            "select": self.select.info() if self.select else None,
            "cmap": GROUP_CMAP.get(self.group, "gray"),  # wavelength-based default
        }

    def _stamp_beam(
        self, hdul: Optional[HDUList], dec: Optional[float] = None
    ) -> Optional[HDUList]:
        """Write the survey's restoring beam into the header when it's missing.

        Radio mosaickers like SkyView strip BMAJ/BMIN/BPA; inject the documented
        beam (arcsec -> degrees) so the renderer can draw the beam ellipse. A
        beam already present in the header (e.g. native VLASS FITS) is left as-is.
        Declination-dependent beams need ``dec``; without it they are skipped
        rather than stamped with a wrong value.
        """
        if self.beam is None or hdul is None:
            return hdul
        hdr = hdul[0].header
        if hdr.get("BMAJ") is not None:
            return hdul
        beam = self.beam
        if callable(beam):
            if dec is None:
                return hdul
            beam = beam(float(dec))
        bmaj_as, bmin_as, bpa_deg = beam
        hdr["BMAJ"] = (bmaj_as / 3600.0, "Restoring beam major axis [deg]")
        hdr["BMIN"] = (bmin_as / 3600.0, "Restoring beam minor axis [deg]")
        hdr["BPA"] = (bpa_deg, "Restoring beam position angle [deg]")
        return hdul


def _is_blank(hdul: Optional[HDUList], uniform_is_blank: bool = True) -> bool:
    """True when the cutout carries no usable signal.

    Reprojection services (hips2fits, the LOFAR cutout server) answer a position
    outside the survey footprint with a correctly-formed but all-NaN image rather
    than an error, so "no coverage" has to be detected from the pixels.

    ``uniform_is_blank`` additionally rejects an image whose finite pixels are
    all identical. That is a good no-coverage signal for a reprojected mosaic but
    a false positive for a genuinely coarse all-sky map — a 4' cutout of the
    34'-beam 1420 MHz Bonn survey is one source pixel blown up, so every output
    pixel really is the same number. SkyView callers pass ``False``.
    """
    import numpy as np

    if hdul is None or len(hdul) == 0:
        return True
    data = hdul[0].data
    if data is None or getattr(data, "size", 0) == 0:
        return True
    values = np.asarray(data, dtype=float).ravel()
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return True
    if uniform_is_blank:
        return bool(finite.min() == finite.max())
    return False


@dataclass
class SkyViewProvider(CutoutProvider):
    """Fetch a cutout from NASA SkyView via astroquery."""

    survey_id: str = ""           # exact SkyView survey name

    timeout: int = 120           # seconds for the SkyView HTTP request

    # astroquery defaults to the http:// (port 80) SkyView form, which hangs on
    # many networks; the https endpoint is reliable. Force it once.
    _HTTPS_URL = "https://skyview.gsfc.nasa.gov/current/cgi/basicform.pl"

    def _get(
        self, coord: SkyCoord, size_arcmin: float, survey_id: str
    ) -> Optional[HDUList]:
        # Imported lazily so the module loads even if astroquery is slow/offline.
        from astroquery.skyview import SkyView

        if SkyView.URL != self._HTTPS_URL:
            SkyView.URL = self._HTTPS_URL
        SkyView.TIMEOUT = self.timeout
        size = size_arcmin * u.arcmin
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                images = SkyView.get_images(
                    position=coord,
                    survey=[survey_id],
                    width=size,
                    height=size,
                    cache=True,
                    show_progress=False,
                )
        except HTTPError as exc:
            # SkyView builds no file at all for a position outside the survey
            # footprint, and the download then 404s. That is "no coverage".
            if exc.code in (400, 404):
                return None
            raise
        if not images:
            return None
        hdul = images[0]
        # Only an entirely non-finite image means no coverage here: coarse
        # all-sky maps legitimately return a uniform patch at small field sizes.
        if _is_blank(hdul, uniform_is_blank=False):
            return None
        return self._stamp_beam(hdul, dec=float(coord.dec.deg))

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        # Plain SkyView surveys are single-band/single-epoch; option is ignored.
        return self._get(coord, size_arcmin, self.survey_id)


@dataclass
class SkyViewBandProvider(SkyViewProvider):
    """A multi-band SkyView survey (SDSS, GLEAM) where ``option`` picks the band."""

    band_surveys: Dict[str, str] = field(default_factory=dict)  # band -> survey id

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        band = option or (self.select.default if self.select else None)
        survey_id = self.band_surveys.get(band)
        if survey_id is None:
            return None
        return self._get(coord, size_arcmin, survey_id)


@dataclass
class HipsProvider(CutoutProvider):
    """Fetch a cutout from a CDS HiPS via the hips2fits service.

    Many modern radio surveys (ASKAP RACS, LOFAR LoLSS, THOR, MeerKAT) publish a
    FITS HiPS but no public cutout API — or one that needs credentials, as CASDA
    does for ASKAP. hips2fits reprojects the HiPS tiles onto a requested TAN grid
    and hands back real (not stretched) pixel values, which is exactly a cutout.

    Pixels are resampled from the HiPS tiling rather than cut from the original
    mosaic, so these are display-grade rather than the survey's own FITS. Set
    ``pixscale`` at or slightly below the HiPS native tile scale.

    ``hips_by_option`` maps a :class:`Select` value to its own HiPS id, for
    surveys published as one HiPS per sub-band (THOR).
    """

    hips: str = ""                 # HiPS id, e.g. "CSIRO/P/RACS/low/I"
    pixscale: float = 2.0          # arcsec / pixel of the generated cutout
    max_pix: int = 2000            # ceiling on the requested image side
    timeout: int = 180
    hips_by_option: Dict[str, str] = field(default_factory=dict)

    _URL = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        import io

        import requests
        from astropy.io import fits

        hips = self.hips
        if self.hips_by_option:
            value = option or (self.select.default if self.select else "")
            hips = self.hips_by_option.get(value, "")
        if not hips:
            return None

        side = min(self.max_pix, max(16, int(round(size_arcmin * 60.0 / self.pixscale))))
        resp = requests.get(
            self._URL,
            params={
                "hips": hips,
                "width": side,
                "height": side,
                "fov": size_arcmin / 60.0,   # field of view in degrees
                "projection": "TAN",
                "coordsys": "icrs",
                "ra": float(coord.ra.deg),
                "dec": float(coord.dec.deg),
                "format": "fits",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        if resp.content[:6] != b"SIMPLE":
            return None  # service error page rather than a FITS payload
        hdul = fits.open(io.BytesIO(resp.content))
        _ = hdul[0].data  # materialise before the response buffer is dropped
        if _is_blank(hdul):
            return None  # outside the footprint: hips2fits returns all-NaN
        return self._stamp_beam(hdul, dec=float(coord.dec.deg))


@dataclass
class LotssProvider(CutoutProvider):
    """Fetch a cutout from the LOFAR Two-metre Sky Survey cutout server.

    ``lofar-surveys.org`` cuts straight from the DR2 mosaics, so unlike the HiPS
    route the pixels are the survey's own. ``size`` is in arcminutes, which the
    app already works in. Positions outside the two DR2 fields come back as an
    HTML error page rather than FITS.
    """

    url: str = "https://lofar-surveys.org/dr2-cutout.fits"
    timeout: int = 180

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        import io

        import requests
        from astropy.io import fits

        resp = requests.get(
            self.url,
            params={
                "pos": f"{float(coord.ra.deg)},{float(coord.dec.deg)}",
                "size": size_arcmin,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200 or resp.content[:6] != b"SIMPLE":
            return None
        hdul = fits.open(io.BytesIO(resp.content))
        _ = hdul[0].data
        if _is_blank(hdul):
            return None
        return self._stamp_beam(hdul, dec=float(coord.dec.deg))


@dataclass
class PanstarrsProvider(CutoutProvider):
    """Fetch a Pan-STARRS1 stacked cutout from the STScI ps1images service.

    PanSTARRS is not in SkyView. STScI serves per-band stacked FITS via a small
    two-step recipe: resolve the skycell filename for the band, then ask the
    ``fitscut`` service for a cutout. Pixel scale is 0.25"/pix.
    """

    pixscale: float = 0.25         # arcsec / pixel
    max_pix: int = 6000            # fitscut size ceiling
    timeout: int = 60

    _FILENAMES = "https://ps1images.stsci.edu/cgi-bin/ps1filenames.py"
    _FITSCUT = "https://ps1images.stsci.edu/cgi-bin/fitscut.cgi"

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        import io

        import requests
        from astropy.io import fits

        band = option or (self.select.default if self.select else "r")
        ra, dec = float(coord.ra.deg), float(coord.dec.deg)

        # 1) Resolve the stacked skycell file covering this position for the band.
        resp = requests.get(
            self._FILENAMES,
            params={"ra": ra, "dec": dec, "filters": band, "type": "stack"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if len(lines) < 2:
            return None  # outside the PanSTARRS footprint (dec < -30) -> no data
        cols = lines[0].split()
        row = dict(zip(cols, lines[1].split()))
        filename = row.get("filename")
        if not filename:
            return None

        # 2) Pull the FITS cutout from fitscut (size is in pixels).
        size_pix = min(self.max_pix, int(round(size_arcmin * 60.0 / self.pixscale)))
        cut = requests.get(
            self._FITSCUT,
            params={
                "ra": ra,
                "dec": dec,
                "size": size_pix,
                "format": "fits",
                "red": filename,
            },
            timeout=self.timeout,
        )
        cut.raise_for_status()
        if not cut.content:
            return None
        hdul = fits.open(io.BytesIO(cut.content))
        _ = hdul[0].data  # materialise before the response buffer is dropped
        return hdul


@dataclass
class CadcVlassProvider(CutoutProvider):
    """Fetch a VLASS cutout from the CADC archive via SODA.

    VLASS is not in SkyView. The Canadian Astronomy Data Centre serves the NRAO
    VLASS images and exposes a server-side cutout (SODA) service; astroquery's
    ``Cadc`` builds the cutout URLs for us. The returned FITS keeps its native
    ``BMAJ``/``BMIN``/``BPA`` (a genuinely elliptical, dec-dependent beam), so
    the renderer draws the real beam with no stamping needed.

    ``product_suffix`` selects the VLASS product: ``"quicklook"`` for the Quick
    Look (QL) images or ``"continuum_imaging"`` for the Single-Epoch (SE)
    continuum images. CADC productIDs end with this suffix. ``option`` selects
    the observing epoch (``None`` => latest).
    """

    collection: str = "VLASS"
    product_suffix: str = "quicklook"
    timeout: int = 180

    def _client(self):
        from astroquery.cadc import Cadc

        if getattr(self, "_cadc", None) is None:
            self._cadc = Cadc()
        return self._cadc

    @staticmethod
    def _major(product_id: str) -> str:
        # productID looks like "VLASS3.2.T14t19.J123040+123000.quicklook"; the
        # first token is the epoch ("VLASS3"). The sub-epoch (.1/.2) is fixed
        # per position by declination, so only the epoch number is user-facing.
        return str(product_id).split(".")[0]

    @staticmethod
    def _sortkey(product_id: str) -> Tuple[int, int]:
        # (epoch, sub-epoch) as integers, so VLASS10.1 would sort after VLASS9.2
        # where a plain string compare would not.
        m = re.match(r"VLASS(\d+)\.(\d+)", str(product_id))
        return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        from astropy.io import fits

        radius = (size_arcmin / 2.0) * u.arcmin
        cadc = self._client()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = cadc.query_region(
                coord, radius=radius, collection=self.collection
            )
            if results is None or len(results) == 0:
                return None

            # Keep only image planes for this product (QL or SE); drop
            # catalogues and the other product type.
            keep = [
                str(pid).endswith(self.product_suffix) and str(dtype) == "image"
                for pid, dtype in zip(
                    results["productID"], results["dataProductType"]
                )
            ]
            ql = results[keep]
            if len(ql) == 0:
                return None

            pids = list(ql["productID"])
            if option in (None, "", "latest"):
                target = self._major(max(pids, key=self._sortkey))
            else:
                target = f"VLASS{option}"
            sel = [self._major(p) == target for p in pids]
            ql = ql[sel]
            if len(ql) == 0:
                return None  # the requested epoch has no coverage here

            urls = cadc.get_image_list(ql, coord, radius)
            if not urls:
                return None
            hdul = fits.open(urls[0])

        # Force the lazy download so the cutout is materialised before caching.
        _ = hdul[0].data
        return hdul


# --- Survey registry -------------------------------------------------------
# Radio surveys are listed in order of increasing frequency and carry a
# ``subgroup`` so the UI can break the (long) radio list into bands. Coverage
# and resolution strings come from the survey documentation and are shown in
# the picker so a survey can be ruled in or out before a fetch is attempted.

_LOW = "Low frequency (< 300 MHz)"
_DECI = "Decimetre (0.3 – 1 GHz)"
_LBAND = "L band (1 – 2 GHz)"
_CM = "Centimetre (> 2 GHz)"

PROVIDERS: list[CutoutProvider] = [
    # ---------------- Radio: low frequency ---------------------------------
    SkyViewProvider(
        key="mhz22", name="DRAO 22 MHz", group="Radio", subgroup=_LOW,
        wavelength="22 MHz", resolution="1.2° × 1.7°", coverage="All sky",
        obs_period="1965-1969",
        survey_id="0022MHz",
        # Roger et al. all-sky map: E-W × N-S beam, secant(zenith angle).
        beam=(1.2 * 3600.0, 1.7 * 3600.0, 0.0),
    ),
    SkyViewProvider(
        key="mhz35", name="Gauribidanur 34.5 MHz", group="Radio", subgroup=_LOW,
        wavelength="34.5 MHz", resolution="26′ × 42′", coverage="Dec −50° to +70°",
        obs_period="1987",
        survey_id="0035MHz",
        beam=(42 * 60.0, 26 * 60.0, 0.0),
    ),
    HipsProvider(
        key="lolss", name="LOFAR LoLSS DR1", group="Radio", subgroup=_LOW,
        wavelength="54 MHz", resolution='15"', coverage="HETDEX field (650 deg²)",
        obs_period="2018-2021",
        hips="astron.nl/P/lolss_dr1", pixscale=3.0,
        beam=(15.0, 15.0, 0.0),
    ),
    SkyViewProvider(
        key="vlssr", name="VLA VLSSr", group="Radio", subgroup=_LOW,
        wavelength="74 MHz", resolution='75"', coverage="Dec > −30°",
        obs_period="~2006",
        survey_id="VLSSr",
        beam=(75.0, 75.0, 0.0),
    ),
    SkyViewBandProvider(
        key="gleam", name="MWA GLEAM", group="Radio", subgroup=_LOW,
        wavelength="72–231 MHz", resolution="≈2–5′ (varies with dec)",
        coverage="Dec < +30°",
        obs_period="2013-2014",
        band_surveys={
            "72-103": "GLEAM 72-103 MHz",
            "103-134": "GLEAM 103-134 MHz",
            "139-170": "GLEAM 139-170 MHz",
            "170-231": "GLEAM 170-231 MHz",
        },
        # Beam varies strongly with declination and band; left unstamped so the
        # renderer draws a scale bar rather than a misleading ellipse.
        select=Select(label="Band", options=("72-103", "103-134", "139-170", "170-231"),
                      default="170-231", fmt="{v} MHz"),
    ),
    LotssProvider(
        key="lotss", name="LOFAR LoTSS DR2", group="Radio", subgroup=_LOW,
        wavelength="144 MHz", resolution='6"', coverage="5 634 deg² (0h + 13h fields)",
        obs_period="2014-2020",
        default=True,
        beam=(6.0, 6.0, 0.0),
    ),
    SkyViewProvider(
        key="tgss", name="GMRT TGSS ADR1", group="Radio", subgroup=_LOW,
        wavelength="150 MHz", resolution='25" (dec-dependent)', coverage="Dec > −53°",
        obs_period="2010-2012",
        default=True,
        survey_id="TGSS ADR1",
        # 25" x 25"/cos(dec-19) for pointings south of +19 deg.
        beam=_dec_beam(25.0, "sec", ref_dec=19.0),
    ),
    # ---------------- Radio: decimetre --------------------------------------
    SkyViewProvider(
        key="wenss", name="WENSS", group="Radio", subgroup=_DECI,
        wavelength="325 MHz", resolution='54" cosec(δ)', coverage="Dec > +30°",
        obs_period="1991-1996",
        survey_id="WENSS",
        beam=_dec_beam(54.0, "cosec"),
    ),
    SkyViewProvider(
        key="mhz408", name="Haslam 408 MHz", group="Radio", subgroup=_DECI,
        wavelength="408 MHz", resolution="51′", coverage="All sky",
        obs_period="1965-1979",
        survey_id="0408MHz",
        beam=(51 * 60.0, 51 * 60.0, 0.0),
    ),
    SkyViewProvider(
        key="sumss", name="MOST SUMSS", group="Radio", subgroup=_DECI,
        wavelength="843 MHz", resolution='45" cosec|δ|', coverage="Dec < −30°",
        obs_period="1997-2007",
        default=True,
        survey_id="SUMSS 843 MHz",
        beam=_dec_beam(45.0, "cosec"),
    ),
    HipsProvider(
        key="racs_low", name="ASKAP RACS-low", group="Radio", subgroup=_DECI,
        wavelength="887.5 MHz", resolution='25"', coverage="Dec < +41°",
        obs_period="2019-2020",
        default=True,
        hips="CSIRO/P/RACS/low/I", pixscale=2.5,
        beam=(25.0, 25.0, 0.0),
    ),
    # ---------------- Radio: L band -----------------------------------------
    HipsProvider(
        key="meerkat_gc", name="MeerKAT Galactic Centre", group="Radio",
        subgroup=_LBAND,
        wavelength="1.28 GHz", resolution='4"', coverage="Galactic Centre mosaic",
        obs_period="2018",
        hips="CDS/P/MeerKAT/Galactic-Centre-1284MHz-StokesI", pixscale=1.1,
        beam=(4.0, 4.0, 0.0),
    ),
    HipsProvider(
        key="apertif", name="Apertif DR1", group="Radio", subgroup=_LBAND,
        wavelength="1.36 GHz", resolution='12" × 15"/sin(δ)',
        coverage="≈1 000 deg² (northern fields)",
        obs_period="2019-2022",
        hips="astron.nl/P/apertif_dr1", pixscale=4.0,
        beam=_dec_beam(12.0, "cosec"),
    ),
    HipsProvider(
        key="racs_mid", name="ASKAP RACS-mid", group="Radio", subgroup=_LBAND,
        wavelength="1.368 GHz", resolution='10"', coverage="Dec < +49°",
        obs_period="2020-2021",
        default=True,
        hips="CSIRO/P/RACS/mid/I", pixscale=2.0,
        beam=(10.0, 10.0, 0.0),
    ),
    HipsProvider(
        key="thor", name="THOR (VLA Galactic plane)", group="Radio",
        subgroup=_LBAND,
        wavelength="1.06–1.95 GHz", resolution='25"',
        coverage="Galactic plane, ℓ = 15°–67°",
        obs_period="2012-2014",
        hips_by_option={
            "1060": "CDS/P/THOR/continuum/1060",
            "1310": "CDS/P/THOR/continuum/1310",
            "1440": "CDS/P/THOR/continuum/1440",
            "1690": "CDS/P/THOR/continuum/1690",
            "1820": "CDS/P/THOR/continuum/1820",
            "1950": "CDS/P/THOR/continuum/1950",
        },
        pixscale=4.0,
        beam=(25.0, 25.0, 0.0),
        select=Select(label="Band",
                      options=("1060", "1310", "1440", "1690", "1820", "1950"),
                      default="1440", fmt="{v} MHz"),
    ),
    SkyViewProvider(
        key="first", name="VLA FIRST", group="Radio", subgroup=_LBAND,
        wavelength="1.4 GHz", resolution='5.4"',
        coverage="≈10 575 deg² (NGP + SGP caps)",
        obs_period="1993-2011",
        default=True,
        survey_id="VLA FIRST (1.4 GHz)",
        # FIRST restoring beam: 5.4" FWHM circular (main survey).
        beam=(5.4, 5.4, 0.0),
    ),
    SkyViewProvider(
        key="nvss", name="NVSS", group="Radio", subgroup=_LBAND,
        wavelength="1.4 GHz", resolution='45"', coverage="Dec > −40°",
        obs_period="1993-1997",
        default=True,
        survey_id="NVSS",
        # NVSS restoring beam: 45" FWHM circular (survey constant).
        beam=(45.0, 45.0, 0.0),
    ),
    SkyViewProvider(
        key="stripe82vla", name="VLA Stripe 82", group="Radio", subgroup=_LBAND,
        wavelength="1.4 GHz", resolution='1.8"', coverage="92 deg² of SDSS Stripe 82",
        obs_period="2007-2009",
        survey_id="Stripe82VLA",
        beam=(1.8, 1.8, 0.0),
    ),
    SkyViewProvider(
        key="bonn1420", name="Stockert/Villa Elisa 1.4 GHz", group="Radio",
        subgroup=_LBAND,
        wavelength="1.42 GHz", resolution="34′", coverage="Dec > −16°",
        obs_period="1972-1974",
        survey_id="1420MHz (Bonn)",
        beam=(34 * 60.0, 34 * 60.0, 0.0),
    ),
    # ---------------- Radio: centimetre and above ---------------------------
    CadcVlassProvider(
        key="vlass_ql", name="VLASS QL", group="Radio", subgroup=_CM,
        wavelength="3 GHz", resolution='≈2.5"', coverage="Dec > −40°",
        obs_period="2017-present",
        default=True,
        # Quick Look images. The FITS carry the real elliptical beam, so no
        # beam= stamping is required here.
        product_suffix="quicklook",
        select=Select(label="Epoch", options=("1", "2", "3", "4"),
                      auto="Latest", fmt="Epoch {v}"),
    ),
    CadcVlassProvider(
        key="vlass_se", name="VLASS SE", group="Radio", subgroup=_CM,
        wavelength="3 GHz", resolution='≈2.5"', coverage="Dec > −40° (epochs 1–3)",
        obs_period="2017-present",
        default=True,
        # Single-Epoch continuum images (deeper, better-calibrated than QL).
        # Not released for every epoch/tile — epoch 4 is Quick Look only so far;
        # missing combinations report "no coverage".
        product_suffix="continuum_imaging",
        select=Select(label="Epoch", options=("1", "2", "3", "4"),
                      auto="Latest", fmt="Epoch {v}"),
    ),
    SkyViewProvider(
        key="gb6", name="GB6 / PMN", group="Radio", subgroup=_CM,
        wavelength="4.85 GHz", resolution="3.5′", coverage="Dec −88° to +75°",
        obs_period="1986-1990",
        survey_id="GB6 (4850MHz)",
        beam=(3.5 * 60.0, 3.5 * 60.0, 0.0),
    ),
    SkyViewProvider(
        key="co", name="CfA CO (1–0)", group="Radio", subgroup=_CM,
        wavelength="115 GHz", resolution="8.8′", coverage="Galactic plane (9 853 deg²)",
        obs_period="1980-2000",
        survey_id="CO",
        beam=(8.8 * 60.0, 8.8 * 60.0, 0.0),
    ),
    # ---------------- Infrared -----------------------------------------------
    SkyViewProvider(
        key="wise_w1", name="WISE W1", group="Infrared",
        wavelength="3.4 um", resolution='6.1"', coverage="All sky",
        obs_period="2010-2011",
        default=True,
        survey_id="WISE 3.4",
    ),
    SkyViewProvider(
        key="wise_w2", name="WISE W2", group="Infrared",
        wavelength="4.6 um", resolution='6.4"', coverage="All sky",
        obs_period="2010-2011",
        survey_id="WISE 4.6",
    ),
    SkyViewProvider(
        key="wise_w3", name="WISE W3", group="Infrared",
        wavelength="12 um", resolution='6.5"', coverage="All sky",
        obs_period="2010-2011",
        survey_id="WISE 12",
    ),
    SkyViewProvider(
        key="wise_w4", name="WISE W4", group="Infrared",
        wavelength="22 um", resolution='12"', coverage="All sky",
        obs_period="2010-2011",
        survey_id="WISE 22",
    ),
    # ---------------- Optical ------------------------------------------------
    SkyViewBandProvider(
        key="sdss", name="SDSS", group="Optical",
        wavelength="ugriz", resolution='1.4"', coverage="≈14 500 deg²",
        obs_period="1998-2011",
        default=True,
        band_surveys={
            "u": "SDSSu", "g": "SDSSg", "r": "SDSSr",
            "i": "SDSSi", "z": "SDSSz",
        },
        select=Select(label="Filter", options=("u", "g", "r", "i", "z"),
                      default="r", fmt="{v} band"),
    ),
    PanstarrsProvider(
        key="panstarrs", name="Pan-STARRS1", group="Optical",
        wavelength="grizy", resolution='1.0"', coverage="Dec > −30°",
        obs_period="2010-2014",
        default=True,
        select=Select(label="Filter", options=("g", "r", "i", "z", "y"),
                      default="r", fmt="{v} band"),
    ),
]

PROVIDER_MAP: dict[str, CutoutProvider] = {p.key: p for p in PROVIDERS}


def get_provider(key: str) -> Optional[CutoutProvider]:
    return PROVIDER_MAP.get(key)
