"""Validate browser-session market locations before restoring local routes."""
import re


def remembered_market(saved,sport,location_hash=None):
    if sport not in {'NBA','NFL','NCAAF'} or not isinstance(saved,dict):return None
    entry=saved.get(sport)
    if not isinstance(entry,dict):return None
    route=entry.get('route')
    if not isinstance(route,str) or not re.fullmatch('#'+sport.lower()+r'-game-\d+',route):return None
    if location_hash is not None and route!=location_hash:return None
    return entry
