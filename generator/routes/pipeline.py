from flask import Blueprint


def build_pipeline_blueprint(handlers) -> Blueprint:
    bp = Blueprint("pipeline", __name__)
    bp.add_url_rule(
        "/api/tower-coverage/calculate",
        view_func=handlers["calculate_tower_coverage_single"],
        methods=["POST"],
    )
    bp.add_url_rule(
        "/api/tower-coverage/calculate-batch",
        view_func=handlers["calculate_tower_coverage_batch"],
        methods=["POST"],
    )
    bp.add_url_rule("/api/elevation", view_func=handlers["download_elevation"], methods=["POST"])
    bp.add_url_rule(
        "/api/elevation-image",
        view_func=handlers["get_elevation_image"],
        methods=["GET"],
    )
    bp.add_url_rule("/api/grid-layers", view_func=handlers["get_grid_layers"], methods=["POST"])
    bp.add_url_rule("/api/path-profile", view_func=handlers["path_profile"], methods=["POST"])
    bp.add_url_rule("/api/link-analysis", view_func=handlers["link_analysis"], methods=["POST"])
    bp.add_url_rule("/api/generate", view_func=handlers["generate"], methods=["POST"])
    bp.add_url_rule(
        "/api/roads/filter-p2p",
        view_func=handlers["filter_p2p"],
        methods=["POST"],
    )
    bp.add_url_rule(
        "/api/roads/select-routes",
        view_func=handlers["select_routes"],
        methods=["POST"],
    )
    bp.add_url_rule(
        "/api/roads/reroute-with-waypoints",
        view_func=handlers["reroute_with_waypoints"],
        methods=["POST"],
    )
    return bp
