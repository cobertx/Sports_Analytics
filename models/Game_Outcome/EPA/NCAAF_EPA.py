"""College provider PPA ratings -> fitted total-scoring-per-play bridge."""
import numpy as np
import pandas as pd
from models.Game_Outcome.PPP.NCAAF_PPP import NCAAFPPPModel
from models.Game_Outcome.PPP.PPP import PPPConfig,PPPError


class NCAAFEPAModel(NCAAFPPPModel):
    def __init__(self,config=None):
        super().__init__(config or PPPConfig(half_life_days=180,ridge_penalty=100,
            minimum_training_games=10,minimum_team_games=1,minimum_calibration_games=8,maximum_calibration_games=120))

    def _history(self,frame,as_of):
        if not {'off_ppa','def_ppa'}<=set(frame):raise PPPError('EPA requires CFBD advanced game PPA; refresh the selected schools.')
        frame=frame.copy()
        frame[['off_ppa','def_ppa']]=frame[['off_ppa','def_ppa']].apply(pd.to_numeric,errors='coerce')
        valid=np.isfinite(frame[['off_ppa','def_ppa']]).all(axis=1)
        good=frame[valid].groupby('game_id').size()
        frame=frame[frame.game_id.isin(good[good.eq(2)].index)]
        return super()._history(frame,as_of)

    def _fit(self,frame):
        if frame.game_id.nunique()<self.config.minimum_training_games:raise PPPError('Not enough college PPA games for EPA.')
        teams={t:i for i,t in enumerate(sorted(set(frame.team)|set(frame.opponent)))};n=len(teams)
        x=np.zeros((len(frame),2+2*n));x[:,0]=1
        x[:,1]=(2*frame.is_home.to_numpy()-1)*(1-frame.neutral.to_numpy())/2
        x[np.arange(len(x)),2+frame.team.map(teams).to_numpy()]=1
        x[np.arange(len(x)),2+n+frame.opponent.map(teams).to_numpy()]=1
        weight=frame.weight.to_numpy(float)*frame.offensive_plays.to_numpy(float)
        penalty=np.full(x.shape[1],self.config.ridge_penalty);penalty[0]=0
        beta=np.linalg.solve(x.T@(weight[:,None]*x)+np.diag(penalty),x.T@(weight*frame.off_ppa.to_numpy(float)))
        for start in [2,2+n]:
            center=beta[start:start+n].mean();beta[start:start+n]-=center;beta[0]+=center
        advantage=x@beta-beta[0]
        bridge_x=np.column_stack([frame.offensive_plays,frame.offensive_plays.to_numpy(float)*advantage])
        w=frame.weight.to_numpy(float);y=frame.points.to_numpy(float)
        h=bridge_x.T@(w[:,None]*bridge_x)+np.diag([0.,25.]);target=bridge_x.T@(w*y)
        solution=np.linalg.solve(h,target)
        candidates=[np.array([max(0,target[0]/h[0,0]),0.]),np.array([0.,max(0,target[1]/h[1,1])]),np.zeros(2)]
        if (solution>=0).all():candidates.append(solution)
        bridge=min(candidates,key=lambda b:float(np.dot(w,(y-bridge_x@b)**2)+25*b[1]**2))
        return dict(teams=teams,coefficients=beta,bridge=bridge)

    def _means(self,frame,ratings,home,away,neutral=False):
        n=len(ratings['teams']);beta=ratings['coefficients'];index=ratings['teams']
        means,details=[],{}
        for team,opponent,sign in [(home,away,1),(away,home,-1)]:
            rows=frame[frame.team.eq(team)];other=frame[frame.team.eq(opponent)]
            if min(len(rows),len(other))<self.config.minimum_team_games:raise PPPError('College EPA needs prior PPA appearances for both schools.')
            plays=(np.average(rows.offensive_plays,weights=rows.weight)+np.average(other.defensive_plays,weights=other.weight))/2
            ppa=float(beta[2+index[team]]+beta[2+n+index[opponent]]+beta[1]*sign*(not neutral)/2)
            rate=max(.001,float(ratings['bridge'][0]+ratings['bridge'][1]*ppa))
            means.append(float(plays*rate))
            details[team]=dict(expected_plays=float(plays),ppa_advantage=ppa,baseline_ppp=float(ratings['bridge'][0]),
                               ppa_conversion=float(ratings['bridge'][1]),prior_games=len(rows))
        return means,details

    def predict(self,*args,**kwargs):
        result=super().predict(*args,**kwargs)
        result.diagnostics.update(forecast_method='EPA',method='CFBD signed game PPA ratings and fitted total-scoring bridge')
        return result
