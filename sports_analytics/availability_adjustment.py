"""Shared injury-conditioned overlay for live game and player market forecasts."""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import math
from pathlib import Path
from threading import RLock

import numpy as np
import pandas as pd

from models.availability_effects import VERSION, estimate
from models.Player_Stats.Usage_Opportunity.distributions import UsageOpportunityForecast
from models.Player_Stats.Usage_Opportunity.historical_data import UsageHistoryRepository, eligible
from models.Player_Stats.Weighted_Historical_Performance.historical_data import identity_name
from models.Player_Stats.Weighted_Historical_Performance.markets import stat_spec
from .availability import AvailabilityStore, OUT
from .data import DataSourceError
from .nba_markets import NBA_TEAM_CODES
from .nfl_markets import NFL_TEAM_CODES
from .page_cache import forecast_revision


def club(value, sport):
    codes = NBA_TEAM_CODES if sport == 'NBA' else NFL_TEAM_CODES
    value = codes.get(value, str(value).upper())
    aliases = ({'BRK':'BKN', 'PHO':'PHX', 'CHO':'CHA', 'WSH':'WAS'} if sport == 'NBA' else
               {'LA':'LAR', 'STL':'LAR', 'WAS':'WSH', 'JAC':'JAX', 'OAK':'LV', 'SD':'LAC'})
    return aliases.get(value, value)


def inputs(players, teams, sport, cutoff, snaps=None):
    """Complete team results plus observed participation, never zero-filled stats.

    NBA absence inference requires a complete team box score (minutes and points
    reconcile). NFL uses the saved all-position snap ledger, not the offensive
    production table: no target/carry row does not establish nonparticipation.
    """
    players, teams = eligible(players, cutoff, sport), eligible(teams, cutoff, sport)
    for frame in (players, teams):
        for column in ('team', 'opponent'):frame[column] = frame[column].map(lambda x: club(x, sport))
    players['_name'] = players.entity_name.map(identity_name)
    teams = teams.drop_duplicates(['game_id', 'team']).copy()
    lookup = teams.set_index(['game_id', 'team'])
    scoring = 'pts' if sport == 'NBA' else 'pf'
    if scoring not in teams:return players, pd.DataFrame(), pd.DataFrame()
    teams['points'] = pd.to_numeric(teams[scoring], errors='coerce')
    teams['points_allowed'] = [lookup[scoring].get((g, o), np.nan) for g, o in zip(teams.game_id, teams.opponent)]
    if sport == 'NFL' and 'pa' in teams:teams['points_allowed'] = teams.points_allowed.fillna(teams.pa)
    teams['won'] = teams.points.gt(teams.points_allowed).astype(float)
    teams['is_home'] = [int(str(m).endswith(' @ '+str(t))) for m, t in zip(teams.get('matchup', teams.team), teams.team)]
    if sport == 'NBA':
        if not {'min', 'pts'} <= set(players):return players, teams, pd.DataFrame()
        totals = players.groupby(['game_id', 'team']).agg(minutes=('min', 'sum'), points=('pts', 'sum'), n=('entity_id', 'nunique'))
        # Rounded NBA minutes can differ slightly across official adapters.
        periods = np.rint((totals.minutes-240)/25)
        complete = (periods.ge(0) & (totals.minutes-(240+25*periods)).abs().le(2.5) & totals.n.ge(7))
        expected = lookup[scoring].reindex(totals.index)
        complete &= totals.points.eq(expected)
        valid = set(totals.index[complete])
        ledger = players[[ 'game_id', 'team', '_name', 'entity_id', 'event_date', 'season', 'min']].copy()
        ledger = ledger[[ (g,t) in valid for g,t in zip(ledger.game_id, ledger.team) ]]
        ledger['played'] = pd.to_numeric(ledger['min'], errors='coerce').gt(0)
    else:
        if snaps is None or not {'game_id', 'team', 'player', 'offense_snaps', 'defense_snaps'} <= set(snaps):
            return players, teams, pd.DataFrame()
        ledger = snaps.copy()
        ledger['game_id'] = ledger.game_id.astype(str)
        ledger['team'] = ledger.team.map(lambda x: club(x, sport))
        ledger['_name'] = ledger.player.map(identity_name)
        for c in ('offense_snaps', 'defense_snaps'):ledger[c] = pd.to_numeric(ledger[c], errors='coerce')
        # A complete NFL side has approximately eleven player-snaps per snap.
        totals = ledger.groupby(['game_id','team'])[['offense_snaps','defense_snaps']].agg(['sum','max'])
        complete = np.ones(len(totals), dtype=bool)
        for c in ('offense_snaps','defense_snaps'):
            complete &= (totals[c]['max'].gt(0) & totals[c]['sum'].ge(10*totals[c]['max'])).to_numpy()
        valid = set(totals.index[complete])
        ledger = ledger[[(g,t) in valid for g,t in zip(ledger.game_id,ledger.team)]]
        metadata = teams[['game_id','team','event_date','season']]
        ledger = ledger.drop(columns=['event_date','season'], errors='ignore').merge(metadata, on=['game_id','team'], validate='many_to_one')
        ledger['entity_id'] = ledger.get('pfr_player_id', ledger['_name']).astype(str)
        ledger['played'] = ledger[['offense_snaps','defense_snaps']].sum(axis=1).gt(0)
    # Only games with a verified complete participation ledger can imply absence.
    teams = teams[[ (g,t) in valid for g,t in zip(teams.game_id,teams.team) ]]
    teams = teams.dropna(subset=['points','points_allowed']).copy()
    return players, teams, ledger


def absence_columns(team_games, ledger, reports, team, cutoff):
    """Restrict absence comparisons to the player's observed club tenure.

    A current structured report confirms the latest club, but no earlier unseen
    player is invented. Intervening appearances for another club end a stint.
    """
    result = team_games[team_games.team.eq(team)].copy(); columns = []; skipped = []
    for report in reports:
        name = identity_name(report['player'])
        player = ledger[ledger['_name'].eq(name)].sort_values(['event_date','game_id'])
        own = player[player.team.eq(team)]
        if own.empty or own.entity_id.nunique() != 1 or player.iloc[-1].team != team:
            skipped.append(report['player']+': no unambiguous current-club participation history')
            continue
        if ('min' in own and pd.to_numeric(own['min'],errors='coerce').mean()<12 or
                'offense_snaps' in own and own[['offense_snaps','defense_snaps']].max(axis=1).mean()<10):
            skipped.append(report['player']+': historical role too small for a stable adjustment')
            continue
        other = player[player.team.ne(team)]
        start = own.event_date.min()
        if len(other):start = own[own.event_date.gt(other.event_date.max())].event_date.min()
        if pd.isna(start):continue
        column = 'absent:'+name
        played_games = set(own.loc[own.played, 'game_id'])
        result[column] = np.where(result.event_date.ge(start), (~result.game_id.isin(played_games)).astype(float), np.nan)
        known = result[column].dropna()
        if known.eq(0).sum() < 3 or known.eq(1).sum() < 3:
            result = result.drop(columns=column)
            skipped.append(report['player']+': need at least three playing and three nonplaying games')
        elif column not in columns:columns.append(column)
    return result, columns, skipped


def adjusted_distribution(base, shift, uncertainty, *, nonnegative):
    """Convolve integer outcome mass with an uncertain additive role change.

    Empirical support stays exact. Analytic distributions retain their tail mass
    in boundary bins (at least ten SD out); no probability mass is renormalized
    away. Fractional shifts split between neighboring integers.
    """
    if hasattr(base, 'values'):
        values, probabilities = np.asarray(base.values, float), np.asarray(base.probabilities, float)
    elif getattr(base, 'family', '') == 'empirical':
        values, probabilities = np.asarray(base.samples, float), np.asarray(base.weights, float)
    else:
        sd = math.sqrt(base.variance)
        low, high = math.floor(base.mean-10*sd)-2, math.ceil(base.mean+10*sd)+2
        if nonnegative:low = max(0, low)
        if high-low > 20000:raise ValueError('Predictive support is too wide for a reliable availability adjustment.')
        values = np.arange(low, high+1, dtype=float)
        cdf = np.array([base.market(float(x), 'Under').win+base.market(float(x), 'Under').push for x in values])
        probabilities = np.maximum(0., np.diff(np.r_[0.,cdf])); probabilities[-1] += max(0.,1-cdf[-1])
    if uncertainty > 1e-8:
        from statistics import NormalDist
        normal = NormalDist(shift, uncertainty)
        offsets = np.arange(math.floor(shift-8*uncertainty)-1, math.ceil(shift+8*uncertainty)+2)
        if len(offsets) > 20000:raise ValueError('Availability-effect uncertainty is too large.')
        cdf = np.array([normal.cdf(float(x)+.5) for x in offsets])
        mass = np.maximum(0.,np.diff(np.r_[0.,cdf])); mass[-1] += max(0.,1-cdf[-1])
    else:
        low = math.floor(shift); fraction = shift-low
        offsets, mass = np.array([low,low+1]), np.array([1-fraction,fraction])
    # Convolution on dense integer bins avoids a support x offsets allocation.
    low, high = int(values.min()), int(values.max())
    if high-low > 20000:raise ValueError('Predictive support is too wide for an availability adjustment.')
    dense = np.zeros(high-low+1); np.add.at(dense,values.astype(int)-low,probabilities)
    combined = np.convolve(dense,mass)
    x = np.arange(len(combined))+low+int(offsets[0])
    if nonnegative:x = np.maximum(0,x)
    unique, inverse = np.unique(x, return_inverse=True)
    p = np.bincount(inverse, weights=combined)
    keep = p > 0
    result = UsageOpportunityForecast(unique[keep].tolist(), p[keep].tolist(), getattr(base,'simulations',10000), deepcopy(base.diagnostics))
    # WHP and all shared distribution/detail renderers use these attributes.
    result.family = 'empirical'; result.nonnegative = nonnegative
    result.samples = result.values; result.weights = result.probabilities
    result.diagnostics['predictive_sd'] = math.sqrt(result.variance)
    return result


def summary(forecast):
    info = getattr(forecast, 'availability', None)
    if info is None:info = getattr(forecast, 'diagnostics', {}).get('availability')
    return info if isinstance(info,dict) else {}


def note(*forecasts):
    """One compact game injury list, shared across all selected methods."""
    information = [summary(forecast) for forecast in forecasts]
    information = [info for info in information if info]
    if not information:return ''
    known = [info for info in information if 'injuries' in info]
    if not known:return 'Game injuries: unavailable.'
    injuries = {(item['team'],item['player'],item['status'])
                for info in known for item in info['injuries']}
    if not injuries:return 'Game injuries: none reported.'
    teams = {}
    for team,player,status in sorted(injuries):
        teams.setdefault(team,[]).append(f'{player} ({status})')
    return 'Game injuries: '+'; '.join(team+' — '+', '.join(players) for team,players in teams.items())+'.'


class AvailabilityAdjuster:
    def __init__(self, repository=None, store=None, data=None, nfl_directory=None):
        self.repository = repository or UsageHistoryRepository(data=data)
        self.store = store or AvailabilityStore()
        self.nfl_directory = Path(nfl_directory or Path(__file__).resolve().parents[1]/'data/modeling/nfl/raw')
        self.lock = RLock(); self.cache = OrderedDict(); self.input_cache = OrderedDict()

    def revision(self, sport):
        return [VERSION, self.store.revision(sport), forecast_revision(self.repository, sport),
                [(str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(self.nfl_directory.glob('snap_counts_*.csv'))]]

    def _inputs(self, snapshot, cutoff):
        sport = snapshot['sport']
        key = repr((sport, snapshot['commence_time'], cutoff.isoformat(), self.revision(sport)))
        with self.lock:
            if key in self.input_cache:
                self.input_cache.move_to_end(key)
                return self.input_cache[key]
        players, teams = self.repository.load(sport, snapshot['commence_time'], [snapshot['home_team'],snapshot['away_team']])
        snaps = None
        if sport == 'NFL':
            year = cutoff.year-int(cutoff.month<3)
            paths = [self.nfl_directory/f'snap_counts_{y}.csv' for y in (year-1,year)]
            frames = [pd.read_csv(p,dtype={'game_id':str,'pfr_player_id':str}) for p in paths if p.exists()]
            if frames:snaps = pd.concat(frames,ignore_index=True)
        value = inputs(players,teams,sport,cutoff,snaps)
        with self.lock:
            self.input_cache[key] = value
            while len(self.input_cache)>4:self.input_cache.popitem(last=False)
        return value

    def apply(self, snapshot, forecast, method):
        if forecast is None or snapshot.get('sport') not in {'NBA','NFL'}:return forecast
        packet, error = self.store.load(snapshot)
        if packet is None and error and error.startswith('No fresh pregame availability archive'):
            return forecast
        info = dict(version=VERSION, applied=[], skipped=[], reason=error or 'No reported confirmed absences for this matchup.')
        if packet:
            info.update(fetched_at=packet['fetched_at'],source_url=packet.get('source_url'))
        # Work on copies: cached original forecasts must remain unmodified.
        result = deepcopy(forecast)
        if hasattr(result,'diagnostics'):result.diagnostics['availability'] = info
        else:result.availability = info
        if error or packet is None:return result
        sport = snapshot['sport']; teams = {club(snapshot[k],sport) for k in ('home_team','away_team')}
        reports = [r for r in packet['reports'] if club(r.get('team_code') or r['team'],sport) in teams]
        info['injuries'] = [dict(team=club(r.get('team_code') or r['team'],sport),player=r['player'],status=r['status'])
                            for r in reports if r['status'].casefold() not in {'active','healthy'}]
        uncertain = [r['player']+' ('+r['status']+')' for r in reports if r['status'].casefold() not in OUT|{'active','healthy'}]
        info['uncertain'] = uncertain
        # Don't carry an Out tag past a supplied return date for a later game.
        def still_out(report):
            value = report.get('return_date')
            if not value:return True
            try:return pd.Timestamp(value).date()>pd.Timestamp(snapshot['commence_time']).date()
            except (TypeError,ValueError):return False
        outs = [r for r in reports if r['status'].casefold() in OUT and still_out(r)]
        if not outs:
            if uncertain:info['reason'] = 'Uncertain statuses remain at baseline; no arbitrary participation probabilities assigned: '+', '.join(uncertain)
            return result
        def exclude_unavailable(value):
            if not hasattr(value,'forecasts'):return
            names = {identity_name(r['player']) for r in outs}
            for key in list(value.forecasts):
                if identity_name(key[0]) in names:
                    value.forecasts.pop(key)
                    value.errors[key] = 'Reported unavailable. Nonparticipation/void is separate from winning an Under; no prop probability supplied.'
                    message = key[0]+': reported unavailable; prop excluded'
                    if message not in info['skipped']:info['skipped'].append(message)
        exclude_unavailable(result)
        cutoff = min(pd.Timestamp(snapshot['pulled_at']),pd.Timestamp(snapshot['commence_time']))
        signature = sha256(repr((VERSION,method,snapshot,packet,forecast,self.revision(sport))).encode()).hexdigest()
        with self.lock:
            if signature in self.cache:
                self.cache.move_to_end(signature)
                return deepcopy(self.cache[signature])
        try:
            players, games, ledger = self._inputs(snapshot,cutoff)
            if games.empty or ledger.empty:raise DataSourceError('Complete historical participation and team results are not saved; baseline retained. Prepare player/team history (and NFL model snap data).')
            if hasattr(result,'forecasts'):
                self._players(snapshot,result,players,games,ledger,outs,cutoff,info)
            else:self._game(snapshot,result,games,ledger,outs,cutoff,info)
            if not info['applied']:info['reason'] = 'Insufficient comparable with/without history, or the absence is already in the recent baseline.'
        except (DataSourceError,OSError,ValueError,KeyError,np.linalg.LinAlgError) as exc:
            # A source/adjustment failure must not remove a valid base forecast.
            result = deepcopy(forecast)
            info.update(applied=[],reason='Availability adjustment unavailable; baseline retained. '+str(exc))
            if hasattr(result,'diagnostics'):result.diagnostics['availability'] = info
            else:result.availability = info
            exclude_unavailable(result)
        with self.lock:
            self.cache[signature] = deepcopy(result)
            while len(self.cache)>32:self.cache.popitem(last=False)
        return result

    def _game(self,snapshot,result,games,ledger,outs,cutoff,info):
        sport = snapshot['sport']
        variance_margin = variance_total = 0.
        for side, other in (('home','away'),('away','home')):
            team = club(snapshot[side+'_team'],sport)
            reports = [r for r in outs if club(r.get('team_code') or r['team'],sport)==team]
            history, columns, skipped = absence_columns(games,ledger,reports,team,cutoff)
            info['skipped'].extend(skipped)
            effect = estimate(history,columns,['points','points_allowed'],cutoff,sport)
            if effect is None or np.allclose(effect.delta,0):continue
            before = [getattr(result,side+'_mean'),getattr(result,other+'_mean')]
            after = np.maximum(0,np.asarray(before)+effect.delta)
            setattr(result,side+'_mean',float(after[0])); setattr(result,other+'_mean',float(after[1]))
            variance_margin += max(0.,float(np.array([1,-1]) @ effect.covariance @ np.array([1,-1])))
            variance_total += max(0.,float(np.ones(2) @ effect.covariance @ np.ones(2)))
            info['applied'].append(dict(team=team,**effect.evidence,
                description=f'{team} scoring {before[0]:.2f} → {after[0]:.2f}, opponent scoring {before[1]:.2f} → {after[1]:.2f} points',
                score_change=(after-np.asarray(before)).tolist()))
        result.margin_sd = math.sqrt(result.margin_sd**2+variance_margin)
        result.total_sd = math.sqrt(result.total_sd**2+variance_total)

    def _players(self,snapshot,result,players,games,ledger,outs,cutoff,info):
        sport = snapshot['sport']
        team_histories = {}; workload_effects = {}
        for key, base in list(result.forecasts.items()):
            subject, market = key
            player = players[players['_name'].eq(identity_name(subject))].sort_values(['event_date','game_id'])
            if player.empty or player.entity_id.nunique()!=1:continue
            team = player.iloc[-1].team
            player = player[player.team.eq(team)]
            if team not in {club(snapshot[k],sport) for k in ('home_team','away_team')}:continue
            if team not in team_histories:
                reports = [r for r in outs if club(r.get('team_code') or r['team'],sport)==team]
                team_histories[team] = absence_columns(games,ledger,reports,team,cutoff)
                info['skipped'].extend(item for item in team_histories[team][2] if item not in info['skipped'])
            history, columns, _ = team_histories[team]
            spec = stat_spec(sport,market)
            if not spec or not columns:continue
            workload = ('min' if sport=='NBA' else 'attempts' if market.startswith('player_pass_') else
                        'carries' if market.startswith('player_rush_') else 'targets' if market.startswith('player_recep') else None)
            if not workload or workload not in player:continue
            effect_key = (str(player.iloc[-1].entity_id),team,workload)
            if effect_key not in workload_effects:
                observed = player[['game_id',workload]].copy().rename(columns={workload:'workload'})
                role_history = history.merge(observed,on='game_id',validate='one_to_one')
                role_history = role_history[pd.to_numeric(role_history.workload,errors='coerce').ge(0)]
                workload_effects[effect_key] = estimate(role_history,columns,['workload'],cutoff,sport)
            effect = workload_effects[effect_key]
            if effect is None or effect.baseline[0]<=0 or np.allclose(effect.delta,0):continue
            # Convert evidence about B's minutes/attempts/carries/targets to a
            # production change using B's original predicted production rate.
            rate = base.mean/effect.baseline[0]
            change = max(-base.mean,float(effect.delta[0]*rate)) if spec.nonnegative else float(effect.delta[0]*rate)
            uncertainty = abs(rate)*math.sqrt(max(0.,float(effect.covariance[0,0])))
            adjusted = adjusted_distribution(base,change,uncertainty,nonnegative=spec.nonnegative)
            adjusted.diagnostics['availability'] = dict(workload=workload,workload_change=float(effect.delta[0]),
                original_mean=base.mean,mean_change=adjusted.mean-base.mean,**effect.evidence)
            if 'opportunity_projection' in adjusted.diagnostics:
                adjusted.diagnostics['opportunity_projection'] = {**adjusted.diagnostics['opportunity_projection'],
                    'injury_conditioned_'+workload:max(0.,float(effect.baseline[0]+effect.delta[0]))}
            result.forecasts[key] = adjusted
            info['applied'].append(dict(player=subject,market=market,**effect.evidence,
                description=f'{subject} {market.removeprefix("player_")}: mean {base.mean:.2f} → {adjusted.mean:.2f}; {workload} change {effect.delta[0]:+.2f}'))
