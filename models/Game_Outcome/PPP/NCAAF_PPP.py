"""College total-points-per-play GLM with college-only forward score errors."""
from dataclasses import asdict
import numpy as np
import pandas as pd
from .PPP import PPPModel,PPPConfig,PPPError
from .NBA_PPP import NBAPPPForecast


class NCAAFPPPForecast(NBAPPPForecast):
    """Full-game college margin excludes ties after overtime, like NBA."""


def college_history(frame,as_of):
    required={'game_id','team','opponent','event_date','available_at','points','points_allowed',
              'offensive_plays','defensive_plays','season','week','neutral','is_home'}
    if not required<=set(frame):raise PPPError('Prepare scoped NCAAF team-game CSV history first.')
    result=frame.copy()
    cutoff=pd.Timestamp(as_of)
    if cutoff.tzinfo is None:raise PPPError('College forecast cutoff must include a timezone.')
    for c in ['event_date','available_at']:result[c]=pd.to_datetime(result[c],utc=True,errors='raise')
    result=result[result.event_date.lt(cutoff.normalize()) & result.available_at.lt(cutoff)
                  & result.available_at.ge(result.event_date)].copy()
    nums=['points','points_allowed','offensive_plays','defensive_plays','season','week','neutral','is_home']
    result[nums]=result[nums].apply(pd.to_numeric,errors='coerce')
    if (result.empty or result.duplicated(['game_id','team']).any() or not np.isfinite(result[nums]).all().all()
            or result[['points','points_allowed']].lt(0).any().any()
            or result[['offensive_plays','defensive_plays']].le(0).any().any()
            or not np.isclose(result[['points','points_allowed','offensive_plays','defensive_plays']].to_numpy(float)%1,0).all()
            or not result.is_home.isin([0,1]).all() or not result.neutral.isin([0,1]).all()
            or not result.groupby('game_id').size().eq(2).all()):
        raise PPPError('College forecasts need complete, finite pairs of completed team games.')
    other=result.merge(result,on='game_id',suffixes=('','_other'))
    other=other[other.team.ne(other.team_other)]
    if (len(other)!=len(result) or not other.opponent.eq(other.team_other).all()
            or not other.points.eq(other.points_allowed_other).all()
            or not other.event_date.eq(other.event_date_other).all()
            or not other.available_at.eq(other.available_at_other).all()
            or not other.is_home.ne(other.is_home_other).all()
            or not other.neutral.eq(other.neutral_other).all()
            or not other.season.eq(other.season_other).all() or not other.week.eq(other.week_other).all()
            or not np.allclose(other.offensive_plays,other.defensive_plays_other)):
        raise PPPError('College paired rows disagree on score, date, venue or observed plays.')
    # Fits total scoring, not an invented offensive/non-offensive decomposition.
    result['offensive_points']=result.points
    result['offensive_points_allowed']=result.points_allowed
    result['non_offensive_points']=0.
    if 'season_type' in result:
        if 'cfbd_week' not in result:result['cfbd_week']=result.week
        result['week']=result.cfbd_week+30*result.season_type.astype(str).str.lower().str.startswith('post').astype(int)
    return result.sort_values(['event_date','game_id','is_home']).reset_index(drop=True)


class NCAAFPPPModel(PPPModel):
    def __init__(self,config=None):
        super().__init__(config or PPPConfig(half_life_days=180,ridge_penalty=30,
            minimum_training_games=10,minimum_team_games=1,minimum_calibration_games=8,maximum_calibration_games=120))

    def _history(self,frame,as_of):
        frame=college_history(frame,as_of)
        frame['weight']=np.exp2(-(pd.Timestamp(as_of)-frame.event_date).dt.total_seconds()/86400/self.config.half_life_days)
        return frame

    def _means(self,frame,ratings,home,away,neutral=False):
        n=len(ratings['teams']);beta=ratings['coefficients'];indices=ratings['teams']
        means,details=[],{}
        for team,opponent,sign in [(home,away,1),(away,home,-1)]:
            own=frame[frame.team.eq(team)];other=frame[frame.team.eq(opponent)]
            if min(len(own),len(other))<self.config.minimum_team_games:
                raise PPPError('Both college schools need earlier appearances for a forecast.')
            plays=(np.average(own.offensive_plays,weights=own.weight)+np.average(other.defensive_plays,weights=other.weight))/2
            rate=float(np.exp(beta[0]+beta[1]*sign*(not neutral)/2+beta[2+indices[team]]+beta[2+n+indices[opponent]]))
            means.append(float(plays*rate))
            details[team]=dict(expected_plays=float(plays),matchup_ppp=rate,prior_games=len(own))
        return means,details

    def predict(self,observations,home_team,away_team,as_of,neutral=False):
        history=self._history(observations,as_of)
        if any(history.team.eq(t).sum()<3 for t in [home_team,away_team]):
            raise PPPError('Each matchup school needs at least 3 completed prior games. Select it in Historical Data.')
        result=super().predict(observations,home_team,away_team,as_of,neutral)
        result.diagnostics.update(sport='NCAAF',opportunity_unit='offensive plays',
            scoring_definition='Total scoreboard points per observed play; non-offensive scoring is not separately modeled.',
            limitations='Scoped selected-school cohort, college-only normal residuals; no conference hierarchy, roster or explicit overtime simulator.')
        return NCAAFPPPForecast(**asdict(result))
