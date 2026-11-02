"""Validate browser-session market locations before restoring local routes."""
import re


def remembered_market(saved,sport,location_hash=None,provider='market'):
    if sport not in {'NBA','NFL','NCAAF'} or not isinstance(saved,dict):return None
    if provider not in {'market','kalshi','polymarket'}:return None
    entries=saved.get('entries') if saved.get('v')==2 else None
    entry=entries.get(provider+':'+sport) if isinstance(entries,dict) else saved.get(sport) if provider=='market' else None
    if not isinstance(entry,dict):return None
    route=entry.get('route')
    pattern='#'+sport.lower()+ (r'-game-\d+' if provider=='market' else
        r'-polymarket-game-\d+' if provider=='polymarket' else r'-kalshi-game-KX'+sport+r'GAME-[A-Z0-9_.-]{1,190}')
    if not isinstance(route,str) or not re.fullmatch(pattern,route):return None
    if location_hash is not None and route!=location_hash:return None
    return entry
