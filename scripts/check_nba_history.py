"""Current NBA season, single appearances and fast prompt-to-history loading."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta,timezone
from io import StringIO
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError,Query,fetch
from sports_analytics.nba_espn import load,identities,IDENTITIES
from sports_analytics.service import DataService
from sports_analytics.dashboard import create_app,ALL_TEAMS
from sports_analytics.prompt_history import button
from sports_analytics.career import CareerService

SEASON=date.today().year-(date.today().month<7)
REGISTRY="""players = [[201935,'Harden','James','James Harden',True],
                       [9090,'Player','Other','Other Player',True]]
teams = [[1610612739,'CLE'],[1610612755,'PHI']]
raise RuntimeError('Registry source must never execute')
"""


def boxes():
    base=dict(game_id='401909093',season=str(SEASON+1),season_type='2',game_date=f'{SEASON}-10-22',
        game_date_time=f'{SEASON}-10-22T19:00:00-0400',athlete_id='3992',athlete_display_name='James Harden',
        athlete_position_abbreviation='G',team_id='5',team_abbreviation='CLE',team_display_name='Cleveland Cavaliers',
        opponent_team_id='20',opponent_team_abbreviation='PHI',team_winner='false',home_away='away',
        team_score='106',opponent_team_score='114',did_not_play='false',minutes='33',points='16',rebounds='3',
        assists='5',steals='1',blocks='0',turnovers='6',fouls='2',plus_minus='-17',field_goals_made='5',
        field_goals_attempted='12',three_point_field_goals_made='3',three_point_field_goals_attempted='6',
        free_throws_made='3',free_throws_attempted='4',offensive_rebounds='0',defensive_rebounds='3')
    return pd.DataFrame([base,dict(base,athlete_id='9999',athlete_display_name='Other Player',team_id='20',
        team_abbreviation='PHI',team_display_name='Philadelphia 76ers',opponent_team_id='5',opponent_team_abbreviation='CLE',
        team_winner='true',home_away='home',team_score='114',opponent_team_score='106')])


def client_for(frame,registry=REGISTRY):
    client=Mock()
    def get(url):
        if url==IDENTITIES:
            if isinstance(registry,Exception):raise registry
            return SimpleNamespace(text=registry)
        return SimpleNamespace(text=frame.to_csv(index=False))
    client.get.side_effect=get
    return client


class NBASeasonHistoryChecks(TestCase):
    def setUp(self):
        self.query=Query('NBA',SEASON,'player')
        identities.cache_clear()

    def test_first_game_normalization_keeps_nba_identity_exact_stats_and_zeroes(self):
        source=load(self.query,client_for(boxes()))
        player=source[source.entity_name.eq('James Harden')].iloc[0]
        self.assertEqual((player.entity_id,player.game_id,player.team,player.opponent),('201935','espn:401909093','CLE','PHI'))
        self.assertEqual((player.pts,player.reb,player.ast,player.blk),(16,3,5,0))
        self.assertEqual(player.fg_pct,5/12);self.assertEqual(player.fg3_pct,.5)
        self.assertEqual(player.event_date,pd.Timestamp(f'{SEASON}-10-22T23:00:00Z'))
        self.assertEqual(player.matchup,'CLE @ PHI')
        self.assertEqual(source.season.unique().tolist(),[SEASON])
        self.assertEqual(source.attrs['source'],'ESPN box scores via SportsDataverse')

    def test_dnp_preseason_wrong_year_and_incomplete_games_are_not_appearances(self):
        raw=boxes()
        extra=[raw.assign(game_id='preseason',season_type='1'),raw.assign(game_id='other-year',season=str(SEASON)),
               raw.assign(game_id='live',team_winner='false'),raw.iloc[:1].assign(game_id='missing-side'),
               raw.iloc[:1].assign(athlete_id='not-played',minutes='0',did_not_play='true')]
        source=load(self.query,client_for(pd.concat([raw,*extra],ignore_index=True)))
        self.assertEqual(len(source),2);self.assertEqual(set(source.game_id),{'espn:401909093'})
        with self.assertRaises(DataSourceError):load(self.query,client_for(raw.assign(season_type='1')))

    def test_team_scores_and_postseason_use_exact_phase_and_same_team_identity(self):
        raw=boxes().rename(columns={'home_away':'team_home_away'}).assign(season_type='3')
        source=load(Query('NBA',SEASON,'team','postseason'),client_for(raw))
        cle=source[source.team.eq('CLE')].iloc[0]
        self.assertEqual((cle.entity_id,cle.pts,cle.plus_minus),('1610612739',106,-8))
        self.assertEqual(source.season_type.unique().tolist(),['postseason'])

    def test_missing_or_ambiguous_crosswalk_never_assigns_a_different_players_nba_id(self):
        for registry in [DataSourceError('Registry offline'),REGISTRY.replace("[9090,'Player','Other','Other Player',True]",
            "[9090,'Harden','James','James Harden',True]")]:
            source=load(self.query,client_for(boxes(),registry))
            self.assertEqual(source[source.entity_name.eq('James Harden')].iloc[0].entity_id,'espn:3992')
        with tempfile.TemporaryDirectory() as directory:
            career=CareerService(Path(directory)/'career.sqlite3')
            with patch.object(career,'available_seasons') as seasons:
                with self.assertRaises(DataSourceError):career.load('NBA','espn:3992','James Harden','pts','regular')
                seasons.assert_not_called()

    def test_current_season_uses_published_boxes_before_slow_nba_stats(self):
        client=client_for(boxes())
        with patch('sports_analytics.data.load_nba',side_effect=AssertionError('Blocked endpoint must not be needed')):
            source=fetch(self.query,client)
        self.assertEqual(source.entity_name.eq('James Harden').sum(),1)
        self.assertTrue(client.get.call_args_list[0].args[0].endswith(f'player_box_{SEASON+1}.csv'))

    def test_original_nba_endpoint_remains_a_fallback(self):
        expected=pd.DataFrame(dict(pts=[1]))
        with patch('sports_analytics.nba_espn.load',side_effect=DataSourceError('Archive not yet published')), \
             patch('sports_analytics.data.load_nba_archive',side_effect=DataSourceError('NBA archive absent')), \
             patch('sports_analytics.data.load_nba',return_value=expected) as direct:
            self.assertIs(fetch(self.query,Mock()),expected)
        direct.assert_called_once()

    def test_simultaneous_page_controls_share_one_download_and_current_cache_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            service=DataService(Path(directory)/'sports.sqlite3')
            source=load(self.query,client_for(boxes()));entered=Event();release=Event()
            def download(query):
                entered.set()
                if not release.wait(5):raise AssertionError('Download was not released')
                return source
            with patch('sports_analytics.service.fetch',side_effect=download) as request,ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(service.load,self.query)
                self.assertTrue(entered.wait(5))
                second=pool.submit(service.load,self.query);release.set()
                self.assertEqual(first.result().revision,second.result().revision)
                request.assert_called_once_with(self.query)
            old=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
            with service.connect() as db:db.execute('UPDATE datasets SET fetched_at=?',(old,))
            with patch('sports_analytics.service.fetch',side_effect=DataSourceError('Offline')):
                stale=service.load(self.query)
            self.assertEqual(stale.status,'stale cache');self.assertEqual(len(stale.frame),2)
            with patch('sports_analytics.service.fetch',return_value=source) as request:
                service.load(self.query);service.load(self.query);service.load(self.query,refresh=True)
                self.assertEqual(request.call_count,2)


class PromptLandingChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=DataService(Path(self.tmp.name)/'sports.sqlite3')
        self.source=load(Query('NBA',SEASON,'player'),client_for(boxes()))
        self.app=create_app(service=self.service,career_service=Mock())
        self.cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in self.app.callback_map.values() if m.get('callback')}
        snapshot=dict(sport='NBA',game_id='123',commence_time=f'{SEASON}-10-24T23:00:00Z')
        row=dict(subject='James Harden',selection='Over',line=15.5,market_key='player_points_alternate',market='Alt Points')
        self.href=button(snapshot,row).href
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline fixture only'))
        network.start();self.addCleanup(network.stop)

    def test_link_loads_its_season_and_player_scope_before_old_controls_settle(self):
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='workspace-page')), \
             patch('sports_analytics.service.fetch',return_value=self.source) as request:
            dataset=self.cb['load_data'](0,0,'NBA',SEASON-1,'team','postseason',None,5,'historical',None,self.href)[0]
        request.assert_called_once_with(Query('NBA',SEASON,'player'))
        entity=self.cb['select_options'](dataset,ALL_TEAMS,None,self.href)[1]
        category=self.cb['select_categories'](dataset,entity,ALL_TEAMS,None,None,self.href)[1]
        metric=self.cb['select_metrics'](dataset,entity,ALL_TEAMS,category,None,self.href)[1]
        figure,cards,records,_=self.cb['analyze'](dataset,entity,metric,5,ALL_TEAMS,self.href,'historical')
        self.assertEqual((entity,metric),('201935','pts'))
        self.assertEqual(len(records),1);self.assertEqual(records[0]['pts'],16)
        self.assertEqual(records[0]['prompt_result'],'Hit');self.assertIsNone(records[0]['baseline'])
        self.assertEqual(figure.layout.shapes[0].y0,15.5)
        self.assertIn('1/1 historical hits',str(figure.layout.annotations))
        self.assertIn('Requires 5 recorded appearances',str(cards))

    def test_explicit_season_changes_are_respected_after_navigation(self):
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='season')), \
             patch('sports_analytics.service.fetch',return_value=self.source) as request:
            self.cb['load_data'](0,0,'NBA',SEASON-1,'player','regular',None,None,'historical',None,self.href)
        request.assert_called_once_with(Query('NBA',SEASON-1,'player'))

    def test_source_failure_reports_an_error_instead_of_remaining_loading_forever(self):
        status=self.cb['historical_market_status'](None,None,None,self.href,'Source is offline.')
        self.assertIn('could not be loaded',status);self.assertNotIn('Loading historical data',status)

    def test_dash_http_navigation_uses_url_scope_and_current_season_cache(self):
        values={'load':0,'refresh':0,'historical-cache-poll':0,'sport':'NBA','season':SEASON-1,'entity-type':'team','season-type':'postseason',
                'college-team':None,'week':5,'workspace-page':'historical','dataset':None,'market-url':self.href}
        key,meta=next((key,m) for key,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='load_data')
        with patch('sports_analytics.service.fetch',return_value=self.source) as request,self.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=['market-url.hash']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        dataset=response.get_json()['response']['dataset']['data']
        self.assertEqual((dataset['season'],dataset['kind']),(SEASON,'player'))
        request.assert_called_once_with(Query('NBA',SEASON,'player'))


if __name__=='__main__':main()
