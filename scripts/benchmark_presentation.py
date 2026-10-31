"""Offline presentation baseline; no API requests or model fitting.

Run before/after with --output and use --compare to verify analytical signatures.
Timings measure Python construction/serialization, not browser rendering.
"""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
import tracemalloc
from unittest.mock import patch
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash._utils import to_json
from sports_analytics.dashboard import create_app
from sports_analytics.backtesting_results import render
from sports_analytics.in_game_view import court_figure, football_figure
from scripts.check_backtesting import saved_game_report


def signature(value):
    return hashlib.sha256(to_json(value).encode()).hexdigest()


def analytical(value):
    """Discard appearance only; retain geometry, data, axes and hover definitions."""
    cosmetic = {'template', 'font', 'titlefont', 'tickfont', 'color', 'colors',
                'colorscale', 'colorway', 'fillcolor', 'gridcolor', 'zerolinecolor',
                'linecolor', 'paper_bgcolor', 'plot_bgcolor', 'bgcolor', 'bordercolor',
                'height', 'margin', 'legend', 'hoverlabel', 'autosize', 'automargin'}
    if isinstance(value, dict):
        return {k: analytical(v) for k, v in value.items() if k not in cosmetic}
    if isinstance(value, list):
        return [analytical(v) for v in value]
    return value


def charts(tree):
    if isinstance(tree, dict):
        if tree.get('type') == 'Graph':
            yield tree['props']['figure']
        for v in tree.values():
            yield from charts(v)
    elif isinstance(tree, list):
        for v in tree:
            yield from charts(v)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    parser.add_argument('--repeats', type=int, default=15)
    parser.add_argument('--native-reference', action='store_true', help='Interleave the native dark theme to isolate theme overhead')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    report = {'browser_measured': False, 'network': 'blocked', 'repeats': args.repeats}
    with patch('requests.sessions.Session.request', side_effect=AssertionError('No remote requests')):
        app = create_app()
        layout = to_json(app.layout)
        callbacks = [{k: v for k, v in c.items() if k != 'callback'} for c in app._callback_list]
        report['callbacks'] = {'count': len(callbacks), 'signature': signature(callbacks)}
        report['layout'] = {'bytes': len(layout.encode()), 'graphs': len(list(charts(json.loads(layout))))}
        cases = {'game_margin': lambda: render(saved_game_report(), 'margin'),
                 'game_total': lambda: render(saved_game_report(), 'total'),
                 'court': lambda: court_figure([]), 'field': lambda: football_figure([])}
        player = saved_game_report()
        player['context'].update(group='player-stats', market='player_points')
        player['records'] = [dict(r, actual=12., predicted=11., baseline=10., lower=8., upper=16., interval_level=.8)
                             for r in player['records']]
        cases['player'] = lambda: render(player)
        report['cases'] = {}
        for name, build in cases.items():
            build()  # warm import/template caches before comparable samples
            samples = []
            for _ in range(args.repeats):
                start = perf_counter()
                encoded = to_json(build())
                samples.append((perf_counter() - start) * 1000)
            tree = json.loads(encoded)
            figures = [tree] if name in {'court', 'field'} else list(charts(tree))
            # Table values/definitions are also part of the unchanged-input baseline.
            def tables(node):
                if isinstance(node, dict):
                    if node.get('type') == 'DataTable':
                        yield {k: node['props'].get(k) for k in ('data', 'columns', 'page_size', 'sort_action', 'filter_action')}
                    for v in node.values(): yield from tables(v)
                elif isinstance(node, list):
                    for v in node: yield from tables(v)
            report['cases'][name] = {'median_ms': round(median(samples), 3), 'bytes': len(encoded.encode()),
                                     'signature': signature([analytical(figures), list(tables(tree))])}
        if args.native_reference:
            # Same source/fixtures and current tables, changing only the theme.
            # Interleave to reduce distortion from changing local CPU conditions.
            def native(**overrides):
                return dict(template='plotly_dark', paper_bgcolor='#111d2e', plot_bgcolor='#111d2e',
                            font=dict(family='Segoe UI, Arial, sans-serif', size=12, color='#b8c8dc')) | overrides
            names = ['dashboard', 'backtesting_results', 'market_view', 'in_game_view']
            samples = {key: [] for key in ('native', 'terminal')}
            for index in range(args.repeats):
                for key in (('native','terminal') if index % 2 else ('terminal','native')):
                    with ExitStack() as stack:
                        if key == 'native':
                            for module in names: stack.enter_context(patch(f'sports_analytics.{module}.chart_style', native))
                        start = perf_counter(); to_json(cases['game_margin']())
                        samples[key].append((perf_counter() - start) * 1000)
            report['same_run_theme_reference'] = {k: {'median_ms': round(median(v), 3)} for k, v in samples.items()}
        tracemalloc.start()
        try:
            to_json(cases['game_margin']())
            report['game_render_python_peak_bytes'] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    if args.compare:
        before = json.loads(args.compare.read_text(encoding='utf-8-sig'))
        assert before['callbacks'] == report['callbacks'], 'Callback graph changed'
        assert before['layout']['graphs'] == report['layout']['graphs'], 'Extra mounted graphs'
        assert report['layout']['bytes'] <= before['layout']['bytes'] * 1.05, 'Initial layout grew over 5%'
        for name, result in report['cases'].items():
            assert result['signature'] == before['cases'][name]['signature'], f'Analytical data changed: {name}'
        report['comparison'] = 'unchanged callback graph, mounted graph count, analytical chart and table signatures'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
