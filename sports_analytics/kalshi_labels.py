"""Sportsbook selection names without changing binary contracts or quote sides."""
import re

from .kalshi_analysis import threshold,player_subject,NBA_STATS,FOOTBALL_STATS


def selection(row, side, snapshot=None):
    """Return (label, ladder group, numeric line) for one actual contract side."""
    snapshot = snapshot or {}
    sport = snapshot.get('sport', '')
    family = row['event_ticker'].split('-', 1)[0].removeprefix('KX' + sport)
    yes = row.get('yes_label') or row['title']
    no = row.get('no_label') or ''
    title = row.get('title', '')
    # Ranges and scalar payoffs cannot be represented as two spread/total bets.
    strike = threshold(row) if row.get('cap_strike') is None and row.get('market_type') in {None, 'binary'} else None
    if family.endswith('SPREAD') and strike:
        from .kalshi_view import matchup, team_identity
        match = re.search(r'^(.+?)\s+(?:wins?|win)\s+by\b', yes, re.I) or re.search(
            r'^(.+?)\s+(?:wins?|win)\s+by\b', title, re.I)
        game = snapshot.get('game')
        teams = matchup(game) if game else None
        if match and teams:
            team = team_identity(match[1].strip(), sport)['name']
            names = [item['name'] for item in teams[0]]
            if team in names:
                other = next((name for name in names if name != team), None)
                if other:
                    line, direction = strike
                    favorite = (side == 'yes') == (direction == 'Over')
                    name, handicap = (team, -line) if favorite else (other, line)
                    return f'{name} {handicap:+g}', name, handicap
    if family.endswith('GAME'):
        if yes.casefold() != 'yes' and no.casefold() not in {'', 'no', yes.casefold()}:
            label = yes if side == 'yes' else no
            return label, label, 0
        from .kalshi_view import matchup, team_identity
        game = snapshot.get('game')
        teams = matchup(game) if game else None
        if teams:
            team = team_identity(yes, sport)['name']
            names = [item['name'] for item in teams[0]]
            other = next((name for name in names if name != team), None)
            if team in names and other:
                label = team if side == 'yes' else other
                return label, label, 0
    if strike and not family.endswith(('GAME', 'SPREAD')):
        line, direction = strike
        direction = direction if side == 'yes' else ('Under' if direction == 'Over' else 'Over')
        event = row.get('event_title', '')
        # Keep player/team/period context when the provider puts it in the title.
        subject = event.split(':', 1)[0].strip() if ':' in event else ''
        if family in (NBA_STATS if sport=='NBA' else FOOTBALL_STATS):subject=player_subject(row) or ''
        if re.search(r'\s(?:at|vs\.?|versus|@)\s', subject, re.I):
            subject = ''
        if family.endswith('TEAMTOTAL') and not subject:
            match = re.match(r'^(.+?)\s+(?:over|under|scores?)\b', yes, re.I)
            if match:subject = match[1].strip()
        context = (subject + ' ') if subject else ''
        return f'{context}{direction} {line:g}', context + direction, line
    # Preserve unusual settlement wording; never invent an opposite team/range.
    if side == 'yes':
        label = yes if yes.casefold() != 'yes' else title
    elif no.casefold() not in {'', 'no', yes.casefold()}:
        label = no
    else:
        label = 'Not: ' + (yes if yes.casefold() != 'yes' else title)
    return label, label, row.get('line') or 0


def selection_rows(rows, snapshot, provider='Kalshi'):
    """Group all unders, overs, or team handicaps into separate sorted ladders."""
    formatter = selection
    if provider == 'Polymarket':
        from .polymarket_labels import selection as formatter
    selections = []
    for row in rows:
        for side in ('yes', 'no'):
            label, group, line = formatter(row, side, snapshot)
            selections.append({**row, '_display_side': side, '_display_label': label,
                               '_display_group': group, '_display_line': line})
    return sorted(selections, key=lambda row: (0 if row['_display_group'].endswith('Under') else
                  1 if row['_display_group'].endswith('Over') else 2, row['_display_group'].casefold(),
                  row['_display_line'], row['ticker'], row['_display_side']))
