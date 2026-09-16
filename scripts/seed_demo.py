#!/usr/bin/env python
"""Seed the bundled demo cutout set (M87) into the permanent ``demo_cache/``.

The demo files are exempt from the keep/remove cleanup, so the example always
loads instantly. Their names match exactly what a default "M87" search requests
(default surveys, 3' field, default bands/epochs), so it is a pure cache hit.

Run from the project root:  PYTHONPATH=. python scripts/seed_demo.py
(Then regenerate the README screenshot with scripts/screenshot_ui.py.)
"""

from __future__ import annotations

import os
import warnings

warnings.filterwarnings("ignore")

from cutout.providers import PROVIDERS
from cutout.resolve import resolve_position
from cutout.service import CutoutError, seed_demo_fits

DEMO_OBJECT = "M87"
DEMO_SIZE_ARCMIN = 3.0


def main() -> None:
    coord, label, name = resolve_position(DEMO_OBJECT)
    ra, dec = float(coord.ra.deg), float(coord.dec.deg)
    print(f"Seeding {name} ({label})  ra={ra:.5f} dec={dec:+.5f}")

    seeded = skipped = 0
    for p in (p for p in PROVIDERS if p.default):
        option = (p.select.default or None) if p.select else None
        try:
            path = seed_demo_fits(p.key, ra, dec, DEMO_SIZE_ARCMIN, option=option)
        except CutoutError as exc:
            # Not every default survey reaches M87 — LoTSS and SUMSS, say, cover
            # other parts of the sky. Those cards just render "no coverage".
            print(f"  {p.key:11} -- skipped ({exc})")
            skipped += 1
            continue
        print(f"  {p.key:11} -> {os.path.basename(path)}")
        seeded += 1
    print(f"Done. {seeded} seeded, {skipped} without coverage at {DEMO_OBJECT}.")


if __name__ == "__main__":
    main()
