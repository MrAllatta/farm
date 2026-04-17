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

## Mix Data Troubleshooting (Phase 2)

When mix-product packing is enabled, use this deterministic recovery order:

1. Validate recipe integrity in admin:
   - exactly one active recipe per product
   - components define exactly one source (`crop` or `product`)
   - if all components are percent-based, total must be `100.00`
2. Confirm pack batch has component rows before posting consumption.
3. Post consumption and verify inventory ledger:
   - one negative `sale_out` ledger row per crop-backed component
   - running balance updated deterministically from latest prior balance
4. For sales traceability, confirm `SalesEvent.pack_batch` is set for detailed entries sourced from same-day pack allocations.
5. If consumption posting fails:
   - keep the batch for audit
   - fix component rows
   - re-run posting; do not hand-edit ledger balances directly.
