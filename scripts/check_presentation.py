"""Offline style contracts and contrast checks; not a browser/visual audit."""
from pathlib import Path
import re
import sys
from unittest import TestCase, main

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash import dash_table
import plotly.graph_objects as go
from sports_analytics.presentation import PALETTE, chart_style, table_cell, table_header, table_filter

ASSETS = Path(__file__).resolve().parents[1] / 'sports_analytics' / 'assets'


def luminance(color):
    channels = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))


def contrast(a, b):
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + .05) / (dark + .05)


class PresentationChecks(TestCase):
    def test_semantic_text_contrast_on_each_surface_and_actions(self):
        for foreground in ('text','muted','accent','probability','positive','negative','warning','comparison'):
            for background in ('bg','panel','raised'):
                with self.subTest(foreground=foreground, background=background):
                    self.assertGreaterEqual(contrast(PALETTE[foreground], PALETTE[background]), 4.5)
        self.assertGreaterEqual(contrast(PALETTE['bg'], PALETTE['accent']), 4.5)
        self.assertGreaterEqual(contrast(PALETTE['positive'], '#10352f'), 4.5)

    def test_css_and_python_palette_stay_in_sync(self):
        css = (ASSETS / 'style.css').read_text(encoding='utf-8')
        tokens = dict(re.findall(r'--([\w-]+):\s*(#[\da-fA-F]{6})\s*[;}]', css))
        for name, color in PALETTE.items():
            self.assertEqual(tokens[name].lower(), color)

    def test_chart_style_preserves_statistical_and_interaction_properties(self):
        figure = go.Figure(go.Bar(x=[0,1,2], y=[.2,.3,.5], customdata=[[0],[1],[2]]))
        figure.update_layout(xaxis=dict(range=[-.5,2.5], tickvals=[0,1,2]),
                              yaxis=dict(range=[0,1], tickformat='.0%'),
                              uirevision='same-input', meta=dict(level=.8))
        before = figure.to_plotly_json()
        figure.update_layout(**chart_style())
        after = figure.to_plotly_json()
        self.assertEqual(after['data'], before['data'])
        for key in ('xaxis','yaxis','uirevision','meta'):
            self.assertEqual(after['layout'][key], before['layout'][key])
        self.assertEqual(sum(figure.data[0].y), 1)

    def test_native_table_validation_and_independent_style_overrides(self):
        table = dash_table.DataTable(data=[dict(price=-110)], columns=[dict(id='price',name='Odds')],
                                     style_cell=table_cell(minWidth='95px'),
                                     style_header=table_header(), style_filter=table_filter())
        self.assertEqual(table.data, [dict(price=-110)])
        self.assertEqual(table.style_cell['minWidth'], '95px')
        value = table_cell(); value['color'] = '#000000'
        self.assertEqual(table_cell()['color'], PALETTE['text'])
        self.assertEqual(table_cell(padding='16px')['padding'], '16px')

    def test_responsive_styles_do_not_own_routing_or_loading(self):
        css = (ASSETS / 'terminal.css').read_text(encoding='utf-8')
        self.assertIn('max-width: 767px', css)
        self.assertIn('min-width: 768px', css)
        self.assertIn('prefers-reduced-motion', css)
        self.assertIn('focus-visible', css)
        self.assertNotIn('display: none', css)
        self.assertNotIn('pointer-events: none', css)
        self.assertNotIn('animation:', css)
        # Sanity check, not a substitute for parsing/rendering in a real browser.
        for path in ASSETS.glob('*.css'):
            source = re.sub(r'/\*.*?\*/', '', path.read_text(encoding='utf-8'), flags=re.S)
            self.assertEqual(source.count('{'), source.count('}'), str(path))


if __name__ == '__main__':
    main()
