"""Scoped college CSV history -> separately fitted PPP/EPA/ELO market estimates."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import threading
import pandas as pd
from .data import DataSourceError
from .ncaaf_model_data import NcaafModelData
from .nfl_model_data import atomic_text
from models.Game_Outcome.PPP.NCAAF_PPP import NCAAFPPPModel,NCAAFPPPForecast
from models.Game_Outcome.EPA.NCAAF_EPA import NCAAFEPAModel
from models.Game_Outcome.ELO.NCAAF_ELO import NCAAFELOModel


class NCAAFMarketForecastService:
    def __init__(self,method='PPP',data=None,model=None):
        self.method=method
        self.data=data or NcaafModelData()
        self.model=model or {'PPP':NCAAFPPPModel,'EPA':NCAAFEPAModel,'ELO':NCAAFELOModel}[method]()
        self._lock=threading.RLock()
        self._memory={}

    def forecast(self,snapshot):
        times=[pd.Timestamp(snapshot[k]) for k in ['pulled_at','commence_time']]
        if any(pd.isna(t) or t.tzinfo is None for t in times):raise DataSourceError('College snapshot/start timestamps require a timezone.')
        cutoff=min(times)
        with self._lock:
            names=[snapshot['home_team'],snapshot['away_team']]
            frame,notices=self.data.prepare(names,snapshot['commence_time'])
            home,away=[self.data.resolve_school(t) for t in names]
            for c in ['event_date','available_at']:frame[c]=pd.to_datetime(frame[c],utc=True)
            frame=frame[frame.event_date.lt(cutoff.normalize()) & frame.available_at.lt(cutoff)].reset_index(drop=True)
            root=Path(__file__).resolve().parents[1]
            files=[Path(__file__),root/'sports_analytics/ncaaf_model_data.py',root/'models/season_weights.py',
                   *[root/p for p in ['models/Game_Outcome/PPP/PPP.py','models/Game_Outcome/PPP/NCAAF_PPP.py',
                     'models/Game_Outcome/PPP/NBA_PPP.py','models/Game_Outcome/EPA/NCAAF_EPA.py',
                     'models/Game_Outcome/ELO/NCAAF_ELO.py','models/Game_Outcome/ELO/ELO.py']]]
            signature=sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
            identity=dict(version=1,method=self.method,config=asdict(self.model.config),home=home,away=away,
                cutoff=cutoff.isoformat(),neutral=bool((snapshot.get('game') or {}).get('neutral',False)),
                implementation=signature,input_sha256=sha256(frame.to_json(orient='table',date_format='iso',index=False).encode()).hexdigest())
            key=sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            if key in self._memory:return self._memory[key]
            path=self.data.directory/'forecasts'/f'{self.method.lower()}_{key}.json'
            if path.exists():
                try:
                    saved=json.loads(path.read_text(encoding='utf-8'))
                    if saved['identity']==identity:
                        forecast=NCAAFPPPForecast(**saved['forecast']);self._memory[key]=forecast;return forecast
                except (ValueError,KeyError,TypeError):pass
            try:forecast=self.model.predict(frame,home,away,cutoff.isoformat(),neutral=identity['neutral'])
            except (ValueError,RuntimeError) as exc:raise DataSourceError(f'NCAAF {self.method} estimate unavailable. {exc}') from None
            forecast.diagnostics.update(forecast_method=self.method,team_aliases=dict(zip(names,[home,away])),
                source_notices=notices,history_directory='data/modeling/ncaaf',snapshot_after_kickoff=times[0]>times[1])
            atomic_text(path,json.dumps(dict(identity=identity,forecast=asdict(forecast)),allow_nan=False,indent=2))
            self._memory[key]=forecast
            return forecast
