'use strict';
/* ---------------- command palette ---------------- */
let palSel = 0, palItems = [];
function palActions() {
  const acts = [
    { t: 'Film Room', k: 'page', f: goHome }, { t: 'Sessions', k: 'page', f: goSessions }, { t: 'Trends', k: 'page', f: goTrends },
    { t: 'Theme · Night Game', k: 'theme', f: () => setSkin('night') }, { t: 'Theme · Championship', k: 'theme', f: () => setSkin('champ') },
    { t: 'Theme · Clay Court', k: 'theme', f: () => setSkin('clay') }, { t: 'Theme · Prime Time', k: 'theme', f: () => setSkin('prime') },
  ];
  (window._pcSessions || []).forEach(s => acts.push({ t: s.label || s.filename || s.session_id, k: 'session', f: () => openSession(s.session_id) }));
  return acts;
}
function fuzzy(q, t) {
  q = q.toLowerCase(); t = t.toLowerCase();
  if (!q) return 1;
  let i = 0, score = 0, last = -2;
  for (const ch of q) {
    const idx = t.indexOf(ch, i);
    if (idx < 0) return 0;
    score += (idx === last + 1 ? 3 : 1); last = idx; i = idx + 1;
  }
  return score + (t.indexOf(q) === 0 ? 10 : 0);
}
function renderPal(q) {
  palItems = palActions().map(a => ({ a, s: fuzzy(q, a.t) })).filter(x => x.s > 0).sort((a, b) => b.s - a.s).map(x => x.a);
  palSel = 0;
  $('palList').innerHTML = palItems.map((a, i) =>
    '<div class="palit' + (i === 0 ? ' sel' : '') + '" onmouseenter="palSel=' + i + ';palPaint()" onclick="palRun(' + i + ')"><span class="pk">' + a.k + '</span>' + esc(a.t) + '</div>').join('')
    || '<div class="empty" style="padding:14px">No matches.</div>';
}
function palPaint() { [...document.querySelectorAll('.palit')].forEach((el, i) => el.classList.toggle('sel', i === palSel)); }
function palRun(i) { const a = palItems[i]; closePal(); if (a) a.f(); }
function openPal() { $('pal').classList.add('on'); $('palIn').value = ''; renderPal(''); $('palIn').focus(); }
function closePal() { $('pal').classList.remove('on'); }
$('palIn').addEventListener('input', e => renderPal(e.target.value));
$('pal').addEventListener('click', e => { if (e.target.id === 'pal') closePal(); });
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    $('pal').classList.contains('on') ? closePal() : openPal();
    return;
  }
  if (!$('pal').classList.contains('on')) return;
  if (e.key === 'Escape') closePal();
  else if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palItems.length - 1, palSel + 1); palPaint(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(0, palSel - 1); palPaint(); }
  else if (e.key === 'Enter') { e.preventDefault(); palRun(palSel); }
});
