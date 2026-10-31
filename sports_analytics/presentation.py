"""Presentation defaults only; never transform analytical values here.

Palette values mirror the CSS tokens in assets/style.css (checked offline).
Callers retain their own dimensions, axes, labels, and interaction identities.
"""
import plotly.graph_objects as go
import plotly.io as pio

PALETTE = dict(bg='#0b1422', panel='#111d2e', raised='#17253a', line='#33465f',
               text='#e7edf6', muted='#a8b8cc', accent='#57dbc4', probability='#75baff',
               positive='#7ddeac', negative='#ffa3af', warning='#f6c667', comparison='#b7b6ff')
FONT = 'Segoe UI, Arial, sans-serif'

# Build once. Retain the native template's trace defaults; only style is changed.
TEMPLATE = go.layout.Template(pio.templates['plotly_dark'])
TEMPLATE.layout.update(
    paper_bgcolor=PALETTE['panel'], plot_bgcolor=PALETTE['panel'],
    font=dict(family=FONT, size=12, color=PALETTE['muted']),
    colorway=[PALETTE[k] for k in ('probability', 'comparison', 'accent', 'warning', 'negative', 'positive')],
    hoverlabel=dict(bgcolor=PALETTE['raised'], bordercolor=PALETTE['line'],
                    font=dict(family=FONT, size=13, color=PALETTE['text'])),
    xaxis=dict(gridcolor='#243249'), yaxis=dict(gridcolor='#243249'))
pio.templates['sports_terminal'] = TEMPLATE


def chart_style(**overrides):
    return dict(template='sports_terminal', paper_bgcolor=PALETTE['panel'], plot_bgcolor=PALETTE['panel'],
                font=dict(family=FONT, size=12, color=PALETTE['muted']), autosize=True) | overrides


def table_cell(**overrides):
    return dict(backgroundColor=PALETTE['panel'], color=PALETTE['text'],
                border='none', borderBottom=f"1px solid {PALETTE['line']}",
                padding='12px', textAlign='left', fontFamily=FONT, fontSize='13px',
                fontVariantNumeric='tabular-nums') | overrides


def table_header(**overrides):
    return dict(backgroundColor=PALETTE['raised'], color=PALETTE['text'],
                fontWeight='600') | overrides


def table_filter(**overrides):
    return dict(backgroundColor=PALETTE['raised'], color=PALETTE['text']) | overrides


def signed_state(value):
    """Color supplements the existing signed number; missing values stay neutral."""
    return 'positive' if value is not None and value > 0 else 'negative' if value is not None and value < 0 else 'neutral'
