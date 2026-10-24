"""Polymarket navigation using the shared exchange presentation."""
import re
from . import kalshi_view as exchange
from .polymarket import PolymarketService


def route(value):
    match=re.fullmatch(r'#(nba|nfl|ncaaf)-polymarket-(market|game-(\d+))',value or '')
    return (match[1].upper(),match[3]) if match else None


def href(sport, game_id=None):
    return f'#{sport.lower()}-polymarket-'+('game-'+game_id if game_id else 'market')


def layout():
    return exchange.layout('Polymarket')


def cards(catalog):
    return exchange.cards(catalog,'Polymarket',href)


def game_header(game):
    return exchange.game_header(game,'Polymarket',PolymarketService.url(game['slug']) if game else None)


def categories(game):
    return game.get('categories',[]) if game else []


def market_sections(snapshot):
    return exchange.market_sections(snapshot,'Polymarket')


def table(snapshot, include_rules=True):
    return exchange.table(snapshot,include_rules,'Polymarket')
