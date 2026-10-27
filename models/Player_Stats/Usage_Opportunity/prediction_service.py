"""Saved snapshots -> reproducible joint simulations -> cached prop estimates.

No HTTP or sportsbook request occurs here. Preparation is an explicit CLI step.
"""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import math
import numpy as np
import pandas as pd

from sports_analytics.data import DataSourceError
from sports_analytics.nba_markets import NBA_TEAM_CODES
from sports_analytics.nfl_markets import NFL_TEAM_CODES
from .config import MODEL_VERSION, UsageOpportunityConfig
from .distributions import UsageOpportunityForecast
from .errors import UsageOpportunityError
from .historical_data import UsageHistoryRepository, eligible, match_player, atomic_json, identity_name
from .market_comparison import compare_price
from .markets import market_spec, prop_key, quote_index
from .nba_model import simulate_nba
from .nfl_model import simulate_nfl


@dataclass
class UsageMarketForecast:
    forecasts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    def selection(self, row):
        forecast = self.forecasts.get(prop_key(row))
        if forecast is None or row.get('line') is None or row.get('selection') not in {'Over', 'Under'}:
            return None
        return forecast.market(float(row['line']), row['selection'])

    def detail(self, row):
        forecast = self.forecasts.get(prop_key(row))
        if forecast is None:
            return self.errors.get(prop_key(row), 'Usage & Opportunity unavailable for this market.')
        d = forecast.diagnostics
        price = self.selection(row)
        comparison = compare_price(price, row.get('odds'))
        mcse = math.sqrt(max(0., price.win*(1-price.win))/forecast.simulations) if price else None
        opportunity = ', '.join(f'{key} {value:.2f}' for key, value in d['opportunity_projection'].items())
        return (f'UO mean {forecast.mean:.2f}; median {forecast.quantile(.5)}; predictive SD {math.sqrt(forecast.variance):.2f}; '
                f'90% interval [{forecast.quantile(.05)}, {forecast.quantile(.95)}]; '
                f'{d["training_games"]} role games; {forecast.simulations:,} simulations; '
                f'latest input {d["last_game_date"]}; opportunity: {opportunity}. '
                + (f'Monte Carlo SE(win) {100*mcse:.2f} pp. ' if mcse is not None else '')
                + (f'Estimated net EV/$1 {comparison["expected_net_per_dollar"]:+.3f}. '
                   if comparison['expected_net_per_dollar'] is not None else '') + d['limitations'])


class UsageOpportunityMarketService:
    def __init__(self, repository=None, config=None):
        self.repository = repository or UsageHistoryRepository()
        self.config = config

    def prepare(self, snapshot, refresh=False):
        return self.repository.prepare(snapshot['sport'], snapshot['commence_time'],
                                       [snapshot['home_team'], snapshot['away_team']], refresh)

    def forecast(self, snapshot):
        sport = snapshot['sport']
        if sport == 'NCAAF':
            from .ncaaf_model import CollegePlayerMarketService
            return CollegePlayerMarketService('UO',self.repository,self.config).forecast(snapshot)
        config = self.config or UsageOpportunityConfig.for_sport(sport)
        if sport not in {'NBA', 'NFL'}:
            raise DataSourceError('Usage & Opportunity is currently available for NBA and NFL only.')
        times = [pd.Timestamp(snapshot[key]) for key in ('pulled_at', 'commence_time')]
        if any(pd.isna(t) or t.tzinfo is None for t in times):
            raise DataSourceError('Snapshot and kickoff timestamps must include a timezone.')
        cutoff = min(times)
        names = [snapshot['home_team'], snapshot['away_team']]
        players, teams = self.repository.load(sport, snapshot['commence_time'], names)
        players, teams = eligible(players, cutoff, sport), eligible(teams, cutoff, sport)
        codes = NBA_TEAM_CODES if sport == 'NBA' else NFL_TEAM_CODES
        clubs = [codes.get(name, name) for name in names]
        teams = teams[teams.team.isin(clubs)].copy()
        # Retain all clubs while resolving current identity. Otherwise filtering
        # first could erase a player's later trade away from this matchup.
        subjects = {identity_name(row['subject']) for row in snapshot['rows'] if row['market_key'].startswith('player_')}
        players = players[players.entity_name.map(identity_name).isin(subjects)].copy()
        if teams.empty or players.empty:
            raise DataSourceError('No saved matchup team/player inputs before the snapshot cutoff. Run Usage_Opportunity --prepare.')
        signature = sha256(b''.join(path.read_bytes() for path in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()
        common = Path(__file__).parent.parent/'Weighted_Historical_Performance'
        signature = sha256((signature + ''.join(sha256((common/name).read_bytes()).hexdigest() for name in
            ['historical_data.py', 'prediction_service.py', 'markets.py', 'market_comparison.py'])).encode()).hexdigest()
        fingerprint = sha256((players.to_json(orient='table', date_format='iso', index=False) +
                              teams.to_json(orient='table', date_format='iso', index=False)).encode()).hexdigest()
        identity = dict(version=MODEL_VERSION, implementation=signature, input_sha256=fingerprint,
                        config=asdict(config), sport=sport, cutoff=cutoff.isoformat(),
                        snapshot={key: snapshot.get(key) for key in
                                  ['game_id', 'home_team', 'away_team', 'pulled_at', 'commence_time', 'rows']})
        digest = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        destination = self.repository.root/'forecasts'/sport.lower()/f'{digest}.json'
        if destination.exists():
            try:
                cached = json.loads(destination.read_text(encoding='utf-8'))
                if cached['identity'] == identity:
                    return UsageMarketForecast(
                        {tuple(record['key']): UsageOpportunityForecast(**record['forecast']) for record in cached['forecasts']},
                        {tuple(record['key']): record['error'] for record in cached['errors']})
            except (KeyError, ValueError, TypeError):
                pass
        result, matched, failed_players = UsageMarketForecast(), {}, {}
        players['_whp_name'] = players.entity_name.map(identity_name)
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):
                continue
            key = prop_key(row)
            if market_spec(sport, row['market_key']) is None:
                result.errors[key] = ('UO requires explicit red-zone opportunities for touchdowns and an order model for '
                                      'first/last scorer. These inputs are not connected; WHP remains a separate option.')
                continue
            if row['subject'] in matched:
                continue
            if row['subject'] in failed_players:
                result.errors[key] = failed_players[row['subject']]
                continue
            try:
                history = match_player(players, snapshot, row['subject'], cutoff)
                latest = history.iloc[-1]
                # Only current-club appearances enter opportunity ratios.
                matched[row['subject']] = dict(team=latest.team, history=history[history.team.eq(latest.team)])
            except ValueError as exc:
                result.errors[key] = str(exc)
                failed_players[row['subject']] = str(exc)
        if matched:
            matched = dict(sorted(matched.items()))
            club_data = {club: rows for club, rows in teams.groupby('team')}
            if set(clubs) != set(club_data):
                raise DataSourceError('Both matchup teams need saved pre-snapshot team history for shared game simulation.')
            rng_seed = int(sha256(f'{sport}:{snapshot["game_id"]}:{config.seed}'.encode()).hexdigest()[:16], 16)
            simulation = (simulate_nba if sport == 'NBA' else simulate_nfl)(matched, club_data, config, np.random.default_rng(rng_seed))
            for row in snapshot['rows']:
                key = prop_key(row)
                if key in result.forecasts or key in result.errors:
                    continue
                spec = market_spec(sport, row['market_key'])
                if spec is None:
                    continue
                subject = row['subject']
                if subject not in simulation.players:
                    result.errors[key] = simulation.errors.get(subject, 'No complete observed history for this player.')
                    continue
                samples = simulation.players[subject]
                if not set(spec.fields) <= set(samples):
                    result.errors[key] = 'The required simulated outcome channels are unavailable.'
                    continue
                values = sum(samples[field] for field in spec.fields)
                diagnostics = {**simulation.diagnostics[subject], 'model_version': MODEL_VERSION,
                               'as_of': cutoff.isoformat(), 'input_sha256': fingerprint,
                               'implementation': signature, 'config': asdict(config)}
                result.forecasts[key] = UsageOpportunityForecast.from_samples(values, diagnostics)
        evaluations = []
        indexed_quotes = quote_index(snapshot['rows'])
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):
                continue
            price = result.selection(row)
            opposite = {'Over': 'Under', 'Under': 'Over'}.get(row['selection'])
            quotes = indexed_quotes.get((*prop_key(row), row['line'], opposite), [])
            evaluations.append(dict(subject=row['subject'], market=row['market_key'], line=row['line'], selection=row['selection'],
                **compare_price(price, row['odds'], quotes[0] if len(quotes) == 1 else None),
                win=price.win if price else None, loss=price.loss if price else None, push=price.push if price else None,
                detail=result.detail(row)))
        atomic_json(destination, dict(identity=identity,
            forecasts=[dict(key=list(key), forecast=asdict(value)) for key, value in result.forecasts.items()],
            errors=[dict(key=list(key), error=value) for key, value in result.errors.items()], evaluations=evaluations))
        return result
