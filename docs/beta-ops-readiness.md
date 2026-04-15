# Beta Farm Ops Initial Pass Runbook

This runbook captures the minimum deterministic gates for the Beta Farm Ops initial pass.
It targets three slices:

- importer determinism and recovery clarity
- critical gate coverage
- repeatable operator verification

## Importer Determinism and Recovery

Use `--validate-only` (or `--preflight`) before apply runs. Both modes emit stable
`row_errors`, `failure_signatures`, and `escalation_summary` payloads.

```bash
python manage.py import_historical_data "data/import_fixtures/mismatch" --validate-only
python manage.py import_historical_data "data/import_fixtures/mismatch"
```

When a fatal exception occurs, the importer now emits deterministic failure context in
`fatal_error`, including mode and transaction behavior:

- `mode` (`validate-only` or `apply`)
- `atomic_apply`
- `dry_run`

The terminal handoff includes:

- summary artifact path to inspect before retry
- explicit non-atomic retry hint for apply-mode diagnostics

## Critical Gate Coverage

Run the importer + route smoke tests from `apps/core/tests.py`:

```bash
python manage.py test apps.core.tests.ImportHistoricalDataCommandTests
python manage.py test apps.core.tests.PrimaryRouteSmokeTests
python manage.py test apps.core.tests.BetaGateEvidenceTests
```

These tests cover:

- deterministic row error ordering and payload contract
- failure signature ownership mapping and escalation grouping
- preflight/apply parity for critical importer error signals
- critical route availability and write-path workflow integration checks

## Operator Expectations

- `--validate-only` must remain read-only.
- Apply mode should default to atomic rollback safety.
- `--non-atomic-apply` should only be used for recovery diagnostics.
- Escalation handoff output is an operator contract and should stay deterministic.
