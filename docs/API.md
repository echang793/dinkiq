# PickleCoach API contract

Base: `http://127.0.0.1:8100/api`. All responses JSON. `static/dashboard.html` is a
reference client — the production UI (Claude Design) builds against this contract.

## Sessions

### `POST /upload` (multipart, field `file`, .mp4/.mov/.m4v, ≤2 GB)
→ `{session_id, duration_s, warning?}` — ingest starts in background.
413 over size; 400 bad type. `warning` set for >30 min videos.

### `GET /sessions`
→ array, newest first:
```json
{"session_id": "abc123", "filename": "match.mp4", "label": "Sat league",
 "played_at": "2026-07-05", "known_dupr": 3.75, "uploaded_at": 1720000000.0,
 "stage": "done", "state": "done", "overall": 1.0}
```
`stage`: queued | ingest | tracking | metrics | events | shots | points | rating | done.
`state`: queued | running | done | error (+ `error` message).

### `GET /session/{id}`
→ status fields above + `meta` + (when ready) `metrics`, `events`, `shots`, `points`, `dupr`.
While processing: `progress` (0–1 in stage), `overall` (0–1 whole job),
`eta_seconds` (int, may be absent early).

Key payload shapes:
- `metrics`: `zone_pct{kitchen,transition,baseline}`, `distance_ft`, `avg_speed_ft_s`,
  `active_seconds`, `median_dist_from_net_ft`, `coverage_pct`, `heatmap` (22×10 grid,
  2 ft cells, court length × width), `camera_cuts`, `subject_track_id`,
  `opponent_track_id` (nullable), `warnings[]`.
- `events`: `rally_count`, `total_hits`, `subject_shots`, `avg_rally_hits`,
  `max_rally_hits`, `avg_rally_seconds`, `play_time_pct`, `swing_count`,
  `opponent_track_id` (nullable), `opponent_swing_count`,
  `rallies[{start,end,hits,duration}]` (seconds).
- `shots`: `available` (false → `reason`), `ball{coverage,segments,frames,stride}`,
  `shots[{t,type}]` (type: drive|dink|drop|medium|unknown), `shot_mix{}`,
  `serves_measured`, `avg_serve_depth_from_baseline_ft`, `deep_serve_pct`.
- `points`: `points_scored`, `points_won`, `points_lost`, `win_pct`,
  `serve_win_pct`, `return_win_pct` (any may be `null` if not enough known-winner
  rallies), `unforced_errors`, `caveat?` (present when ball tracking was
  unavailable — winners then assume the rally's last shot was in),
  `outcomes[{server,winner,unforced_error}]` (winner/server: subject|opponent|
  unknown, aligned index-for-index with `events.rallies`).
- `dupr`: `available`, `band` (quarter steps, e.g. 3.5), `band_raw`, `confidence`
  (0–1), `dimensions{name:{label,value,band,weight}}`, `caveats[]`, `tips[]`.

### `GET /session/{id}/frame` → first-frame JPEG (calibration backdrop)
### `GET /session/{id}/clip/{n}` → rally clip MP4 (n = rally index from `events.rallies`)
### `GET /session/{id}/report.pdf` → one-page PDF summary (DUPR card, stats, tips). 409 if session isn't `done`.

### `POST /session/{id}/calibrate`
```json
{"corners_px": [[x,y],[x,y],[x,y],[x,y]],
 "kitchen_px": [[x,y],[x,y],[x,y],[x,y]],
 "self_px": [x,y]}
```
Click order both quads: far-left, far-right, near-right, near-left.
Corner coords may lie OUTSIDE the frame (off-screen estimates); kitchen + self must
be in-frame. `kitchen_px` optional but strongly improves accuracy.
Allowed post-ingest AND on done/error sessions (recalibration — reuses detection,
fast). 409 otherwise. Queues analysis (`stage: queued`).

### `POST /session/{id}/reprocess` → rerun analysis with existing calibration.
### `PATCH /session/{id}/meta` — any of `{label, played_at: "YYYY-MM-DD", known_dupr: 2.0–8.0}` → merged meta.
### `POST /sessions/delete` — `{"ids": [...]}` → `{deleted:[], errors:[{id,error}]}`. Permanent.

## Trends

### `GET /progress`
→ completed sessions oldest-first:
```json
{"session_id":"abc","label":"Sat league","date":"2026-07-05","kitchen_pct":31.2,
 "transition_pct":24.0,"distance_ft":1204.5,"avg_rally_hits":6.1,
 "dupr_band":3.75,"dupr_confidence":0.8,"known_dupr":null}
```
`date` = `played_at` if set, else upload date. The fitness-app trend feed.

### `GET /compare?a={id}&b={id}`
→ `{a: {...summary}, b: {...summary}, diffs: {field: {a, b, delta}}}` for two
`done` sessions (409 if either isn't). Summary fields: `kitchen_pct`,
`transition_pct`, `distance_ft`, `avg_speed_ft_s`, `coverage_pct`, `rally_count`,
`avg_rally_hits`, `play_time_pct`, `points_won`, `points_lost`, `win_pct`,
`dupr_band`, `dupr_confidence` (plus `label`/`played_at`, excluded from `diffs`).
`delta = b - a`; `null` when either side lacks the value (e.g. no DUPR estimate).

## UI guidance

- Poll `GET /session/{id}` ~1.5 s while `state` ∈ {queued, running}; drive the
  progress bar from `overall` + `eta_seconds`.
- Always render `metrics.warnings` and `dupr.caveats` prominently — honesty about
  accuracy is a product feature.
- `shots.available == false` is a normal outcome (ball untrackable), not an error;
  show `reason` and the rest of the results.
- `points.outcomes[i].winner == "unknown"` is common and expected (noisy audio,
  no clear swing match) — don't treat rows with unknown winners as errors; just
  omit the win/loss badge for that rally.
- DUPR framing: "your play resembles ~X.X", never a certified rating.
