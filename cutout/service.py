"""Fetch + cache layer sitting between the web app and the providers."""

from __future__ import annotations

import os
from typing import Optional

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.io.fits import HDUList

from .providers import CutoutProvider, get_provider

_ROOT = os.path.dirname(os.path.dirname(__file__))

# Transient user cutouts: written on demand and deleted in 'remove' cache mode.
CACHE_DIR = os.environ.get("CUTOUT_CACHE_DIR", os.path.join(_ROOT, "cache"))
# Permanent bundled demo cutouts (M87). Looked up first and NEVER deleted, so
# the example always loads instantly regardless of the keep/remove setting.
DEMO_DIR = os.environ.get("CUTOUT_DEMO_DIR", os.path.join(_ROOT, "demo_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(DEMO_DIR, exist_ok=True)


class CutoutError(RuntimeError):
    """Raised when a cutout cannot be produced (e.g. no survey coverage)."""


def _cache_name(
    provider_key: str,
    ra: float,
    dec: float,
    size: float,
    option: Optional[str] = None,
) -> str:
    name = f"{provider_key}_{ra:.5f}_{dec:+.5f}_{size:.3f}"
    if option:
        name += f"_{option}"  # surveys without a selector keep the base name
    return name + ".fits"


def _has(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 0


def _fetch_to(
    path: str, provider: CutoutProvider, ra: float, dec: float,
    size_arcmin: float, option: Optional[str],
) -> str:
    """Fetch a cutout from the provider and write it atomically to ``path``."""
    coord = SkyCoord(ra, dec, unit="deg")
    try:
        hdul = provider.fetch(coord, size_arcmin, option=option)
    except Exception as exc:  # noqa: BLE001 - normalise upstream failures
        raise CutoutError(f"{provider.name}: {exc}") from exc
    if hdul is None:
        raise CutoutError(f"{provider.name}: no coverage at this position.")

    tmp = path + ".tmp"
    # Some survey FITS (e.g. FIRST/DSS) carry non-ASCII chars in COMMENT/HISTORY
    # cards that fail strict verification; cache them verbatim instead.
    hdul.writeto(tmp, overwrite=True, output_verify="ignore")
    os.replace(tmp, path)
    return path


def get_cutout_fits(
    provider_key: str,
    ra: float,
    dec: float,
    size_arcmin: float,
    option: Optional[str] = None,
) -> str:
    """Return a path to a cached FITS cutout, fetching it if necessary.

    Looks in the permanent demo cache first, then the transient user cache, then
    fetches into the user cache. ``option`` selects a survey's choice axis —
    epoch for VLASS, filter band for SDSS/PanSTARRS; ignored by surveys without
    a selector. Raises :class:`CutoutError` if there is no coverage or the
    upstream fetch fails.
    """
    provider: Optional[CutoutProvider] = get_provider(provider_key)
    if provider is None:
        raise CutoutError(f"Unknown survey '{provider_key}'.")

    # Surveys without a selector ignore the option entirely (no cache fork).
    if provider.select is None:
        option = None

    name = _cache_name(provider_key, ra, dec, size_arcmin, option)

    demo_path = os.path.join(DEMO_DIR, name)
    if _has(demo_path):
        return demo_path  # bundled demo asset — permanent, never removed

    cache_path = os.path.join(CACHE_DIR, name)
    if _has(cache_path):
        return cache_path

    return _fetch_to(cache_path, provider, ra, dec, size_arcmin, option)


def seed_demo_fits(
    provider_key: str,
    ra: float,
    dec: float,
    size_arcmin: float,
    option: Optional[str] = None,
) -> str:
    """Fetch a cutout straight into the permanent demo cache (for seeding)."""
    provider: Optional[CutoutProvider] = get_provider(provider_key)
    if provider is None:
        raise CutoutError(f"Unknown survey '{provider_key}'.")
    if provider.select is None:
        option = None
    name = _cache_name(provider_key, ra, dec, size_arcmin, option)
    return _fetch_to(os.path.join(DEMO_DIR, name), provider, ra, dec, size_arcmin, option)


def load_fits(path: str) -> HDUList:
    return fits.open(path)


def remove_cutout_fits(path: str) -> None:
    """Delete a cached FITS file in 'remove' mode.

    Only ever deletes from the transient ``CACHE_DIR``; demo-cache assets (and
    anything outside the user cache) are left untouched.
    """
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent != os.path.abspath(CACHE_DIR):
            return
        os.remove(path)
    except OSError:
        pass
