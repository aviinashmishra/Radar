/* Phone and tablet layer, shared by the Nazar hub and every sector dashboard (served as /static/mobile.js and
   /nazar/static/mobile.js). Like ux.js it only rearranges what the pages already render; theme.css styles it.
     phone (<= 760 px)   a bottom tab bar replaces the sidebar / top menu; "More" opens a bottom sheet with every page,
                         the sectors, search and the theme; the sector strip becomes one button that opens a sector
                         sheet; the top bar slides away while you read down and comes back when you scroll up; swipe
                         left / right on a tabbed page to change tab; long lists load in batches
     phone + tablet      tab strips and the sector strip keep the open item in view; wide tables scroll inside their
                         card with the first column pinned
   Loaded with `defer`, so it runs after the page's own scripts and after the Nazar bar. */
(function () {
  const doc = document, root = doc.documentElement;
  const q = (s, r = doc) => r.querySelector(s), qa = (s, r = doc) => [...r.querySelectorAll(s)];
  const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const PHONE = matchMedia("(max-width: 760px)");
  const DASH = !!q("#nav a.item");           // a sector dashboard (sidebar navigation)
  const HUB = !DASH && !!q("nav.topnav");    // the Nazar hub (top menu)
  if (!DASH && !HUB) return;
  const N = window.NAZAR || null;            // set by the hub on a sector page: the sector list for the switcher
  const view = q("#view");

  /* ---------- icons ---------- */
  const svg = (d, fill) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true"${fill ? ' style="fill:currentColor;stroke:none"' : ""}>${d}</svg>`;
  const IC = {
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    pulse: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    markets: '<path d="M3 17l5-6 4 4 5-7 4 4"/><path d="M3 21h18"/>',
    universe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    themes: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
    lens: '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    sectors: '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/>',
    about: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.4"/>',
    more: '<circle cx="5" cy="12" r="1.9"/><circle cx="12" cy="12" r="1.9"/><circle cx="19" cy="12" r="1.9"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    auto: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    caret: '<path d="M6 9l6 6 6-6"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
  };

  /* ---------- the pages, read from the page's own navigation ---------- */
  const HUBICON = { "": "eye", pulse: "pulse", markets: "markets", universe: "universe", themes: "themes", lens: "lens", sectors: "sectors", about: "about" };
  const routeOf = href => (href || "").replace(/^.*#\/?/, "").split("/")[0];
  const current = () => { const r = routeOf(location.hash); return DASH ? ({ brand: "brands", segment: "segments", settings: "about" }[r] || r || "today") : r; };
  const PAGES = [];
  if (DASH) {
    let grp = "";
    for (const el of qa("#nav > .grp, #nav > a.item")) {
      if (el.classList.contains("grp")) { grp = el.textContent.trim(); continue; }
      if (el.classList.contains("op") && root.dataset.public === "1") continue;
      const use = q("svg", el);
      PAGES.push({ route: routeOf(el.getAttribute("href")), href: el.getAttribute("href"), label: (q("span", el) || el).textContent.trim(), icon: use ? use.outerHTML : svg(IC.about), group: grp, op: el.classList.contains("op") });
    }
  } else {
    for (const a of qa("nav.topnav a")) {
      const r = routeOf(a.getAttribute("href"));
      PAGES.push({ route: r, href: a.getAttribute("href"), label: a.textContent.trim(), icon: svg(IC[HUBICON[r]] || IC.about), group: "" });
    }
  }
  const PRIMARY = DASH ? ["today", "news", "social", "brands"] : ["", "pulse", "markets", "sectors"];
  const page = r => PAGES.find(p => p.route === r);

  /* ---------- bottom sheet ---------- */
  let openSheet = null;
  function sheet(html, onBind) {
    closeSheet(true);
    const scrim = doc.createElement("div"); scrim.className = "mx-scrim";
    const sh = doc.createElement("div"); sh.className = "mx-sheet"; sh.setAttribute("role", "dialog"); sh.setAttribute("aria-modal", "true");
    sh.innerHTML = `<div class="mx-grab" aria-hidden="true"></div><button class="mx-x" aria-label="Close">${svg(IC.close)}</button>${html}`;
    doc.body.append(scrim, sh); root.classList.add("mx-lock");
    requestAnimationFrame(() => { scrim.classList.add("on"); sh.classList.add("on"); });
    const close = () => closeSheet();
    scrim.onclick = close; q(".mx-x", sh).onclick = close;
    sh.addEventListener("click", e => { if (e.target.closest("a[href]")) setTimeout(close, 0); });
    /* drag the sheet down to dismiss it (from its handle, or anywhere once its content is scrolled to the top) */
    let y0 = null, dy = 0;
    sh.addEventListener("touchstart", e => { if (sh.scrollTop > 0) return; y0 = e.touches[0].clientY; dy = 0; sh.style.transition = "none"; }, { passive: true });
    sh.addEventListener("touchmove", e => { if (y0 == null) return; dy = Math.max(0, e.touches[0].clientY - y0); sh.style.transform = dy ? `translateY(${dy}px)` : ""; }, { passive: true });
    sh.addEventListener("touchend", () => { if (y0 == null) return; y0 = null; sh.style.transition = ""; sh.style.transform = ""; if (dy > 90) close(); }, { passive: true });
    openSheet = { scrim, sh };
    if (onBind) onBind(sh);
    setTimeout(() => { const f = q("a.on, button.on, a, button:not(.mx-x)", sh); if (f && !PHONE.matches) f.focus(); }, 50);
    return sh;
  }
  function closeSheet(now) {
    if (!openSheet) return;
    const { scrim, sh } = openSheet; openSheet = null; root.classList.remove("mx-lock");
    if (now) { scrim.remove(); sh.remove(); return; }
    scrim.classList.remove("on"); sh.classList.remove("on");
    setTimeout(() => { scrim.remove(); sh.remove(); }, 320);
  }
  doc.addEventListener("keydown", e => { if (e.key === "Escape" && openSheet) closeSheet(); });

  /* the sector picker (sector dashboards inside Nazar) */
  const sectorTiles = () => !N || !(N.sectors || []).length ? "" :
    `<div class="mx-grid mx-secs">${N.sectors.map(s => `<a class="mx-sec ${s.key === N.sector ? "on" : ""}" href="/s/${esc(s.key)}${esc(location.hash || "")}" style="--c:${esc(s.color)}"><span class="ic">${esc(s.icon || "●")}</span><span class="t"><b>${esc(s.short || s.label)}</b><small>${esc(s.label)}</small></span></a>`).join("")}</div>`;
  const openSectors = () => sheet(`<h3 class="mx-h">Sectors <span>each one is a full radar of its own</span></h3>${sectorTiles()}<a class="mx-wide" href="/">${svg(IC.eye)} Nazar home: every sector at once</a>`);

  /* ---------- "More": every page, sectors, search, theme ---------- */
  function themeRow() {
    const t = root.dataset.theme || "auto";
    return `<div class="mx-theme" role="group" aria-label="Theme">${[["light", "Light", IC.sun], ["dark", "Dark", IC.moon], ["auto", "Auto", IC.auto]].map(([k, l, i]) => `<button data-mxt="${k}" class="${t === k ? "on" : ""}">${svg(i)}<span>${l}</span></button>`).join("")}</div>`;
  }
  function openMore() {
    const cur = current();
    const groups = [];
    for (const p of PAGES) { let g = groups.find(x => x.name === p.group); if (!g) groups.push(g = { name: p.group, items: [] }); g.items.push(p); }
    const live = q("#livetxt");
    const html = `<button class="mx-searchrow" data-mxsearch>${svg(IC.search)}<span>${DASH ? "Search a company or page, or ask" : "Search companies, moves, or ask Nazar"}</span></button>
      ${N && (N.sectors || []).length ? `<button class="mx-secrow" data-mxsec style="--c:${esc(N.color || "var(--accent)")}"><span class="ic">${esc(N.icon || "●")}</span><span class="t"><small>Sector</small><b>${esc(N.label || "")}</b></span><span class="go">Switch</span></button>` : ""}
      ${groups.map(g => `${g.name ? `<h3 class="mx-h">${esc(g.name)}</h3>` : `<h3 class="mx-h">Pages</h3>`}<div class="mx-grid">${g.items.map(p => `<a class="mx-tile ${p.route === cur ? "on" : ""}" href="${esc(p.href)}">${p.icon}<span>${esc(p.label)}</span></a>`).join("")}</div>`).join("")}
      <h3 class="mx-h">Appearance</h3>${themeRow()}
      ${DASH && N ? `<a class="mx-wide" href="/">${svg(IC.eye)} Nazar home: every sector at once</a>` : ""}
      ${live && live.textContent.trim() ? `<div class="mx-foot"><span class="dot ${q("#dot") && q("#dot").classList.contains("off") ? "off" : ""}"></span>${esc(live.textContent.trim())}</div>` : ""}`;
    sheet(html, sh => {
      q("[data-mxsearch]", sh).onclick = () => { closeSheet(true); const s = q("#searchbtn"); if (s) s.click(); };
      const sw = q("[data-mxsec]", sh); if (sw) sw.onclick = openSectors;
      qa("[data-mxt]", sh).forEach(b => b.onclick = () => {
        const real = q(`#theme button[data-t="${b.dataset.mxt}"]`); if (real) real.click();
        qa("[data-mxt]", sh).forEach(x => x.classList.toggle("on", x === b));
      });
    });
  }

  /* ---------- bottom tab bar ---------- */
  const bar = doc.createElement("div");   // not a <nav>: the dashboard styles every nav element as its sidebar
  bar.className = "mx-tabbar"; bar.setAttribute("role", "navigation"); bar.setAttribute("aria-label", "Sections");
  bar.innerHTML = PRIMARY.map(r => page(r)).filter(Boolean).map(p => `<a href="${esc(p.href)}" data-r="${esc(p.route)}"><span class="ic">${p.icon}</span><span class="l">${esc(p.label)}</span></a>`).join("")
    + `<button type="button" data-more aria-haspopup="dialog"><span class="ic">${svg(IC.more, true)}</span><span class="l">More</span></button>`;
  doc.body.appendChild(bar);
  const moreBtn = q("[data-more]", bar);
  moreBtn.onclick = openMore;
  function syncBar() {
    const cur = current(); let hit = false;
    qa("a[data-r]", bar).forEach(a => { const on = a.dataset.r === cur; a.classList.toggle("on", on); if (on) { hit = true; a.setAttribute("aria-current", "page"); } else a.removeAttribute("aria-current"); });
    /* a page that is not one of the four shows itself in the More slot, so you always see where you are */
    const p = !hit && page(cur);
    moreBtn.classList.toggle("on", !!p);
    moreBtn.innerHTML = p ? `<span class="ic">${p.icon}</span><span class="l">${esc(p.label)}</span>` : `<span class="ic">${svg(IC.more, true)}</span><span class="l">More</span>`;
  }
  addEventListener("hashchange", () => { syncBar(); closeSheet(true); });
  syncBar();

  /* ---------- the sector strip on a sector dashboard ---------- */
  const nzbar = q(".nz-bar");
  if (nzbar && N) {
    const me = (N.sectors || []).find(s => s.key === N.sector) || N;
    const b = doc.createElement("button");
    b.type = "button"; b.className = "nz-cur"; b.setAttribute("aria-haspopup", "dialog"); b.style.setProperty("--c", me.color || N.color || "var(--accent)");
    b.innerHTML = `<i></i><span>${esc(me.label || me.short || "Sector")}</span>${svg(IC.caret)}`;
    b.onclick = openSectors;
    const eye = q(".nz-eye", nzbar); (eye ? eye.after(b) : nzbar.prepend(b));
    /* tablet: the strip scrolls; keep the open sector in view */
    const strip = q(".nz-sectors", nzbar), on = strip && q(".nz-sec.on", strip);
    if (on) centerIn(strip, on);
  }
  /* the hub's search had no icon: give it one (the phone shows the icon alone) */
  const sb = q("#searchbtn");
  if (sb && !q("svg", sb)) sb.insertAdjacentHTML("afterbegin", svg(IC.search));
  if (sb) sb.setAttribute("aria-label", "Search");

  /* ---------- the top bar steps aside while you read down (phone) ---------- */
  let lastY = scrollY, ticking = false;
  addEventListener("scroll", () => {
    if (ticking) return; ticking = true;
    requestAnimationFrame(() => {
      ticking = false; const y = scrollY;
      if (!PHONE.matches || openSheet) { root.classList.remove("mx-away"); lastY = y; return; }
      if (y > lastY + 8 && y > 160) root.classList.add("mx-away");
      else if (y < lastY - 8 || y < 80) root.classList.remove("mx-away");
      if (Math.abs(y - lastY) > 8) lastY = y;
    });
  }, { passive: true });

  /* ---------- tab strips: the open tab stays in view; swipe the page to change tab (phone) ---------- */
  function centerIn(box, el) {
    const br = el.getBoundingClientRect(), xr = box.getBoundingClientRect();
    box.scrollLeft += (br.left - xr.left) - (box.clientWidth - br.width) / 2;
  }
  function tabStrips() {
    for (const t of qa("#view .tabs")) {
      const over = t.scrollWidth > t.clientWidth + 2;
      t.classList.toggle("mx-of", over);
      const on = q("button.on", t);
      if (over && on && !t.dataset.mxc) { t.dataset.mxc = "1"; centerIn(t, on); }
    }
  }
  doc.addEventListener("click", e => {
    const b = e.target.closest(".tabs button"); if (!b) return;
    requestAnimationFrame(() => {
      const t = b.closest(".tabs"); if (t && t.scrollWidth > t.clientWidth + 2) centerIn(t, q("button.on", t) || b);
      tables(); folds();   // a panel that was hidden has its tables and lists measured only now
    });
  });
  const pagerTabs = () => qa("#view .tabs:not(.inline)").find(t => qa("button", t).length > 1 && !t.closest(".card"));
  const scrollsX = el => { for (; el && el !== view; el = el.parentElement) { if (el.scrollWidth > el.clientWidth + 2 && /(auto|scroll)/.test(getComputedStyle(el).overflowX)) return true; } return false; };
  let sx = 0, sy = 0, st = 0, armed = false;
  if (DASH && view) {
    view.addEventListener("touchstart", e => {
      armed = false; if (!PHONE.matches || e.touches.length !== 1) return;
      const t = e.touches[0]; sx = t.clientX; sy = t.clientY; st = Date.now();
      armed = sx > 24 && sx < innerWidth - 24 && !scrollsX(e.target) && !e.target.closest("input,select,textarea,svg,.mx-sheet") && !!pagerTabs();
    }, { passive: true });
    view.addEventListener("touchend", e => {
      if (!armed) return; armed = false;
      const t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy;
      if (Math.abs(dx) < 70 || Math.abs(dy) > Math.abs(dx) * 0.55 || Date.now() - st > 650) return;
      const tabs = pagerTabs(); if (!tabs) return;
      const bs = qa("button", tabs), i = bs.findIndex(b => b.classList.contains("on")), nx = bs[i + (dx < 0 ? 1 : -1)];
      if (!nx) return;
      nx.click();
      requestAnimationFrame(() => {
        const panel = q("#view > .tabpanel:not([hidden])");
        if (!panel) return;
        panel.classList.remove("mx-in-l", "mx-in-r"); void panel.offsetWidth; panel.classList.add(dx < 0 ? "mx-in-r" : "mx-in-l");
        /* read down into the old tab? the new one starts just under the (sticky) tab strip */
        const pt = panel.getBoundingClientRect().top, tb = tabs.getBoundingClientRect().bottom;
        if (pt < tb) scrollTo({ top: scrollY + pt - tb - 10, behavior: "smooth" });
      });
    }, { passive: true });
  }

  /* ---------- wide tables: their own scroller, first column pinned ---------- */
  function tables() {
    for (const t of qa("#view table")) {
      if (t.closest(".mx-tw") || !t.offsetWidth) continue;
      const p = t.parentElement;
      if (t.scrollWidth <= p.clientWidth + 4 && t.offsetWidth <= p.clientWidth + 4) continue;
      const w = doc.createElement("div"); w.className = "mx-tw";
      t.before(w); w.appendChild(t); t.classList.add("mx-wide");
      const upd = () => { w.classList.toggle("mx-l", w.scrollLeft > 4); w.classList.toggle("mx-end", w.scrollLeft + w.clientWidth >= w.scrollWidth - 4); };
      w.addEventListener("scroll", upd, { passive: true }); upd();
    }
  }

  /* ---------- long lists load in batches on a phone ---------- */
  const ITEMS = [".ucard", ".fr", ".story", ".act", ".sc", ".bc", ".leader"];
  const FIRST = 20, STEP = 25;
  /* a list is folded once per content: the container remembers its first item, so expanding it sticks, and a list
     whose content was replaced (a filter changed) folds again */
  const folded = c => c._mxFirst && c._mxFirst.isConnected && c._mxFirst.parentElement === c;
  function folds() {
    if (!PHONE.matches || !view) return;
    for (const sel of ITEMS) {
      for (const c of new Set(qa("#view " + sel).map(e => e.parentElement))) {
        if (!c || folded(c) || q(":scope > .ux-more", c)) continue;
        const items = qa(":scope > " + sel, c).filter(e => !e.classList.contains("ux-hide"));
        if (items.length <= FIRST + 10) continue;
        c._mxFirst = items[0];
        items.slice(FIRST).forEach(e => e.classList.add("mx-hide"));
        const b = doc.createElement("button"); b.type = "button"; b.className = "mx-more";
        const label = () => { const left = qa(":scope > .mx-hide", c).length; b.textContent = `Show ${Math.min(STEP, left)} more · ${left} left`; return left; };
        b.onclick = () => { qa(":scope > .mx-hide", c).slice(0, STEP).forEach(e => e.classList.remove("mx-hide")); if (!label()) b.remove(); };
        label();
        if (c.matches(".grid")) { b._mxFor = c; c.after(b); } else c.appendChild(b);   // a grid keeps its button below it
      }
    }
  }
  /* a grid replaced by a re-render leaves its old button behind: drop it */
  function orphanButtons() { qa("#view .mx-more").forEach(b => { if (b._mxFor && (!b._mxFor.isConnected || !folded(b._mxFor))) b.remove(); }); }

  /* ---------- the subtitle under a page title: two lines, tap for the rest (phone) ---------- */
  const sub = q("#subtitle");
  if (sub) sub.addEventListener("click", e => { if (PHONE.matches && !e.target.closest("a,button")) sub.classList.toggle("mx-open"); });

  /* ---------- after every render ---------- */
  let tm = null;
  function polish() {
    orphanButtons();
    tables(); tabStrips(); folds();
    if (sub) sub.classList.remove("mx-open");
  }
  if (view) {
    new MutationObserver(ms => {
      if (ms.every(m => [...m.addedNodes].every(n => n.nodeType !== 1 || n.classList.contains("mx-more") || n.classList.contains("mx-tw") || n.classList.contains("ux-more")))) return;
      clearTimeout(tm); tm = setTimeout(polish, 120);
    }).observe(view, { childList: true, subtree: true });
    addEventListener("hashchange", () => { clearTimeout(tm); tm = setTimeout(polish, 400); });
  }
  let rz = null; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { tabStrips(); tables(); }, 200); });
  polish();
})();
