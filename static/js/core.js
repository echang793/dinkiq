'use strict';
const $ = id => document.getElementById(id);
// Path prefix this app is mounted under, injected by server._spa() from the
// reverse proxy's X-Forwarded-Prefix. Empty when served directly (localhost,
// LAN), so URLs stay root-absolute exactly as before. Behind the vantage
// Caddy hub the app lives at /app/dinkiq/, and every root-absolute URL here
// would otherwise resolve against the hub origin instead of this app.
const BASE = window.__BASE__ || '';
let sid = null, pollTimer = null;
// Sample data is opt-in (?demo=1) and always labelled. A network failure must never
// quietly swap in fake data: it reads like real results and fake writes look saved.
const DEMO = new URLSearchParams(location.search).has('demo');
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
  const reduce = noMotion();
  if (!document.startViewTransition || reduce || document.hidden) { applySkin(k); return; }
  const x = ev ? ev.clientX : innerWidth - 60, y = ev ? ev.clientY : 30;
  const end = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  viewTransition(() => applySkin(k), () => {
    document.documentElement.animate(
      { clipPath: ['circle(0px at ' + x + 'px ' + y + 'px)', 'circle(' + end + 'px at ' + x + 'px ' + y + 'px)'] },
      { duration: 600, easing: 'cubic-bezier(.22,1,.36,1)', pseudoElement: '::view-transition-new(root)' }
    );
  });
}

// A view transition can be aborted (tab hidden, DOM update timeout); the update itself must
// still happen exactly once, and the rejection must not surface as an unhandled error.
function viewTransition(update, animate) {
  let done = false;
  const run = () => { if (!done) { done = true; update(); } };
  const t = document.startViewTransition(run);
  t.ready.then(animate).catch(run);
  t.finished.catch(run);
  t.updateCallbackDone.catch(run);   // all three promises reject when the transition is aborted
}

async function api(p, opt) {
  if (DEMO) return mockApi(p, opt);
  let r;
  try { r = await fetch(BASE + '/api' + p, opt); }
  catch (e) {
    if (e instanceof TypeError) { setOffline(true); throw "Can't reach DinkIQ"; }
    throw e;
  }
  setOffline(false);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw (e.detail || ('HTTP ' + r.status)); }
  return r.json();
}
function frameURL() { return DEMO ? demoFrameURL() : BASE + '/api/session/' + sid + '/frame?ts=' + Date.now(); }

/* ---------------- settings (stored on this device) ---------------- */
let prefs = {};
try { prefs = JSON.parse(localStorage.getItem('pc_prefs')) || {}; } catch (e) { prefs = {}; }
function savePrefs() { try { localStorage.setItem('pc_prefs', JSON.stringify(prefs)); } catch (e) {} applyPrefs(); }
function me() { return (prefs.me || '').trim(); }
// "?player=Eric" for the endpoints that scope to one player; empty until a name is set
function playerQS(first) { return me() ? (first || '?') + 'player=' + encodeURIComponent(me()) : ''; }
// Effects (particles, tilt, scanlines, stinger, count-ups): automatic = off for people who asked the OS
// for reduced motion and on touch screens, where they cost battery and nobody hovers.
function noMotion() {
  if (prefs.fx === 'off') return true;
  if (matchMedia('(prefers-reduced-motion:reduce)').matches) return true;
  return prefs.fx !== 'on' && matchMedia('(pointer:coarse)').matches;
}
function applyPrefs() { document.documentElement.dataset.fx = noMotion() ? 'off' : 'on'; }
applyPrefs();
function openSettings() {
  $('setMe').value = prefs.me || '';
  $('setHand').value = prefs.hand || '';
  $('setFx').value = prefs.fx || 'auto';
  const d = $('settings');
  if (d.showModal) d.showModal(); else d.setAttribute('open', '');
}
function closeSettings() { const d = $('settings'); if (d.close) d.close(); else d.removeAttribute('open'); }
function saveSettings() {
  const before = me();
  prefs.me = $('setMe').value.trim().slice(0, 80);
  prefs.hand = $('setHand').value || '';
  prefs.fx = $('setFx').value === 'auto' ? '' : $('setFx').value;
  savePrefs(); closeSettings(); toast('Settings saved');
  if (me() !== before) { try { loadSessions(); loadTrend(); loadFocusReminder(); } catch (e) {} }
}

/* ---------------- connection banner ---------------- */
let offline = false, offlineTimer = null;
function showBar(msg, cls, retry) {
  const bar = $('netBar');
  $('netMsg').textContent = msg;
  $('netRetry').style.display = retry ? '' : 'none';
  bar.className = 'netbar' + (cls ? ' ' + cls : '');
  bar.hidden = false;
  document.documentElement.style.setProperty('--bar-h', bar.offsetHeight + 'px');
}
function hideBar() {
  $('netBar').hidden = true;
  document.documentElement.style.setProperty('--bar-h', '0px');
}
function setOffline(on) {
  if (DEMO || on === offline) return;
  offline = on;
  if (on) {
    showBar("Can't reach DinkIQ — your data is safe on the server. Retrying…", 'off', true);
    offlineTimer = setInterval(probeBackend, 5000);
  } else {
    clearInterval(offlineTimer); offlineTimer = null;
    hideBar();
    // the page was loaded against a dead server: refill the lists that came back empty
    try { loadSessions(); } catch (e) {}
    try { loadTrend(); } catch (e) {}
    try { loadFocusReminder(); } catch (e) {}
  }
}
function probeBackend() { return api('/sessions').then(() => true, () => false); }
async function retryBackend() {
  if (await probeBackend()) toast('Reconnected');
  else toast("Still can't reach DinkIQ", true);
}
if (DEMO) showBar('SAMPLE DATA — this is a demo; nothing here is real and nothing is saved', '', false);

function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast on' + (err ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 3200);
}

/* ---------------- odometer count-up ---------------- */
const ease3 = t => 1 - Math.pow(1 - t, 3);
function animateStats(container) {
  const reduce = noMotion();
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
  const reduce = noMotion();
  if (instant || !booted || !document.startViewTransition || reduce || document.hidden) { doSwap(); return; }
  viewTransition(doSwap, () => {
    document.documentElement.animate(
      [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-16px) scale(.995)' }],
      { duration: 280, easing: 'cubic-bezier(.4,0,.7,.2)', pseudoElement: '::view-transition-old(root)' });
    document.documentElement.animate(
      [{ opacity: 0, transform: 'translateY(22px)' }, { opacity: 1, transform: 'none' }],
      { duration: 480, easing: 'cubic-bezier(.22,1,.36,1)', pseudoElement: '::view-transition-new(root)' });
  });
}
/* ---------------- routes ----------------
   #/ home · #/s/<id> a session (whatever screen its state calls for) · #/sessions · #/trends ·
   #/compare?ids=a,b,c. The hash is the source of truth: back/forward, reload and shared links all
   land where they should; the go*() helpers just change the hash. */
let booted = false;
function nav(h) { if (location.hash === h || (h === '#/' && !location.hash)) route(); else location.hash = h; }
function goHome() { nav('#/'); }
function goSessions() { nav('#/sessions'); }
function goTrends() { nav('#/trends'); }
function viewHome() { stopPoll(); show('home'); loadSessions(); loadTrend(); loadFocusReminder(); }
function viewSessions() { stopPoll(); show('sessions'); loadSessionsPage(); }
function viewTrends() { stopPoll(); show('trends'); loadTrendsPage(); }
function route() {
  if (typeof setCalFull === 'function') setCalFull(false);
  const [path, qs] = (location.hash || '#/').slice(1).split('?');
  const parts = path.split('/').filter(Boolean), q = new URLSearchParams(qs || '');
  if (parts[0] === 's' && parts[1]) return viewSession(decodeURIComponent(parts[1]));
  if (parts[0] === 'sessions') return viewSessions();
  if (parts[0] === 'trends') return viewTrends();
  if (parts[0] === 'compare') return viewCompare((q.get('ids') || '').split(',').filter(Boolean));
  return viewHome();
}
addEventListener('hashchange', route);
function toggleNavMore(e) { e.stopPropagation(); $('navMorePanel').classList.toggle('open'); }
document.addEventListener('click', () => { const p = $('navMorePanel'); if (p) p.classList.remove('open'); });
