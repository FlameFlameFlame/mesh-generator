from generator.routes.optimization import build_optimization_blueprint
from generator.routes.pipeline import build_pipeline_blueprint
from generator.routes.projects import build_projects_blueprint
from generator.routes.sites import build_sites_blueprint


def register_blueprints(app, handlers) -> None:
    app.register_blueprint(build_projects_blueprint(handlers))
    app.register_blueprint(build_sites_blueprint(handlers))
    app.register_blueprint(build_pipeline_blueprint(handlers))
    app.register_blueprint(build_optimization_blueprint(handlers))
