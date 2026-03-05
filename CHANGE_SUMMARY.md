# Change Summary

- 2026-03-05: Fixed export parameter drift in `generator/app.py` by replacing hardcoded `routes.json` parameters with merged runtime/export parameters.
- 2026-03-05: Unified parameter propagation so `config.yaml`, `routes.json`, and `status.json` share the same parameter set.
- 2026-03-05: Added regression test `tests/test_save_project.py` to validate exported route parameters and tower limits.
