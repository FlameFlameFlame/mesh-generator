import os
from pathlib import Path


def pytest_ignore_collect(collection_path, config):
    """Skip heavy e2e suite unless explicitly enabled."""
    if os.environ.get("RUN_E2E") == "1":
        return False
    p = Path(str(collection_path))
    return "tests" in p.parts and "e2e" in p.parts
