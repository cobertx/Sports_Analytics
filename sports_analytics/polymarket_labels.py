"""Sportsbook labels for Polymarket's explicitly ordered outcome tokens."""
import re

from .kalshi import numeric

NUMBER = r'[+-]?\d+(?:\.\d+)?'
SIGNED = re.compile(r'^(.+?)\s*\(([+-]\d+(?:\.\d+)?)\)$|^(.+?)\s+([+-]\d+(?:\.\d+)?)$')


def signed_team(value):
    match = SIGNED.fullmatch(re.sub(r'^Spread\s*:\s*', '', value.strip(), flags=re.I))
    return ((match[1] or match[3]).strip(), float(match[2] or match[4])) if match else None


def subject(title, kind):
    if kind.endswith('totals') and not kind.endswith('team_totals'):return ''
    value = title.split(':', 1)[0].strip() if ':' in title else ''
    if not value:
        stat = re.escape(kind.replace('_',' '))
        match = re.match(r'^(.+?)\s+'+stat+r'\b', title, re.I)
        if match and not re.search(r'\d|\b(?:over|under|will)\b', match[1], re.I):value = match[1].strip()
    return '' if re.search(r'\s(?:at|vs\.?|versus|@)\s', value, re.I) else value


def market_section(kind, question, category):
    title = section_title(kind.replace('_',' ').title())
    if category in {'Player Props','Passing Props','Receiving Props','Rushing Props','Touchdowns & Scoring'}:
        name = subject(question, kind)
        return name+' - '+title if name else question
    return title


def selection(row, side, snapshot=None):
    """Return label/group/line while preserving the provider's actual side order."""
    from .kalshi_view import team_identity, matchup
    snapshot = snapshot or {}
    sport = snapshot.get('sport', '')
    kind = str(row.get('sports_market_type') or '').lower()
    label = str(row.get(side+'_label') or side.title()).strip()
    title = row.get('title', '')
    line = numeric(row.get('line'))
    game = snapshot.get('game') or {}
    found = matchup(game) if game.get('sport') else None
    teams = [team['name'] for team in found[0]] if found else []
    name = team_identity(label, sport)['name']
    if kind == 'moneyline':
        return (name if name in teams else label), (name if name in teams else label), 0
    if kind.endswith('spreads'):
        explicit = signed_team(label)
        if explicit:
            team, handicap = explicit
            name = team_identity(team, sport)['name']
            return f'{name} {handicap:+g}', name, handicap
        primary = signed_team(title)
        if primary:
            team, handicap = primary
            primary_name = team_identity(team, sport)['name']
            # The signed team in the question owns this handicap. Outcome index
            # alone cannot establish whether a token is the favorite or underdog.
            if name in teams and primary_name in teams:
                handicap = handicap if name == primary_name else -handicap
                return f'{name} {handicap:+g}', name, handicap
            if label.casefold() in {'yes','no'} and primary_name in teams:
                other = next((team for team in teams if team != primary_name), None)
                if other:
                    name, handicap = (primary_name, handicap) if label.casefold() == 'yes' else (other, -handicap)
                    return f'{name} {handicap:+g}', name, handicap
    else:
        direction = re.fullmatch(r'(Over|Under)(?:\s+('+NUMBER+r'))?', label, re.I)
        if direction:
            direction_name = direction[1].title()
            if line is None:line = numeric(direction[2])
        else:
            direction_name = None
            # Some markets use Yes/No tokens for a clearly stated O/U question.
            bounds = re.findall(r'\b(Over|Under)\s+('+NUMBER+r')', title, re.I)
            if label.casefold() in {'yes','no'} and len(bounds) == 1:
                direction_name = bounds[0][0].title()
                if label.casefold() == 'no':direction_name = 'Under' if direction_name == 'Over' else 'Over'
                if line is None:line = numeric(bounds[0][1])
        if direction_name and line is None:
            match = re.search(r'\b(?:Over|Under|O/U)\s*('+NUMBER+r')', title, re.I)
            if match:line = numeric(match[1])
        if direction_name and line is not None:
            name = subject(title, kind)
            context = name+' ' if name else ''
            return f'{context}{direction_name} {line:g}', context+direction_name, line
    if label.casefold() in {'yes','no'}:
        label = title if label.casefold() == 'yes' else 'Not: '+title
    elif kind != 'moneyline' and not kind.endswith('spreads'):
        label = f'{title} — {label}' if title else label
    return label, label, line or 0


def section_title(title):
    return {'Moneyline':'Game winner', 'Spreads':'Alternate spreads', 'Totals':'Total points',
            'Team Totals':'Team totals'}.get(title, title)
