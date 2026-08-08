# Crown implementation handoff

## Delivered

- Isolated `crown/` Python backend with distinct state, lock, configuration,
  notification cache, dashboard data and systemd units.
- Recovered the supplied Crown dashboard assets into `crown/dashboard/`.
  The recovered archive `data.json` is retained as an artifact but is excluded
  from installation/update; a state-derived `data.json` is generated instead.
- PinnAPI Edge adapter accepts only prematch full-match (`periods.num_0`)
  fixtures/lines, keeps PinnAPI's home-handicap sign, requires quarter lines,
  and blocks missing/inferred timestamps by default.
- Titan007 Crown HDC/HIL adapter selects **only company ID 3**.  It does not
  substitute a visible/masked Crown name or any other bookmaker.
- Event mapping is a strict two-hop canonical bridge: Titan Chinese → HKJC
  Chinese (ICU Traditional→Simplified conversion, reviewed aliases,
  qualifiers, direct home/away, kickoff and ambiguity gates), then HKJC
  English → PinnAPI English.  Direct Titan → PinnAPI is forbidden across
  scripts.  HKJC CHL requires the same strict unique event with both official
  HKJC team IDs, but has no simulated-bet path until a verified sharp corners
  baseline exists.
- Prediction cards merge by match ID, so an empty tick cannot erase the
  earlier sweep.  They are conservatively pruned only six hours after kickoff.
- Rules are preserved: 23:59 first forecast, T-30 information only, T-5-only
  idempotent simulated bets, no real-betting path, idempotent notifications.
- Settlement uses observed-then-absent PinnAPI live scores after 105 minutes;
  Titan identity and exact official HKJC IDs are guarded fallback paths.
- Crown systemd timers (2 minutes and 23:59 HKT) are installed disabled.
  Update restarts a timer only if it was already enabled and never touches
  `/var/lib/footbreak/crown`.
- Crown dashboard is nginx Basic Auth on port 8082.  Footbreak remains 8081;
  no port-80 site is installed.

## Offline validation run

```text
python -m compileall -q crown
python -m unittest discover -s crown/tests -t .
bash crown/validate.sh
bash -n crown/validate.sh deploy/crown-run.sh deploy/setup.sh deploy/update.sh
systemd-analyze verify <units with local installation paths>
```

The regression tests cover Asian-line normalization/quarter settlement, PinnAPI
full-match parsing, source freshness, strict unique event matching, Chinese →
bilingual HKJC → English PinnAPI bridging, ambiguity rejection, HKJC team-ID
guard, Titan company ID 3 selection, sweep/tick prediction persistence,
dashboard permissions, T-30/T-5 ledger idempotency and notification
deduplication.

## Explicit blockers before enablement

1. No PinnAPI, Titan007, HKJC or Telegram network call was made in this work.
   Validate the paid PinnAPI Edge account in the deployed environment.
2. Confirm PinnAPI emits a trustworthy source timestamp.  The safe default
   rejects inferred timestamps; only set
   `CROWN_ALLOW_INFERRED_PINNAPI_TIMESTAMP=1` after evidence-based validation.
3. Confirm Titan007 still exposes Crown in company ID 3 for both Asian pages,
   and that its visible current triple parsing matches live HTML.
4. CHL has no currently verified PinnAPI sharp corners comparison.  It remains
   display-only and fail-closed by design.
5. Exercise PinnAPI-live, Titan and HKJC settlement fallbacks against known
   completed simulations before trusting automatic settlement.
6. Local nginx syntax validation was not run because the nginx binary/mime
   configuration is unavailable in this workspace.  `deploy/setup.sh` and
   `deploy/update.sh` both run `nginx -t` before reload.

## Exact next commands on the droplet

```bash
cd /opt/footbreak
sudo bash crown/validate.sh
sudo nano /etc/footbreak-crown.env
# Keep CROWN_ENABLED=0 until the next manual validation step.

# After adding a valid paid PinnAPI Edge key (or ensuring PINNAPI_* exists in
# /etc/footbreak.env), make a deliberate one-pass validation:
sudoedit /etc/footbreak-crown.env   # set CROWN_ENABLED=1; keep Telegram 0
sudo /opt/footbreak/deploy/crown-run.sh sweep
sudo /opt/footbreak/deploy/crown-run.sh tick
sudo cat /var/lib/footbreak/crown/health.json
sudo cat /var/www/crown/data.json | python3 -m json.tool | head -80
sudo systemctl status crown-tick.service --no-pager

# Only after reviewing actual fixture matching, freshness, exact lines,
# simulation idempotency and dashboard output:
sudo systemctl enable --now crown-tick.timer crown-sweep.timer
sudo systemctl list-timers 'crown*'
```
