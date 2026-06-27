"""Resolve a user-supplied position string into a SkyCoord.

Accepts:
  * Object names resolved via Sesame (e.g. "M31", "3C 273", "NGC 5128").
  * Decimal degrees:        "10.6847 41.2691"  or  "10.6847, 41.2691"
  * Sexagesimal (hms/dms):  "00:42:44 +41:16:09"  or  "00 42 44, +41 16 09"
"""

import re

import astropy.units as u
from astropy.coordinates import SkyCoord


class ResolveError(ValueError):
    """Raised when a position string cannot be interpreted."""


# A token that looks like part of a coordinate (digits plus +-.: only).
_COORDISH = re.compile(r"^[+\-]?[\d.:]+$")


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _coordish(tok: str) -> bool:
    return bool(_COORDISH.match(tok)) and any(ch.isdigit() for ch in tok)


def _make_coord(ra_str: str, dec_str: str) -> SkyCoord:
    ra_str, dec_str = ra_str.strip(), dec_str.strip()
    if _is_number(ra_str) and _is_number(dec_str):
        # Plain decimal degrees (no internal spaces / colons).
        return SkyCoord(float(ra_str) * u.deg, float(dec_str) * u.deg)
    # Sexagesimal: RA in hours, Dec in degrees. Handles ':' or space separators.
    return SkyCoord(ra_str, dec_str, unit=(u.hourangle, u.deg))


def resolve_position(text: str):
    """Return (SkyCoord, label, resolved_name).

    ``resolved_name`` is the object name if a name lookup was used, else None.
    """
    s = (text or "").strip()
    if not s:
        raise ResolveError("Empty position.")

    ra_str = dec_str = None
    if "," in s:
        # Comma separates RA and Dec; each side may use spaces (sexagesimal).
        left, _, right = s.partition(",")
        ra_str, dec_str = left.strip(), right.strip()
    else:
        toks = s.split()
        # All-coordish tokens: 2 -> "ra dec"; 4/6 -> "h m [s] d m [s]".
        if toks and all(_coordish(t) for t in toks) and len(toks) in (2, 4, 6):
            if len(toks) == 2:
                ra_str, dec_str = toks
            else:
                half = len(toks) // 2
                ra_str = " ".join(toks[:half])
                dec_str = " ".join(toks[half:])

    if ra_str and dec_str:
        try:
            coord = _make_coord(ra_str, dec_str)
        except Exception as exc:  # noqa: BLE001 - report parse failure to user
            raise ResolveError(f"Could not parse coordinates: {exc}") from exc
        return coord, _format_label(coord), None

    # Fall back to name resolution via Sesame.
    try:
        coord = SkyCoord.from_name(s)
    except Exception as exc:  # noqa: BLE001
        raise ResolveError(
            f"'{s}' is not valid coordinates and could not be resolved as an object name."
        ) from exc
    return coord, _format_label(coord), s


def _format_label(coord: SkyCoord) -> str:
    ra = coord.ra.to_string(unit=u.hour, sep=":", precision=2, pad=True)
    dec = coord.dec.to_string(unit=u.deg, sep=":", precision=1, alwayssign=True, pad=True)
    return f"{coord.ra.deg:.5f}, {coord.dec.deg:.5f}  ({ra} {dec})"
