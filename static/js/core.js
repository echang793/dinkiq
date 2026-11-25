'use strict';
const $ = id => document.getElementById(id);
// Path prefix this app is mounted under, injected by server._spa() from the
// reverse proxy's X-Forwarded-Prefix. Empty when served directly (localhost,
// LAN), so URLs stay root-absolute exactly as before. Behind the vantage
// Caddy hub the app lives at /app/dinkiq/, and every root-absolute URL here
// would otherwise resolve against the hub origin instead of this app.
const BASE = window.__BASE__ || '';
let sid = null, pollTimer = null, DEMO = false;
// must match dupr.py's BAND_MIN/BAND_MAX — every band the backend can ever
// output is clamped to this range, so the gauge/bars/radar all scale to it
// instead of a generic 2-8 DUPR scale (which would make a perfect 5.0 score,
// the tool's actual ceiling, look like a middling ~3.5 on an 8-point axis)
const DUPR_BAND_MIN = 2.5, DUPR_BAND_MAX = 5.0;
const DUPR_BAND_SPAN = DUPR_BAND_MAX - DUPR_BAND_MIN;
let lastSession = null;
// keyboard-only users otherwise can't activate the many span/div "buttons"
// (nav, back links, card shortcuts) — delegate Enter/Space to a click()
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.getAttribute &&
      e.target.getAttribute('role') === 'button' && e.target.tagName !== 'BUTTON') {
    e.preventDefault();
    e.target.click();
  }
});
const SKINS = {
  night: { hi:'245,197,66', mid:'235,180,85', lo:'130,105,55' },
  champ: { hi:'255,210,77', mid:'240,195,95', lo:'135,110,55' },
  clay:  { hi:'255,138,92', mid:'235,135,100', lo:'140,80,60' },
  prime: { hi:'57,213,255', mid:'85,185,230', lo:'45,105,140' },
};
function currentSkin(){ return document.documentElement.dataset.skin || 'night'; }
function applySkin(k){
  document.documentElement.dataset.skin = k;
  localStorage.setItem('pc_skin', k);
  document.querySelectorAll('.skinbtn').forEach(b => b.classList.toggle('on', b.dataset.k === k));
  try { if ($('results').classList.contains('on') && lastSession && lastSession.metrics) drawHeatmap(lastSession.metrics); } catch(e){}
}
function setSkin(k, ev){
  if (k === currentSkin()) return;
  const reduce = matchMedia('(prefers-reduced-motion:reduce)').matches;
  if (!document.startViewTransition || reduce) { applySkin(k); return; }
  const x = ev ? ev.clientX : innerWidth - 60, y = ev ? ev.clientY : 30;
  const end = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  document.startViewTransition(() => applySkin(k)).ready.then(() => {
    document.documentElement.animate(
      { clipPath: ['circle(0px at ' + x + 'px ' + y + 'px)', 'circle(' + end + 'px at ' + x + 'px ' + y + 'px)'] },
      { duration: 600, easing: 'cubic-bezier(.22,1,.36,1)', pseudoElement: '::view-transition-new(root)' }
    );
  });
}

/* ---------------- API with demo fallback ---------------- */
// A 404 here only means "no FastAPI backend at all" when it comes from one
// of these boot-time index calls (e.g. this file served statically with no
// /api/* routes behind it at all). A 404 from any OTHER endpoint is a
// normal per-resource "not found" (deleted session, no scouting data yet,
// etc.) and must NOT flip the whole app into demo/fake-data mode — every
// other call already has its own try/catch that degrades that one widget.
const DEMO_PROBE_PATHS = ['/sessions', '/progress'];
async function api(p, opt) {
  if (DEMO) return mockApi(p, opt);
  try {
    const r = await fetch(BASE + '/api' + p, opt);
    const isProbe = DEMO_PROBE_PATHS.some(probe => p === probe || p.startsWith(probe + '?'));
    if (r.status === 404 && !DEMO && isProbe) { enableDemo(); return mockApi(p, opt); }
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw (e.detail || ('HTTP ' + r.status)); }
    return r.json();
  } catch (e) {
    if (e instanceof TypeError) { enableDemo(); return mockApi(p, opt); }
    throw e;
  }
}
function frameURL() { return DEMO ? demoFrameURL() : BASE + '/api/session/' + sid + '/frame?ts=' + Date.now(); }
let demoRetryTimer = null;
function enableDemo(){
  if (DEMO) return;
  DEMO = true;
  $('demoChip').style.display = 'inline-block';
  setTimeout(() => { try { loadSessions(); } catch(e){} try { loadTrend(); } catch(e){} }, 0);
  // a single dropped request shouldn't trap the tab in fake data forever —
  // keep checking, and reload (a clean DEMO=false) the moment the server answers
  if (!demoRetryTimer) demoRetryTimer = setInterval(() => {
    fetch(BASE + '/api/sessions').then(r => { if (r.ok) location.reload(); }).catch(() => {});
  }, 5000);
}
function retryBackend() {
  fetch(BASE + '/api/sessions').then(r => {
    if (r.ok) location.reload();
    else toast("Still can't reach the server — will keep retrying");
  }).catch(() => toast("Still can't reach the server — will keep retrying"));
}

function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast on' + (err ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 3200);
}

/* ---------------- odometer count-up ---------------- */
const ease3 = t => 1 - Math.pow(1 - t, 3);
function animateStats(container) {
  const reduce = matchMedia('(prefers-reduced-motion:reduce)').matches;
  container.querySelectorAll('.v').forEach(el => {
    if (el.dataset.anim === 'done') return;
    el.dataset.anim = 'done';
    if (reduce) return;
    const html = el.innerHTML;
    const m = html.match(/[\d,]+(\.\d+)?/);
    if (!m) return;
    const pre = html.slice(0, m.index), post = html.slice(m.index + m[0].length);
    const reel = '<b>' + '0123456789'.split('').join('</b><b>') + '</b>';
    el.innerHTML = pre + '<span class="odo">' + m[0].split('').map((ch, i) =>
      /\d/.test(ch)
        ? '<span class="od"><span class="or" data-d="' + ch + '" style="transition-delay:' + (i * 70) + 'ms">' + reel + '</span></span>'
        : '<span>' + ch + '</span>').join('') + '</span>' + post;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      el.querySelectorAll('.or').forEach(r => { r.style.transform = 'translateY(-' + (+r.dataset.d * 1.15) + 'em)'; });
    }));
    setTimeout(() => { if (el.isConnected && el.dataset.anim === 'done') el.innerHTML = html; }, 2000);
  });
}

/* ---------------- screens ---------------- */
function show(id, instant) {
  const doSwap = () => {
    ['home','calib','proc','results','sessions','trends','compare'].forEach(s => {
      const el = $(s);
      if (s === id) { el.classList.remove('on'); void el.offsetWidth; el.classList.add('on'); }
      else el.classList.remove('on');
    });
    const nav = { home:'navFilm', sessions:'navSessions', trends:'navTrends' };
    ['navFilm','navSessions','navTrends'].forEach(n => { const e = $(n); if (e) e.classList.toggle('here', nav[id] === n); });
    window.scrollTo({ top: 0 });
  };
  const reduce = matchMedia('(prefers-reduced-motion:reduce)').matches;
  if (instant || !document.startViewTransition || reduce) { doSwap(); return; }
  document.startViewTransition(doSwap).ready.then(() => {
    document.documentElement.animate(
      [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-16px) scale(.995)' }],
      { duration: 280, easing: 'cubic-bezier(.4,0,.7,.2)', pseudoElement: '::view-transition-old(root)' });
    document.documentElement.animate(
      [{ opacity: 0, transform: 'translateY(22px)' }, { opacity: 1, transform: 'none' }],
      { duration: 480, easing: 'cubic-bezier(.22,1,.36,1)', pseudoElement: '::view-transition-new(root)' });
  });
}
function goHome() { clearInterval(pollTimer); show('home'); loadSessions(); loadTrend(); loadFocusReminder(); }
function goSessions() { clearInterval(pollTimer); show('sessions'); loadSessionsPage(); }
function goTrends() { clearInterval(pollTimer); show('trends'); loadTrendsPage(); }
function toggleNavMore(e) { e.stopPropagation(); $('navMorePanel').classList.toggle('open'); }
document.addEventListener('click', () => { const p = $('navMorePanel'); if (p) p.classList.remove('open'); });
