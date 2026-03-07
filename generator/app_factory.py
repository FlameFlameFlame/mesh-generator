from flask import Flask, jsonify

from generator.app_context import build_default_context


def create_app(config: dict | None = None) -> Flask:
    """Create the Flask app with default extensions used by the API layer."""
    app = Flask(__name__)
    if config:
        app.config.update(config)

    ctx = build_default_context(app.logger)
    app.extensions["app_context"] = ctx
    # Backward-compat references while tests migrate to context-first.
    app.extensions["app_state"] = ctx.state
    app.extensions["optimization_manager"] = ctx.jobs

    @app.errorhandler(500)
    def _handle_500(e):
        return jsonify({"error": f"Internal server error: {e}"}), 500

    return app
