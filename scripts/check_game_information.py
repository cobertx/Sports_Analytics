"""Offline per-game news, roof/forecast semantics and nonblocking Dash checks."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys,tempfile
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from sports_analytics.dashboard import create_app
from sports_analytics.information import InformationService,news_items
from sports_analytics.game_information import (ADDITIONAL_INFORMATION,GameInformationService,
    scope,relevant_news,weather_point,kickoff_weather,roof)
from sports_analytics.game_information_view import conditions,contents
from sports_analytics.data import DataSourceError
from sports_analytics.schedule import scheduled_games
from check_information import article,forecasts,NOW
from check_kalshi import components


def game(sport='NFL',identifier='123'):
    away,home=('Baltimore Ravens','Buffalo Bills') if sport=='NFL' else ('Boston Celtics','Brooklyn Nets')
    return dict(id=identifier,sport=sport,start='2026-10-24T20:30:00Z',time_tbd=False,
        away=dict(id='33' if sport=='NFL' else '2',name=away,abbreviation='BAL' if sport=='NFL' else 'BOS'),
        home=dict(id='2' if sport=='NFL' else '17',name=home,abbreviation='BUF' if sport=='NFL' else 'BKN'),
        venue='Test Stadium',venue_details=dict(id='50',fullName='Test Stadium',indoor=False,
            address=dict(city='Orchard Park',state='NY',country='USA')))


def summary(g):
    return dict(header=dict(id=g['id'],competitions=[dict(venue=g['venue_details'],competitors=[
        dict(homeAway=side,team=dict(id=g[side]['id'],displayName=g[side]['name'])) for side in ('away','home')])]),
        gameInfo=dict(venue=g['venue_details']))


class GameInformationChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=NOW;self.client=Mock();self.g=game()
        self.info=InformationService(self.tmp.name,self.client,now=lambda:self.clock)
        self.addCleanup(self.info._refresh.shutdown,wait=True)
        self.service=GameInformationService(self.info)
        def source(url,**kwargs):
            if url.endswith('/summary'):return summary(self.g)
            if url.endswith('/news'):
                return dict(articles=[article('Ravens sign Test Player','1','Baltimore Ravens'),
                    article('Bills quarterback ruled out with injury','2','Buffalo Bills'),
                    article('Celtics waive Test Player','3','Boston Celtics')])
            if '/points/' in url:return dict(properties=dict(forecastHourly='https://api.weather.gov/gridpoints/BUF/1,2/forecast/hourly'))
            if '/forecast/hourly' in url:return forecasts()
            if '/alerts/active' in url:return dict(features=[])
            raise AssertionError('Unexpected source '+url)
        self.client.json.side_effect=source
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external HTTP'))
        network.start();self.addCleanup(network.stop)

    def settle(self):self.info._refresh.tasks.join()

    def loaded(self,news=True):
        self.service.load(self.g,news=news);self.settle()
        packet=self.service.load(self.g,news=news);self.settle()
        return self.service.load(self.g,news=news)

    def app(self):
        source=Mock(path=Path(self.tmp.name)/'app.sqlite3');market=Mock();market.history.return_value=[]
        app=create_app(service=source,information_service=self.info,game_information_service=self.service,
                       nfl_market_service=market,nba_market_service=market,ncaaf_market_service=market)
        callbacks={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback')}
        return app,callbacks,market

    def test_both_team_feeds_and_league_feed_are_deduplicated_and_game_scoped(self):
        packet=self.loaded()
        self.assertFalse(packet['pending']);self.assertEqual(len(packet['items']),2)
        self.assertEqual({i['matched_teams'][0] for i in packet['items']},{'Baltimore Ravens','Buffalo Bills'})
        calls=[c for c in self.client.json.call_args_list if c.args[0].endswith('/news')]
        self.assertEqual(len(calls),3)
        self.assertEqual({c.kwargs['params'].get('team') for c in calls},{None,'33','2'})
        self.assertEqual(packet['venue']['indoor'],False)
        self.assertIn('Not domed',str(conditions(packet,self.g)))
        self.assertIn('Kickoff weather',str(conditions(packet,self.g)))
        self.assertIn('65°',str(conditions(packet,self.g)))
        self.assertIn('area forecast',str(conditions(packet,self.g)))
        shown=str(contents(packet,self.g,NOW))
        self.assertIn('Injury News',shown);self.assertIn('Roster News',shown)
        self.assertNotIn('Celtics waive',shown)

    def test_cold_load_returns_immediately_and_deduplicates_background_fetches(self):
        entered,release=Event(),Event();self.addCleanup(release.set)
        original=self.client.json.side_effect
        def blocked(url,**kwargs):entered.set();release.wait(3);return original(url,**kwargs)
        self.client.json.side_effect=blocked
        before=perf_counter();first=self.service.load(self.g,news=True)
        self.assertLess(perf_counter()-before,.5);self.assertTrue(first['pending']);self.assertTrue(entered.wait(1))
        self.service.load(self.g,news=True);release.set();self.settle()
        self.loaded();calls=[c for c in self.client.json.call_args_list if c.args[0].endswith('/summary')]
        self.assertEqual(len(calls),1)

    def test_restart_reads_local_news_and_weather_without_network_or_timestamp_changes(self):
        packet=self.loaded();self.client.json.reset_mock()
        second=InformationService(self.tmp.name,self.client,now=lambda:self.clock)
        self.addCleanup(second._refresh.shutdown,wait=True)
        restored=GameInformationService(second).load(self.g,news=True)
        self.assertEqual(restored,packet);self.client.json.assert_not_called()

    def test_failed_refresh_keeps_saved_reports_and_original_times(self):
        packet=self.loaded();stamp=packet['news'][0]['cached']['fetched_at']
        self.clock+=timedelta(minutes=11);self.client.json.side_effect=DataSourceError('Source temporarily unavailable')
        self.service.load(self.g,news=True);self.settle();stale=self.service.load(self.g,news=True)
        self.assertEqual(stale['items'],packet['items']);self.assertEqual(stale['news'][0]['cached']['fetched_at'],stamp)
        self.assertTrue(stale['errors']);self.assertIn('refresh overdue',str(conditions(stale,self.g)))

    def test_id_tags_and_full_names_match_without_city_only_false_matches(self):
        g=game('NBA');first=article('Test Player injury update','4','Unmatched source spelling')
        first['categories'][0].update(id=9999,teamId=2)
        unrelated=article('Los Angeles player is out','5','Los Angeles Lakers')
        items=news_items(dict(articles=[first,unrelated]))
        packet=dict(scope=dict(sport='NBA'),cached=dict(data=dict(items=items)))
        self.assertEqual(relevant_news([packet],g)[0]['matched_teams'],['Boston Celtics'])
        self.assertEqual(len(relevant_news([packet],g)),1)
        self.assertEqual(items[0]['team_ids'],['2'])
        self.assertEqual(relevant_news([packet],game()),[])  # IDs overlap between sports.

    def test_wrong_event_or_team_metadata_never_changes_venue(self):
        original=self.client.json.side_effect
        def wrong(url,**kwargs):
            value=original(url,**kwargs)
            if url.endswith('/summary'):value['header']['id']='999'
            return value
        self.client.json.side_effect=wrong
        self.service.load(self.g);self.settle();packet=self.service.load(self.g)
        self.assertIn('another game',' '.join(packet['errors']))
        self.assertEqual(packet['venue'],self.g['venue_details'])

    def test_forecast_hour_matches_kickoff_and_never_uses_nearest_unrelated_hour(self):
        packet=self.loaded();hour=kickoff_weather(packet['weather'],self.g)
        self.assertEqual(hour['temperature'],65)
        later={**self.g,'start':'2026-11-07T20:30:00Z'}
        self.assertIsNone(kickoff_weather(packet['weather'],later))
        packet['scope']=scope(later)
        self.assertIn('outside the available hourly forecast window',str(conditions(packet,later)))
        self.assertIsNone(kickoff_weather(packet['weather'],{**self.g,'time_tbd':True}))

    def test_roof_unknown_closed_retractable_and_foreign_venues_are_explicit(self):
        self.assertIn('Domed',roof(dict(roof_type='dome')))
        self.assertIn('position not reported',roof(dict(roof_type='retractable')))
        self.assertIn('not reported',roof({}))
        self.assertIsNone(weather_point(dict(address=dict(city='London',country='England'))))
        packet=self.loaded();packet['venue']['indoor']=True
        self.assertIn('indoor playing conditions can differ',str(conditions(packet,self.g)))

    def test_additional_tab_exists_without_odds_and_never_fits_a_model(self):
        app,cb,market=self.app()
        for sport in ('NBA','NFL'):
            tabs,value,status=cb['snapshot_categories'](None,f'#{sport.lower()}-game-123',None)
            self.assertEqual([t.value for t in tabs],[ADDITIONAL_INFORMATION]);self.assertEqual(value,ADDITIONAL_INFORMATION)
            market.history.reset_mock()
            self.assertEqual(cb['show_saved_markets'](ADDITIONAL_INFORMATION,None,f'#{sport.lower()}-game-123',None,'PPP','WHP',None,'market'),([],no_update))
            market.history.assert_not_called()
        self.assertEqual(cb['snapshot_categories'](None,'#ncaaf-game-123',None)[:2],([],None))
        self.assertEqual(cb['mispricing_visibility'](ADDITIONAL_INFORMATION),({'display':'none'},{'display':'none'}))
        with app.server.test_client() as client:
            self.assertEqual(client.get('/_dash-layout').status_code,200)
            self.assertEqual(client.get('/_dash-dependencies').status_code,200)
            self.assertEqual(client.get('/assets/game_information.css').status_code,200)

    def test_callbacks_skip_other_pages_and_hide_previous_game_reports(self):
        _,cb,_=self.app();packet=self.loaded()
        for page,location in [('kalshi','#nfl-kalshi-market'),('historical','#historical-team'),('market','#nfl-game-999')]:
            with self.assertRaises(PreventUpdate):cb['load_game_information'](self.g,page,location,ADDITIONAL_INFORMATION,0,0,None)
        other=game(identifier='999')
        shown=cb['game_header_conditions'](packet,other,'market','#nfl-game-999')
        self.assertIn('Loading',str(shown));self.assertNotIn('65°',str(shown))
        self.assertEqual(cb['game_header_conditions'](packet,game('NBA'),'market','#nba-game-123'),[])
        with patch('sports_analytics.game_information_view.ctx',SimpleNamespace(triggered_id='game-information-poll')):
            self.assertIs(cb['load_game_information'](self.g,'market','#nfl-game-123',ADDITIONAL_INFORMATION,0,1,packet),no_update)

    def test_schedule_preserves_actual_event_venue_and_team_identifiers(self):
        g=self.g
        competition=dict(id=g['id'],date=g['start'],status=dict(type=dict(state='pre')),venue=g['venue_details'],
            competitors=[dict(homeAway=side,team=dict(id=g[side]['id'],displayName=g[side]['name'],abbreviation=g[side]['abbreviation'])) for side in ('home','away')])
        event=dict(id=g['id'],date=g['start'],competitions=[competition])
        result=scheduled_games([event],NOW,NOW+timedelta(days=7))[0]
        self.assertEqual(result['venue_details'],g['venue_details']);self.assertEqual(result['home']['id'],'2')


if __name__=='__main__':main()
