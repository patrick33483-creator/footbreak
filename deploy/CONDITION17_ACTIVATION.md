# Condition 17 two-phase production activation

Condition 17's legacy 18-row compatibility cohort is default-off. Deploying a
commit, restarting services, rebuilding dashboards, or running the read-only
preflight does not activate it.

The runtime enables this one compatibility path only when the fixed file below
exists as a private, owner-controlled, regular, single-link file:

`/var/lib/footbreak/activation/condition17-legacy-cohort-v1.json`

The marker must contain exactly:

```json
{
  "quarter_line_sha256": "<reviewed SHA-256>",
  "schema": "footbreak-condition17-legacy-cohort-v1",
  "wilson_validation_sha256": "<reviewed SHA-256>"
}
```

Both hashes are checked against the exact loaded source files. A missing,
malformed, linked, over-permissive, stale, or source-mismatched marker leaves
the path disabled. Therefore a later deployment that changes either source
file automatically returns the compatibility path to the disabled state until
that version is reviewed again.

Safe sequence:

1. Review and deploy the exact commit. The activation marker must remain
   absent.
2. Run the manual condition 17 production preflight with the reviewed commit,
   Git tree, module digest, and production identity/evidence hashes.
3. Treat any failed or incomplete run as `NO-GO`. Do not create the production
   marker.
4. After a `GO`, activation is a separate operator-controlled change. Create
   the marker atomically with mode `0400`, while holding the Footbreak writer
   lock, using the exact reviewed source hashes. Restarting or waiting for the
   next service invocation then applies the gate.

The preflight creates only a runner-local synthetic marker so its deep-copy
simulation can exercise the post-activation branch. It never creates, changes,
or removes the production marker, ledger, services, environment, providers,
Telegram state, dashboards, or deployment checkout.
