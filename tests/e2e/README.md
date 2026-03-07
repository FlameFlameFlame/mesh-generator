# Playwright Smoke Validation

Run from `mesh-generator`:

```bash
RUN_E2E=1 poetry run pytest -q tests/e2e/test_playwright_smoke.py
```

What it validates:
- default output directory is `/Users/timur/Documents/src/LoraMeshPlanner/projects`
- save/load flow to a project directory under `projects/`
- cache and grid bundle creation + cache reuse log hits
- tower coverage progress UI appears and coverage data is produced

Artifacts:
- screenshots and logs are written to `tests/e2e/artifacts/<timestamp>/`
- project output is written to `../projects/playwright-smoke-<timestamp>/`
