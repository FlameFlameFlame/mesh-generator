from flask import Blueprint


def build_optimization_blueprint(handlers) -> Blueprint:
    bp = Blueprint("optimization", __name__)
    bp.add_url_rule(
        "/api/run-optimization",
        view_func=handlers["run_optimization"],
        methods=["POST"],
    )
    bp.add_url_rule(
        "/api/cancel-optimization",
        view_func=handlers["cancel_optimization"],
        methods=["POST"],
    )
    bp.add_url_rule(
        "/api/optimization-result",
        view_func=handlers["get_optimization_result"],
        methods=["GET"],
    )
    bp.add_url_rule(
        "/api/optimization-stream",
        view_func=handlers["optimization_stream"],
        methods=["GET"],
    )
    return bp
