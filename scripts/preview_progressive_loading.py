"""Offline browser fixture: main history renders while career work is held.

Run with the project Python, then open http://127.0.0.1:8052/#historical-player.
Use Release career to complete the controlled source request. No live API pulls.
"""
import argparse
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import Input,Output,html
from check_performance import PagePerformanceChecks
from check_mispricing import components


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8052)
    args=parser.parse_args()
    fixture=PagePerformanceChecks();fixture.setUp();release=Event()
    def career(*args):
        release.wait()
        return SimpleNamespace(frame=pd.DataFrame(dict(season=[2024,2025],value=[10.,20.],team=['BOS','BOS'])),
            phase='regular',source='offline fixture',searched=[2024,2025],failed=[])
    fixture.career.load.side_effect=career
    for component in components(fixture.app.layout):
        identity=getattr(component,'id',None)
        if identity=='season':component.value=2025
        if identity=='entity-type':component.value='player'
    fixture.app.layout.children.insert(0,html.Div([
        html.Button('Release career',id='fixture-release',n_clicks=0),
        html.Span('Offline fixture · career source held',id='fixture-status',role='status'),
    ],style={'position':'fixed','right':'20px','top':'12px','zIndex':1000,'background':'#111d2e','padding':'10px'}))
    @fixture.app.callback(Output('fixture-status','children'),Input('fixture-release','n_clicks'),prevent_initial_call=True)
    def finish(clicks):
        release.set()
        return 'Offline fixture · career released'
    try:fixture.app.run(host='127.0.0.1',port=args.port,debug=False)
    finally:
        release.set()
        fixture.app._career_tasks.queue.shutdown()
        fixture.app._page_tasks.queue.shutdown()
        fixture.doCleanups()


if __name__=='__main__':main()
