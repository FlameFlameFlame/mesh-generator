import logging
import os
import re
import threading
from dataclasses import dataclass, field

from flask import current_app

from generator.optimization_manager import OptimizationJobManager
from generator.runtime_state import AppState


@dataclass
class AppContext:
    state: AppState
    jobs: OptimizationJobManager
    logger: logging.Logger
    default_output_dir: str
    project_name_re: re.Pattern
    workspace_root: str
    calc_layer_to_filename: dict
    low_mast_warn_threshold_m: float = 5.0
    thread_local: threading.local = field(default_factory=threading.local)


def build_default_context(logger: logging.Logger) -> AppContext:
    workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    default_output_dir = os.path.join(workspace_root, "projects")
    return AppContext(
        state=AppState(),
        jobs=OptimizationJobManager(),
        logger=logger,
        default_output_dir=default_output_dir,
        project_name_re=re.compile(r"^[A-Za-z0-9 _().-]{1,120}$"),
        workspace_root=workspace_root,
        calc_layer_to_filename={
            "towers": "towers.geojson",
            "edges": "visibility_edges.geojson",
            "grid_cells": "grid_cells.geojson",
            "grid_cells_full": "grid_cells_full.geojson",
            "gap_repair_hexes": "gap_repair_hexes.geojson",
            "coverage": "coverage.geojson",
        },
    )


def get_app_context() -> AppContext:
    return current_app.extensions["app_context"]
