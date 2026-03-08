from flask import Blueprint


def build_projects_blueprint(handlers) -> Blueprint:
    bp = Blueprint("projects", __name__)
    bp.add_url_rule("/", view_func=handlers["index"], methods=["GET"])
    bp.add_url_rule("/api/projects", view_func=handlers["list_projects"], methods=["GET"])
    bp.add_url_rule("/api/projects/create", view_func=handlers["create_project"], methods=["POST"])
    bp.add_url_rule("/api/projects/rename", view_func=handlers["rename_project"], methods=["POST"])
    bp.add_url_rule("/api/projects/runs", view_func=handlers["list_project_runs"], methods=["GET"])
    bp.add_url_rule("/api/projects/load-run", view_func=handlers["load_project_run"], methods=["POST"])
    bp.add_url_rule("/api/projects/delete-run", view_func=handlers["delete_project_run"], methods=["POST"])
    bp.add_url_rule("/api/projects/open", view_func=handlers["open_project"], methods=["POST"])
    bp.add_url_rule("/api/export", view_func=handlers["export"], methods=["POST"])
    bp.add_url_rule("/api/load", view_func=handlers["load_project"], methods=["POST"])
    bp.add_url_rule("/api/pick-file", view_func=handlers["pick_file"], methods=["POST"])
    return bp
