# Crown / 皇冠 backend

This tree is isolated from `system/`: it has its own ledger, prediction cache,
notification state, live-score cache, dashboard data, environment file, lock,
nginx site, and systemd timers.

## Safety contract

- It is **simulation-only**.  There is no order-placement client, no broker
  credential field, and no configuration switch that can enable real betting.
- `首預` runs in the 23:59 HKT sweep, `T-30` records information only, and
  `T-5` is the only stage allowed to create one idempotent simulated bet.
- HDC and HIL need the Titan007 **Crown company ID 3** quote and a unique,
  exact-line PinnAPI full-match reference.  Missing, stale, reversed, or
  ambiguous data produces `DATA_MISSING`, not a prediction.
- CHL is display-only unless the HKJC event passes strict unique
  team/home-away/kickoff matching.  It has no simulated-bet path until a
  verified sharp corners baseline is available.
- Settlement accepts a PinnAPI live score only after that event was observed
  live, later disappeared, and 105 minutes elapsed.  Titan fallback validates
  stored fixture identity; HKJC fallback accepts only an exact official HKJC
  match ID and confirmed full-match result.
- Telegram uses `CROWN_TELEGRAM_*`, not Footbreak's variables, and is disabled
  by default.  Notification IDs are persisted before later runs can repeat it.

## Configuration

`/etc/footbreak-crown.env` is created from
`deploy/footbreak-crown.env.example`.  The expected PinnAPI Edge names are:

```ini
PINNAPI_API_KEY=
# Or platform-injected CUSTOM_CRED_PINNAPI_COM_TOKEN
PINNAPI_BASE_URL=https://pinnapi.com
CROWN_ENABLED=0
CROWN_TELEGRAM_ENABLED=0
```

The runners load `/etc/footbreak.env` first for a pre-existing PinnAPI Edge
credential and then load `/etc/footbreak-crown.env` as an override.  No setup
or update script copies a credential between those files.

## Offline validation

```bash
cd /opt/footbreak
bash crown/validate.sh
```

Do not enable either Crown timer until an operator has validated fixture
matching, source freshness, exact line mapping, dashboard output, and
simulation/notification idempotency against live data.
