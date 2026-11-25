"""The three demo apps (Converse, Customs Document Validation, Token Boss) as Flask blueprints.
Each has a JSON API under /api/<app>/; the pages are in static/apps.js."""


def register(app):
    from . import common, converse, validation, tokenboss
    for m in (common, converse, validation, tokenboss):
        app.register_blueprint(m.bp)
