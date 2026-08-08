# Footbreak PinnAPI Edge migration handoff

## Delivered

- `system/sharp.py` now uses PinnAPI Edge directly and preserves Footbreak's
  legacy `list_fixtures`, `match_fixture`, `fetch_odds`, and `structure`
  interface for `run_predict.py`, `recommend.py`, and dashboard consumers.
- PinnAPI fixtures are projected into the existing English fixture shape.
  PinnAPI HDC is kept in its native home-perspective orientation: negative
  means the home team gives.  HIL uses the same over/under `H`/`L` convention.
- No OpticOdds request or persistent stale fixture cache remains in the live
  Footbreak sharp path.  A PinnAPI fetch/line failure raises `ProviderError`.
- The first complete local PinnAPI observation is persisted as Footbreak's
  opening reference.  This is explicitly not a vendor historical opening.
- `deploy/run.sh` loads `/etc/footbreak.env`, then
  `/etc/footbreak-crown.env`, without logging either file.  It publishes
  `hkjc-dashboard/data.json` only after a completely successful `run_all.sh`.
- `system/run_all.sh` is fail-fast.  A sharp/prediction failure stops before
  record/settle/dashboard work and returns nonzero to systemd.

## Offline validation

```text
python -m unittest discover -s system/tests -t .    # 5 passed
python -m unittest discover -s crown/tests -t .     # 12 passed
python -m compileall -q system bin crown
bash -n system/run_all.sh deploy/run.sh
```

## Compatibility gaps

1. PinnAPI Edge does not provide the Optic-style historical-opening endpoint.
   Footbreak consequently uses its first valid local PinnAPI snapshot as the
   opening reference; the first pass reports no movement baseline.
2. This adapter has no verified PinnAPI football corners market.  `CHL` sharp
   input remains empty and is fail-closed; HDC/HIL/HAD keep working.
3. Footbreak's old settlement/result and independent-fatigue code still has
   OpticOdds-shaped fallback code.  If a due simulated bet needs a result and
   that provider is unavailable, the new fail-fast runner exits nonzero rather
   than silently rebuilding stale data.  Porting settlement to Crown's
   PinnAPI-live/Titan/HKJC guarded result path is the next migration step.
