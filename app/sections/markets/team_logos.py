"""Local team logo routes."""

from flask import abort, send_from_directory
from app.sections.markets.market_view import NBA_LOGO_DIRECTORY, NBA_LOGO_FILES, NFL_LOGO_DIRECTORY, NFL_LOGO_FILES, NCAAF_LOGO_DIRECTORY, NCAAF_LOGO_FILES


def register(app):
    @app.server.get("/images/nfl/team-logos/<filename>")
    def nfl_team_logo(filename):
        if filename not in NFL_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NFL_LOGO_DIRECTORY, filename, mimetype="image/png")

    @app.server.get("/images/nba/team-logos/<filename>")
    def nba_team_logo(filename):
        if filename not in NBA_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NBA_LOGO_DIRECTORY, filename, mimetype="image/png")

    @app.server.get("/images/ncaaf/team-logos/<filename>")
    def ncaaf_team_logo(filename):
        if filename not in NCAAF_LOGO_FILES.values():
            abort(404)
        return send_from_directory(NCAAF_LOGO_DIRECTORY, filename, mimetype="image/png")
