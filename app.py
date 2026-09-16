"""Astronomical image cutout server.

A small Flask app that serves image cutouts from multiple sky surveys, modelled
on the CIRADA cutout service (cutouts.cirada.ca). Surveys are pulled from NASA
SkyView, CADC (VLASS), CDS hips2fits and the LOFAR surveys cutout server; more
can be added in cutout/providers.py.
"""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from cutout.providers import GROUP_ORDER, PROVIDERS, get_provider
from cutout.render import _image_hdu, _obs_date_from_header, render_png
from cutout.resolve import ResolveError, resolve_position
from cutout.service import (
    CutoutError,
    get_cutout_fits,
    load_fits,
    remove_cutout_fits,
)

app = Flask(__name__)

MAX_WORKERS = 8
MIN_SIZE_ARCMIN = 0.5
MAX_SIZE_ARCMIN = 60.0


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Whether to keep cached FITS on disk by default. Default is to *remove* the
# FITS once the PNG is rendered (set CUTOUT_KEEP_FITS=1 to keep them). The UI
# checkbox overrides this per request.
KEEP_FITS_DEFAULT = _truthy(os.environ.get("CUTOUT_KEEP_FITS", "0"))


def _keep_flag() -> bool:
    """Resolve the keep-FITS flag for this request (query param > env default)."""
    value = request.args.get("keep")
    return KEEP_FITS_DEFAULT if value is None else _truthy(value)


@app.route("/")
def index():
    # group -> subgroup -> [survey info]. Subgroups (the radio frequency bands)
    # keep their PROVIDERS order; surveys with no subgroup sit under the key ""
    # and the template renders them without a sub-heading.
    groups: dict[str, dict[str, list]] = {}
    for p in PROVIDERS:
        groups.setdefault(p.group, {}).setdefault(p.subgroup, []).append(p.info())
    # Order groups by wavelength (Radio, Infrared, Optical); any extra groups
    # fall to the end in their natural order.
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    ordered = dict(sorted(groups.items(), key=lambda kv: rank.get(kv[0], len(rank))))
    return render_template(
        "index.html", groups=ordered, keep_default=KEEP_FITS_DEFAULT
    )


@app.route("/api/surveys")
def surveys():
    return jsonify([p.info() for p in PROVIDERS])


@app.route("/api/cutout")
def cutout():
    """Resolve a position and report which requested surveys have a cutout.

    Query params: position, size (arcmin), surveys (comma-separated keys).
    Returns JSON with the resolved coordinates and per-survey availability plus
    preview / FITS URLs.
    """
    position = request.args.get("position", "").strip()
    if not position:
        return jsonify(error="Please enter a position or object name."), 400

    try:
        size = float(request.args.get("size", "3"))
    except ValueError:
        return jsonify(error="Size must be a number (arcmin)."), 400
    size = max(MIN_SIZE_ARCMIN, min(MAX_SIZE_ARCMIN, size))

    keys = [k for k in request.args.get("surveys", "").split(",") if k]
    if not keys:
        keys = [p.key for p in PROVIDERS if p.default]

    try:
        coord, label, resolved_name = resolve_position(position)
    except ResolveError as exc:
        return jsonify(error=str(exc)), 400

    ra, dec = float(coord.ra.deg), float(coord.dec.deg)

    # Read per-survey selector params and the keep-FITS flag now: request
    # context isn't available inside the worker threads below.
    sel_params = {k: (request.args.get(f"sel_{k}") or None) for k in keys}
    keep = _keep_flag()
    keep_q = "1" if keep else "0"

    def _one(key: str, option) -> dict:
        provider = get_provider(key)
        result = {"key": key}
        if option and provider is not None and provider.select is not None:
            result["variant"] = provider.select.render(option)
        try:
            path = get_cutout_fits(key, ra, dec, size, option=option)
            result["available"] = True
            result.update(_obs_info(path, provider))
            params = f"survey={key}&ra={ra:.5f}&dec={dec:.5f}&size={size:.3f}"
            if option:
                params += f"&sel={option}"
            params += f"&keep={keep_q}"  # carry the cache choice into both URLs
            result["preview_url"] = f"/api/preview.png?{params}"
            result["fits_url"] = f"/api/fits?{params}"
        except CutoutError as exc:
            result["available"] = False
            result["error"] = str(exc)
        return result

    def _probe(key: str) -> list:
        # Per-survey selection (sel_<key>). "all" fans out into one card per
        # option; surveys without a selector ignore the parameter.
        sel = sel_params.get(key)
        provider = get_provider(key)
        select = provider.select if provider is not None else None
        if sel == "all" and select is not None:
            cards = [_one(key, v) for v in select.options]
            shown = [c for c in cards if c["available"]]
            if shown:
                return shown  # only options that actually exist at this position
            return [{"key": key, "available": False,
                     "error": "no coverage at this position."}]
        if select is None:
            return [_one(key, None)]
        # Apply the survey default when nothing (or the auto entry) is chosen.
        value = sel if sel not in (None, "", "latest") else (select.default or None)
        return [_one(key, value)]

    results: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_probe, k): k for k in keys}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    # Preserve the requested survey order; flatten the per-survey card lists.
    ordered = [card for k in keys for card in results.get(k, [])]
    return jsonify(
        position={"ra": ra, "dec": dec, "label": label, "name": resolved_name},
        size=size,
        results=ordered,
    )


@app.route("/api/preview.png")
def preview():
    key, ra, dec, size = _cutout_params()
    option = request.args.get("sel") or None
    cmap = request.args.get("cmap", "gray")
    stretch = request.args.get("stretch", "zscale")
    invert = request.args.get("invert", "0") in ("1", "true", "yes")
    keep = _keep_flag()
    provider = get_provider(key)
    try:
        path = get_cutout_fits(key, ra, dec, size, option=option)
        with load_fits(path) as hdul:
            png = render_png(
                hdul, cmap=cmap, stretch=stretch, invert=invert,
                obs_period=provider.obs_period if provider else "",
            )
    except CutoutError as exc:
        abort(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        abort(500, str(exc))
    if not keep:
        remove_cutout_fits(path)  # drop the FITS once the PNG is made
    return Response(png, mimetype="image/png")


@app.route("/api/fits")
def fits_download():
    key, ra, dec, size = _cutout_params()
    option = request.args.get("sel") or None
    keep = _keep_flag()
    try:
        # In remove mode the FITS was deleted after the preview, so this may
        # re-fetch it from the survey before serving.
        path = get_cutout_fits(key, ra, dec, size, option=option)
    except CutoutError as exc:
        abort(404, str(exc))
    with open(path, "rb") as fh:
        data = fh.read()
    if not keep:
        remove_cutout_fits(path)  # serve from memory, don't retain on disk
    opt_bit = f"_{option}" if option else ""
    fname = f"{key}{opt_bit}_{ra:.5f}{dec:+.5f}_{size:.1f}arcmin.fits"
    return send_file(
        io.BytesIO(data),
        mimetype="application/fits",
        as_attachment=True,
        download_name=fname,
    )


def _obs_info(path: str, provider) -> dict:
    """When this cutout was observed: the FITS date if it has one, else the
    survey's campaign span. ``exact`` tells the UI which of the two it got."""
    try:
        with load_fits(path) as hdul:
            date = _obs_date_from_header(_image_hdu(hdul).header)
    except Exception:  # noqa: BLE001 - a header we can't read is not fatal here
        date = None
    if date:
        return {"obs_date": date, "obs_exact": True}
    period = provider.obs_period if provider else ""
    return {"obs_date": period, "obs_exact": False} if period else {}


def _cutout_params():
    key = request.args.get("survey", "")
    try:
        ra = float(request.args.get("ra"))
        dec = float(request.args.get("dec"))
        size = float(request.args.get("size"))
    except (TypeError, ValueError):
        abort(400, "Missing or invalid ra/dec/size.")
    return key, ra, dec, size


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
