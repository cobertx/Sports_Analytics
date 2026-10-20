"""Component-specific historical neutralization and uncertain upcoming defense."""
import numpy as np
from .schedule_strength import recency
from .lineup_adjustment import scenario_draws

NBA_CONVERSION={('two_pm','two_pa'):'nba:two_p',('fg3m','fg3a'):'nba:three_p',('ftm','fta'):'nba:ft_p'}
NFL_CONVERSION={('receptions','targets'):'nfl:catch',('interceptions','incompletions'):'nfl:int_rate'}
YARDS={'receiving_yards':'nfl:rec_ypc','rushing_yards':'nfl:rush_ypc'}


class MatchupContext:
    def __init__(self, model, sport, opponents, config, cutoff, rng, *, neutral=False, scenarios=()):
        self.model,self.sport,self.opponents,self.config=model,sport,opponents,config
        self.neutral=neutral
        self.profiles={}
        self.draws={}
        self.fallbacks=set()
        size=config.baseline.simulations
        # A common Bayesian-bootstrap weight per defensive game preserves
        # dependence between opportunity and efficiency component estimates.
        bootstrap={}
        for offense,defense in sorted(opponents.items()):
            scenario=scenario_draws(scenarios,offense,cutoff,size,rng)
            components=sorted({r['component'] for r in model.records})
            for component in components:
                profile=model.profile(defense,component)
                if profile is None:
                    continue
                records=profile['records']
                weights=recency(len(records),config.defense_half_life)
                for record in records:
                    key=(defense,record['game_id'])
                    if key not in bootstrap:
                        bootstrap[key]=rng.exponential(size=size)
                random_weights=np.column_stack([bootstrap[defense,r['game_id']] for r in records])
                random_weights*=weights
                expected=np.array([r['expected'] for r in records])
                actual=np.array([r['actual'] for r in records])
                ratios=(random_weights@actual)/(random_weights@expected)
                factors=np.clip(profile['alpha']*ratios+1-profile['alpha'],config.factor_floor,config.factor_ceiling)
                if component in scenario:
                    factors*=scenario[component]
                self.draws[(offense,component)]=np.ones(size) if neutral else factors
                self.profiles[(offense,component)]={k:v for k,v in profile.items() if k!='records'} | {
                    'games':len(records),'last_game_date':max(r['event_date'][:10] for r in records),
                    'draw_mean':float(factors.mean()),'draw_sd':float(factors.std()),
                    'scenario_applied':component in scenario}

    def component(self, frame, base):
        if self.sport=='NFL' and 'position' in frame:
            positions=frame.sort_values('event_date').position.dropna().astype(str).str.upper().replace({'FB':'RB','HB':'RB'})
            if len(positions) and positions.iloc[-1] in {'WR','TE','RB','QB'}:
                candidate=base+':'+positions.iloc[-1]
                team=str(frame.iloc[-1].team)
                if (team,candidate) in self.draws:
                    return candidate
        return base

    def historical(self, frame, component):
        if component.count(':')>1 and 'position' in frame:
            roles=frame.position.astype('string').str.upper().replace({'FB':'RB','HB':'RB'})
            components=[component.rsplit(':',1)[0]+':'+str(role) for role in roles]
        else:
            components=[component]*len(frame)
        return np.array([self.model.historical.get((feature,str(game),str(team)),1.)
                         for feature,game,team in zip(components,frame.game_id,frame.team)])

    def current(self, offense, component):
        if (offense,component) not in self.draws:
            self.fallbacks.add((offense,component))
            return np.ones(self.config.baseline.simulations)
        return self.draws[(offense,component)]

    def ratio(self, offense, frame, component, values):
        return np.asarray(values,float)/self.historical(frame,component)*self.current(offense,component)

    def neutral_successes(self, frame, success, trial):
        base=(NBA_CONVERSION if self.sport=='NBA' else NFL_CONVERSION)[success,trial]
        component=self.component(frame,base)
        # Fractional neutral successes enter the existing power likelihood only
        # after validating the actual integer observations; never overwrite them.
        return np.minimum(frame[trial].to_numpy(float),np.maximum(0.,frame[success].to_numpy(float)/self.historical(frame,component)))

    def conversion(self, offense, frame, success, trial, draws):
        base=(NBA_CONVERSION if self.sport=='NBA' else NFL_CONVERSION)[success,trial]
        return np.clip(draws*self.current(offense,self.component(frame,base)),0,1)

    def neutral_yards(self, frame, yards, count):
        return frame[yards].to_numpy(float)/self.historical(frame,self.component(frame,YARDS[yards]))

    def yards(self, offense, frame, yards, count, values):
        return np.rint(values*self.current(offense,self.component(frame,YARDS[yards]))).astype(int)

    def shares(self, frame, shares):
        if self.sport!='NFL':
            return shares
        offense=str(frame.iloc[-1].team)
        for column,base in [('targets','nfl:target_share'),('carries','nfl:carry_share')]:
            component=self.component(frame,base)
            # No position factor means leave allocation neutral, not multiply
            # again by the team-wide target or run-volume adjustment.
            if component!=base:
                shares[column]=self.ratio(offense,frame,component,shares[column])
        return shares

    def diagnostics(self, offense):
        return {component:profile for (team,component),profile in self.profiles.items() if team==offense}
