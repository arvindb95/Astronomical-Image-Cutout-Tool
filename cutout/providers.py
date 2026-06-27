"""Survey providers — pluggable sources of image cutouts.

Each provider knows how to fetch a FITS cutout for a given sky position and
angular size. New surveys are added by appending a provider to ``PROVIDERS``.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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


@dataclass
class Select:
    """A per-survey choice axis shown as a dropdown in the UI.

    One generic mechanism serves both the VLASS observing *epoch* and the
    SDSS/PanSTARRS optical *filter*: pick one option, or "All" to fan out into
    one cutout per option. The chosen value is passed to :meth:`fetch` as
    ``option``; ``None`` means the survey's default/auto choice.
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
    # Synthesised restoring beam (bmaj_arcsec, bmin_arcsec, bpa_deg) for radio
    # surveys; ``None`` for non-radio surveys (which get an angular scale bar).
    beam: Optional[Tuple[float, float, float]] = None
    # Optional choice axis (epoch / filter). ``None`` => no selector shown.
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
            "wavelength": self.wavelength,
            "default": self.default,
            "select": self.select.info() if self.select else None,
            "cmap": GROUP_CMAP.get(self.group, "gray"),  # wavelength-based default
        }

    def _stamp_beam(self, hdul: Optional[HDUList]) -> Optional[HDUList]:
        """Write the survey's restoring beam into the header when it's missing.

        Radio mosaickers like SkyView strip BMAJ/BMIN/BPA; inject the documented
        beam (arcsec -> degrees) so the renderer can draw the beam ellipse. A
        beam already present in the header (e.g. native VLASS FITS) is left as-is.
        """
        if self.beam is None or hdul is None:
            return hdul
        hdr = hdul[0].header
        if hdr.get("BMAJ") is not None:
            return hdul
        bmaj_as, bmin_as, bpa_deg = self.beam
        hdr["BMAJ"] = (bmaj_as / 3600.0, "Restoring beam major axis [deg]")
        hdr["BMIN"] = (bmin_as / 3600.0, "Restoring beam minor axis [deg]")
        hdr["BPA"] = (bpa_deg, "Restoring beam position angle [deg]")
        return hdul


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
        if not images:
            return None
        return self._stamp_beam(images[0])

    def fetch(
        self, coord: SkyCoord, size_arcmin: float, option: Optional[str] = None
    ) -> Optional[HDUList]:
        # Plain SkyView surveys are single-band/single-epoch; option is ignored.
        return self._get(coord, size_arcmin, self.survey_id)


@dataclass
class SkyViewBandProvider(SkyViewProvider):
    """A multi-band SkyView survey (e.g. SDSS) where ``option`` picks the band."""

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
    def _subkey(product_id: str) -> str:
        # First two tokens, e.g. "VLASS3.2" — used to order/break ties.
        parts = str(product_id).split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else str(product_id)

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
                target = self._major(max(pids, key=self._subkey))
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
# Mirrors the core surveys offered by the CIRADA cutout service, plus optical
# (SDSS, PanSTARRS). New surveys are added by appending a provider here.
PROVIDERS: list[CutoutProvider] = [
    SkyViewProvider(
        key="first", name="VLA FIRST", group="Radio",
        wavelength="1.4 GHz", default=True,
        survey_id="VLA FIRST (1.4 GHz)",
        # FIRST restoring beam: 5.4" FWHM circular (main survey).
        beam=(5.4, 5.4, 0.0),
    ),
    SkyViewProvider(
        key="nvss", name="NVSS", group="Radio",
        wavelength="1.4 GHz", default=True,
        survey_id="NVSS",
        # NVSS restoring beam: 45" FWHM circular (survey constant).
        beam=(45.0, 45.0, 0.0),
    ),
    CadcVlassProvider(
        key="vlass_ql", name="VLASS QL", group="Radio",
        wavelength="3 GHz", default=True,
        # Quick Look images. The FITS carry the real elliptical beam, so no
        # beam= stamping is required here.
        product_suffix="quicklook",
        select=Select(label="Epoch", options=("1", "2", "3"),
                      auto="Latest", fmt="Epoch {v}"),
    ),
    CadcVlassProvider(
        key="vlass_se", name="VLASS SE", group="Radio",
        wavelength="3 GHz", default=True,
        # Single-Epoch continuum images (deeper, better-calibrated than QL).
        # Not released for every epoch/tile; missing combinations report
        # "no coverage".
        product_suffix="continuum_imaging",
        select=Select(label="Epoch", options=("1", "2", "3"),
                      auto="Latest", fmt="Epoch {v}"),
    ),
    SkyViewBandProvider(
        key="sdss", name="SDSS", group="Optical",
        wavelength="ugriz", default=True,
        band_surveys={
            "u": "SDSSu", "g": "SDSSg", "r": "SDSSr",
            "i": "SDSSi", "z": "SDSSz",
        },
        select=Select(label="Filter", options=("u", "g", "r", "i", "z"),
                      default="r", fmt="{v} band"),
    ),
    PanstarrsProvider(
        key="panstarrs", name="Pan-STARRS1", group="Optical",
        wavelength="grizy", default=True,
        select=Select(label="Filter", options=("g", "r", "i", "z", "y"),
                      default="r", fmt="{v} band"),
    ),
    SkyViewProvider(
        key="wise_w1", name="WISE W1", group="Infrared",
        wavelength="3.4 um", default=True,
        survey_id="WISE 3.4",
    ),
    SkyViewProvider(
        key="wise_w2", name="WISE W2", group="Infrared",
        wavelength="4.6 um",
        survey_id="WISE 4.6",
    ),
    SkyViewProvider(
        key="wise_w3", name="WISE W3", group="Infrared",
        wavelength="12 um",
        survey_id="WISE 12",
    ),
    SkyViewProvider(
        key="wise_w4", name="WISE W4", group="Infrared",
        wavelength="22 um",
        survey_id="WISE 22",
    ),
]

PROVIDER_MAP: dict[str, CutoutProvider] = {p.key: p for p in PROVIDERS}


def get_provider(key: str) -> Optional[CutoutProvider]:
    return PROVIDER_MAP.get(key)
