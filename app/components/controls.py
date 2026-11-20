"""Shared dashboard form fields and KPI cards."""

from dash import html


def field(label, component, hint=None):
    return html.Div([html.Label(label, htmlFor=component.id), component,
                     html.Small(hint) if hint else None], className="field")

def kpi(label, value, detail):
    return html.Div([html.Span(label, className="eyebrow"), html.Strong(value),
                     html.Small(detail)], className="kpi")
