/* Nazar bar: injected into every sector dashboard by the hub. Shows which sector this radar is, lets you jump to any
   other sector (the hub remembers the choice in a cookie) or back to Nazar's own overview. */
(function () {
  const N = window.NAZAR; if (!N) return;
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  document.documentElement.style.setProperty("--sector", N.color || "#2a6fe0");
  const eye = '<svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="30" fill="#0f2a5c"/><circle cx="32" cy="32" r="21" fill="#fff"/><circle cx="32" cy="32" r="13" fill="#2a78d6"/><circle cx="32" cy="32" r="6" fill="#0b0b0b"/><circle cx="28" cy="27" r="2.2" fill="#fff"/></svg>';
  const bar = document.createElement("div");
  bar.className = "nz-bar";
  bar.setAttribute("role", "navigation"); bar.setAttribute("aria-label", "Nazar sectors");
  /* whose seat the dashboard is written from: Neutral or any company of the sector (stored per sector, default neutral) */
  const who = `<label class="nz-who" title="Read this sector as a neutral analyst, or as one company (its risks, price matches and own-sales panels)">
      <span class="nz-who-l">${N.self ? "written for" : "view"}</span>
      <select class="nz-persp" aria-label="Written for"><option value="">${esc(N.self ? "Neutral" : "Neutral view")}</option>${N.self ? `<option value="${esc(N.self)}" selected>${esc(N.self)}</option>` : ""}</select>
    </label>`;
  bar.innerHTML = `<a class="nz-eye" href="/" title="Nazar: all sectors">${eye}<span>Nazar</span></a>
    <div class="nz-sectors">${(N.sectors || []).map(s => `<a class="nz-sec ${s.key === N.sector ? "on" : ""}" href="/s/${esc(s.key)}${location.hash || ""}" style="--c:${esc(s.color)}" title="${esc(s.label)}"><i></i>${esc(s.short || s.label)}</a>`).join("")}</div>
    ${who}`;
  document.body.insertBefore(bar, document.body.firstChild);
  /* fill the seat picker with every brand of this sector; switching posts to the sector's own API and reloads */
  const sel = bar.querySelector(".nz-persp");
  fetch("/api/perspective").then(r => r.ok ? r.json() : null).then(st => {
    if (!st) return;
    const cur = st.self || "";
    sel.innerHTML = `<option value="">Neutral view</option>` + (st.options || []).map(o => `<option value="${esc(o.name)}" ${o.name === cur ? "selected" : ""}>${esc(o.name)}</option>`).join("");
    bar.querySelector(".nz-who-l").textContent = cur ? "written for" : "view";
  }).catch(() => {});
  sel.onchange = async () => {
    sel.disabled = true;
    try {
      const r = await fetch("/api/perspective", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ brand: sel.value }) });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      location.reload();
    } catch (e) { sel.disabled = false; alert("Could not switch: " + e.message); }
  };
  /* the radar's sidebar is 100vh sticky and its tab strips stick to the top: give both the bar's height */
  const fix = () => { const h = bar.offsetHeight; document.documentElement.style.setProperty("--barh", h + "px"); const nav = document.querySelector("nav"); if (nav) { nav.style.height = `calc(100vh - ${h}px)`; nav.style.top = h + "px"; } };
  fix(); addEventListener("resize", fix);
  /* keep the current #/route when switching sector */
  addEventListener("hashchange", () => bar.querySelectorAll(".nz-sec").forEach(a => { a.href = a.href.split("#")[0] + (location.hash || ""); }));
  if (N.sector) document.title = `Nazar · ${N.label}`;
  /* a neutral sector has no "home brand": the tiles, chips and cards the radar built around one would only show dashes */
  /* a seat without first-party figures (any brand other than the YAML home brand) has no at-risk money or own sales either */
  if (!N.self || !N.own_data) {
    const seatLabel = N.self ? (N.self + " revenue").toLowerCase() : null;   // the "<seat> revenue" tile needs first-party figures
    const RX = N.self ? /at risk|upside|own sales|money at stake/i : /home brand|at risk|upside|own sales|on the shelf|you|your/i;
    const prune = () => {
      document.querySelectorAll("#view .kpi, #view .xk").forEach(k => { const l = k.querySelector(".l"); if (l && (RX.test(l.textContent) || (seatLabel && l.textContent.trim().toLowerCase() === seatLabel))) k.remove(); });
      document.querySelectorAll("#view .chip").forEach(c => { if (/home brand/i.test(c.textContent)) c.remove(); });
      const H2 = N.self ? /^(money at stake|own sales by state|pressure on )/i : /^(scale vs (home brand|you)|pressure on (home brand|you)|(home brand|you) vs|money at stake|(home brand|your) strongholds|own sales by state)/i;
      document.querySelectorAll("#view .card > h2").forEach(h => { if (H2.test(h.textContent.trim())) h.parentElement.remove(); });
    };
    const view = document.getElementById("view") || document.body;
    new MutationObserver(prune).observe(view, { childList: true, subtree: true });
    prune();
  }
})();
