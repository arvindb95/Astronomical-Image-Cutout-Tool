"use strict";

const form = document.getElementById("cutout-form");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const submitBtn = document.getElementById("submit");

function renderOpts() {
  let cmap = document.getElementById("cmap").value;
  let invert = "0";
  const auto = cmap === "auto";  // pick each survey's wavelength-based colormap
  if (cmap === "gray_r") { cmap = "gray"; invert = "1"; }
  return { stretch: document.getElementById("stretch").value, cmap, invert, auto };
}

function setStatus(html, cls) {
  statusEl.innerHTML = html;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

function buildPreviewUrl(base, opts) {
  const u = new URL(base, window.location.origin);
  u.searchParams.set("stretch", opts.stretch);
  u.searchParams.set("cmap", opts.cmap);
  u.searchParams.set("invert", opts.invert);
  return u.pathname + u.search;
}

function card(result, surveyMeta, opts) {
  const meta = surveyMeta[result.key] || { name: result.key, wavelength: "", cmap: "gray" };
  const el = document.createElement("div");
  el.className = "card" + (result.available ? "" : " unavailable");

  const variantTag = result.variant ? ` <em class="epoch-tag">· ${result.variant}</em>` : "";
  const head = `<div class="head"><span class="title">${meta.name}${variantTag}</span>` +
               `<span class="band">${meta.wavelength || ""}</span></div>`;

  if (result.available) {
    // In auto mode each survey uses its wavelength-based colormap (no inversion).
    const eff = opts.auto
      ? { stretch: opts.stretch, cmap: meta.cmap || "gray", invert: "0" }
      : opts;
    const src = buildPreviewUrl(result.preview_url, eff);
    el.innerHTML = head +
      `<div class="imgwrap loading"><img alt="${meta.name} cutout" /></div>` +
      `<div class="foot"><span class="band">PNG preview</span>` +
      `<a href="${result.fits_url}" download>Download FITS</a></div>`;
    const wrap = el.querySelector(".imgwrap");
    const img = el.querySelector("img");
    img.onload = () => wrap.classList.remove("loading");
    img.onerror = () => {
      wrap.classList.remove("loading");
      wrap.innerHTML = `<div class="nocov">Preview failed</div>`;
    };
    img.src = src;
  } else {
    el.innerHTML = head + `<div class="nocov">${result.error || "No coverage"}</div>`;
  }
  return el;
}

async function run(ev) {
  ev.preventDefault();
  const position = document.getElementById("position").value.trim();
  if (!position) { setStatus("Enter a position or object name.", "err"); return; }

  const size = document.getElementById("size").value || "3";
  const surveys = [...form.querySelectorAll('input[name="survey"]:checked')].map(c => c.value);
  if (surveys.length === 0) { setStatus("Select at least one survey.", "err"); return; }

  const surveyMeta = {};
  form.querySelectorAll(".chk").forEach(chk => {
    const input = chk.querySelector("input");
    surveyMeta[input.value] = {
      name: chk.querySelector("span").textContent,
      wavelength: chk.querySelector("em").textContent,
      cmap: input.dataset.cmap || "gray",
    };
  });

  submitBtn.disabled = true;
  resultsEl.innerHTML = "";
  setStatus("Resolving position and fetching cutouts… (first fetch from SkyView can take a while)");

  const keep = document.getElementById("keep-fits").checked ? "1" : "0";
  const params = new URLSearchParams({ position, size, surveys: surveys.join(","), keep });
  // Per-survey selection (epoch/filter) for surveys that expose a dropdown.
  form.querySelectorAll(".sel-select").forEach(sel => {
    const key = sel.dataset.survey;
    if (sel.value && surveys.includes(key)) params.set("sel_" + key, sel.value);
  });
  try {
    const resp = await fetch("/api/cutout?" + params.toString());
    const data = await resp.json();
    if (!resp.ok) { setStatus(data.error || "Request failed.", "err"); return; }

    const p = data.position;
    const nameBit = p.name ? `<b>${p.name}</b> → ` : "";
    setStatus(`${nameBit}<span class="coord">${p.label}</span> · ${data.size}′ field`, "ok");

    const opts = renderOpts();
    const avail = data.results.filter(r => r.available).length;
    data.results.forEach(r => resultsEl.appendChild(card(r, surveyMeta, opts)));
    if (avail === 0) setStatus(statusEl.innerHTML + " · no survey covers this position.", "ok");
  } catch (e) {
    setStatus("Network or server error: " + e.message, "err");
  } finally {
    submitBtn.disabled = false;
  }
}

form.addEventListener("submit", run);
