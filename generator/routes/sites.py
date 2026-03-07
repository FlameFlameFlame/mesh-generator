from flask import Blueprint


def build_sites_blueprint(handlers) -> Blueprint:
    bp = Blueprint("sites", __name__)
    bp.add_url_rule("/api/sites", view_func=handlers["get_sites"], methods=["GET"])
    bp.add_url_rule("/api/sites", view_func=handlers["add_site"], methods=["POST"])
    bp.add_url_rule("/api/sites/<int:idx>", view_func=handlers["update_site"], methods=["PUT"])
    bp.add_url_rule("/api/sites/<int:idx>", view_func=handlers["delete_site"], methods=["DELETE"])
    bp.add_url_rule(
        "/api/sites/<int:idx>/detect-city",
        view_func=handlers["detect_city_boundary"],
        methods=["POST"],
    )
    bp.add_url_rule("/api/clear", view_func=handlers["clear_project"], methods=["POST"])
    bp.add_url_rule(
        "/api/clear-calculations",
        view_func=handlers["clear_calculations"],
        methods=["POST"],
    )
    bp.add_url_rule("/api/coverage", view_func=handlers["get_coverage"], methods=["GET"])
    bp.add_url_rule(
        "/api/tower-coverage",
        view_func=handlers["get_tower_coverage"],
        methods=["GET"],
    )
    return bp
