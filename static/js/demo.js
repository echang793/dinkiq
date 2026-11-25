'use strict';
/* =============================================================
   DEMO MODE — used only when the FastAPI backend is unreachable
   ============================================================= */
const demo = {
  sessions: [
    { session_id:'d3', filename:'open_play_thu.mp4', label:'Thursday open play', played_at:'2026-07-09', known_dupr:null, uploaded_at:1783900000, stage:'shots', state:'running', overall:.62 },
    { session_id:'d2', filename:'league_wk4.mov', label:'League week 4', played_at:'2026-07-05', known_dupr:3.75, uploaded_at:1783500000, stage:'done', state:'done', overall:1 },
    { session_id:'d1', filename:'rec_game.mp4', label:'Rec game w/ Sam', played_at:'2026-06-28', known_dupr:null, uploaded_at:1782900000, stage:'done', state:'done', overall:1 },
  ],
  progress: [
    { session_id:'d0', label:'First upload', date:'2026-06-14', kitchen_pct:19.4, transition_pct:41.0, distance_ft:1010, avg_rally_hits:4.2, dupr_band:3.0, dupr_confidence:.55, known_dupr:null },
    { session_id:'d1', label:'Rec game w/ Sam', date:'2026-06-28', kitchen_pct:24.8, transition_pct:35.5, distance_ft:1130, avg_rally_hits:5.0, dupr_band:3.25, dupr_confidence:.62, known_dupr:null },
    { session_id:'d2', label:'League week 4', date:'2026-07-05', kitchen_pct:31.2, transition_pct:24.0, distance_ft:1204, avg_rally_hits:6.1, dupr_band:3.5, dupr_confidence:.78, known_dupr:3.75 },
  ],
  detail: {
    session_id:'d2', filename:'league_wk4.mov', stage:'done', state:'done', overall:1,
    meta:{ label:'League week 4', played_at:'2026-07-05', known_dupr:3.75 },
    metrics:{
      zone_pct:{ kitchen:31.2, transition:24.0, baseline:44.8 }, distance_ft:1204.5, avg_speed_ft_s:2.4,
      active_seconds:1712, median_dist_from_net_ft:11.2, coverage_pct:64, camera_cuts:2,
      heatmap_cell_ft:2, heatmap: null,
      warnings:['Camera cut detected at 12:40 — 45 s of play excluded from movement metrics.'],
    },
    events:{
      rally_count:38, total_hits:322, subject_shots:96, avg_rally_hits:6.1, max_rally_hits:19,
      avg_rally_seconds:9.8, play_time_pct:36, swing_count:104,
      rallies: Array.from({length:10}, (_,i) => ({ start: 40+i*95, end: 40+i*95+6+i, hits: 4+((i*3)%14), duration: +(5+(i*1.7)%12).toFixed(1) })),
    },
    shots:{
      available:true, ball:{ coverage:.71, segments:41, frames:52210, stride:2 },
      shot_mix:{ drive:31, dink:38, drop:12, medium:9, unknown:6 },
      serves_measured:true, avg_serve_depth_from_baseline_ft:6.2, deep_serve_pct:64,
    },
    points:{
      points_scored:24, points_won:14, points_lost:10, win_pct:58,
      serve_win_pct:61, return_win_pct:54, unforced_errors:7,
      outcomes: Array.from({length:10}, (_,i) => ({
        server: i%2 ? 'opponent' : 'subject',
        winner: [ 'subject','opponent','subject','subject','unknown','opponent','subject','opponent','subject','subject' ][i],
        unforced_error: [false,true,false,false,false,true,false,false,false,false][i],
      })),
    },
    dupr:{
      available:true, band:3.5, band_raw:3.42, confidence:.78,
      dimensions:{
        kitchen:{ label:'Kitchen presence', value:31.2, band:3.75, weight:.3 },
        positioning:{ label:'Positioning discipline', value:.71, band:3.5, weight:.2 },
        consistency:{ label:'Rally consistency', value:6.1, band:3.5, weight:.2 },
        variety:{ label:'Shot variety', value:.6, band:3.25, weight:.15 },
        serve:{ label:'Serve depth', value:6.2, band:3.0, weight:.15 },
      },
      caveats:['Single camera angle — depth positions on the far court are approximate.',
               'Only 38 rallies sampled; estimates firm up with 2–3 more sessions.'],
      tips:['Your serve lands 6.2 ft from the baseline on average. Deeper serves (within 4 ft) push returners back and buy your team time to reach the kitchen.',
            "You reached the kitchen line on 31% of points. Try 'third shot drop → move up together' — players a band above you live at 40%+."],
    },
  },
};
(function genHeatmap(){
  const rows = 22, cols = 10, g = [];
  for (let r = 0; r < rows; r++) { g.push([]); for (let q = 0; q < cols; q++) {
    const nearKitchen = Math.exp(-Math.pow(r - 14.5, 2) / 6 - Math.pow(q - 3.2, 2) / 7) * .95;
    const base = Math.exp(-Math.pow(r - 20, 2) / 8 - Math.pow(q - 5.5, 2) / 9) * .7;
    const noise = Math.random() * .06;
    g[r].push(r < 11 ? 0 : +(nearKitchen + base + noise).toFixed(3));
  } }
  demo.detail.metrics.heatmap = g;
})();
let demoProc = null;
function demoUpload(f, done) {
  let frac = 0;
  const iv = setInterval(() => {
    frac = Math.min(1, frac + .13 + Math.random() * .1);
    setUpProgress(frac, frac * (f.size || 2e8), f.size || 2e8);
    if (frac >= 1) {
      clearInterval(iv);
      demoProc = null;
      sid = 'dnew';
      demo.sessions.unshift({ session_id:'dnew', filename:f.name, label:null, played_at:null, known_dupr:null,
        uploaded_at: Date.now()/1000, stage:'ingest', state:'done', overall:0 });
      done({ session_id:'dnew', duration_s: 1820, warning: (f.size || 0) > 8e8 ? 'Video longer than 30 minutes — analysis will take a while.' : undefined });
    }
  }, 220);
}
function demoFrameURL() {
  const W = 960, H = 540, c = document.createElement('canvas');
  c.width = W; c.height = H;
  const x = c.getContext('2d');
  const sky = x.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, '#1c2430'); sky.addColorStop(.45, '#27313d'); sky.addColorStop(.46, '#31502e'); sky.addColorStop(1, '#3a6136');
  x.fillStyle = sky; x.fillRect(0, 0, W, H);
  const far = { l: 330, r: 630, y: 250 }, near = { l: 130, r: 830, y: 505 };
  const lerp = (a, b, t) => a + (b - a) * t;
  const edge = (t) => ({ l: lerp(far.l, near.l, t), r: lerp(far.r, near.r, t), y: lerp(far.y, near.y, t) });
  x.fillStyle = '#3468a5';
  x.beginPath(); x.moveTo(far.l, far.y); x.lineTo(far.r, far.y); x.lineTo(near.r, near.y); x.lineTo(near.l, near.y); x.closePath(); x.fill();
  const k1 = edge(.36), k2 = edge(.64);
  x.fillStyle = '#2e8b6f';
  x.beginPath(); x.moveTo(k1.l, k1.y); x.lineTo(k1.r, k1.y); x.lineTo(k2.r, k2.y); x.lineTo(k2.l, k2.y); x.closePath(); x.fill();
  x.strokeStyle = '#e8edf4'; x.lineWidth = 3;
  x.beginPath(); x.moveTo(far.l, far.y); x.lineTo(far.r, far.y); x.lineTo(near.r, near.y); x.lineTo(near.l, near.y); x.closePath(); x.stroke();
  [k1, k2].forEach(e => { x.beginPath(); x.moveTo(e.l, e.y); x.lineTo(e.r, e.y); x.stroke(); });
  x.beginPath(); x.moveTo((far.l+far.r)/2, far.y); x.lineTo((k1.l+k1.r)/2, k1.y);
  x.moveTo((k2.l+k2.r)/2, k2.y); x.lineTo((near.l+near.r)/2, near.y); x.stroke();
  const mid = edge(.5);
  x.fillStyle = 'rgba(10,14,18,.75)';
  x.fillRect(mid.l - 26, mid.y - 40, (mid.r - mid.l) + 52, 40);
  x.strokeStyle = '#0a0e12'; x.lineWidth = 4;
  x.strokeRect(mid.l - 26, mid.y - 40, (mid.r - mid.l) + 52, 40);
  x.strokeStyle = 'rgba(232,237,244,.85)'; x.lineWidth = 3;
  x.beginPath(); x.moveTo(mid.l - 26, mid.y - 40); x.lineTo(mid.r + 26, mid.y - 40); x.stroke();
  const player = (px, py, s, col) => {
    x.fillStyle = col;
    x.beginPath(); x.arc(px, py - 34*s, 9*s, 0, 7); x.fill();
    x.beginPath(); x.roundRect ? x.roundRect(px - 8*s, py - 26*s, 16*s, 26*s, 6*s) : x.rect(px - 8*s, py - 26*s, 16*s, 26*s); x.fill();
  };
  player(430, 245, .8, '#c7cdd6'); player(560, 242, .8, '#aab2bd');
  player(300, 470, 1.3, '#e8edf4'); player(640, 480, 1.3, '#d0d6de');
  x.fillStyle = 'rgba(0,0,0,.35)'; x.fillRect(0, 0, W, 26);
  x.fillStyle = '#9aa5b1'; x.font = '12px monospace'; x.fillText('DEMO FRAME — synthetic court for design preview', 12, 17);
  return c.toDataURL('image/jpeg', .85);
}
function mockApi(p, opt) {
  const method = (opt && opt.method) || 'GET';
  return new Promise(res => setTimeout(() => res(mockRoute(p, method, opt)), 120));
}
function mockRoute(p, method, opt) {
  if (p === '/sessions') return demo.sessions;
  if (p === '/progress') return demo.progress;
  if (p.indexOf('/compare') === 0) {
    const q = new URLSearchParams(p.split('?')[1] || '');
    const mk = id => {
      const row = demo.sessions.find(s => s.session_id === id) || {};
      const pr = demo.progress.find(s => s.session_id === id);
      const base = pr || { kitchen_pct:26, distance_ft:1080, avg_rally_hits:5, dupr_band:3.25 };
      return { label: row.label || (pr && pr.label) || id, played_at: row.played_at || (pr && pr.date),
        kitchen_pct: base.kitchen_pct, transition_pct: base.transition_pct ?? 30, distance_ft: base.distance_ft,
        avg_speed_ft_s: 2.3, coverage_pct: Math.round((base.kitchen_pct||26)*1.9), rally_count: 32 + (id.charCodeAt(1)||0)%12,
        avg_rally_hits: base.avg_rally_hits, play_time_pct: 34, points_won: 12, points_lost: 9,
        win_pct: 57, dupr_band: base.dupr_band, dupr_confidence: base.dupr_confidence ?? .7 };
    };
    const A = mk(q.get('a')), B = mk(q.get('b')), diffs = {};
    ['kitchen_pct','transition_pct','distance_ft','avg_speed_ft_s','coverage_pct','rally_count','avg_rally_hits','play_time_pct','points_won','points_lost','win_pct','dupr_band','dupr_confidence'].forEach(k => {
      const av = A[k], bv = B[k];
      diffs[k] = { a: av, b: bv, delta: (av != null && bv != null) ? +(bv - av).toFixed(2) : null };
    });
    return { a: A, b: B, diffs };
  }
  if (p === '/sessions/delete') {
    const ids = JSON.parse(opt.body).ids;
    demo.sessions = demo.sessions.filter(s => !ids.includes(s.session_id));
    demo.progress = demo.progress.filter(s => !ids.includes(s.session_id));
    return { deleted: ids, errors: [] };
  }
  let mm = p.match(/^\/session\/([^/]+)\/calibrate$/) || p.match(/^\/session\/([^/]+)\/reprocess$/);
  if (mm && method === 'POST') { demoProc = { t0: Date.now(), dur: 26000 }; return { ok: true }; }
  mm = p.match(/^\/session\/([^/]+)\/meta$/);
  if (mm) {
    const body = JSON.parse(opt.body);
    Object.assign(demo.detail.meta, body);
    const row = demo.sessions.find(s => s.session_id === mm[1]);
    if (row) Object.assign(row, body);
    return demo.detail.meta;
  }
  mm = p.match(/^\/session\/([^/]+)$/);
  if (mm) {
    const id = mm[1];
    if (demoProc) {
      const el = (Date.now() - demoProc.t0) / demoProc.dur;
      if (el >= 1) { demoProc = null; const row = demo.sessions.find(s => s.session_id === id); if (row) { row.stage='done'; row.state='done'; row.overall=1; } }
      else {
        const stages = ['queued','tracking','metrics','events','shots','rating'];
        const st = stages[Math.min(stages.length - 1, Math.floor(el * stages.length))];
        return { session_id:id, stage:st, state:'running', overall:el, progress:(el*stages.length)%1, eta_seconds: Math.round((1-el)*demoProc.dur/1000), meta: demo.detail.meta };
      }
    }
    const row = demo.sessions.find(s => s.session_id === id);
    if (row && row.stage === 'ingest') return { session_id:id, stage:'ingest', state:'done', overall:0, meta:{} };
    if (row && row.state === 'running') return { session_id:id, stage:row.stage, state:'running', overall:row.overall, eta_seconds: 340, meta:{ label: row.label } };
    return Object.assign({}, demo.detail, { session_id: id });
  }
  return {};
}
