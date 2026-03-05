from generator.app import _normalize_mesh_parameters


def test_normalize_mesh_parameters_defaults_to_strict_los():
    params = _normalize_mesh_parameters({})
    assert params["min_fresnel_clearance_m"] == 0.0


def test_normalize_mesh_parameters_preserves_explicit_budget_mode():
    params = _normalize_mesh_parameters({"min_fresnel_clearance_m": None})
    assert "min_fresnel_clearance_m" in params
    assert params["min_fresnel_clearance_m"] is None
