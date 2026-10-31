"""Patch live quote cells without replacing native accordions or closed panels."""
import hashlib
import json
from dash import Patch, no_update
from . import kalshi_view as view
from .lazy_markets import markets as lazy_markets

PRICES = ('yes_bid','yes_ask','no_bid','no_ask','last_price')
DYNAMIC = (*PRICES,'volume','volume_24h','open_interest','yes_bid_size','yes_ask_size',
           'metadata_updated','metadata_fetched_at','yes_book_timestamp','no_book_timestamp',
           'yes_implied','no_implied','yes_probability','no_probability','yes_edge','no_edge',
           'yes_match','no_match','model_method','model_detail')
VISIBLE = {'yes_ask','no_ask','yes_probability','no_probability','yes_match','no_match','model_method','model_detail'}


def assign(patch, path, value):
    target = patch
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def render(snapshot, previous=None, *, table_open=False, rules_open=False, provider='Kalshi'):
    previous = previous if isinstance(previous,dict) else {}
    rows = snapshot['rows']
    identity = [snapshot['sport'],snapshot['game_id'],snapshot['category'],snapshot.get('game'),snapshot.get('view_scope'),
                [{key:value for key,value in row.items() if key not in DYNAMIC} for row in rows]]
    structure = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    lazy_view=previous.get('lazy_view')
    values = [[row.get(key) for key in DYNAMIC] for row in rows]
    before = previous.get('values',[])
    changed = (structure != previous.get('structure') or not lazy_markets.exists(lazy_view) or not isinstance(before,list) or
               len(before)!=len(rows) or any(not isinstance(value,list) or len(value)!=len(DYNAMIC) for value in before))
    full_table = table_open and (changed or not previous.get('table_open'))
    full_rules = rules_open and (changed or not previous.get('rules_open'))
    table, tips, rules, sections = no_update,no_update,no_update,no_update
    if full_table or full_rules:
        built_table,built_tips,built_rules = view.table(snapshot,include_rules=full_rules,provider=provider,
                                                       build_rows=full_table,build_tooltips=full_table)
        if full_table: table,tips=built_table,built_tips
        if full_rules: rules=built_rules
    if not table_open and (changed or previous.get('table_open')): table,tips=[],[]
    if not rules_open and (changed or previous.get('rules_open')): rules=[]
    if changed:
        lazy_view=lazy_markets.view()
        sections=view.market_sections(snapshot,provider,view_id=lazy_view)
    else:
        changed_rows = {index for index,value in enumerate(values) if value!=before[index]}
        if changed_rows:
            patch=Patch()
            indices={id(row):index for index,row in enumerate(rows)}
            for group,group_rows in enumerate(view.market_groups(snapshot).values()):
                ordered=view.market_rows(group_rows)
                lazy=len(ordered)>6 or group>=2
                count=lazy_markets.update(lazy_view,group,lambda limit,ordered=ordered:
                    view.market_body(ordered,snapshot,provider,limit)) if lazy else len(ordered)
                for offset,row in enumerate(ordered):
                    if lazy and (count is None or offset>=count):continue
                    index=indices[id(row)]
                    if index not in changed_rows: continue
                    fields={key for key,old,new in zip(DYNAMIC,before[index],values[index]) if old!=new}
                    if not fields & VISIBLE: continue
                    base=[group,'props','children',1,'props','children']
                    path=(base+[1,'props','children',1,'props','children','props','children','props','children',offset]
                          if lazy else base+[1,'props','children',offset])
                    assign(patch,path+['props','children'],view.comparison_rows(row,snapshot,provider))
            if patch.to_plotly_json()['operations']: sections=patch
        if table_open and not full_table:
            patch=Patch()
            for index,row in enumerate(rows):
                for field,old,new in zip(DYNAMIC,before[index],values[index]):
                    if old!=new:
                        patch[index][field]=100*new if field in PRICES and new is not None else new
                if snapshot.get('fetched_at')!=previous.get('fetched_at'):
                    patch[index]['quote_fetched_at']=snapshot.get('fetched_at')
            if patch.to_plotly_json()['operations']: table=patch
    state=dict(context=[snapshot['sport'],snapshot['game_id'],snapshot['category']],
               view_scope=snapshot.get('view_scope'),
               structure=structure,values=values,fetched_at=snapshot.get('fetched_at'),
               table_open=bool(table_open),rules_open=bool(rules_open),lazy_view=lazy_view)
    return table,tips,rules,sections,state
