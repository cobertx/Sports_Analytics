"""Small navigation contexts and pure UI descriptions; no model work."""
from functools import wraps
import json
import re

from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
from .data import SOURCES
from .nba_markets import NBA_MARKETS
from .nfl_markets import NFL_MARKETS
from .ncaaf_markets import NCAAF_MARKETS
from .kalshi_analysis import FILTER
from .schedule import FANDUEL_LINKS
from .historical_view import navigation as historical_navigation, historical_route
from .historical_links import history_target
from .market_navigation import remembered_market
from .backtesting_view import backtesting_route, summary_route as backtest_summary_route, PERFORMANCE_ROUTE
from .information import route as information_route
from . import kalshi_view as default_view
from .kalshi_view import route as kalshi_route
from .filtered_results import route as filtered_results_route
from . import market_summary, polymarket_view, research_view

PAGES=('historical','market','kalshi','polymarket','market-summary','filtered-results','backtesting','in-game','information','odds','pnl')


def scoped_callback(app,page,*dependencies,**options):
    """Wake only the active workspace; keep original function arguments intact.

    Global navigation values remain server-validated State. The small context
    changes only while its workspace is active, so leaving a page generates no
    data request there. Independent controls still use their original Inputs.
    """
    converted=[];count=0;route=False
    for dependency in dependencies:
        if isinstance(dependency,(Input,State)):
            count+=1
            if isinstance(dependency,Input) and dependency.component_id=='market-url':route=True
            if isinstance(dependency,Input) and isinstance(dependency.component_id,str) and dependency.component_id in {'market-url','sport','workspace-page'}:
                dependency=State(dependency.component_id,dependency.component_property)
        converted.append(dependency)
    def decorate(function):
        @wraps(function)
        def dispatch(*values):
            if len(values)==count+4:
                *values,context,current_page,sport,location=values
                active='backtesting' if sport=='RESEARCH' else 'odds' if sport=='ODDS_API' else 'pnl' if sport=='PNL' else current_page
                if (not isinstance(context,dict) or context.get('v')!=1 or active!=page or
                    (context.get('sport'),context.get('page'))!=(sport,current_page) or
                    ('hash' in context and context['hash']!=(location or ''))):
                    raise PreventUpdate
            return function(*values)
        return app.callback(*converted,Input('nav-'+page+('' if route else '-active'),'data'),State('workspace-page','value'),
                            State('sport','value'),State('market-url','hash'),**options)(dispatch)
    return decorate


def register_shell(app,*outputs):
    # Generate the finite UI descriptors from the existing Python presentation
    # function. This keeps labels/visibility consistent without a server request.
    views=[];lookup={}
    for page in PAGES:
        for sport in ('NBA','NFL','NCAAF','ODDS_API','RESEARCH','PNL'):
            for kind in ('team','player'):
                value=navigate(page,sport,kind)
                if value not in views:views.append(value)
                lookup[f'{page}:{sport}:{kind}']=views.index(value)
    app.clientside_callback('function(page,sport,kind){const views='+json.dumps(views)+',lookup='+json.dumps(lookup)+';'
        'return views[lookup[[page,sport,kind||"team"].join(":")]??lookup[["historical",sport,"team"].join(":")]];}',
        *outputs,Input('workspace-page','value'),Input('sport','value'),Input('entity-type','value'))


def register_sidebar(app,*outputs):
    from dash._utils import to_json
    links={f'{sport}:{kind}':sport_page_navigation('historical',sport,kind)[0]
           for sport in ('NBA','NFL','NCAAF') for kind in ('team','player')}
    app.clientside_callback('''function(page,sport,kind,saved){
        const links='''+to_json(links)+''';
        const active=value=>'sport-page-link'+(page===value?' active':'');
        const entry=saved?.[sport], remembered=entry&&new RegExp('^#'+sport.toLowerCase()+'-game-[0-9]+$').test(entry.route||'');
        return [links[[sport,kind].join(':')]||[],{display:page==='historical'&&['NBA','NFL','NCAAF'].includes(sport)?'grid':'none'},
          active('historical'),active('market'),active('backtesting'),remembered?entry.route:'#'+sport.toLowerCase()+'-games'];
    }''',*outputs,Input('workspace-page','value'),Input('sport','value'),Input('entity-type','value'),Input('market-return','data'))


def register_visibility(app,function,*outputs):
    views=[];lookup={}
    for page in PAGES:
        for sport in ('NBA','NFL','NCAAF','RESEARCH','ODDS_API','PNL'):
            value=function(page,sport)
            if value not in views:views.append(value)
            lookup[page+':'+sport]=views.index(value)
    app.clientside_callback('function(page,sport){const views='+json.dumps(views)+',lookup='+json.dumps(lookup)+';'
        'return views[lookup[page+":"+sport]??lookup["historical:"+sport]];}',
        *outputs,Input('workspace-page','value'),Input('sport','value'))


def register_research_visibility(app,*outputs):
    app.clientside_callback('''function(sport,hash){
        const research=sport==='RESEARCH',hidden={display:'none'},
            player=/^#research-player-stats-(?!comparison$)[a-z0-9-]+$/.test(hash||'');
        return [research?hidden:{},research?hidden:{},research?hidden:{},
            {display:research?'block':'none'},{display:research&&player?'block':'none'}];
    }''',*outputs,Input('sport','value'),Input('market-url','hash'))


def register(app):
    # Stop view polling on hidden pages while the independent global job
    # monitor and worker processes continue. Keep each job's disabled policy.
    for identity in ('backtest-poll','comparison-poll','performance-poll'):
        app.clientside_callback("function(page,sport){return page==='backtesting'||sport==='RESEARCH'?-1:0;}",
            Output(identity,'max_intervals'),Input('workspace-page','value'),Input('sport','value'))
    identities=['nav-'+page+suffix for page in PAGES for suffix in ('','-active')]
    app.clientside_callback('''function(hash,sport,page,...previous){
        const names='''+json.dumps(PAGES)+''', last=previous.pop(),
          active=sport==='RESEARCH'?'backtesting':sport==='ODDS_API'?'odds':sport==='PNL'?'pnl':page,
          key=sport+':'+active, entering=key!==last, no=window.dash_clientside.no_update;
        const values=names.flatMap((name,i)=>{if(name!==active)return [no,no];
            return [0,1].map(offset=>{const before=previous[i*2+offset], value={v:1,sport,page,
                visit:(before?.visit||0)+(entering?1:0)};
              if(!offset)value.hash=hash||'';
              return JSON.stringify(before)===JSON.stringify(value)?no:value;});});
        return [...values,entering?key:no];
    }''',*[Output(identity,'data') for identity in identities],Output('nav-active','data'),
        Input('market-url','hash'),Input('sport','value'),Input('workspace-page','value'),
        *[State(identity,'data') for identity in identities],State('nav-active','data'))


def market_game_id(value):
    match=re.fullmatch(r'#(?:nba|nfl|ncaaf)-game-(\d+)',value or '')
    return match[1] if match else None


def market_game_sport(value):
    return value[1:].split('-game-',1)[0].upper() if market_game_id(value) else None


def navigate(page, sport,kind='team'):
    odds = sport == "ODDS_API"
    personal=sport=='PNL'
    research=sport=='RESEARCH'
    market = page == "market" and not odds and not research and not personal
    backtesting = research or (page == 'backtesting' and not odds and not personal)
    in_game = page == 'in-game' and sport in {'NBA','NFL'}
    kalshi = page == 'kalshi' and sport in SOURCES
    polymarket = page == 'polymarket' and sport in SOURCES
    summary = page == 'market-summary' and sport in SOURCES
    filtered = page == 'filtered-results' and sport in SOURCES
    information=page=='information' and sport in {'NBA','NFL'}
    exchange = 'Polymarket' if polymarket else 'Kalshi' if kalshi else None
    intro = ('P&L HISTORY','Your betting history and recorded balances.',
             'Track actual stakes, settlements and cash transfers across your accounts.') if personal else ('STRATEGY & RESEARCH','Study the method. Test your parameters.',
             'Explore forecasting strategies and run reproducible historical experiments across NBA, NFL and NCAAF.') if research else ("THE ODDS API", "Your monthly credits at a glance.",
             "Monitor your account's credit usage and connection status.") if odds else (
            'THE BACKTESTING LAB','Explore your forecasting models.',
            f'Game Outcome and Player Stats evaluation methods for {sport}.') if backtesting else (
            'INFORMATION VARIABLE' if information else 'MARKET DATA' if summary or filtered else 'THE '+exchange.upper()+' EXCHANGE' if exchange else "ON THE COURT / ON THE FIELD" if in_game else "THE UPCOMING SLATE" if market else "THE HISTORICAL LAB",
            f'{sport} · Filtered Results' if filtered else f'{sport} · Information Variable' if information else f'{sport} · Market Summary' if summary else f'{sport} · {exchange} Market' if exchange else f'{sport} · Visualized In-Game Data' if in_game else "The next game starts here." if market else f'{sport} · {"Player" if kind=="player" else "Team"} Statistics',
            'Browse saved Quick Filtering matches across upcoming games.' if filtered else 'Read official league reports, attributed news and NFL weather information.' if information else 'Compare market odds, implied probabilities and model forecasts across all three providers.' if summary else f'Browse game contracts and current exchange quotes from {exchange}’s public API.' if exchange else 'See recorded shots and offensive plays on an interactive court or field.' if in_game else "Explore upcoming NBA, NFL, and college football matchups." if market else
            ('Explore saved college player performances and game logs.' if sport=='NCAAF' else
             'Explore historical player performances and career statistics.') if kind=='player' else
            'Explore historical team performances and statistics.')
    return ({"display": "none" if market or odds or backtesting or in_game or exchange or summary or filtered or information or personal else "grid"}, {"display": "block" if market else "none"},
            f"{sport} · Upcoming games", FANDUEL_LINKS.get(sport, FANDUEL_LINKS["NBA"]), not market,
            *intro, {"display": "none" if odds or personal else "grid"}, {"display": "block" if odds else "none"}, not odds,
            {'display':'block' if backtesting else 'none'})


def sport_page_navigation(page,sport,kind,market_return=None):
    visible=page=='historical' and sport in SOURCES
    active=lambda value:'sport-page-link'+(' active' if page==value else '')
    links=historical_navigation(sport,kind)
    if sport=='NCAAF' and kind=='player':
        links=[html.A('Team Statistics',href='#historical-team',className='backtesting-model-link',**{'aria-current':'false'})]
        links.append(html.Span('Player Statistics (saved)',className='backtesting-model-link active',**{'aria-current':'page'}))
    remembered=remembered_market(market_return,sport)
    return (links,{'display':'grid' if visible else 'none'},
        active('historical'),active('market'),active('backtesting'),remembered['route'] if remembered else f'#{sport.lower()}-games')


def restore_game_route(location_hash,sport=None,page=None):
    def destination(next_sport,next_page):
        # Re-emitting an unchanged sport wakes every sport-dependent page.
        return (no_update if next_sport==sport else next_sport,
                no_update if next_page==page else next_page)
    if location_hash=='#pnl-history':return destination('PNL','pnl')
    if market_summary.route(location_hash):return destination(market_summary.route(location_hash),'market-summary')
    if filtered_results_route(location_hash):return destination(filtered_results_route(location_hash),'filtered-results')
    if information_route(location_hash):return destination(information_route(location_hash)['sport'],'information')
    if kalshi_route(location_hash):return destination(kalshi_route(location_hash)[0],'kalshi')
    if polymarket_view.route(location_hash):return destination(polymarket_view.route(location_hash)[0],'polymarket')
    in_game = re.fullmatch(r'#(nba|nfl)-in-game-data', location_hash or '')
    if in_game:return destination(in_game[1].upper(),'in-game')
    if location_hash=='#research' or research_view.route(location_hash):return destination('RESEARCH','backtesting')
    if historical_route(location_hash):
        target=history_target(location_hash)
        return destination(target['sport'] if target else no_update,'historical')
    summary=backtest_summary_route(location_hash)
    if summary:return destination(summary['sport'] or no_update,'backtesting')
    if location_hash in {'#backtesting',PERFORMANCE_ROUTE} or backtesting_route(location_hash):
        return destination(no_update,'backtesting')
    schedule=re.fullmatch(r'#(nba|nfl|ncaaf)-games',location_hash or '')
    if schedule:return destination(schedule[1].upper(),'market')
    if not market_game_id(location_hash):
        raise PreventUpdate
    return destination(market_game_sport(location_hash), "market")



def exchange_visibility(provider,page, sport, location_hash, frequency, selected, game, category=None):
    prefix=provider.lower()
    view=polymarket_view if provider=='Polymarket' else default_view
    active = page == prefix and sport in {'NBA','NFL','NCAAF'}
    route = view.route(location_hash)
    detail = active and route and route[0] == sport and bool(route[1])
    frequency = frequency if frequency in {0, 2, 5, 10, 30} else 5
    live = active and detail and game and game.get('sport') == sport and selected == 'live' and frequency > 0 and category!=FILTER
    return ({'display': 'grid' if active else 'none'}, {} if sport in {'NBA','NFL','NCAAF'} else {'display': 'none'},
            'sport-page-link' + (' active' if active else ''), view.href(sport),
            {'display': 'none' if detail else 'block'}, {'display': 'block' if detail else 'none'}, view.href(sport),
            not active, not bool(live), 1000 * (frequency or 5))



def market_detail_labels(location_hash):
    sport = market_game_sport(location_hash) or "NFL"
    credits = len(NBA_MARKETS) if sport == "NBA" else len(NCAAF_MARKETS) if sport == "NCAAF" else 17
    return f"#{sport.lower()}-games", f"FanDuel only · Up to {credits} credits per new pull"



def forecast_methods(location_hash, previous):
    options = [{"label": "PPP", "value": "PPP"},
               {"label": "EPA", "value": "EPA"},
               {"label": "ELO / Power Rating", "value": "ELO"}]
    return options, previous if previous in {"PPP", "EPA", "ELO"} else "PPP"



def research_visibility(sport,location_hash):
    research=sport=='RESEARCH';hidden={'display':'none'}
    route=research_view.route(location_hash)
    return (*(hidden if research else {} for _ in range(3)),{'display':'block' if research else 'none'},
            {'display':'block' if research and route and route[0]=='player-stats' and route[1]!='comparison' else 'none'})



def in_game_visibility(page, sport):
    supported = sport in {'NBA', 'NFL'}
    nba, nfl = ({}, {'display': 'none'}) if sport == 'NBA' else ({'display': 'none'}, {})
    return ({'display': 'grid' if supported and page == 'in-game' else 'none'},
            {} if supported else {'display': 'none'}, f'#{sport.lower()}-in-game-data',
            'sport-page-link' + (' active' if page == 'in-game' else ''), nba, nba, nfl, nfl,
            {} if sport == 'NFL' else {'display': 'none'})



def summary_visibility(page,sport):
    active=page=='market-summary' and sport in SOURCES
    credits=len({'NBA':NBA_MARKETS,'NFL':NFL_MARKETS,'NCAAF':NCAAF_MARKETS}.get(sport,{}))
    return ({'display':'grid' if active else 'none'},{} if sport in SOURCES else {'display':'none'},
            'market-data-navigation'+(' active' if page in {'market','kalshi','polymarket','market-summary','filtered-results'} else ''),
            f'#{sport.lower()}-market-summary','sport-page-link'+(' active' if active else ''),
            f'FanDuel: up to {credits} API credits per pull. Kalshi / Polymarket: public data.')



def filtered_visibility(page,sport):
    active=page=='filtered-results' and sport in SOURCES
    return ({'display':'grid' if active else 'none'},f'#{sport.lower()}-filtered-results',
        'sport-page-link'+(' active' if active else ''),not active)

