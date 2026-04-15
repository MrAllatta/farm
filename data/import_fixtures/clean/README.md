## Clean Fixture Pack

This fixture pack is intended to pass preflight/import without mismatches.

Included files:

- `blocks.csv`: minimum valid block input for reference tier smoke coverage.
- `manifest.json`: canonical validate-only gate expectation for this pack.

Expected canonical outcomes:

- validate-only run: `status=ok`, `totals={"created":0,"updated":0,"skipped":1,"error":0}`
