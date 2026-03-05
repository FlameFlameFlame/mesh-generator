# Change Summary

- 2026-03-05: Fixed export parameter drift in `generator/app.py` by replacing hardcoded `routes.json` parameters with merged runtime/export parameters.
- 2026-03-05: Unified parameter propagation so `config.yaml`, `routes.json`, and `status.json` share the same parameter set.
- 2026-03-05: Added regression test `tests/test_save_project.py` to validate exported route parameters and tower limits.
- 2026-03-05: Switched tower coverage to runtime-only mode; `/api/tower-coverage` now serves the last computed in-memory result instead of loading `tower_coverage.geojson`.
- 2026-03-05: Added runtime tower coverage endpoints: `POST /api/tower-coverage/calculate` (single source) and `POST /api/tower-coverage/calculate-batch` (batch sources).
- 2026-03-05: Removed `tower_coverage.geojson` from optimization output loading/copying while keeping stale-file cleanup support in `clear-calculations`.
- 2026-03-05: Added UI controls for runtime tower coverage: `Calc Selected`, `Calc All Shown`, and map-click `Point Coverage` mode.
- 2026-03-05: Added tower-marker source selection and algorithm-aware batch source collection (`dp`/`greedy`/`both`) with H3 deduplication.
- 2026-03-05: Added API regression tests in `tests/test_tower_coverage_api.py` (runtime cache serve, elevation requirement, single/batch wiring).
