# Granular condition signals — verification

Branch: `granular-condition-signals-20260814`

Implemented:

- Shared fail-closed granular condition mining in `analysis/granular_conditions.py`.
- Current-era Footbreak and Crown dashboard payloads with bounded public rankings and upcoming-card matches.
- T-30 matches limited to available First/T-30 history; T-5 can use paths through T-5.
- Fresh-stage-only T-30/T-5 granular notifications with versioned fixture/market/stage IDs.
- Legacy Footbreak/Crown notification dispatch paths retired.
- Responsive ranking/card styling and static asset cache-buster updates.

Verification completed:

```text
python3 -m unittest discover -s analysis/tests -q
Ran 215 tests ... OK

python3 -m unittest discover -s system/tests -q
Ran 171 tests ... OK (skipped=10 retired notification-path tests)

python3 -m unittest discover -s crown/tests -q
Ran 138 tests ... OK (skipped=11 retired notification-path tests)

python3 -m py_compile ...
node --check hkjc-dashboard/app.js
node --check crown/dashboard/app.js
bash -n deploy/*.sh system/*.sh crown/validate.sh
git diff --check
```

No commit, push, deployment, production access, or Telegram send was performed.
