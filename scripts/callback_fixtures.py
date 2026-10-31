"""Exercise unchanged calculation contracts and scoped HTTP callback inputs."""
from sports_analytics.navigation import (navigate,restore_game_route,sport_page_navigation,
    exchange_visibility,market_detail_labels,forecast_methods,research_visibility,in_game_visibility,summary_visibility,filtered_visibility)


def callback_functions(app):
    result={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__
            for m in app.callback_map.values() if m.get('callback')}
    result.update(navigate=navigate,restore_game_route=restore_game_route,sport_page_navigation=sport_page_navigation,
        research_visibility=research_visibility,in_game_visibility=in_game_visibility,summary_visibility=summary_visibility,filtered_visibility=filtered_visibility,
        market_detail_labels=market_detail_labels,forecast_methods=forecast_methods,
        kalshi_visibility=lambda *args:exchange_visibility('Kalshi',*args),
        polymarket_visibility=lambda *args:exchange_visibility('Polymarket',*args))
    return result


def wire_value(item,values):
    identity=item['id']
    if identity.startswith('{'):return values.get(identity,[])
    page=values.get('workspace-page') or values.get('_navigation_page')
    sport=values.get('sport')
    for key in ('dataset','kalshi-context','polymarket-context','market-detail-context'):
        sport=sport or (values.get(key) or {}).get('sport')
    sport=sport or 'NFL'
    if identity.startswith('nav-'):
        domain=identity.removeprefix('nav-').removesuffix('-active')
        values.setdefault('_navigation_page',domain)
        page=page or ('backtesting' if domain=='backtesting' else domain)
        value=dict(v=1,sport=sport,page=page,visit=1)
        if not identity.endswith('-active'):value['hash']=values.get('market-url') or ''
        return values.get(identity) or value
    if identity=='sport':return sport
    if identity=='workspace-page':
        return page
    return values.get(identity)
