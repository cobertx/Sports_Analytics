"""Predictive integer mass from the same settlement functions as game quotes."""
from types import SimpleNamespace
from hashlib import sha256
from dash import html
from models.Game_Outcome.PPP.PPP import MarketProbability
from app.sections.markets.player_distribution import distribution,payload_panel,threshold_curve
from sports_analytics.display_time import format_eastern


def distributions(forecast, source, method):
    result={};errors=[]
    home,away=source['home_team'],source['away_team']
    def margin(value,side):
        settled=forecast.spread(forecast.home_team,-value)
        return MarketProbability(settled.loss,settled.win,settled.push)
    for key,title,mean,sd,settle,nonnegative in (
        ('margin',f'{home} margin vs {away}',forecast.margin_mean,forecast.margin_sd,margin,False),
        ('total','Combined game points',forecast.total_mean,forecast.total_sd,forecast.total,True)):
        try:
            fitted=SimpleNamespace(mean=mean,variance=sd**2,market=settle,nonnegative=nonnegative)
            payload=distribution(fitted)
            payload.update(method=method,kind='game',player=title,stat='Home margin (points)' if key=='margin' else 'Total points',
                description='Signed home-minus-away margin. ' if key=='margin' else 'Combined final score, including overtime. ')
            result[key]=payload
        except (ValueError,TypeError,OverflowError) as exc:errors.append(f'{title}: {exc}')
    return result,errors


def curve(payloads, market, group, *, home=False):
    payload=(payloads or {}).get('margin' if market=='alternate_spreads' else 'total')
    if not payload:
        return None
    if market=='alternate_spreads':
        # Away margin is the negative of home margin. The handicap has
        # the opposite sign of the margin required to cover.
        return threshold_curve(payload,above=home,reverse_axis=home)
    return threshold_curve(payload,above=group=='gt')


def panel(models,method,scope):
    payloads=models.get('game_distributions',{})
    content=[html.P('Predictive outcomes from the selected forecast, using the same integer settlement probabilities as the market estimates. '
        'Bars show probability at each exact score or margin; the cumulative graph shows probability at or below that value. '
        'Historical cutoff: '+(format_eastern(models['cutoff']) if models.get('cutoff') else 'unavailable')+'.',className='chart-note')]
    for key,payload in payloads.items():
        content.extend([html.H4(payload['player']),payload_panel(payload,scope+'-'+key,payload['player']+' '+method+' predictive distribution')])
    if not payloads:content.append(html.P('The game forecast distribution is unavailable for this selection.',className='chart-note'))
    content.extend(html.P(error,className='chart-note') for error in models.get('distribution_errors',[]))
    return html.Details([html.Summary('Forecast distributions · '+method),*content],open=False,
        id='kalshi-distributions-'+sha256(scope.encode()).hexdigest()[:16],
        className='sportsbook-market kalshi-market game-distribution-market',key=scope,
        **{'data-market-scope':'game-distribution|'+scope})
