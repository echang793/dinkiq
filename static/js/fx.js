'use strict';
/* ---------------- portal morph (row → results hero) ---------------- */
let portalFrom = null;
function runPortal(r0) {
  requestAnimationFrame(() => {
    const hero = $('duprHero');
    if (!hero) return;
    const r1 = hero.getBoundingClientRect();
    const d = document.createElement('div');
    d.className = 'portal';
    d.style.cssText = 'left:' + r0.left + 'px;top:' + r0.top + 'px;width:' + r0.width + 'px;height:' + r0.height + 'px';
    document.body.appendChild(d);
    d.animate([
      { left: r0.left + 'px', top: r0.top + 'px', width: r0.width + 'px', height: r0.height + 'px', opacity: 1 },
      { left: r1.left + 'px', top: r1.top + 'px', width: r1.width + 'px', height: r1.height + 'px', opacity: .9 }
    ], { duration: 430, easing: 'cubic-bezier(.22,1,.36,1)', fill: 'forwards' }).onfinish = () => {
      d.animate([{ opacity: .9 }, { opacity: 0 }], { duration: 240, fill: 'forwards' }).onfinish = () => d.remove();
    };
  });
}

/* ---------------- ambient particle court + parallax ---------------- */
(function ambient() {
  const cv = $('fxCourt');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  let W = 0, H = 0, pts = [], mx = -9e9, my = -9e9;
  function targets() {
    W = cv.width = innerWidth; H = cv.height = innerHeight;
    const cw = Math.min(300, W * .34), ch = cw * 2.1, x0 = W / 2 - cw / 2, y0 = H / 2 - ch / 2, T = [];
    const seg = (x1, y1, x2, y2, n) => { for (let i = 0; i <= n; i++) T.push([x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n]); };
    seg(x0, y0, x0 + cw, y0, 9); seg(x0 + cw, y0, x0 + cw, y0 + ch, 18);
    seg(x0 + cw, y0 + ch, x0, y0 + ch, 9); seg(x0, y0 + ch, x0, y0, 18);
    seg(x0, y0 + ch * .34, x0 + cw, y0 + ch * .34, 7); seg(x0, y0 + ch * .66, x0 + cw, y0 + ch * .66, 7);
    seg(x0, y0 + ch * .5, x0 + cw, y0 + ch * .5, 7);
    seg(x0 + cw / 2, y0, x0 + cw / 2, y0 + ch * .34, 4); seg(x0 + cw / 2, y0 + ch * .66, x0 + cw / 2, y0 + ch, 4);
    pts = T.map(t => ({ tx: t[0], ty: t[1], x: t[0] + (Math.random() - .5) * 260, y: t[1] + (Math.random() - .5) * 260, vx: 0, vy: 0 }));
  }
  targets();
  addEventListener('resize', targets);
  if (window.ResizeObserver) new ResizeObserver(targets).observe(document.documentElement);
  addEventListener('pointermove', e => {
    mx = e.clientX; my = e.clientY;
    const px = e.clientX / W - .5, py = e.clientY / H - .5;
    const g = $('fxGrid');
    if (g) g.style.transform = 'translate(' + (-px * 18) + 'px,' + (-py * 14) + 'px)';
    cv.style.transform = 'translate(' + (-px * 32) + 'px,' + (-py * 26) + 'px)';
  });
  function frame() {
    requestAnimationFrame(frame);
    if (document.hidden || noMotion()) { ctx.clearRect(0, 0, W, H); return; }
    ctx.clearRect(0, 0, W, H);
    const sk = SKINS[currentSkin()] || SKINS.night;
    ctx.fillStyle = 'rgba(' + sk.hi + ',.32)';
    const t = performance.now() / 900;
    pts.forEach(p => {
      p.vx += (p.tx - p.x) * .004; p.vy += (p.ty - p.y) * .004;
      const dx = p.x - mx, dy = p.y - my, d2 = dx * dx + dy * dy;
      if (d2 < 12000) { const f = (12000 - d2) / 12000; p.vx += dx * f * .018; p.vy += dy * f * .018; }
      p.vx *= .92; p.vy *= .92;
      p.x += p.vx + Math.sin(p.ty + t) * .08; p.y += p.vy;
      ctx.beginPath(); ctx.arc(p.x, p.y, 1.4, 0, 7); ctx.fill();
    });
  }
  requestAnimationFrame(frame);
})();

/* ---------------- magnetic buttons ---------------- */
document.addEventListener('pointermove', e => {
  const b = e.target.closest && e.target.closest('button');
  if (!b || b.classList.contains('skinbtn') || b.disabled || noMotion()) return;
  const r = b.getBoundingClientRect();
  const dx = (e.clientX - r.left - r.width / 2) / r.width * 8;
  const dy = (e.clientY - r.top - r.height / 2) / r.height * 6;
  b.style.transform = 'translate(' + dx.toFixed(1) + 'px,' + (dy - 2).toFixed(1) + 'px)';
  b._mag = 1;
}, true);
document.addEventListener('pointerout', e => {
  const b = e.target.closest && e.target.closest('button');
  if (b && b._mag) { b.style.transform = ''; b._mag = 0; }
}, true);
