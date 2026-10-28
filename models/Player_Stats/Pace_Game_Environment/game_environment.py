"""One paired environment draw feeds volume, game mix and player roles."""
import numpy as np
import pandas as pd
from models.Player_Stats.Usage_Opportunity.opportunity_model import observed,weights,history_weights
from models.season_weights import apply_season_weights


class GameEnvironment:
    def __init__(self, sport, model, clubs, config, rng):
        self.sport,self.model,self.config = sport,model,config
        self.projections = {club:model.predict(club,opponent) for club,opponent in zip(clubs,clubs[::-1])}
        self.bandwidth = max(1.,float(model.history.margin.std()))
        paired = []
        errors = pd.DataFrame(model.residuals).tail(2*config.league_window)
        for _,game in errors.groupby('game_id',sort=False):
            if len(game) == 2:
                records = [row for _,row in game.iterrows()]
                paired.extend([records,records[::-1]])
        if not paired:
            raise ValueError('No paired chronological pace residuals are available.')
        # Both orientations of a donor game have exactly equal prior mass.
        pair_weights = np.repeat(weights(len(paired)//2,config.scenario_half_life),2)
        pair_weights = apply_season_weights(pair_weights, pd.DataFrame([r[0] for r in paired]),
                                             config.baseline.sport, config.baseline.prior_season_weight)
        target_margin = self.projections[clubs[0]]['margin']
        distance = np.array([r[0].predicted_margin-target_margin for r in paired])/self.bandwidth
        pair_weights *= np.exp(-.5*distance**2)
        pair_weights /= pair_weights.sum()
        indices = rng.choice(len(paired),config.baseline.simulations,p=pair_weights)
        drawn = [[paired[index][i] for index in indices] for i in range(2)]
        margin = np.array([r.margin-r.predicted_margin for r in drawn[0]])+target_margin
        self.margins = {clubs[0]:margin,clubs[1]:-margin}
        self.volume,self.duration,self.regulation,self.extra = {},{},{},{}
        if sport == 'NBA':
            base = np.mean([p['volume'] for p in self.projections.values()])
            residual = np.mean([[r.residual for r in d] for d in drawn],axis=0)
            regulation = np.maximum(1.,base+residual)
            duration = 48+np.array([r.extra for r in drawn[0]])
            for club in clubs:
                self.volume[club] = regulation*duration/48
                self.duration[club] = duration
                self.regulation[club],self.extra[club] = regulation,self.volume[club]-regulation
        else:
            for club,records in zip(clubs,drawn):
                self.regulation[club] = np.maximum(1.,self.projections[club]['volume']+np.array([r.residual for r in records]))
                self.extra[club] = np.array([r.extra for r in records])
                self.volume[club] = self.regulation[club]+self.extra[club]
                self.duration[club] = np.full(config.baseline.simulations,60.)
        self.margins_by_game = model.history.set_index(['game_id','team']).margin.to_dict()

    def draw_rows(self, sample, config, rng):
        club = str(sample.iloc[0].team)
        source_margin = np.array([self.margins_by_game.get((str(r.game_id),club),np.nan)
                                  for _,r in sample.iterrows()])
        if not np.isfinite(source_margin).all():
            raise ValueError('A player/team role game has no paired scoring/volume context.')
        base = history_weights(sample,config.role_half_life,config)
        kernel = np.exp(-.5*((self.margins[club][:,None]-source_margin)/self.bandwidth)**2)
        probabilities = base[None,:]*(self.config.role_smoothing+(1-self.config.role_smoothing)*kernel)
        probabilities /= probabilities.sum(axis=1)[:,None]
        indices = (rng.random(config.simulations)[:,None] > np.cumsum(probabilities,axis=1)).sum(axis=1)
        return sample.iloc[np.minimum(indices,len(sample)-1)].reset_index(drop=True)

    def team_states(self, teams, config, rng, columns):
        return {club:self.draw_rows(observed(rows,columns,config.minimum_games,config.team_window),config,rng)
                for club,rows in teams.items()}

    def nba_pool(self, team, state, channel):
        # Use the paired possession estimate used in training, rather than the
        # individual team's slightly different box-score estimate.
        # Cached game totals avoid recomputing possession estimates per draw.
        total = self.model.history.assign(total=lambda f:f.volume*f.duration/48).set_index(['game_id','team']).total.to_dict()
        source = np.array([total[(str(game),team)] for game in state.game_id])
        return state[channel].to_numpy(float)/source*self.volume[team]

    def nfl_opportunities(self, team, state, rng):
        plays = np.maximum(1,np.rint(self.volume[team])).astype(int)
        historical = (state.attempts+state.carries+state.sacks_suffered).to_numpy(float)
        passing = (state.attempts+state.sacks_suffered).to_numpy(float)
        sacks = state.sacks_suffered.to_numpy(float)
        scrambles,other = np.zeros_like(sacks),np.zeros_like(sacks)
        if 'pge_reg_plays' in state:
            valid = state.pge_reg_plays.notna().to_numpy()
            historical[valid] = (state.pge_reg_plays+state.pge_ot_plays).to_numpy(float)[valid]
            passing[valid] = state.pge_pass.to_numpy(float)[valid]
            sacks[valid] = state.pge_sack.to_numpy(float)[valid]
            scrambles[valid] = state.pge_scramble.to_numpy(float)[valid]
            other[valid] = state.pge_other.to_numpy(float)[valid]
        p_pass = np.divide(passing,historical,out=np.zeros_like(passing),where=historical>0)
        if ((p_pass<0)|(p_pass>1)).any():
            raise ValueError('Invalid designed-pass count in saved play-by-play.')
        dropbacks = rng.binomial(plays,p_pass)
        exits = np.column_stack([sacks,scrambles,other])
        exit_rates = np.divide(exits,passing[:,None],out=np.zeros_like(exits),where=passing[:,None]>0)
        if (exit_rates.sum(axis=1)>1+1e-8).any():
            raise ValueError('Non-attempt pass outcomes exceed designed pass plays.')
        from models.Player_Stats.Usage_Opportunity.opportunity_model import allocate
        removed = allocate(dropbacks,exit_rates,rng)
        designed_runs = plays-dropbacks
        return plays,removed[:,-1],designed_runs+removed[:,1],dict(
            designed_passes=dropbacks,designed_runs=designed_runs,
            sacks=removed[:,0],scrambles=removed[:,1],non_attempts=removed[:,2])

    def role_shares(self, drawn, shares):
        if self.sport=='NFL' and 'pge_scramble' in shares:
            designed = drawn.carries.to_numpy(float)-drawn.pge_scramble.to_numpy(float)
            total = drawn.team_carries.to_numpy(float)-drawn.team_pge_scramble.to_numpy(float)
            if (designed<0).any() or (total<0).any() or (designed>total+1e-8).any():
                raise ValueError('Observed scrambles/carries do not reconcile with matched box totals.')
            shares['carries'] = np.divide(designed,total,out=np.zeros_like(designed),where=total>0)
        return shares

    def allocate_carries(self, pool, roles, audit, rng):
        from models.Player_Stats.Usage_Opportunity.opportunity_model import allocate
        designed = allocate(pool-audit['scrambles'],np.column_stack([r['carries'] for r in roles]),rng)
        scramble = allocate(audit['scrambles'],np.column_stack([
            r.get('pge_scramble',np.zeros(len(pool))) for r in roles]),rng)
        return designed+scramble

    def diagnostics(self, team):
        return dict(**self.projections[team],mean_total_volume=float(self.volume[team].mean()),
                    volume_unit='possessions' if self.sport=='NBA' else 'offensive plays',
                    mean_regulation_volume=float(self.regulation[team].mean()),mean_extra_volume=float(self.extra[team].mean()),
                    mean_duration=float(self.duration[team].mean()),
                    overtime_probability=float((self.extra[team]>0).mean()),
                    scenario_margin_mean=float(self.margins[team].mean()),
                    source_modes=sorted(self.model.history.source.unique()),
                    pace_validation=self.model.validation(),
                    last_game_date=self.model.history.event_date.max().date().isoformat())
