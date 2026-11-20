"""Shared dashboard chart styling."""

import plotly.graph_objects as go
from app.components.presentation import chart_style


def empty_figure(message="Load historical data to explore a team or player"):
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                          font={"color": "#9caec5", "size": 14})
    return style_figure(figure)


def style_figure(figure):
    figure.update_layout(**chart_style(),
                         margin={"l": 45, "r": 25, "t": 20, "b": 45}, height=350,
                         legend={"orientation": "h", "y": 1.12}, hovermode="x unified")
    figure.update_xaxes(gridcolor="#223047", zeroline=False)
    figure.update_yaxes(gridcolor="#223047", zeroline=False)
    return figure
