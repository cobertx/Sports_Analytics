"""Odds API credit usage callbacks."""

import csv
from dash import Input, Output, ctx, html
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
from sports_analytics.data import DataSourceError
from sports_analytics.odds_api import OddsApiService
from sports_analytics.nfl_markets import NflMarketService
from app.shell.navigation import scoped_callback
from sports_analytics.display_time import format_eastern
from sports_analytics.usage_log import SPORTS as USAGE_SPORTS, usage_summary
from app.components.charts import style_figure
from app.components.charts import empty_figure
from app.components.controls import kpi


def credit_figure(usage):
    if not usage.total:
        return empty_figure("Connected · The API reports a zero credit allocation")
    figure = go.Figure(go.Pie(labels=["Used", "Unused"], values=[usage.used, usage.remaining],
                             hole=0.73, sort=False, direction="clockwise", textinfo="none",
                             marker={"colors": ["#f6c667", "#57dbc4"], "line": {"color": "#111d2e", "width": 3}},
                             hovertemplate="%{label}: %{value:,.0f} credits (%{percent})<extra></extra>"))
    figure = style_figure(figure)
    figure.update_layout(height=370, margin={"l": 24, "r": 24, "t": 24, "b": 45}, hovermode="closest",
                         legend={"orientation": "h", "x": 0.5, "xanchor": "center", "y": -0.05},
                         annotations=[{"text": f"<b>{100 * usage.used / usage.total:.1f}%</b><br>used",
                                       "x": 0.5, "y": 0.5, "showarrow": False, "font": {"size": 23, "color": "#e7edf6"}}])
    return figure

def usage_figures(summary):
    colors = ["#57dbc4", "#8197ff", "#f6c667"]
    values = [summary["monthly"][sport]["credits"] for sport in USAGE_SPORTS]
    composition = empty_figure("No reported credit usage logged this month")
    if sum(values):
        composition = style_figure(go.Figure(go.Pie(labels=list(USAGE_SPORTS), values=values,
                                                   sort=False, textinfo="percent+label", marker={"colors": colors},
                                                   hovertemplate="%{label}: %{value:,.0f} credits (%{percent})<extra></extra>")))
        composition.update_layout(hovermode="closest", height=370, margin={"l": 24, "r": 24, "t": 24, "b": 45},
                                  legend={"orientation": "h", "x": .5, "xanchor": "center", "y": -.05})
    daily = empty_figure("Pull live markets to start your daily usage history")
    if summary["daily"]:
        daily = go.Figure()
        dates = [row["date"] for row in summary["daily"]]
        totals = [sum(row[sport] for sport in USAGE_SPORTS) for row in summary["daily"]]
        daily.add_trace(go.Scatter(x=dates, y=totals, name="Total", mode="lines+markers",
                                   line={"color": "#e7edf6", "width": 3},
                                   hovertemplate="%{x|%m/%d/%Y}<br>%{y:,.0f} credits<extra>Total</extra>"))
        for sport, color in zip(USAGE_SPORTS, colors):
            daily.add_trace(go.Scatter(x=dates, y=[row[sport] for row in summary["daily"]],
                                       name=sport, mode="lines+markers", visible="legendonly", line={"color": color, "width": 2},
                                       hovertemplate="%{x|%m/%d/%Y}<br>%{y:,.0f} credits<extra>%{fullData.name}</extra>"))
        daily = style_figure(daily)
        daily.update_xaxes(title="Date (EDT)", type="date", tickformat="%m/%d/%Y")
        daily.update_yaxes(title="Credits used", rangemode="tozero", dtick=1 if max(totals) < 5 else None)
    return composition, daily


def register(app, odds_service, api_usage_log, market_services):
    @scoped_callback(app,'odds',Output("odds-composition", "figure"), Output("odds-sport-summary", "children"),
                  Output("odds-history-chart", "figure"), Output("odds-history-table", "data"),
                  Output("odds-log-status", "children"),
                  Input("sport", "value"), Input("odds-refresh", "n_clicks"), Input("odds-timer", "n_intervals"),
                  Input("market-refresh-signal", "data"))
    def logged_usage(sport, _refresh, _timer, _market_pull):
        if sport != "ODDS_API":
            raise PreventUpdate
        try:
            account = odds_service.account_id()
            if not isinstance(account, str):
                raise DataSourceError("Connect your account to view its recorded credit history.")
            # Import older saved pulls once; reading stored snapshots never requests odds.
            for markets in market_services.values():
                if isinstance(markets, NflMarketService):
                    markets.export_existing()
            labels = odds_service.account_labels() if isinstance(odds_service,OddsApiService) else {account:'Primary'}
            summary = usage_summary(api_usage_log.records(list(labels)), account_labels=labels)
        except (DataSourceError, OSError, ValueError, csv.Error) as exc:
            return (empty_figure("Credit history unavailable"),
                    [kpi(sport + " CREDITS THIS MONTH", "—", "History unavailable") for sport in USAGE_SPORTS],
                    empty_figure("Credit history unavailable"), [], f"Credit history unavailable. {exc}")
        composition, daily = usage_figures(summary)
        metrics = []
        for sport in USAGE_SPORTS:
            item = summary["monthly"][sport]
            detail = f"{summary['month']} · {item['pulls']:,} logged requests"
            if item["unknown"]:
                detail += f" · {item['unknown']:,} unreported charges"
            metrics.append(kpi(sport + " CREDITS THIS MONTH", f"{item['credits']:,}", detail))
        note = (f"{summary['month']} (EDT) · {len(summary['history']):,} recorded requests · "
                "Saved in data/API Log/api_credit_usage.csv. Totals include reported charges only.")
        if summary["unknown"]:
            note += f" {summary['unknown']:,} requests have unreported charges."
        return composition, metrics, daily, summary["history"], note

    @scoped_callback(app,'odds',Output("odds-usage", "figure"), Output("odds-summary", "children"), Output("odds-status", "children"),
                  Input("sport", "value"), Input("odds-refresh", "n_clicks"), Input("odds-timer", "n_intervals"),
                  running=[(Output("odds-refresh", "disabled"), True, False)])
    def odds_usage(sport, _refresh, _timer):
        if sport != "ODDS_API":
            raise PreventUpdate
        try:
            usage = odds_service.usage(refresh=ctx.triggered_id == "odds-refresh")
        except DataSourceError as exc:
            return empty_figure("Connect to The Odds API to view monthly credits"), [
                kpi("CREDITS USED", "—", "Awaiting connection"), kpi("CREDITS UNUSED", "—", "Awaiting connection"),
                kpi("TOTAL CREDITS", "—", "Awaiting connection")], str(exc)
        status = ("Saved usage snapshot" if usage.stale else "Connected to The Odds API") + f" · Last checked {format_eastern(usage.checked_at)}."
        if usage.notice:
            status += " " + usage.notice
        metrics = [
            kpi("CREDITS USED", f"{usage.used:,}", "Current quota cycle"),
            kpi("CREDITS UNUSED", f"{usage.remaining:,}", "Remaining before quota resets"),
            kpi("TOTAL CREDITS", f"{usage.total:,}", "Used + unused credits")]
        if usage.accounts:
            def account_row(account):
                saved=account.usage
                state=('Exhausted' if account.exhausted else 'Preferred for requests' if account.active else 'Standby')
                if saved is None:state+=' · Usage unavailable'
                elif saved.stale:state+=' · Saved / refresh failed'
                return html.Tr([html.Td(account.label),
                    *[html.Td(f'{value:,}' if saved else '—') for value in
                      ((saved.used,saved.remaining,saved.total) if saved else (None,None,None))],
                    html.Td([state,html.Small(' '+(account.notice or (saved.notice if saved else '')))]),
                    html.Td(format_eastern(saved.checked_at) if saved else '—')])
            metrics.append(html.Div([
                html.H3('API connections'),
                html.Table([html.Thead(html.Tr([html.Th(label) for label in ['Connection','Used','Unused','Total','Status','Last checked']])),
                            html.Tbody([account_row(a) for a in usage.accounts])],className='mispricing-table'),
                html.P('Combined totals sum the reported counters from these connections. Each subscription may have a different reset date.',className='chart-note'),
            ],className='panel mispricing-table-scroll',style={'gridColumn':'1 / -1'}))
        return credit_figure(usage), metrics, status
