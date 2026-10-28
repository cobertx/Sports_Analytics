"""Offline information sources, cache freshness, nonblocking reads and page isolation."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.information import InformationService,PAGES,news_items,official_sources,route,time_text
from sports_analytics.information_view import news,weather
from check_kalshi import components

NOW=datetime(2026,10,24,20,tzinfo=timezone.utc)


def article(headline='Test Player ruled out with knee injury',identifier='123',team='Boston Celtics'):
    return dict(headline=headline,published='2026-10-24T18:00:00Z',links=dict(web=dict(href='https://www.espn.com/nba/story/_/id/'+identifier+'/news')),
                categories=[dict(type='team',description=team)])


def forecasts():
    return dict(properties=dict(updateTime='2026-10-24T19:30:00Z',periods=[dict(startTime='2026-10-24T20:00:00Z',endTime='2026-10-24T21:00:00Z',
        temperature=65,temperatureUnit='F',windSpeed='10 to 15 mph',windDirection='NW',shortForecast='Cloudy',probabilityOfPrecipitation={'value':None}),
        dict(startTime='2026-10-24T21:00:00Z',endTime='2026-10-24T22:00:00Z',temperature=18,temperatureUnit='C',windSpeed=None,windDirection=None,
             shortForecast='Rain',probabilityOfPrecipitation={'value':60})]))


class InformationChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.clock=NOW;self.client=Mock();self.service=InformationService(temporary.name,self.client,now=lambda:self.clock)
        self.addCleanup(self.service._refresh.shutdown,wait=True)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests in regression tests'))
        network.start();self.addCleanup(network.stop)

    def settle(self):self.service._refresh.tasks.join()

    def news_packet(self):
        self.client.json.return_value=dict(articles=[article(),article('Celtics sign Test Guard','456')])
        self.service.load('news',sport='NBA');self.settle()
        return self.service.load('news',sport='NBA')

    def weather_client(self,alerts_error=False):
        def source(url,**kwargs):
            if '/points/' in url:return dict(properties=dict(forecastHourly='https://api.weather.gov/gridpoints/IND/1,2/forecast/hourly',
                relativeLocation=dict(properties=dict(city='Indianapolis',state='IN'))))
            if '/forecast/hourly' in url:return forecasts()
            if '/alerts/active' in url:
                if alerts_error:raise DataSourceError('Alerts unavailable')
                return dict(features=[dict(id='https://api.weather.gov/alerts/example',properties=dict(headline='Test weather advisory',event='Advisory',
                    severity='Moderate',sent='2026-10-24T19:00:00Z',expires='2026-10-25T01:00:00Z'))])
            raise AssertionError('Unexpected source '+url)
        self.client.json.side_effect=source

    def test_only_attributed_matching_headlines_safe_links_and_publisher_team_tags_are_saved(self):
        items=news_items(dict(articles=[article(),article(),article('Celtics sign Test Guard','456'),article('Game recap','789'),
            dict(headline='Player injury',links=None),dict(headline='Player injury',links={'web':{'href':'javascript:alert(1)'}})]))
        self.assertEqual(len(items),2);self.assertEqual(items[0]['kinds'],['injuries']);self.assertEqual(items[1]['kinds'],['roster'])
        self.assertEqual(items[0]['teams'],['Boston Celtics'])
        self.assertEqual(set(items[0]),{'headline','url','published','kinds','teams','team_ids','source'})
        with self.assertRaises(DataSourceError):news_items(dict(error='invalid'))

    def test_news_reads_never_wait_for_http_and_identical_requests_share_one_download(self):
        entered,release=Event(),Event()
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('News source was not released')
            return dict(articles=[article()])
        self.client.json.side_effect=slow
        try:
            start=perf_counter();pending=self.service.load('news',sport='NBA');elapsed=perf_counter()-start
            self.assertLess(elapsed,1);self.assertTrue(entered.wait(2));self.assertTrue(pending['pending']);self.assertIsNone(pending['cached'])
            for _ in range(4):self.service.load('news',sport='NBA',refresh=True)
            self.assertEqual(self.client.json.call_count,1)
        finally:release.set()
        self.settle();cached=self.service.load('news',sport='NBA')
        self.assertFalse(cached['pending']);self.assertEqual(len(cached['cached']['data']['items']),1)
        restarted=InformationService(self.service.directory,self.client,now=lambda:self.clock)
        self.addCleanup(restarted._refresh.shutdown,wait=True)
        self.assertEqual(restarted.load('news',sport='NBA')['cached'],cached['cached']);self.assertEqual(self.client.json.call_count,1)

    def test_failed_updates_keep_original_timestamps_and_manual_retry_bypasses_backoff(self):
        original=self.news_packet()['cached'];self.clock+=timedelta(minutes=6)
        self.client.json.side_effect=DataSourceError('Publisher offline')
        pending=self.service.load('news',sport='NBA');self.assertEqual(pending['cached'],original);self.settle()
        failed=self.service.load('news',sport='NBA');self.assertEqual(failed['error'],'Publisher offline')
        self.assertEqual(failed['cached']['fetched_at'],original['fetched_at'])
        calls=self.client.json.call_count
        for _ in range(3):self.service.load('news',sport='NBA')
        self.assertEqual(self.client.json.call_count,calls)
        self.client.json.side_effect=None;self.client.json.return_value=dict(articles=[article('Team signs New Player')])
        self.service.load('news',sport='NBA',refresh=True);self.settle()
        refreshed=self.service.load('news',sport='NBA')
        self.assertIsNone(refreshed['error']);self.assertGreater(refreshed['cached']['fetched_at'],original['fetched_at'])

    def test_failed_manual_refresh_of_fresh_cache_remains_visible_and_invalid_json_does_not_replace_it(self):
        original=self.news_packet()['cached'];self.client.json.return_value=dict(not_articles=[])
        self.service.load('news',sport='NBA',refresh=True);self.settle()
        failed=self.service.load('news',sport='NBA')
        self.assertIn('invalid news response',failed['error']);self.assertEqual(failed['cached'],original)

    def test_weather_preserves_reported_units_missing_values_and_partial_alert_failure(self):
        self.weather_client();self.service.load('weather',latitude=39.7684,longitude=-86.1581);self.settle()
        packet=self.service.load('weather',latitude=39.7684,longitude=-86.1581)
        content=weather(packet,39.7684,-86.1581,self.clock)
        self.assertIn('65°F',str(content));self.assertIn('18°C',str(content));self.assertIn('60%',str(content))
        self.assertIn('Unavailable',str(content));self.assertNotIn('None%',str(content));self.assertIn('Test weather advisory',str(content))
        self.assertEqual(weather(packet,40,-86,self.clock),[])
        self.assertIn('no future hours',str(weather(packet,39.7684,-86.1581,self.clock+timedelta(days=1))))
        self.weather_client(alerts_error=True)
        result=self.service.weather(39.7684,-86.1581)
        partial={**packet,'cached':{**packet['cached'],'data':result}}
        self.assertIn('alerts could not be checked',str(weather(partial,39.7684,-86.1581,self.clock)))
        self.assertNotIn('No active alerts',str(weather(partial,39.7684,-86.1581,self.clock)))

    def test_weather_uses_only_nws_forecast_urls_and_rejects_invalid_scope_before_network(self):
        for latitude,longitude in [(None,-86),(True,-86),(91,-86),(39,float('nan')),(39,-181)]:
            with self.assertRaises(ValueError):self.service.load('weather',latitude=latitude,longitude=longitude)
        with self.assertRaises(ValueError):self.service.load('weather',sport='NBA',latitude=39,longitude=-86)
        self.client.json.assert_not_called()
        self.client.json.return_value=dict(properties=dict(forecastHourly='https://untrusted.example/forecast'))
        with self.assertRaises(DataSourceError):self.service.weather(39,-86)
        self.assertEqual(self.client.json.call_count,1)

    def test_source_pages_filters_and_navigation_remain_isolated_for_each_sport(self):
        packet=self.news_packet();app=create_app(information_service=self.service)
        cb={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback')}
        self.assertEqual(len(PAGES['NBA']),2);self.assertEqual(len(PAGES['NFL']),2)
        for sport in ['NBA','NFL']:
            for kind,title in PAGES[sport]:
                href=f'#{sport.lower()}-information-{kind}'
                self.assertEqual(route(href),dict(sport=sport,kind=kind))
                self.assertEqual(cb['restore_game_route'](href),(sport,'information'))
                output=cb['information_visibility'](sport,'information',href)
                self.assertEqual(output[4],dict(sport=sport,kind=kind));self.assertIn(title,output[5])
                self.assertEqual(cb['navigate']('information',sport)[0],{'display':'none'})
        self.assertIsNone(route('#nba-information-weather'));self.assertIsNone(route('#ncaaf-information-news'))
        self.assertEqual(cb['information_visibility']('NCAAF','historical','')[0],{'display':'none'})
        self.assertEqual(cb['in_game_supported_page']('NCAAF','information'),'historical')
        with self.assertRaises(PreventUpdate):cb['information_load'](None,0,0,39,-86,None)
        context=dict(sport='NBA',kind='injuries')
        shown=news(packet,context,'all','__all__',None)
        self.assertIn('knee injury',str(shown));self.assertNotIn('sign Test Guard',str(shown))
        shown=news(packet,dict(sport='NBA',kind='roster'),'all','Boston Celtics','guard')
        self.assertIn('sign Test Guard',str(shown));self.assertNotIn('knee injury',str(shown))
        self.assertEqual(news(packet,dict(sport='NFL',kind='news'),'all','__all__',None),[])
        self.assertIn('nba-injury-report-2026-27-season',official_sources('NBA',NOW)[0]['url'])
        self.assertIn('EDT',time_text(NOW.isoformat()));self.assertIn('EST',time_text('2026-12-01T20:00:00Z'))
        ids=[c.id for c in components(app.layout) if isinstance(getattr(c,'id',None),str)]
        self.assertEqual(len(ids),len(set(ids)))

    def test_dash_http_load_and_content_callbacks_serialize_without_refetching(self):
        packet=self.news_packet();app=create_app(information_service=self.service)
        values={'information-context':dict(sport='NBA',kind='injuries'),'information-refresh':0,'information-timer':0,
                'information-latitude':39.7684,'information-longitude':-86.1581,'information-data':None,'information-kind':'all',
                'information-team':'__all__','information-search':None}
        calls=self.client.json.call_count
        with app.server.test_client() as client:
            for name in ['information_load','information_contents']:
                key,meta=next((key,m) for key,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**i,value=values[i['id']]) for i in meta['inputs']],state=[dict(**i,value=values[i['id']]) for i in meta['state']],
                    changedPropIds=['information-context.data']))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                result=response.get_json()['response']
                if name=='information_load':values['information-data']=result['information-data']['data']
                else:self.assertIn('ESPN',str(result));self.assertIn('https://www.espn.com',str(result))
        self.assertEqual(self.client.json.call_count,calls)


if __name__=='__main__':main()
