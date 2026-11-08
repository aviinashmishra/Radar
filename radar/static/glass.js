/* Glass layer: the small interactions that make the frosted surfaces feel physical. Pure presentation, no data.
   1. pointer spotlight: the surface under the pointer gets --mx/--my so its ::before sheen follows the cursor
   2. ambient parallax: --px/--py on <html> (−.5…+.5) nudge the colour blobs behind the page
   3. scroll state: <html data-scrolled="1"> after 12px, so sticky bars can tighten up
   Everything is throttled to one animation frame and switched off for reduced-motion or touch-only devices. */
(function () {
  const root = document.documentElement;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fine = matchMedia("(pointer: fine)").matches;
  let raf = 0, px = 0, py = 0, target = null, tx = 0, ty = 0;
  const SURF = ".card,.kpi,.xk,.act,.sc";
  function frame() {
    raf = 0;
    if (target) { target.style.setProperty("--mx", tx + "px"); target.style.setProperty("--my", ty + "px"); }
    if (!still) { root.style.setProperty("--px", px.toFixed(3)); root.style.setProperty("--py", py.toFixed(3)); }
  }
  if (fine) {
    addEventListener("pointermove", e => {
      const el = e.target instanceof Element ? e.target.closest(SURF) : null;
      if (el) { const r = el.getBoundingClientRect(); tx = e.clientX - r.left; ty = e.clientY - r.top; }
      target = el;
      px = e.clientX / innerWidth - .5; py = e.clientY / innerHeight - .5;
      if (!raf) raf = requestAnimationFrame(frame);
    }, { passive: true });
  }
  const onScroll = () => { const s = scrollY > 12 ? "1" : "0"; if (root.dataset.scrolled !== s) root.dataset.scrolled = s; };
  addEventListener("scroll", onScroll, { passive: true }); onScroll();
})();
