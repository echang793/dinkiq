'use strict';
/* Practice: this week's drills, goals, and a log. Single user, so it is one page of simple lists. */
let lastPlan = null;
const MIN_CHOICES = [10, 15, 20, 30, 45];

async function loadPractice() {
  if (DEMO) {
    $('practiceStats').innerHTML = '';
    ['planBody', 'goalBody', 'practiceLog'].forEach(id => { $(id).innerHTML = '<div class="empty">Practice planning needs the live backend.</div>'; });
    return;
  }
  let plan, goals, log, streak;
  try {
    [plan, goals, log, streak] = await Promise.all([api('/plan' + playerQS()), api('/goals' + playerQS()), api('/practice'), api('/streak' + playerQS())]);
  } catch (e) { return; }
  lastPlan = plan;
  $('practiceStats').innerHTML =
    statCard(nz(streak.current_streak_days), 'day streak') +
    statCard(nz(streak.sessions_this_week), 'sessions this week') +
    statCard(nz(plan.week_minutes, ' min'), 'practised this week') +
    statCard(nz(streak.practice_days), 'practice days logged');
  renderPlan(plan);
  renderGoals(goals);
  renderPracticeLog(log.practice);
}

function renderPlan(plan) {
  if (!plan.available) {
    $('planBody').innerHTML = '<div class="empty">Finish a session with a skill estimate and your drills for the week appear here.</div>';
    return;
  }
  const mins = id => '<select class="minsel" id="pm_' + id + '" aria-label="Minutes">' +
    MIN_CHOICES.map(m => '<option' + (m === 20 ? ' selected' : '') + '>' + m + '</option>').join('') + '</select>';
  $('planBody').innerHTML =
    '<div class="heatnote" style="margin-bottom:10px">Your weakest skills over your last ' + plan.sessions_considered +
    ' session' + (plan.sessions_considered === 1 ? '' : 's') + (plan.items[0].maintenance ? ' — nothing is weak, so this one just keeps it sharp' : '') + '</div>' +
    plan.items.map(i =>
      '<div class="planitem' + (i.done ? ' done' : '') + '">' +
      '<div class="pi-mark" aria-hidden="true">' + (i.done ? '✓' : '○') + '</div>' +
      '<div class="pi-body"><div class="pi-t"><b>' + esc(i.drill.name) + '</b> <span class="mut">· ' + esc(i.drill.reps) + ' · ' + esc(i.label) + '</span>' +
        (i.done ? ' <span class="sr">done this week</span>' : '') + '</div>' +
      '<div class="pi-d">' + esc(i.drill.description) + '</div></div>' +
      '<div class="pi-act">' + mins(i.dimension) + ' min <button class="sm' + (i.done ? ' ghost' : '') + '" onclick="logDrill(\'' + i.dimension + '\')">' +
      (i.done ? 'Log another' : 'I did this') + '</button></div></div>').join('');
}
async function logDrill(dim) {
  const item = lastPlan && lastPlan.items.find(i => i.dimension === dim);
  if (!item) return;
  await postPractice({ drill: item.drill.name, dimension: dim, minutes: +$('pm_' + dim).value || 20 });
}

function renderGoals(g) {
  const dimSel = $('goalDim'), keep = dimSel.value;
  dimSel.innerHTML = g.dimensions.map(d => '<option value="' + d.dimension + '">' + esc(d.label) + ' (' + (d.higher_is_better ? 'higher' : 'lower') + ' is better)</option>').join('');
  if (keep) dimSel.value = keep;
  const rows = g.goals.map(x =>
    '<div class="goalrow' + (x.achieved ? ' ok' : '') + '"><div class="gl-top"><b>' + esc(x.label) + '</b>' +
    '<span class="mut">' + (x.higher_is_better ? 'reach ' : 'get down to ') + fmtN(x.target) + esc(x.unit) +
    (x.current != null ? ' · now ' + fmtN(x.current) + esc(x.unit) : ' · no rated session yet') + '</span>' +
    (x.achieved ? '<span class="dchip better">✓ reached</span>' : '') +
    '<button class="ghost sm" onclick="clearGoal(\'' + x.dimension + '\')" aria-label="Remove the ' + esc(x.label) + ' goal">Remove</button></div>' +
    '<div class="bar" style="grid-template-columns:1fr 46px"><div class="track"><div class="fill" data-w="' + x.progress_pct + '%"></div></div><span class="val">' + Math.round(x.progress_pct) + '%</span></div></div>').join('');
  const sug = g.suggested.map(s =>
    '<button type="button" class="chip" onclick="setGoal(\'' + s.dimension + '\',' + s.target + ')">+ ' + esc(s.label) + ' → ' + fmtN(s.target) + esc(s.unit) + '</button>').join('');
  $('goalBody').innerHTML = (rows || '<div class="empty">No goals yet. Pick a target below, or tap a suggestion.</div>') +
    (sug ? '<div class="heatnote" style="margin:14px 0 8px">Suggested: the next tier on each skill</div><div class="chips" style="margin-top:0">' + sug + '</div>' : '');
  animateBars($('goalBody'));
}
async function setGoal(dim, target) {
  try {
    await api('/goals/' + dim, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target }) });
    toast('Goal set'); loadPractice();
  } catch (e) { toast('Could not set the goal: ' + e, true); }
}
function setGoalFromForm() {
  const v = parseFloat($('goalTarget').value);
  if (!isFinite(v) || v < 0) { toast('Enter a target number', true); return; }
  setGoal($('goalDim').value, v);
}
async function clearGoal(dim) {
  try { await api('/goals/' + dim, { method: 'DELETE' }); loadPractice(); }
  catch (e) { toast('Could not remove it: ' + e, true); }
}

function renderPracticeLog(rows) {
  $('practiceLog').innerHTML = rows.length
    ? '<div class="plog">' + rows.slice(0, 30).map(p =>
        '<div class="plrow"><span class="mut">' + esc(p.date) + '</span><span>' + esc(p.drill) + '</span><span class="mut">' + p.minutes + ' min</span>' +
        '<button class="ghost sm" onclick="deletePractice(\'' + esc(p.id) + '\')" aria-label="Delete this entry">✕</button></div>').join('') + '</div>'
    : '<div class="empty">Nothing logged yet.</div>';
}
async function postPractice(body) {
  try {
    await api('/practice', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('Logged'); loadPractice();
  } catch (e) { toast('Could not log it: ' + e, true); }
}
function logPracticeFromForm() {
  const drill = $('plDrill').value.trim();
  if (!drill) { toast('Say what you worked on', true); return; }
  postPractice({ drill, minutes: Math.max(1, Math.min(600, parseInt($('plMin').value, 10) || 20)) }).then(() => { $('plDrill').value = ''; });
}
async function deletePractice(id) {
  try { await api('/practice/' + id, { method: 'DELETE' }); loadPractice(); }
  catch (e) { toast('Could not delete it: ' + e, true); }
}
