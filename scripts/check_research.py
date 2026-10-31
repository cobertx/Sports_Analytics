"""Offline research parameter, navigation and real replay integration checks."""
from dataclasses import asdict,replace
from pathlib import Path
import sys,tempfile,json
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_backtesting import team_history
from scripts.callback_fixtures import callback_functions
from sports_analytics.backtesting_service import BacktestingService,estimation_history,game_model
from sports_analytics.backtesting_view import CATALOG
from sports_analytics.research_config import default_config,configured,parameter_fields,form_parameters
from sports_analytics import research_view
from sports_analytics.dashboard import create_app


def player_inputs():
    players=[];teams=[]
    for i,date in enumerate(pd.date_range('2025-08-01',periods=35,freq='3D',tz='UTC')):
        meta=dict(game_id=str(i),event_date=date,available_at=date+pd.Timedelta(hours=8),season_type='regular')
        home,away=110+i%9,105+i%6
        for club,other,is_home,points,allowed in [('BOS','DET',1,home,away),('DET','BOS',0,away,home)]:
            teams.append(dict(**meta,team=club,opponent=other,is_home=is_home,entity_id=club,entity_name=club,
                pts=points,points=points,points_allowed=allowed,plus_minus=points-allowed,min=240.,
                fga=90+i%3,fg3a=30,fgm=44,fg3m=12,fta=20,ftm=16,oreb=10,dreb=30,ast=25,stl=8,blk=5,tov=12))
        for name,team,other,offset in [('Player A','BOS','DET',10),('Player B','DET','BOS',100),('Player C','BOS','DET',40)]:
            players.append(dict(**meta,entity_id=name,entity_name=name,team=team,opponent=other,
                pts=offset+i%5,min=34.,fga=20,fg3a=7,fgm=10,fg3m=3,fta=5,ftm=4,oreb=2,dreb=5,ast=7,stl=1,blk=1,tov=2))
    return pd.DataFrame(players),pd.DataFrame(teams)


def callbacks_for(service):
    app=create_app(backtesting_service=service)
    callbacks=callback_functions(app)
    return app,callbacks


class ResearchChecks(TestCase):
    def test_every_configuration_leaf_is_editable_and_round_trips_for_all_sports(self):
        for sport in ['NBA','NFL','NCAAF']:
            for model in CATALOG:
                default=default_config(sport,model.group,model.key)
                editor=research_view.editor(sport,model.group,model.key)
                ids=[row.children[1].id for row in editor];values=[row.children[1].value for row in editor]
                self.assertEqual({i['path'] for i in ids},{path for path,_,_ in parameter_fields(default)})
                self.assertEqual(form_parameters(sport,model.group,model.key,ids,values),asdict(default))
                self.assertTrue(all(path.split('.')[-1] in research_view.HELP for path,_,_ in parameter_fields(default)))
                self.assertEqual(research_view.page(sport,f'#research-{model.group}-{model.key}').to_plotly_json()['props']['data-model'],model.key)
                self.assertEqual(len(research_view.METHODS[model.key]),4)

    def test_invalid_values_nested_settings_and_stale_forms_are_rejected(self):
        cfg=default_config('NBA','player-stats','oam')
        changed=configured(cfg,dict(baseline=dict(simulations=1000,seed=7,role_window=8),defense_prior=12))
        self.assertEqual((changed.baseline.simulations,changed.baseline.role_window,changed.defense_prior),(1000,8,12))
        self.assertEqual(cfg.baseline.simulations,12000)
        for override in [dict(unknown=1),dict(baseline=dict(seed=True)),dict(baseline=dict(simulations=1000.5)),
                         dict(baseline=dict(role_half_life=float('nan'))),dict(factor_floor=1.2),
                         dict(baseline=dict(role_window=1000,efficiency_window=5))]:
            with self.assertRaises(ValueError):configured(cfg,override)
        with self.assertRaises(ValueError):configured(default_config('NBA','game-outcome','elo'),dict(sport='NFL'))
        with self.assertRaises(ValueError):form_parameters('NBA','player-stats','whp',[],[])

    def test_workspace_navigation_scope_and_team_changes_keep_parameter_edits(self):
        from dash import no_update
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory)
            players,_=player_inputs();service.player_inputs=lambda sport:players
            app,callbacks=callbacks_for(service)
            self.assertEqual(callbacks['restore_game_route']('#research-player-stats-whp'),('RESEARCH','backtesting'))
            self.assertIsNone(research_view.route('#research-game-outcome-../../bad'))
            self.assertEqual(len(research_view.navigation('NBA','#research-player-stats-whp')),2)
            self.assertEqual(callbacks['navigate']('historical','RESEARCH')[-1],{'display':'block'})
            visible=callbacks['research_visibility']('RESEARCH','#research-player-stats-whp')
            self.assertTrue(all(value=={'display':'none'} for value in visible[:3]))
            self.assertEqual(visible[-1],{'display':'block'})
            configured_page=callbacks['configure_backtest']('RESEARCH','#research-player-stats-whp','backtesting',None,'NBA','BOS')
            context=configured_page[0]
            self.assertEqual(context,dict(sport='NBA',group='player-stats',model='whp',workspace='research',team_filter='BOS'))
            self.assertEqual(configured_page[2],'__all__')
            self.assertIn(dict(label='All players',value='__all__'),configured_page[1])
            with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='backtest-context')):
                initial=callbacks['research_parameters'](context,0,None)
                retained=callbacks['research_parameters']({**context,'team_filter':'DET'},0,initial[2])
                self.assertIs(retained[0],no_update)
            with app.server.test_client() as client:
                layout=client.get('/_dash-layout').get_json()
                self.assertIn('Strategy & Research',json.dumps(layout))
                self.assertEqual(client.get('/_dash-dependencies').status_code,200)

    def test_edited_game_parameters_reach_fits_change_forecasts_and_save_independently(self):
        frame=team_history().assign(season_type='regular')
        with tempfile.TemporaryDirectory() as directory,patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
            service=BacktestingService(root=directory,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
            service.game_inputs=lambda *args:(frame,None)
            common=dict(minimum_training_games=6,minimum_team_games=1,minimum_calibration_games=4,maximum_calibration_games=8)
            args=dict(sport='NCAAF',group='game-outcome',key='ppp',entity='A',limit=2,workspace='research')
            first=service.run(**args,config_overrides=dict(common,half_life_days=8,ridge_penalty=4))
            second=service.run(**args,config_overrides=dict(common,half_life_days=180,ridge_penalty=80))
            self.assertNotEqual(first['run_id'],second['run_id'])
            self.assertNotEqual(first['records'][0]['predicted'],second['records'][0]['predicted'])
            self.assertEqual(first['config']['half_life_days'],8)
            for row in first['records']:
                pair=frame[frame.game_id.eq(row['game_id'])]
                h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
                model=game_model('NCAAF','ppp');model.config=configured(model.config,first['config'])
                prior=estimation_history(frame,h.event_date,paired=True)
                expected=model.predict(prior,h.team,a.team,h.event_date.isoformat(),False)
                self.assertEqual(expected.margin_mean,row['predicted'])
            restored=service.saved('NCAAF','game-outcome','ppp',first['run_id'])
            self.assertEqual(restored['config'],first['config'])
            self.assertEqual(restored['context']['workspace'],'research')
            self.assertEqual(game_model('NCAAF','ppp').config.half_life_days,180)

    def test_all_player_and_team_cohorts_keep_histories_separate_and_limit_distinct_games(self):
        players,_=player_inputs()
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
            service.player_inputs=lambda sport:players
            overrides=dict(window=8,half_life=2,minimum_games=3,minimum_calibration=4,maximum_calibration=8,prior_strength=0)
            args=dict(sport='NBA',group='player-stats',key='whp',entity='__all__',market='player_points',limit=3,
                      workspace='research',config_overrides=overrides,lookback='Model Default')
            all_players=service.run(**args)
            team=service.run(**args,team_filter='BOS')
            self.assertEqual(len(all_players['records']),9);self.assertEqual(len(team['records']),6)
            self.assertEqual(len({r['game_id'] for r in all_players['records']}),3)
            self.assertEqual(service.available_games('NBA','player-stats','whp','__all__','player_points',team_filter='BOS'),35)
            for record in all_players['records']:
                offset=10 if record['label'].startswith('Player A') else 100 if record['label'].startswith('Player B') else 40
                self.assertTrue(offset<=record['predicted']<=offset+4,record)
                self.assertLess(pd.Timestamp(record['latest_training'],tz='UTC'),pd.Timestamp(record['date']))
                from models.Player_Stats.Weighted_Historical_Performance.core import WeightedHistoricalPerformance
                own=players[players.entity_name.eq(record['label'].split(' · ')[0])].assign(value=lambda f:f.pts,opportunity=lambda f:f['min'])
                reference=WeightedHistoricalPerformance(configured(default_config('NBA','player-stats','whp'),overrides)).forecast(own,pd.Timestamp(record['date']))
                self.assertEqual(reference.mean,record['predicted'])
            self.assertTrue(all(not r['label'].startswith('Player B') for r in team['records']))
            self.assertNotEqual(team['run_id'],all_players['run_id'])

    def test_nested_parameters_reach_real_uo_oam_pge_and_matchups_simulate_once(self):
        players,teams=player_inputs()
        from models.Player_Stats.Usage_Opportunity.prediction_service import UsageOpportunityMarketService
        from models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service import OpponentAdjustedMarketService
        from models.Player_Stats.Pace_Game_Environment.prediction_service import PaceGameEnvironmentMarketService
        with tempfile.TemporaryDirectory() as directory,patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
            for key,kind in [('uo',UsageOpportunityMarketService),('oam',OpponentAdjustedMarketService),('pge',PaceGameEnvironmentMarketService)]:
                repository=SimpleNamespace(root=Path(directory)/'cache'/key,load=lambda *args:(players,teams))
                engine=kind(repository=repository);forecast=engine.forecast;calls=[]
                def capture(snapshot):
                    result=forecast(snapshot);calls.append((snapshot,result));return result
                engine.forecast=capture
                service=BacktestingService(root=Path(directory)/key,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
                service.player_inputs=lambda sport:players
                service.game_inputs=lambda *args:(teams,None)
                service.player_service=lambda _:engine
                service.phase_map=lambda _:{}
                base=dict(simulations=1000,seed=42,role_window=8,team_window=8,efficiency_window=12,minimum_games=3)
                config=base if key=='uo' else dict(baseline=base,league_window=16,minimum_league_games=4)
                if key=='oam':config.update(defense_window=8,minimum_defense_games=2,factor_ceiling=1.5)
                result=service.run('NBA','player-stats',key,'__all__','player_points',limit=2,
                                   workspace='research',config_overrides=config)
                self.assertEqual(len(calls),2,key)
                self.assertEqual(len(result['records']),6,key)
                actual=engine.config.baseline if hasattr(engine.config,'baseline') else engine.config
                self.assertEqual((actual.simulations,actual.seed,actual.role_window),(1000,42,8))
                for snapshot,report in calls:
                    self.assertEqual(len(snapshot['rows']),3)
                    self.assertTrue(report.forecasts,report.errors)
                    self.assertTrue(all(f.simulations==1000 for f in report.forecasts.values()))

    def test_dash_research_submission_validates_parameters_and_namespaces_saved_page(self):
        from dash import no_update
        with tempfile.TemporaryDirectory() as directory:
            service=BacktestingService(root=directory);app,callbacks=callbacks_for(service)
            context=dict(sport='NBA',group='player-stats',model='whp',workspace='research',team_filter='BOS')
            controls=research_view.editor('NBA','player-stats','whp')
            ids=[row.children[1].id for row in controls];values=[row.children[1].value for row in controls]
            values[next(i for i,item in enumerate(ids) if item['path']=='window')]=12
            common=['__all__','player_points',None,None,10,5,None,'exclude','Model Default',None,
                    [dict(label='All players',value='__all__')],{}]
            with patch.object(service,'start',return_value='research-job') as start, \
                    patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='backtest-run')):
                result=callbacks['run_backtest'](1,context,0,*common,None,None,values,ids)
                kwargs=start.call_args.kwargs
                self.assertEqual(kwargs['config_overrides']['window'],12)
                self.assertEqual((kwargs['workspace'],kwargs['team_filter']),('research','BOS'))
                self.assertIn('research/NBA/player-stats/whp',result[8])
                run_key=next(key for key,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='run_backtest')
                metadata=app.callback_map[run_key]
                inputs={'backtest-run':1,'backtest-context':context,'backtest-poll':0,'backtest-history-open':0}
                state_values=[*common,None,values,ids]
                self.assertEqual(len(state_values),len(metadata['state']))
                payload=dict(output=run_key,outputs=[dict(id=o.component_id,property=o.component_property) for o in metadata['output']],
                    inputs=[dict(**item,value=inputs[item['id']]) for item in metadata['inputs']],
                    state=[dict(**item,value=value) for item,value in zip(metadata['state'],state_values)],
                    changedPropIds=['backtest-run.n_clicks'])
                with app.server.test_client() as client:
                    response=client.post('/_dash-update-component',json=payload)
                    self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                    self.assertIn('research/NBA/player-stats/whp',response.get_json()['response']['backtest-pages']['data'])
                self.assertEqual(start.call_args.kwargs['config_overrides']['window'],12)
                values[next(i for i,item in enumerate(ids) if item['path']=='window')]=1.5
                start.reset_mock()
                invalid=callbacks['run_backtest'](1,context,0,*common,None,None,values,ids)
                start.assert_not_called();self.assertIs(invalid[0],no_update)
                self.assertIn('whole number',invalid[1])


if __name__=='__main__':main()
