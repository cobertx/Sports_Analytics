"""College-only Elo/power with scoring per observed play and own calibration."""
from dataclasses import asdict
from .ELO import ELOConfig,ELOModel
from models.Game_Outcome.PPP.NCAAF_PPP import college_history,NCAAFPPPForecast
from models.Game_Outcome.PPP.PPP import PPPError


class NCAAFELOModel(ELOModel):
    def __init__(self,config=None):
        super().__init__(config or ELOConfig(sport='NCAAF',k_factor=24,home_elo=55,season_carry=.2,
            power_ridge=20,scoring_ridge=30,minimum_training_games=10,minimum_team_games=1,
            minimum_calibration_games=8,maximum_calibration_games=120))

    def _history(self,frame,as_of):
        frame=college_history(frame,as_of)
        # The shared Elo scoring kernel names its exposure columns 'drives'.
        # This internal view deliberately fits points/play; CSV drive fields
        # remain real CFBD drives and are never replaced with invented values.
        frame['offensive_drives']=frame.offensive_plays
        frame['defensive_drives']=frame.defensive_plays
        return frame

    def predict(self,observations,home_team,away_team,as_of,neutral=False):
        history=self._history(observations,as_of)
        if any(history.team.eq(t).sum()<3 for t in [home_team,away_team]):
            raise PPPError('Each college school needs at least 3 completed games for ELO.')
        result=super().predict(observations,home_team,away_team,as_of,neutral)
        result.diagnostics.update(sport='NCAAF',opportunity_unit='offensive plays',
            limitations='College selected-school cohort, total scoring/play, college-only calibration; no conference hierarchy or roster adjustments.')
        return NCAAFPPPForecast(**asdict(result))
