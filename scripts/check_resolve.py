#!/usr/bin/env python
"""Check cutout.resolve against the coordinate formats we promise to accept.

pytest is not installed in the project env, so this is a plain script in the
style of scripts/seed_demo.py: it prints a table and exits non-zero on failure.

Run from the project root:  PYTHONPATH=. python scripts/check_resolve.py
Add --network to also exercise the Sesame name-lookup cases.
"""

from __future__ import annotations

import sys
import warnings

warnings.filterwarnings("ignore")

from cutout.resolve import ResolveError, resolve_position

TOL_DEG = 1.0 / 3600.0  # 1 arcsec

# (input, expected RA deg, expected Dec deg, note). Expected values are the
# literal reading of the string, verified against astropy.
CASES = [
    # --- formats that already worked: regression guards --------------------
    ("187.7059 12.3911", 187.70590, +12.39110, "decimal degrees"),
    ("187.7059, 12.3911", 187.70590, +12.39110, "decimal, comma"),
    ("187.7059 -12.3911", 187.70590, -12.39110, "negative dec"),
    ("12:30:49.4 +12:23:28", 187.70583, +12.39111, "sexagesimal, colons"),
    ("12 30 49.4 +12 23 28", 187.70583, +12.39111, "sexagesimal, 6 tokens"),
    ("12 30 49.4, +12 23 28", 187.70583, +12.39111, "sexagesimal, comma"),
    ("12:30:49.4 12:23:28", 187.70583, +12.39111, "unsigned dec"),
    ("  187.7059 ,  12.3911  ", 187.70590, +12.39110, "stray whitespace"),
    # --- newly accepted ----------------------------------------------------
    ("12h30m49.4s +12d23m28s", 187.70583, +12.39111, "hms/dms letters"),
    ("12h30m49.4s+12d23m28s", 187.70583, +12.39111, "hms/dms, no space"),
    ("12h 30m 49.4s +12d 23m 28s", 187.70583, +12.39111, "hms/dms, spaced"),
    ("12h30m49.4s +12°23'28\"", 187.70583, +12.39111, "degree/prime symbols"),
    ("187.7059d 12.3911d", 187.70590, +12.39110, "explicit 'd' marker"),
    ("187.7059° 12.3911°", 187.70590, +12.39110, "degree signs"),
    ("187.7059 −12.3911", 187.70590, -12.39110, "unicode minus"),
    ("187.7059 12.3911", 187.70590, +12.39110, "non-breaking space"),
    ("123049.4+122328", 187.70583, +12.39111, "compact IAU, no prefix"),
    ("J123049.4+122328", 187.70583, +12.39111, "compact IAU, J prefix"),
    ("J1230+1223", 187.50000, +12.38333, "compact IAU, short form"),
    ("B123049.4+122328", 188.33805, +12.11546, "compact IAU, B1950"),
    ("12 30 49.4 +12 23", 187.70583, +12.38333, "5 tokens (dec to arcmin)"),
    ("galactic 283.78 +74.49", 187.70616, +12.38984, "galactic, leading word"),
    ("283.78 +74.49 galactic", 187.70616, +12.38984, "galactic, trailing word"),
    ("gal 283.78 -5.2", 149.69115, -61.47345, "galactic, abbreviated"),
    ("l=283.78 b=74.49", 187.70616, +12.38984, "galactic, l=/b="),
]

# Inputs that must raise ResolveError rather than silently mis-parse.
ERRORS = [
    ("", "empty"),
    ("   ", "whitespace only"),
    ("187.7059 95.0", "declination out of range"),
]

# Name lookups (need the network).
NAMES = [
    ("M87", 187.7059, +12.3911),
    ("3C 273", 187.2779, +2.0524),
]


def main() -> int:
    failures = 0
    print(f"{'input':34s} {'got':26s} {'expected':26s} note")
    print("-" * 108)
    for text, era, edec, note in CASES:
        try:
            coord, _, name = resolve_position(text)
            ra, dec = coord.ra.deg, coord.dec.deg
            ok = abs(ra - era) < TOL_DEG and abs(dec - edec) < TOL_DEG
            # A literal coordinate parse must never report a resolved name:
            # that is what tells the UI it was read, not looked up.
            if name is not None:
                ok = False
                note += "  [went to Sesame!]"
            got = f"{ra:.5f} {dec:+.5f}"
        except Exception as exc:  # noqa: BLE001
            ok, got = False, f"ERROR {type(exc).__name__}: {str(exc)[:30]}"
        if not ok:
            failures += 1
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {text!r:32s} {got:26s} {era:.5f} {edec:+.5f}     {note}")

    print()
    for text, note in ERRORS:
        try:
            resolve_position(text)
            failures += 1
            print(f"FAIL {text!r:32s} expected ResolveError ({note})")
        except ResolveError:
            print(f"ok   {text!r:32s} raised ResolveError ({note})")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {text!r:32s} raised {type(exc).__name__}, want ResolveError")

    if "--network" in sys.argv:
        print()
        for text, era, edec in NAMES:
            try:
                coord, _, name = resolve_position(text)
                ok = (abs(coord.ra.deg - era) < 0.01
                      and abs(coord.dec.deg - edec) < 0.01
                      and name == text)
            except Exception:  # noqa: BLE001
                ok = False
            failures += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} {text!r:32s} Sesame name lookup")

    print()
    total = len(CASES) + len(ERRORS) + (len(NAMES) if "--network" in sys.argv else 0)
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
