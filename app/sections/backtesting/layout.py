"""Backtesting and research workspace."""

from dash import html
from app.sections.backtesting.backtesting_summary import layout as backtest_summary_layout
from app.sections.backtesting.performance_summary import layout as performance_layout
from app.sections.backtesting.backtesting_results import layout as backtesting_results_layout
from app.sections.research.comparison_view import layout as comparison_layout
from app.sections.research.layout import research_setup


def layout():
    return html.Section([html.Div(id='backtesting-content'),backtest_summary_layout(),performance_layout(),
        research_setup(),
        comparison_layout(),
        backtesting_results_layout()],
                 id='backtesting-page',className='backtesting-page',style={'display':'none'})
