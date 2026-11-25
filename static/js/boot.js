'use strict';
/* ---------------- boot sequence (first visit) ---------------- */
function endBoot() {
  const b = $('boot');
  if (!b || !b.isConnected || b._out) return;
  b._out = 1;
  localStorage.setItem('pc_booted', '1');
  b.classList.add('out');
  setTimeout(() => b.remove(), 520);
}
(function bootSeq() {
  const b = $('boot');
  if (!b) return;
  if (localStorage.getItem('pc_booted') || noMotion()) { b.remove(); return; }
  b.classList.add('go');
  setTimeout(endBoot, 2600);
})();

/* ---------------- boot ---------------- */
function attachTilt(el) {
  if (!el || el._tilt) return; el._tilt = true;
  el.classList.add('tilt');
  el.addEventListener('pointerenter', () => { if (!noMotion()) el.style.animation = 'none'; });
  el.addEventListener('pointermove', e => {
    if (noMotion()) return;
    const r = el.getBoundingClientRect();
    const rx = ((e.clientY - r.top) / r.height - .5) * -7, ry = ((e.clientX - r.left) / r.width - .5) * 7;
    el.style.transform = 'perspective(700px) rotateX(' + rx.toFixed(2) + 'deg) rotateY(' + ry.toFixed(2) + 'deg)';
  });
  el.addEventListener('pointerleave', () => { el.style.transform = ''; });
}
buildRibbon();
attachTilt(document.querySelector('.figcard'));
applySkin(localStorage.getItem('pc_skin') || 'night');
route();
booted = true;
