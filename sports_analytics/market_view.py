"""Saved NFL markets, presented as category tabs and player odds ladders."""

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from urllib.parse import quote

from dash import dcc, html
import plotly.graph_objects as go

from Models.PPP import PPPError, implied_probability
from .display_time import eastern_datetime, format_eastern
from .nfl_markets import NFL_TEAM_CODES

NFL_LOGO_DIRECTORY = Path(__file__).resolve().parent.parent / "images" / "NFL" / "Team Logos"
NFL_LOGO_FILES = {path.stem.casefold(): path.name for path in NFL_LOGO_DIRECTORY.glob("*.png")}
CATEGORIES = [("Game", "Game Lines"), ("Passing", "Passing Props"), ("Receiving", "Receiving Props"),
              ("Rushing", "Rushing Props"), ("Touchdowns", "TD Scorer Props")]
MARKET_NAMES = {
    "player_pass_yds_alternate": ("Alt Passing Yds", "Passing Yards"),
    "player_pass_tds_alternate": ("Alt Passing TDs", "Passing TDs"),
    "player_pass_attempts_alternate": ("Alt Pass Attempts", "Pass Attempts"),
    "player_pass_completions_alternate": ("Alt Completions", "Completions"),
    "player_pass_interceptions_alternate": ("Alt Interceptions", "Interceptions"),
    "player_rush_yds_alternate": ("Alt Rushing Yds", "Rushing Yards"),
    "player_rush_attempts_alternate": ("Alt Rush Attempts", "Rush Attempts"),
    "player_rush_tds_alternate": ("Alt Rushing TDs", "Rushing TDs"),
    "player_reception_yds_alternate": ("Alt Receiving Yds", "Receiving Yards"),
    "player_receptions_alternate": ("Alt Receptions", "Receptions"),
    "player_reception_tds_alternate": ("Alt Receiving TDs", "Receiving TDs"),
    "player_tds": ("Total Touchdowns", "TDs"),
}


def latest_pull_text(snapshot):
    if not snapshot:
        return "Latest data: Not pulled yet"
    return "Latest data: " + format_eastern(snapshot["pulled_at"])


def recent_market_data(snapshot, now=None):
    if not snapshot:
        return False
    try:
        age = eastern_datetime(now or datetime.now(timezone.utc)) - eastern_datetime(snapshot["pulled_at"])
        return timedelta(0) <= age < timedelta(hours=24)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def latest_pull_status(snapshot, now=None):
    recent = recent_market_data(snapshot, now)
    return html.Span([
        html.Span("Latest data:", className="latest-pull-label"),
        html.Strong(format_eastern(snapshot["pulled_at"]) if snapshot else "Not pulled yet", className="latest-pull-timestamp"),
    ], className="market-data-recent" if recent else "market-data-old",
        title="Market data pulled within the last 24 hours" if recent else
              "Market data is at least 24 hours old" if snapshot else "No saved market data")


def logo_badge(name, abbreviation):
    filename = NFL_LOGO_FILES.get(name.casefold())
    return html.Span(html.Img(src="/images/nfl/team-logos/" + quote(filename), alt=name + " logo", className="team-logo")
                     if filename else abbreviation, className="team-badge team-badge-logo")


def game_header(game, snapshot=None):
    if not game and snapshot:
        game = snapshot.get("game") or {"start": snapshot["commence_time"],
            "away": {"name": snapshot["away_team"], "abbreviation": snapshot.get("away_abbreviation", "AWAY")},
            "home": {"name": snapshot["home_team"], "abbreviation": snapshot.get("home_abbreviation", "HOME")}}
    if not game:
        return html.H2("Saved game markets")
    def team(side):
        item = game[side]
        return html.Div([logo_badge(item["name"], item["abbreviation"]), html.Strong(item["abbreviation"]),
                         html.Small(item["name"])], className="matchup-team")
    return html.Div([team("away"), html.Div([html.Strong(format_eastern(game["start"]) if not game.get("time_tbd") else
                                                       format_eastern(game["start"])[:10] + " EDT"),
                                           html.Span("Time TBD") if game.get("time_tbd") else None,
                                           html.Small("NFL · FanDuel")], className="matchup-start"),
                     team("home")], className="matchup-banner")


def selection_text(row):
    subject, selection, line = row["subject"], row["selection"], row["line"]
    key = row["market_key"]
    if key in MARKET_NAMES:
        unit = MARKET_NAMES[key][1]
        if line is not None and selection == "Over" and line >= 0:
            return f"{subject} {math.floor(line) + 1}+ {unit}"
        if line is not None:
            return f"{subject} {selection} {line:g} {unit}"
        return f"{subject} {selection} {unit}"
    if key == "alternate_spreads":
        return f"{subject} {line:+g}" if line is not None else subject
    if key == "alternate_totals":
        return f"{selection} {line:g} Points" if line is not None else selection
    suffix = {"player_anytime_td": "Anytime TD", "player_1st_td": "First TD", "player_last_td": "Last TD"}.get(key, "")
    return f"{subject} · {suffix}" + (" · No" if selection == "No" else "")


def _market_probability(row, forecast):
    if forecast is None or row["line"] is None:
        return None
    if row["market_key"] == "alternate_totals":
        return forecast.total(row["line"], row["selection"])
    if row["market_key"] == "alternate_spreads":
        team = NFL_TEAM_CODES.get(row["subject"])
        return forecast.spread(team, row["line"])
    return None


def probability_text(value):
    return f"{100 * value:.2f}%" if value is not None else "—"


def criteria_match(probability, edge, criteria):
    if probability is None or edge is None or not isinstance(criteria, dict):
        return False
    thresholds = (criteria.get("probability"), criteria.get("edge"))
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in thresholds):
        return False
    # Avoid treating floating-point subtraction at an exact threshold as higher.
    return all(value > threshold and not math.isclose(value, threshold, rel_tol=0, abs_tol=1e-12)
               for value, threshold in zip((probability, edge), thresholds))


def _line_values(row, forecast):
    try:
        implied = implied_probability(row["odds"])
    except PPPError:
        implied = None
    try:
        estimate = _market_probability(row, forecast)
    except PPPError:
        estimate = None
    probability = estimate.probability if estimate else None
    edge = probability - implied if probability is not None and implied is not None else None
    return implied, estimate, edge


def _comparison_figure(snapshot, forecast, market_key, group, method="PPP"):
    spreads = market_key == "alternate_spreads"
    selected = sorted([row for row in snapshot["rows"] if row["market_key"] == market_key and row["line"] is not None
                       and (row["subject"] if spreads else row["selection"]) == group], key=lambda row: row["line"])
    if not selected:
        return None
    lines = [row["line"] for row in selected]
    city = group.rsplit(" ", 1)[0] if spreads else group
    labels = [f"{city} {'-' if line < 0 else '+'} {abs(line):g}" if spreads else f"{group} {line:g}" for line in lines]
    figure = go.Figure()
    if forecast:
        probabilities = [(forecast.spread(NFL_TEAM_CODES[group], line) if spreads else
                          forecast.total(line, group)).probability for line in lines]
        figure.add_trace(go.Scatter(x=lines, y=probabilities, name=f"{method} probability", mode="lines+markers",
                                   line={"color": "#57dbc4", "width": 2.5}, marker={"size": 4},
                                   fill="tozeroy", fillcolor="rgba(87, 219, 196, 0.16)", connectgaps=False,
                                   customdata=labels,
                                   hovertemplate=f"%{{customdata}}<br>{method} %{{y:.2%}}<extra>%{{fullData.name}}</extra>"))
    probabilities = []
    for row in selected:
        try:
            probabilities.append(implied_probability(row["odds"]))
        except PPPError:
            probabilities.append(None)
    figure.add_trace(go.Scatter(x=lines, y=probabilities, name="Market implied probability", mode="lines+markers",
                               line={"color": "#8197ff", "width": 2.5}, marker={"symbol": "diamond", "size": 5},
                               fill="tozeroy", fillcolor="rgba(129, 151, 255, 0.16)", connectgaps=False,
                               customdata=[[label, f"{row['odds']:+g}" if row["odds"] is not None else "Unreported"]
                                           for label, row in zip(labels, selected)],
                               hovertemplate="%{customdata[0]}<br>Market %{y:.2%}<br>Odds %{customdata[1]}<extra>%{fullData.name}</extra>"))
    figure.update_layout(template="plotly_dark", paper_bgcolor="#111d2e", plot_bgcolor="#111d2e", height=420,
                         font={"family": "Segoe UI, sans-serif", "color": "#b8c8dc"},
                         margin={"l": 55, "r": 20, "t": 65, "b": 115},
                         legend={"orientation": "h", "y": 1.18, "font": {"size": 11}}, hovermode="x unified")
    ticks = list(range(0, len(lines), max(1, math.ceil(len(lines) / 6))))
    if ticks[-1] != len(lines) - 1:
        ticks.append(len(lines) - 1)
    figure.update_xaxes(title="Team handicap" if spreads else "Game total line (points)", gridcolor="#223047",
                        tickmode="array", tickvals=[lines[index] for index in ticks], ticktext=[labels[index] for index in ticks],
                        tickangle=-35, automargin=True)
    figure.update_yaxes(title="Win probability, excluding pushes", tickformat=".0%", range=[0, 1], gridcolor="#223047")
    if not forecast:
        figure.add_annotation(text=f"{method} estimate unavailable; market probabilities shown", x=.5, y=1.02,
                              xref="paper", yref="paper", showarrow=False)
    return figure


def ppp_comparison(snapshot, forecast=None, error=None, method="PPP"):
    details = forecast.diagnostics if forecast else {}
    if forecast:
        last_date = date.fromisoformat(details["last_game_date"]).strftime("%m/%d/%Y") if details.get("last_game_date") else "—"
        status = (f"{method} forecast: {forecast.away_team} {forecast.away_mean:.1f} · {forecast.home_team} {forecast.home_mean:.1f} · "
                  f"Total {forecast.total_mean:.1f} · Home margin {forecast.margin_mean:+.1f}. "
                  f"{details.get('training_games', '—')} training games; {details.get('validation_games', '—')} held-out games. "
                  f"As of {format_eastern(details['as_of'])}. Last training game {last_date}.")
        if details.get("snapshot_after_kickoff"):
            status += " Pregame estimate; this market snapshot was captured after kickoff."
        if details.get("source_notices"):
            status += " " + " ".join(details["source_notices"])
    else:
        status = error or f"{method} estimate unavailable."
    methodology = ("The EPA model uses the PDF's fitted scoring bridge and normal score-error baseline; key-number probabilities are approximate. "
                   "No drive-state simulator, injury or weather adjustment is applied.") if method == "EPA" else (
                   "The PPP model uses the PDF's normal baseline with historical forecast errors; key-number probabilities are approximate. No injury or weather adjustment is applied.")
    children = [html.Div(status, className="load-status ppp-status", role="status"),
                html.P(f"Market probabilities include the bookmaker margin. {method} probabilities and fair odds exclude pushes; hover a model value to see its win, loss and push probabilities. "
                       + methodology,
                       className="ppp-comparison-note")]
    charts = []
    for key, group, title in [
            ("alternate_spreads", snapshot["home_team"], "Spread probabilities by handicap"),
            ("alternate_spreads", snapshot["away_team"], "Spread probabilities by handicap"),
            ("alternate_totals", "Over", "Total points probabilities by line"),
            ("alternate_totals", "Under", "Total points probabilities by line")]:
        figure = _comparison_figure(snapshot, forecast, key, group, method)
        if figure:
            charts.append(html.Section([html.Div(html.H2(f"{title} · {group}"), className="panel-heading"),
                                        dcc.Graph(figure=figure, config={"displaylogo": False})], className="panel ppp-chart"))
    children.append(html.Div(charts, className="ppp-comparison-grid"))
    children.append(html.P("Curves show probabilities for each line, rather than probabilities that sum across alternate lines. "
                           "Market lines connect quotes from the selected saved snapshot; estimates never trigger an odds pull.", className="ppp-comparison-note"))
    return html.Div(children, className="ppp-comparison")


def market_sections(snapshot, category, forecast=None, model_error=None, criteria=None, method="PPP"):
    groups = OrderedDict()
    for row in snapshot["rows"]:
        if row["group"] != category:
            continue
        player = row["subject"] if row["market_key"] in MARKET_NAMES else ""
        groups.setdefault((row["market_key"], player), []).append(row)
    sections = []
    for index, ((key, player), rows) in enumerate(groups.items()):
        title = f"{player} - {MARKET_NAMES[key][0]}" if player else rows[0]["market"]
        rows = sorted(rows, key=lambda row: (row["subject"].casefold() if not player else "",
                                            0 if row["selection"] == "Over" else 1,
                                            row["line"] if row["line"] is not None else 0))
        comparison = key in {"alternate_spreads", "alternate_totals"}
        values = {id(row): _line_values(row, forecast) for row in rows} if comparison else {}
        matches = {identity: criteria_match(estimate.probability if estimate else None, edge, criteria)
                   for identity, (_, estimate, edge) in values.items()}
        match_count = sum(matches.values())
        def outcome(row):
            initials = "".join(part[0] for part in row["subject"].split()[:2])
            price = f"{row['odds']:+g}" if row["odds"] is not None else "—"
            if comparison:
                implied, estimate, edge = values[id(row)]
                detail = (f"Win {probability_text(estimate.win)} · Loss {probability_text(estimate.loss)} · "
                          f"Push {probability_text(estimate.push)}; refunds excluded from fair-price probability") if estimate else model_error or f"{method} unavailable"
                fair = f"{estimate.odds:+.0f}" if estimate and estimate.odds is not None else "—"
                return html.Div([
                    html.Span(selection_text(row), className="market-selection"),
                    html.Span(price, className="market-price", title="American odds from this saved snapshot"),
                    html.Span(probability_text(implied), className="market-probability", title="Raw break-even probability; includes bookmaker margin"),
                    html.Span(probability_text(estimate.probability) if estimate else "—", className="ppp-probability", title=detail),
                    html.Span(fair, className="ppp-fair-odds", title=detail),
                    html.Span(f"{100 * edge:+.2f} pp" if edge is not None else "—", className="market-edge",
                              title=f"{method} probability minus market implied probability, in percentage points"),
                ], className="market-outcome market-line-comparison" + (" market-criteria-match" if matches[id(row)] else ""))
            return html.Div([
                html.Span(initials, className="player-marker", **{"aria-hidden": "true"}),
                html.Span(selection_text(row), className="market-selection"),
                html.Span(price, className="market-price", title=f"American odds · {row['selection']} {row['line'] if row['line'] is not None else ''} · Updated {format_eastern(row['updated']) if row['updated'] != 'Not reported' else row['updated']}"),
            ], className="market-outcome")
        sections.append(html.Details([
            html.Summary([html.Strong(title),
                          html.Span(f"{match_count} matches", className="criteria-match-count") if match_count else None,
                          html.Span("⌄", className="market-chevron", **{"aria-hidden": "true"})]),
            html.Div([html.Span(label) for label in ["Selection", "Market odds", "Market implied", f"{method} probability", f"{method} fair odds", "Edge"]],
                     className="market-comparison-heading market-line-comparison") if comparison else None,
            html.Div([outcome(row) for row in rows[:6]], className="market-outcomes"),
            html.Details([html.Summary(f"Show more · {len(rows) - 6} selections"),
                          html.Div([outcome(row) for row in rows[6:]])], className="more-market-lines",
                         open=any(matches.get(id(row), False) for row in rows[6:])) if len(rows) > 6 else None,
        ], open=index < 2 or bool(match_count), className="sportsbook-market" + (" market-criteria-group" if match_count else "")))
    if category == "Game" and any(row["market_key"] in {"alternate_spreads", "alternate_totals"} for row in snapshot["rows"]):
        sections.append(ppp_comparison(snapshot, forecast, model_error, method))
    return sections or [html.P("No saved selections in this category.", className="saved-market-empty")]
