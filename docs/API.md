# DinkIQ API contract

Base: `http://127.0.0.1:8100/api`. JSON unless stated. `static/dashboard.html` is the
reference client. **`tests/test_api_docs.py` fails if this file and the app disagree** —
every route has exactly one `### `METHOD /path`` heading, using the app's parameter names.

## Conventions

- **Auth.** With `DINKIQ_PASSWORD` set, every request needs HTTP Basic (any username,
  that password) unless it is genuinely local or arrives via the reverse proxy carrying
  `X-Dinkiq-Proxy: $DINKIQ_PROXY_SECRET`. Unauthenticated → `401` plain text with
  `WWW-Authenticate`. The Host header is never trusted on its own.
- **Errors.** `HTTPException`s are `{"detail": "message"}`; validation failures are `422`
  `{"detail": [{loc, msg, type, input}...]}`. Bulk endpoints answer `200` with an
  `errors[]` list instead of failing the whole call.
- **Numbers.** `NaN`/`Infinity` never reach a client: they are serialized as `null`.
- **Session ids** are plain names (`[A-Za-z0-9_][A-Za-z0-9_-]{0,63}`); server-made ids are
  12 hex chars. Anything else (including `.trash`) is `400 bad session id`.
- **Status.** `stage`: `new` | `ingest` | `queued` | `tracking` | `metrics` | `events` |
  `shots` | `points` | `rating` | `done` (or the stage that failed). `state`: `idle` |
  `queued` | `running` | `done` | `error` (+ `error` message). A session whose
  `status.json` is unreadable is reported as `state: error` so it can be recalibrated.
- **Busy sessions** (`state` ∈ `running`, `queued`) cannot be deleted or have their video
  replaced (`409` / an `errors[]` entry).

## Health

### `GET /health`
`200` only when every check passes, else `503` with the same body — alert on the status
code. `{ok, checks{ffmpeg,ffprobe,disk,data_dir,weights,worker}: {ok, detail}, uptime_s,
sessions_bytes, queue{queued, running}}`. `disk` fails below 2 GB free; `weights` checks the
pose model file; `worker` fails only if the analysis worker thread has died.

## Uploading

One path for new sessions **and** replacing a video: resumable and chunked (a phone on
cellular loses a single big POST to one dropped connection). There is no multipart upload.

### `POST /upload/init`
`{filename, match_type?: "singles"|"doubles", size?: bytes, session_id?}` →
`{upload_id, offset: 0}`. `filename` must end `.mp4`/`.mov`/`.m4v` (`400`). `size` lets the
server refuse early: `413` over 2 GB, `507` if it won't fit with a 2 GB disk reserve.
`session_id` makes this a *replacement* for that session (`404` unknown, `400` malformed,
`409` still processing). `429` when 6 uploads are already open (idle ones are reclaimed).

### `GET /upload/{upload_id}`
→ `{upload_id, offset}` — bytes stored so far; ask this after a dropped connection.
`404` unknown/expired (partials expire after 24 h).

### `PUT /upload/{upload_id}`
Query `offset` (must equal the server's offset), raw bytes as the body → `{upload_id, offset}`.
`409` on an offset mismatch (`detail` carries the true offset — re-slice and continue),
`413` past 2 GB, `507` if the disk is nearly full. One writer per upload at a time.

### `POST /upload/{upload_id}/finish`
`{filename, match_type?, session_id?}` → `{session_id, duration_s, warning?, capture_hint?}`;
ingest starts in the background. Without `session_id` a new session is created;
with it the video is swapped in **only if it probes as a readable video** (else `400`, original
kept), derived analysis is cleared and the session returns to `ingest`. `503` if ffmpeg/
ffprobe is missing from the service's PATH (the upload is kept for retry). `warning` for
videos over 30 min; `capture_hint` when the source is larger than the 720p30 analysis needs, or recorded in
portrait. Ingest re-encodes anything that isn't already 720p, constant 30.00 fps, upright H.264.

## Sessions

### `GET /sessions`
Newest first. Optional filters: `label`, `opponent` (case-insensitive substrings),
`date_from`/`date_to` (`YYYY-MM-DD`, inclusive, on `played_at`), `context`
(`practice`|`league`|`tournament`).
```json
{"session_id": "abc123def456", "filename": "match.mp4", "label": "Sat league",
 "played_at": "2026-07-05", "known_dupr": 3.75, "notes": null, "opponent": "Sam",
 "player": null, "context": "league", "disk_bytes": 47710000, "uploaded_at": 1720000000.0,
 "stage": "done", "state": "done", "overall": 1.0,
 "kitchen_pct": 31.2, "avg_rally_hits": 6.1, "dupr_band": 3.5, "dupr_confidence": 0.45}
```
The last four appear only when `stage == "done"`; `queue_position` appears while queued.

### `GET /session/{sid}`
Status fields + `meta` + whichever of `metrics`, `events`, `hits`, `shots`, `points`, `dupr` exist
(an unreadable artifact is simply omitted). While processing: `progress` (0–1 in stage),
`overall` (0–1), `eta_seconds`; `queue_position` while queued.

- `metrics`: `zone_pct{kitchen,transition,baseline}` + `zone_scope` (`"live"` = measured during rallies,
  `"all"` = the whole session, used when under ~30 s of live play was tracked) + `live_seconds`,
  `identity_issues[]` (e.g. `partner_not_found`: the partner tap landed on nobody, so the subject is
  analysed alone), `distance_ft`, `avg_speed_ft_s`,
  `active_seconds`, `frames_analyzed`, `median_dist_from_net_ft`, `coverage_pct`,
  `heatmap` (grid) + `heatmap_cell_ft`, `movement_curve[]`, `camera_cuts`,
  `secondary_court_tracks`, `match_type`, `subject_track_id`, `partner_track_id`,
  `opponent_track_ids[]`, `opponent_track_id`, `warnings[]`.
- `events`: `rally_count`, `total_hits`, `subject_shots`, `avg_rally_hits`, `max_rally_hits`,
  `avg_rally_seconds`, `play_time_pct`, `rallies[{start,end,hits,duration}]` (seconds),
  `swing_count`, `partner_swing_count`, `opponent_swing_count` (swings = wrist motion relative to the body
  over the player's height), `isolated_hits` (hits in no rally: serve faults, unreturned serves), track ids as above.
- `hits`: one row per corroborated paddle hit, `[{t, hitter, rally, n}]` — `hitter` ∈ `subject`|
  `partner`|`opponent1`|`opponent2`|`unknown`, `rally` the index into `events.rallies` (null for an
  isolated hit: a serve fault, ace or unreturned serve), `n` its 0-based position in that rally.
- `shots`: `available` (false → `reason`; ball metrics are withheld when the ball track doesn't show up after
  hits more often than at random moments), `ball{coverage,segments,raw_segments,frames,stride,hit_ball_rate,
  chance_ball_rate,hit_ball_lift}`,
  `shots[{t,type,mph?,mph_source?,swing_mph?,x,y}]` (type: drive|dink|drop|medium|unknown; `mph_source`
  `"ground"` = hitter→bounce distance over flight time, `"screen"` = fallback; an implausible speed has no mph
  and type `unknown`; subject's own hits only), `shot_mix{}`,
  `avg_shot_mph`, `top_shot_mph`, `top_shot_t`, `avg_swing_mph`, `serves_measured`,
  `avg_serve_depth_from_baseline_ft`, `deep_serve_pct`, `serves[]`, `serve_side_counts{}`,
  `opponent_shots{shots_tracked, side_counts, dominant_side, dominant_side_pct, landings[]}`,
  `bounces_px[]` (pixel-space, capped).
- `points`: `outcomes[{server,last_hitter,winner,unforced_error,corrected?}]` (index-aligned with
  `events.rallies`; winner ∈ `my_team`|`opp_team`|`unknown` — **unknown whenever no usable ball track
  saw where the last shot landed**; it is never assumed), `points_scored`, `points_won`, `points_lost`, `win_pct`, `serve_win_pct`,
  `return_win_pct` (any `null` if unknown), `unforced_errors`, `hits_by_player{}`,
  `serve_side_win_rates{}`, `shot_type_outcomes{}`, `caveat?` (ball tracking unavailable).
- `dupr`: `available` (false → `reason`, e.g. under 45 s of the player tracked), `band` (quarter
  steps), `band_low`/`band_high` (the range to show: ±0.25 when confident, up to ±1.25 when little is
  known), `band_raw`, `confidence` (0–1) + `confidence_level` (`low`|`medium`|`high`), `calibrated`
  (fitted against known-rating footage for this match type), `at_ceiling`/`at_floor` (the rubric spans
  about 2.5–5.0; a stronger player reads the same), only dimensions that were actually measured,
  `dimensions{name:{label,value,band,weight,...}}`, `caveats[]`, `tips[]`, `drills[]`.
  Always present it as "resembles ~X.X" with its range and confidence — never a bare number, never a
  certified rating.

### `PATCH /session/{sid}/meta`
Any of `{label, played_at, known_dupr: 2.0–8.0, notes, opponent, player, context}` → merged
meta. `played_at` must be a real `YYYY-MM-DD` (`""` clears it). `context` ∈
`practice`|`league`|`tournament`. A field sent as `null` is **cleared** (removed from the meta —
this is how `known_dupr` and `context` are unset); a field not sent is left alone.

### `PATCH /session/{sid}/points/{idx}`
`{winner: "my_team"|"opp_team"|"unknown", unforced_error?}` — override a detected rally
winner; recomputes the points summary (`404` index out of range). Corrections live in
`points.json`, so a recalibration or reprocess discards them.

### `PATCH /session/{sid}/clip/{n}/note`
`{note}` (≤500 chars; empty removes it) → `{clip_notes}`.

### `GET /session/{sid}/clip/{n}` → rally clip MP4 (`n` indexes `events.rallies`)
### `GET /session/{sid}/frame` → first-frame JPEG (calibration backdrop)
### `GET /session/{sid}/highlights.mp4`
Reel of the top rallies, built on first request and cached until recalibration. `409` unless
`done`; `404` if there are no clips.
### `GET /session/{sid}/report.pdf` → one-page PDF (`409` unless `done`)
### `GET /session/{sid}/card.png` → shareable stat card (`409` unless `done`)

### `GET /session/{sid}/subject-marker`
`{subject, partner, opponent, opponents[]}` — earliest-frame pixel boxes `{frame,x1,y1,x2,y2}`
for the "who are we tracking" overlay. `200` with nulls (never `404`) when tracking isn't ready.

### `GET /session/{sid}/synergy`
Doubles partner positioning report, computed on demand. `{available: false, reason}` for
singles or missing data; `409` unless `done`.

### `POST /session/{sid}/calibrate`
```json
{"corners_px": [[x,y],[x,y],[x,y],[x,y]],
 "kitchen_px": [[x,y],[x,y],[x,y],[x,y]],
 "self_px": [x,y], "partner_px": [x,y]}
```
Click order for both quads: far-left, far-right, near-right, near-left. Corner coordinates
may lie outside the frame; every point must be `[x, y]`, finite and within ±20 000 px
(`422` otherwise — `NaN`/`Infinity` are rejected). `kitchen_px` is optional but improves
accuracy. `partner_px` is required for doubles sessions (`400`). Allowed once ingest is done
and on `done`/`error` sessions (recalibration reuses detection, so it's fast); `409` otherwise.
Saves the corners for `last-calibration` and queues analysis.

### `GET /last-calibration`
`{available: false}` or `{available: true, corners_px, kitchen_px, frame_size}` — the last
court corners, reusable for a fixed camera. Self/partner clicks are never saved.

### `POST /session/{sid}/reprocess`
Rerun analysis with the existing calibration. `409` without a calibration or while processing.

### `POST /sessions/delete`
`{"ids": [...]}` → `{deleted: [], errors: [{id, error}]}`. **Soft delete** into the trash,
restorable for 7 days. Busy sessions are skipped with `error: "session is still processing"`.

### `GET /sessions/trash`
→ `[{session_id, label, filename, deleted_at}]`, newest first. Also purges expired items.

### `POST /sessions/restore`
`{"ids": [...]}` → `{restored: [], errors: [{id, error}]}`.

### `GET /sessions/export.csv`
CSV of every completed session's summary stats. Text cells starting `=`, `+`, `-` or `@`
are prefixed with `'` so a spreadsheet won't execute them.

## Trends and comparison

### `GET /progress`
Completed sessions, oldest first. `?player=NAME` keeps only that player's footage (sessions with no
`player` tag count as theirs; case-insensitive):
```json
{"session_id":"abc","label":"Sat league","date":"2026-07-05","kitchen_pct":31.2,
 "transition_pct":24.0,"distance_ft":1204.5,"avg_rally_hits":6.1,"dupr_band":3.75,
 "dupr_confidence":0.8,"known_dupr":null,"weakest_dimension":"nvz_discipline",
 "weakest_label":"...","dimensions":{}}
```
`date` is `played_at` if set, else the upload date.

### `GET /compare`
Query `a`, `b`. → `{a: {...summary}, b: {...summary}, diffs: {field: {a, b, delta}}}` for two
`done` sessions (`409` if either isn't). `delta = b - a`; `null` when either side lacks the value.

### `GET /compare/multi`
Query `ids` (comma-separated, ≥2) → `{sessions: [summary, ...]}` — no deltas.

### `GET /opponents/{name}/history`
Case-insensitive exact match on a session's `opponent`:
`{opponent, matches, wins, losses, win_pct, sessions[summary]}` (a "win" is a session with
more points won than lost).

### `GET /opponents/{name}/scouting`
History plus the opponent's shot-landing tendency: `{shots_tracked, side_counts,
dominant_side, dominant_side_pct, ...}`.

### `GET /streak`
Consecutive-day play streak. `?player=NAME` scopes it to that player's sessions (untagged sessions
count as theirs; case-insensitive); without it, sessions whose `player` is unset or `"You"`:
`{current_streak_days, longest_streak_days, sessions_this_week, days_played}`.

### `GET /leaderboard`
`{players: [{player, sessions, latest_band, best_band, win_pct, total_distance_ft}]}`
ranked by latest band; sessions with no `player` count as `"You"`.

## UI guidance

- Poll `GET /session/{sid}` ~1.5 s while `state` ∈ {queued, running}; drive the progress bar
  from `overall` + `eta_seconds`.
- Always render `metrics.warnings` and `dupr.caveats` prominently — honesty about accuracy is a
  product feature.
- `shots.available == false` is a normal outcome (ball untrackable), not an error; show
  `reason` and the rest.
- `points.outcomes[i].winner == "unknown"` is common and expected — omit the badge, don't error.
- DUPR framing: "your play resembles ~X.X", never a certified rating.
