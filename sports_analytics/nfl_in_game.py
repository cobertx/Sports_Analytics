"""Drive navigation and recorded personnel/coverage percentages for NFL games."""
from collections import Counter
import re

from dash import html

ALL = '__all__'


def drive_key(event):
    return f"{event['team']}:{event['drive']}"


def drives(events):
    groups = {}
    for event in sorted(events, key=lambda r: r['order']):
        if not event.get('drive'):
            continue
        groups.setdefault(drive_key(event), []).append(event)
    return groups


def drive_label(rows):
    first = rows[0]
    result = next((r['drive_result'] for r in reversed(rows) if r.get('drive_result')), 'Result unavailable')
    period = first.get('period')
    quarter = f'Q{period}' if period and period <= 4 else f'OT{period - 4}' if period else '?'
    defense = f" vs {first['defense']}" if first.get('defense') else ''
    return f"Drive {first['drive']} · {first['team']} offense{defense} · {quarter} {first['clock']} · {result}"


def drive_navigation(events, selected):
    grouped = drives(events)
    if not grouped:
        return html.P('No recorded drives for this game.', className='in-game-help')
    teams = list(dict.fromkeys(rows[0]['team'] for rows in grouped.values()))
    columns = []
    for team in teams:
        buttons = []
        for key, rows in grouped.items():
            if rows[0]['team'] != team:
                continue
            yards = [r['yards'] for r in rows if r.get('yards') is not None]
            result = next((r['drive_result'] for r in reversed(rows) if r.get('drive_result')), 'Result unavailable')
            detail = f"{len(rows)} offensive plays · {sum(yards):g} net yd" if yards else f'{len(rows)} offensive plays · Yards unavailable'
            buttons.append(html.Button([
                html.Strong(f"Drive {rows[0]['drive']} · {result}"), html.Span(detail),
                html.Small(drive_label(rows).split(' · ', 2)[-1])],
                id={'type': 'in-game-drive-button', 'drive': key}, n_clicks=0, title=drive_label(rows),
                className='nfl-drive-button' + (' active' if selected == key else ''),
                **{'aria-pressed': 'true' if selected == key else 'false'}))
        columns.append(html.Div([html.H3(team + ' offensive drives'), *buttons], className='nfl-drive-column'))
    return [html.Div([html.H3('Drives from both sides'),
        html.Button('Show all drives', id={'type': 'in-game-drive-button', 'drive': ALL}, n_clicks=0,
            className='export-button')], className='in-game-heading'),
        html.P('Choose a drive from either team to focus the field and usage breakdowns. '
               'Play counts and net yards include the recorded offensive plays; kicks and no-play penalties are excluded.', className='in-game-help'),
        html.Div(columns, className='nfl-drive-grid')]


def distribution(events, field):
    """Share of every eligible play, including an explicit Unknown category."""
    def label(value):
        if value is None or str(value).strip().lower() in {'', 'na', 'nan', 'none', 'null'}:
            return 'Unknown'
        if isinstance(value, bool):
            return 'Yes' if value else 'No'
        if isinstance(value, (int, float)):
            return f'{value:g}'
        value = str(value).replace('_', ' ').strip().upper()
        if field == 'coverage_family':
            return {'MAN COVERAGE': 'MAN', 'ZONE COVERAGE': 'ZONE'}.get(value, value)
        if field in {'offense_personnel', 'defense_personnel'}:
            return personnel_label(value, field == 'offense_personnel')
        if field == 'qb_location':
            return {'S': 'SHOTGUN', 'U': 'UNDER CENTER', 'P': 'PISTOL'}.get(value, value)
        return value
    counts = Counter(label(r.get(field)) for r in events)
    total = len(events)
    return [dict(label=name, count=count, percent=100 * count / total) for name, count in
            sorted(counts.items(), key=lambda item: (item[0] == 'Unknown', -item[1], item[0]))]


def personnel_label(value, offense):
    positions = Counter()
    for part in value.split(','):
        match = re.fullmatch(r'\s*(\d+)\s+([A-Z]+)\s*', part)
        if not match:
            return value
        positions[match[2]] += int(match[1])
    if offense:
        backs = sum(positions[p] for p in ['RB', 'FB', 'HB'])
        te, wr = positions['TE'], positions['WR']
        line = sum(positions[p] for p in ['OL', 'T', 'G', 'C', 'OT', 'OG'])
        label = f'{backs} RB/FB, {te} TE, {wr} WR'
        if backs + te + wr == 5 and positions.get('QB', 1) == 1 and line in {0, 5}:
            return f'{backs}{te} personnel · ' + label
        return label + (f' · {line} OL' if line else '') + (f" · {positions['QB']} QB" if positions.get('QB', 1) != 1 else '')
    groups = [['DL', 'DE', 'DT', 'NT'], ['LB', 'ILB', 'MLB', 'OLB'], ['DB', 'CB', 'FS', 'SS', 'S']]
    if set(positions) - {p for group in groups for p in group}:
        return value
    return ' · '.join(f'{sum(positions[p] for p in group)} {group[0]}' for group in groups)


def usage_card(title, events, field, note):
    values = distribution(events, field)
    known = sum(v['count'] for v in values if v['label'] != 'Unknown')
    rows = [html.Div([
        html.Div([html.Span(v['label']), html.Strong(f"{v['percent']:.1f}%"), html.Small(f"{v['count']} plays")],
                 className='nfl-usage-label'),
        html.Div(html.Div(style={'width': f"{v['percent']:.5f}%"}), className='nfl-usage-track')],
        className='nfl-usage-row' + (' unknown' if v['label'] == 'Unknown' else '')) for v in values]
    return html.Section([html.H3(title), html.P(f'{known}/{len(events)} eligible plays recorded. ' + note,
        className='in-game-help'), *(rows if known else [html.P('Charting unavailable for this selection.', className='nfl-charting-unavailable')]),
        *([] if known else rows)], className='nfl-usage-card')


def insights(events, data):
    if not events:
        return html.P('Select a drive or change the filters to see personnel and play-calling percentages.', className='in-game-help')
    calls = [r for r in events if r.get('kind') in {'run', 'pass'}]
    dropbacks = [r for r in calls if r.get('call') == 'Dropback']
    sides = []
    for team in dict.fromkeys(r['team'] for r in events):
        team_events = [r for r in events if r['team'] == team]
        team_calls = [r for r in calls if r['team'] == team]
        team_passes = [r for r in dropbacks if r['team'] == team]
        defense = next((r.get('defense') for r in team_events if r.get('defense')), 'Opponent')
        cards = [usage_card('Offensive personnel · ' + team, team_events, 'offense_personnel', 'Share of selected offensive plays; packages grouped by recorded position counts.'),
                 usage_card('Defensive personnel · ' + defense, team_events, 'defense_personnel', 'Share of plays faced; DL/LB/DB groups are personnel, not inferred formations.'),
                 usage_card('Run / dropback share · ' + team, team_calls, 'call', 'Passes, sacks and marked QB scrambles are dropbacks; kneels/spikes excluded.'),
                 usage_card('Offensive formation · ' + team, team_events, 'offense_formation', 'Recorded formation; personnel packages do not determine alignment.'),
                 usage_card('Man / zone · ' + defense, team_passes, 'coverage_family', 'Share of selected dropbacks faced, including uncharted plays.'),
                 usage_card('Coverage scheme · ' + defense, team_passes, 'coverage_scheme', 'Recorded coverages; no inference from personnel or play outcome.'),
                 usage_card('Defenders in box · ' + defense, team_events, 'defenders_in_box', 'Recorded pre-snap box count.'),
                 usage_card('Pass rushers · ' + defense, team_passes, 'pass_rushers', 'Number of rushers on selected dropbacks faced.'),
                 usage_card('Blitzers · ' + defense, team_passes, 'blitzers', 'Recorded blitzer count, not a proxy from total rushers.'),
                 usage_card('Play action · ' + team, team_passes, 'play_action', 'Share of selected dropbacks.'),
                 usage_card('RPO · ' + team, team_calls, 'rpo', 'Share of selected run/dropback plays.'),
                 usage_card('Defensive formation · ' + defense, team_events, 'defense_formation', 'Only explicitly charted alignment is shown; personnel alone is not a formation.')]
        sides.append(html.Div([html.H3(f'{team} offense vs {defense} defense'), html.Div(cards, className='nfl-usage-grid')]))
    info = data.get('charting', {})
    status = ' · '.join(f"{s['name']}: {s['matched']} game plays matched; saved {s['saved_at']}" for s in info.get('sources', []))
    return [html.P('Breakdowns follow the field filters and individual-play selection. Percentages use all eligible plays in each card, '
                   'including Unknown; uncharted plays are never treated as a recorded No. Personnel values list players by position '
                   '(for example, 1 RB, 1 TE, 3 WR). Defensive personnel are the opponent units on those plays.', className='in-game-help'),
            html.P(status or 'Load personnel & coverage to retrieve available participation and FTN charting. Base run/dropback data works without it.', className='in-game-help'),
            *[html.P(message, className='load-status') for message in info.get('notices', [])], *sides]
