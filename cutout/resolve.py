"""Resolve a user-supplied position string into a SkyCoord.

Accepts:
  * Object names resolved via Sesame (e.g. "M31", "3C 273", "NGC 5128").
  * Decimal degrees:        "10.6847 41.2691"  or  "10.6847, 41.2691"
  * Sexagesimal (hms/dms):  "00:42:44 +41:16:09"  or  "00 42 44, +41 16 09"
  * Unit markers:           "12h30m49.4s +12d23m28s"  ·  "12h30m49.4s +12°23'28\""
  * Compact IAU names:      "J123049.4+122328"  ·  "123049.4+122328"
  * Galactic:               "galactic 283.78 +74.49"  ·  "l=283.78 b=74.49"

The parsing cascade is: normalise -> frame prefix -> compact IAU -> split into
an RA/Dec pair -> astropy -> Sesame. Anything that fails the coordinate path
falls through to a name lookup, so widening the coordinate patterns can never
make a previously-resolvable object name stop working.
"""

import re
import unicodedata

import astropy.units as u
from astropy.coordinates import FK4, Angle, SkyCoord


class ResolveError(ValueError):
    """Raised when a position string cannot be interpreted."""


# --- paste hygiene ---------------------------------------------------------
# Characters that arrive when a position is copied out of a paper, a PDF or a
# web table. Applied BEFORE any NFKC pass: NFKC expands U+2033 into two prime
# characters and leaves U+2212 MINUS alone, so doing it the other way round
# would mangle the very characters this table exists to fix.
_CHAR_MAP = {
    # dashes and minus signs -> ASCII hyphen
    0x2212: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-",
    0xFE58: "-", 0xFE63: "-", 0xFF0D: "-",
    0xFF0B: "+",
    # primes and smart quotes -> arcmin / arcsec marks
    0x2032: "'", 0x2035: "'", 0x00B4: "'", 0x2018: "'", 0x2019: "'", 0x02B9: "'",
    0x2033: '"', 0x2036: '"', 0x201C: '"', 0x201D: '"', 0x02BA: '"',
    # degree lookalikes -> the real degree sign (astropy reads U+00B0 natively)
    0x00BA: "°", 0x02DA: "°",
    # exotic spaces -> plain space; zero-width junk -> nothing
    0x00A0: " ", 0x2007: " ", 0x2009: " ", 0x202F: " ", 0x205F: " ", 0x3000: " ",
    0x200B: "", 0xFEFF: "",
}


def _normalise(text: str) -> str:
    """Tidy a pasted string without changing its meaning."""
    s = (text or "").translate(_CHAR_MAP)
    s = unicodedata.normalize("NFKC", s)   # after the table: fullwidth digits etc.
    s = s.strip().strip("()[]").strip()
    s = s.rstrip(",;")
    return " ".join(s.split())


# --- frames ----------------------------------------------------------------
_FRAME_WORDS = {
    "galactic": ("galactic", True),
    "gal": ("galactic", True),
    "icrs": ("icrs", False),
    "j2000": ("icrs", False),
    "fk5": ("fk5", False),
    "fk4": ("fk4", False),
    "b1950": ("fk4", False),
}

# "l=283.78 b=74.49" (either order), comma optional.
_LB = re.compile(r"^l\s*=\s*(\S+?)\s*,?\s+b\s*=\s*(\S+)$", re.I)
_BL = re.compile(r"^b\s*=\s*(\S+?)\s*,?\s+l\s*=\s*(\S+)$", re.I)


def _strip_frame(s: str):
    """Pull a leading/trailing frame keyword off the string.

    Returns ``(frame, remainder, note, force_deg)``. ``force_deg`` is set for
    galactic input, whose longitude is conventionally degrees and must never be
    read as hours. Matching is on whole tokens only, so an object whose name
    merely starts with "Gal" is not mangled.
    """
    toks = s.split()
    if len(toks) >= 3:
        head = toks[0].lower().rstrip(",")
        if head in _FRAME_WORDS:
            frame, force = _FRAME_WORDS[head]
            return frame, " ".join(toks[1:]), None, force
        tail = toks[-1].lower()
        if tail in _FRAME_WORDS:
            frame, force = _FRAME_WORDS[tail]
            return frame, " ".join(toks[:-1]), None, force
    return None, s, None, False


def _galactic_pair(s: str):
    """Return ``(l_str, b_str)`` for the ``l=… b=…`` form, else ``None``."""
    m = _LB.match(s)
    if m:
        return m.group(1), m.group(2)
    m = _BL.match(s)
    if m:
        return m.group(2), m.group(1)
    return None


# --- compact IAU designations ----------------------------------------------
# "J123049.4+122328", "123049.4+122328", "J1230+1223", "B123049.4+122328".
# The fixed-width \d{2} groups mean a decimal pair such as "187.7059+12.3911"
# cannot match, so plain coordinates are never mistaken for a designation.
_IAU = re.compile(
    r"^(?P<pre>[JB])?"
    r"(?P<rh>\d{2})(?P<rm>\d{2})(?P<rs>\d{2}(?:\.\d*)?)?"
    r"(?P<sign>[+-])"
    r"(?P<dd>\d{2})(?P<dm>\d{2})(?P<ds>\d{2}(?:\.\d*)?)?$"
)


def _parse_iau(s: str):
    """Parse a compact IAU designation literally, or return ``None``.

    This runs before the Sesame fallback on purpose: "J123049.4+122328" states
    its own position, and reading it literally is both instant and exact,
    whereas a name lookup returns the catalogued source nearby instead.
    """
    m = _IAU.match(s)
    if m is None:
        return None
    g = m.groupdict()
    ra = f"{g['rh']} {g['rm']}" + (f" {g['rs']}" if g["rs"] else "")
    dec = f"{g['sign']}{g['dd']} {g['dm']}" + (f" {g['ds']}" if g["ds"] else "")
    frame = FK4(equinox="B1950") if g["pre"] == "B" else "icrs"
    coord = SkyCoord(ra, dec, unit=(u.hourangle, u.deg), frame=frame)
    return coord.icrs if g["pre"] == "B" else coord


# --- splitting an RA/Dec pair ----------------------------------------------
# A coordinate token: digits plus separators, unit letters and symbols. Any
# other letter disqualifies it, so "3C 273" and "NGC 5128" still go to Sesame
# without a wasted parse attempt.
_COORDISH = re.compile(r"^[+\-]?[\d.:hdms°'\" ]*\d[\d.:hdms°'\" ]*$", re.I)

# A sign that starts the declination in a run-together string such as
# "12h30m49.4s+12d23m28s" — it must follow a digit or a unit terminator.
_GLUED = re.compile(r"(?<=[\ds'\"°m])(?=[+-]\d)")


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _coordish(tok: str) -> bool:
    return bool(_COORDISH.match(tok)) and any(ch.isdigit() for ch in tok)


def _split_pair(s: str):
    """Split a position string into ``(ra_str, dec_str)``, or return ``None``."""
    if s.count(",") == 1:
        left, _, right = s.partition(",")
        if left.strip() and right.strip():
            return left.strip(), right.strip()
        return None
    if s.count(",") > 1:
        s = s.replace(",", " ")
        s = " ".join(s.split())

    if " " not in s:
        parts = _GLUED.split(s)
        return (parts[0], parts[1]) if len(parts) == 2 else None

    toks = s.split()
    # An explicit sign is the most reliable marker of where Dec begins, and it
    # works for any token count: "12h 30m 49.4s +12d 23m 28s", "12 30 49.4 +12 23".
    for i in range(1, len(toks)):
        if toks[i][0] in "+-":
            return " ".join(toks[:i]), " ".join(toks[i:])

    n = len(toks)
    if n == 2:
        return toks[0], toks[1]
    if n in (4, 6):
        half = n // 2
        return " ".join(toks[:half]), " ".join(toks[half:])
    if n == 5:
        # RA carries seconds, Dec is truncated at arcminutes.
        return " ".join(toks[:3]), " ".join(toks[3:])
    return None


def _ra_unit(tok: str) -> u.Unit:
    """Fallback unit for a right ascension that carries no unit marker.

    This is the one place the degrees-vs-hours decision lives, and it matters:
    ``SkyCoord("187.7059 12.3911", unit=(hourangle, deg))`` yields RA 295.59
    because astropy reads the bare number as *hours*. A plain decimal is degrees
    by convention; anything sexagesimal is hours. Tokens that do carry a marker
    ("12h30m49.4s", "187.7059d") override whatever is passed here, so they need
    no special handling.
    """
    return u.deg if _is_number(tok) else u.hourangle


def _make_coord(ra_str: str, dec_str: str, frame=None, force_deg: bool = False):
    ra_str, dec_str = ra_str.strip(), dec_str.strip()
    ra_unit = u.deg if force_deg else _ra_unit(ra_str)
    coord = SkyCoord(ra_str, dec_str, unit=(ra_unit, u.deg), frame=frame or "icrs")
    return coord.icrs if frame not in (None, "icrs") else coord


def resolve_position(text: str):
    """Return (SkyCoord, label, resolved_name).

    ``resolved_name`` is the object name if a name lookup was used, else None.
    """
    raw = (text or "").strip()
    s = _normalise(raw)
    if not s:
        raise ResolveError("Empty position.")

    frame, body, _, force_deg = _strip_frame(s)
    note = None
    coord = None
    parse_error = None

    # 1) "l=… b=…" states its own frame.
    lb = _galactic_pair(body if frame else s)
    if lb is not None:
        frame, force_deg = "galactic", True
        body = f"{lb[0]} {lb[1]}"

    # 2) A compact IAU designation is a literal position, not a name.
    if frame is None:
        try:
            coord = _parse_iau(body)
        except Exception:  # noqa: BLE001 - malformed designation: keep looking
            coord = None
        if coord is not None:
            note = f"parsed from designation {body}"

    # 3) An explicit RA/Dec pair.
    if coord is None:
        pair = _split_pair(body)
        if pair and _coordish(pair[0]) and _coordish(pair[1]):
            try:
                coord = _make_coord(pair[0], pair[1], frame, force_deg)
                if frame == "galactic":
                    note = f"from galactic l={pair[0]} b={pair[1]}"
                elif frame == "fk4":
                    note = "B1950 → ICRS"
            except Exception as exc:  # noqa: BLE001 - fall through to Sesame
                parse_error = exc

    if coord is not None:
        return coord, _format_label(coord, note), None

    # 4) Name resolution via Sesame, the last resort.
    try:
        coord = SkyCoord.from_name(raw)
    except Exception as exc:  # noqa: BLE001
        if parse_error is not None:
            raise ResolveError(
                f"Could not parse coordinates: {parse_error}"
            ) from parse_error
        raise ResolveError(
            f"'{raw}' is not valid coordinates and could not be resolved as an "
            "object name."
        ) from exc
    return coord, _format_label(coord), raw


def _format_label(coord: SkyCoord, note: str = None) -> str:
    ra = coord.ra.to_string(unit=u.hour, sep=":", precision=2, pad=True)
    dec = coord.dec.to_string(unit=u.deg, sep=":", precision=1, alwayssign=True, pad=True)
    label = f"{coord.ra.deg:.5f}, {coord.dec.deg:.5f}  ({ra} {dec})"
    return f"{label} · {note}" if note else label
