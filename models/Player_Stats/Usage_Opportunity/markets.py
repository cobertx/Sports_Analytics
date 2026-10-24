"""Only forecast targets supported by actual observed opportunity features."""
from models.Player_Stats.Weighted_Historical_Performance.markets import stat_spec


def market_spec(sport, market_key):
    if sport not in {'NBA', 'NFL', 'NCAAF'}:
        return None
    key = market_key.removesuffix('_alternate')
    if sport in {'NFL', 'NCAAF'} and (key.endswith('_tds') or key in {'player_anytime_td', 'player_1st_td', 'player_last_td'}):
        return None  # Explicit red-zone/scorer-order data is not connected.
    return stat_spec(sport, market_key)


def prop_key(row):
    return row['subject'], row['market_key'].removesuffix('_alternate')


def quote_index(rows):
    """Retain duplicates: an ambiguous opposite price must not be de-vigged."""
    quotes = {}
    for row in rows:
        quotes.setdefault((*prop_key(row), row['line'], row['selection']), []).append(row['odds'])
    return quotes
