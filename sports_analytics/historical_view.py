"""Historical team/player page routes and sidebar links."""
import re
from dash import html
from .data import PLAYER_SPORTS,SOURCES


def historical_route(location_hash):
    location_hash=(location_hash or '').partition('?')[0]
    if location_hash=='#historical':return 'team'
    match=re.fullmatch(r'#historical-(team|player)',location_hash or '')
    return match[1] if match else None


def statistics_kind(sport,requested):
    return 'player' if requested=='player' and sport in PLAYER_SPORTS else 'team'


def navigation(sport,kind):
    if sport not in SOURCES:return []
    pages=[('team','Team Statistics')]
    if sport in PLAYER_SPORTS:pages.append(('player','Player Statistics'))
    selected=statistics_kind(sport,kind)
    return [html.A(label,href=f'#historical-{value}',
        className='backtesting-model-link'+(' active' if value==selected else ''),
        **{'aria-current':'page' if value==selected else 'false'}) for value,label in pages]
