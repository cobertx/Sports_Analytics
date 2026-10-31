"""Offline checks for recorded coordinates, season indexing, filtering and routing."""
import csv
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash.exceptions import PreventUpdate
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from sports_analytics.in_game_data import InGameService, nba_shot, nfl_play, valid_game, charting_fields
from sports_analytics import in_game_view as view
from sports_analytics import nfl_in_game as nfl
from sports_analytics.service import DataService


NBA_ID = '0022500001'
NFL_ID = '2025_01_ARI_NO'


def shot(**changes):
    row = dict(game_id=NBA_ID, action_number='7', order_index='1', is_field_goal='1', period='1',
                clock='PT11M38.00S', team_tricode='HOU', person_id='123', player_name_i='A. Player',
                x_legacy='-179', y_legacy='179', shot_result='Missed', shot_value='3',
                description='MISS Player 3PT')
    return {**row, **changes}


def play(**changes):
    row = dict(game_id=NFL_ID, play_id='63', posteam='ARI', play_type='run', play_deleted='0',
               qtr='1', time='14:56', down='1', ydstogo='10', yardline_100='78', yards_gained='3',
               run_location='right', rusher_player_name='J.Conner', drive='1', epa='-.19',
               touchdown='0', desc='Conner right tackle for 3 yards')
    return {**row, **changes}


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'wt', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=sorted(set().union(*(r.keys() for r in rows))))
        writer.writeheader(); writer.writerows(rows)


class InGameChecks(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = Mock()
        self.client.json.side_effect = AssertionError('Unexpected network call')
        self.service = InGameService(data=DataService(self.root / 'sports.sqlite3'), directory=self.root / 'in_game',
                                     model_directory=self.root / 'models', client=self.client)
        self.nba = self.root / 'models/nba/epa/raw/nba_play_by_play_2026.csv.gz'
        self.nfl = self.root / 'models/nfl/raw/play_by_play_2025.csv'

    def test_nba_coordinates_misses_and_non_shots(self):
        event = nba_shot(shot())
        self.assertEqual((event['x'], event['y'], event['made'], event['value']), (-17.9, 17.9, False, 3))
        row = shot(); row['is_field_goal'] = '0'
        self.assertIsNone(nba_shot(row))
        row = shot(); row.update(x_legacy='', y_legacy='nan', shot_result='')
        missing = nba_shot(row)
        self.assertIsNone(missing['x']); self.assertIsNone(missing['y']); self.assertIsNone(missing['made'])
        self.assertFalse(view.located_shot(missing))

    def test_cdn_fields_and_game_identity_validation(self):
        self.client.json.side_effect = None
        self.client.json.return_value = {'game': {'gameId': NBA_ID, 'actions': [dict(actionNumber=7, isFieldGoal=1,
            xLegacy=0, yLegacy=0, shotResult='Made', actionType='3pt', period=5, personId=123, playerName='Player')]}}
        result = self.service.game('NBA', 2025, NBA_ID)
        self.assertEqual((result['events'][0]['x'], result['events'][0]['value']), (0, 3))
        self.assertEqual(self.service.game('NBA', 2025, NBA_ID), result)
        self.assertEqual(self.client.json.call_count, 1)
        self.client.json.return_value['game']['gameId'] = '0022500002'
        with self.assertRaisesRegex(Exception, 'match'):
            self.service._nba_live(NBA_ID)

    def test_nfl_actual_field_position_gain_and_direction(self):
        event = nfl_play(play(play_type='pass', pass_location='left', yards_gained='0',
                              air_yards='45', incomplete_pass='1', passer_player_name='QB'))
        self.assertEqual((event['start'], event['end'], event['direction']), (22, 22, 'left'))
        loss = nfl_play(play(yards_gained='-5', run_location=''))
        self.assertEqual((loss['start'], loss['end'], loss['direction']), (22, 17, 'unknown'))
        missing = nfl_play(play(yardline_100='', yards_gained='nan'))
        self.assertIsNone(missing['start']); self.assertIsNone(missing['end'])
        self.assertFalse(view.located_play(missing))
        self.assertIsNone(nfl_play(play(play_type='no_play')))
        self.assertIsNone(nfl_play(play(play_deleted='1')))
        self.assertIsNone(nfl_play(play(play_type='punt')))

    def test_charting_preserves_false_zero_and_unknown_without_inferring_coverage(self):
        fields = charting_fields(dict(offense_personnel='1 RB, 1 TE, 3 WR', defense_personnel='4 DL, 2 LB, 5 DB',
            is_play_action='FALSE', is_rpo='TRUE', n_blitzers='0', defenders_in_box='6', defense_man_zone_type='NA'))
        self.assertIs(fields['play_action'], False)
        self.assertIs(fields['rpo'], True)
        self.assertEqual((fields['blitzers'], fields['defenders_in_box']), (0, 6))
        self.assertIsNone(fields['coverage_family']); self.assertIsNone(fields['coverage_scheme'])
        self.assertEqual(nfl_play(play(qb_scramble='1'))['call'], 'Dropback')
        self.assertEqual(nfl_play(play())['call'], 'Run')
        self.assertEqual(nfl_play(play(play_type='qb_kneel'))['call'], 'QB kneel')

    def test_personnel_grouping_preserves_packages_and_does_not_call_them_formations(self):
        self.assertEqual(nfl.personnel_label('1 QB, 1 C, 2 G, 2 T, 1 RB, 1 TE, 3 WR', True),
            '11 personnel · 1 RB/FB, 1 TE, 3 WR')
        self.assertEqual(nfl.personnel_label('1 RB, 1 FB, 1 TE, 2 WR', True),
            '21 personnel · 2 RB/FB, 1 TE, 2 WR')
        self.assertNotIn('personnel ·', nfl.personnel_label('1 QB, 2 C, 4 T, 1 RB, 2 TE, 1 WR', True))
        self.assertEqual(nfl.personnel_label('2 CB, 1 FS, 1 SS, 2 DE, 1 DT, 2 OLB, 2 ILB', False),
            '3 DL · 4 LB · 4 DB')

    def test_usage_percentages_include_unknown_and_keep_real_no_values(self):
        events = [dict(coverage_family=value) for value in ['MAN_COVERAGE', 'zone', None, '']]
        counts = {r['label']: r for r in nfl.distribution(events, 'coverage_family')}
        self.assertEqual({name: r['percent'] for name, r in counts.items()}, {'MAN': 25, 'ZONE': 25, 'Unknown': 50})
        self.assertEqual(sum(r['count'] for r in counts.values()), 4)
        flags = nfl.distribution([dict(play_action=True), dict(play_action=False), dict(play_action=None)], 'play_action')
        self.assertEqual({r['label']: r['count'] for r in flags}, {'Yes': 1, 'No': 1, 'Unknown': 1})

    def test_drives_are_separate_for_both_teams_even_when_numbers_repeat(self):
        events = [nfl_play(play()), nfl_play(play(play_id='70', posteam='NO', defteam='ARI', drive='1')),
                  nfl_play(play(play_id='80', drive='2'))]
        self.assertEqual(list(nfl.drives(events)), ['ARI:1', 'NO:1', 'ARI:2'])
        self.assertEqual(view.filter_events(events, 'NFL', drive='NO:1'), [events[1]])
        self.assertIn('NO offense vs ARI', nfl.drive_label([events[1]]))

    def test_supplementary_charting_joins_exact_ids_and_does_not_erase_fields(self):
        write_csv(self.nfl, [play(), play(play_id='70', play_type='pass', defteam='NO')])
        participation = self.nfl.parent / 'pbp_participation_2025.csv'
        ftn = self.nfl.parent / 'ftn_charting_2025.csv'
        write_csv(participation, [dict(nflverse_game_id=NFL_ID, play_id='70', offense_personnel='1 RB, 1 TE, 3 WR',
            defense_personnel='4 DL, 2 LB, 5 DB', defense_man_zone_type='MAN_COVERAGE', defense_coverage_type='COVER_1'),
            dict(nflverse_game_id='2025_02_ARI_NO', play_id='63', offense_personnel='WRONG GAME')])
        write_csv(ftn, [dict(nflverse_game_id=NFL_ID, nflverse_play_id='70.0', ftn_play_id='999',
            is_play_action='false', n_blitzers='0', qb_location='S'),
            dict(nflverse_game_id=NFL_ID, nflverse_play_id='1234', ftn_play_id='63', is_play_action='true', n_blitzers='9')])
        data = self.service.game('NFL', 2025, NFL_ID)
        first, second = data['events']
        self.assertIsNone(first['offense_personnel']); self.assertIsNone(first['play_action'])
        self.assertEqual((second['offense_personnel'], second['coverage_family']), ('1 RB, 1 TE, 3 WR', 'MAN_COVERAGE'))
        self.assertIs(second['play_action'], False); self.assertEqual(second['blitzers'], 0)
        self.assertEqual([r['matched'] for r in data['charting']['sources']], [1, 1])
        self.assertEqual([r['drive_play_number'] for r in data['events']], [1, 2])
        with patch('sports_analytics.in_game_data.pd.read_csv', side_effect=AssertionError('No repeated season scan')):
            self.assertEqual(self.service.game('NFL', 2025, NFL_ID), data)
        write_csv(ftn, [dict(nflverse_game_id=NFL_ID, nflverse_play_id='70', is_play_action='true')])
        changed = self.service.game('NFL', 2025, NFL_ID)
        self.assertIs(changed['events'][1]['play_action'], True)
        self.assertEqual(changed['events'][1]['coverage_family'], 'MAN_COVERAGE')
        self.client.get.assert_not_called()

    def test_supplementary_download_failure_keeps_base_game_available(self):
        from sports_analytics.data import DataSourceError
        write_csv(self.nfl, [play()])
        self.client.get.side_effect = DataSourceError('Charting not released')
        data = self.service.game('NFL', 2025, NFL_ID, charting=True)
        self.assertEqual(len(data['events']), 1)
        self.assertEqual(len(data['charting']['notices']), 2)
        self.assertIsNone(data['events'][0]['coverage_family'])

    def test_defensive_return_is_not_offensive_touchdown(self):
        defense = nfl_play(play(touchdown='1', td_team='NO', interception='1'))
        offense = nfl_play(play(touchdown='1', td_team='ARI'))
        self.assertFalse(defense['touchdown']); self.assertTrue(offense['touchdown'])

    def test_nba_archive_index_is_reused_and_invalidated(self):
        write_csv(self.nba, [shot()])
        first = self.service.game('NBA', 2025, NBA_ID)
        # A different game queries the same source index without parsing the CSV again.
        with patch('sports_analytics.in_game_data.pd.read_csv', side_effect=AssertionError('Repeated season scan')):
            self.assertEqual(self.service._indexed(self.nba, 'NBA', NBA_ID), first['events'])
        row = shot(); row.update(x_legacy='220', shot_result='Made')
        write_csv(self.nba, [row])
        changed = self.service.game('NBA', 2025, NBA_ID)
        self.assertEqual((changed['events'][0]['x'], changed['events'][0]['made']), (22, True))
        self.assertNotEqual(first['archive_revision'], changed['archive_revision'])
        self.client.json.assert_not_called()

    def test_index_rebuild_is_atomic_on_invalid_source(self):
        write_csv(self.nfl, [play()])
        first = self.service.game('NFL', 2025, NFL_ID)
        write_csv(self.nfl, [dict(game_id=NFL_ID, invalid='oops')])
        with self.assertRaisesRegex(Exception, 'required'):
            self.service.game('NFL', 2025, NFL_ID)
        with self.service.connect() as db:
            count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        self.assertEqual(count, len(first['events']))

    def test_nfl_saved_source_reused_without_download_or_rescan(self):
        write_csv(self.nfl, [play(), play(play_id='70', yards_gained='-5')])
        first = self.service.game('NFL', 2025, NFL_ID)
        with patch('sports_analytics.in_game_data.pd.read_csv', side_effect=AssertionError('Repeated season scan')):
            second = self.service.game('NFL', 2025, NFL_ID)
        self.assertEqual(first, second)
        self.assertEqual(len(first['events']), 2)
        self.client.get.assert_not_called()

    def test_catalog_reuses_prepared_nba_and_nfl_games(self):
        write_csv(self.root / 'models/nba/team_games_2025_regular.csv', [dict(game_id=NBA_ID, team='HOU',
            opponent='OKC', is_home='0', event_date='2025-10-21', season_type='regular')])
        write_csv(self.root / 'models/nfl/raw/games.csv', [dict(game_id=NFL_ID, season='2025', game_type='REG',
            week='1', gameday='2025-09-07', home_team='NO', away_team='ARI', home_score='13'),
            dict(game_id='2025_02_ARI_NO', season='2025', game_type='REG', week='2', gameday='2025-09-14',
                 home_team='NO', away_team='ARI', home_score='')])
        nba = self.service.catalog('NBA', 2025)
        nfl = self.service.catalog('NFL', 2025)
        self.assertEqual(nba['games'][0]['matchup'], 'HOU @ OKC')
        self.assertEqual(len(nfl['games']), 1)
        self.client.get.assert_not_called()

    def test_refresh_failure_displays_saved_game_with_notice(self):
        write_csv(self.nba, [shot()])
        first = self.service.game('NBA', 2025, NBA_ID)
        from sports_analytics.data import DataSourceError
        self.client.json.side_effect = DataSourceError('offline')
        second = self.service.game('NBA', 2025, NBA_ID, refresh=True)
        self.assertEqual(first['events'], second['events'])
        self.assertIn('Refresh failed', second['notice'])

    def test_nfl_refresh_failure_preserves_a_csv_without_a_manifest(self):
        from sports_analytics.data import DataSourceError
        write_csv(self.nfl, [play()])
        first = self.service.game('NFL', 2025, NFL_ID)
        self.client.get.side_effect = DataSourceError('offline')
        second = self.service.game('NFL', 2025, NFL_ID, refresh=True)
        self.assertEqual(first['events'], second['events'])
        self.assertIn('Refresh failed', second['notice'])

    def test_nba_filters_unknown_result_and_half_court_heaves(self):
        base = nba_shot(shot())
        made = {**base, 'id': '2', 'made': True, 'period': 5, 'y': 55}
        unknown = {**base, 'id': '3', 'made': None, 'x': None}
        filtered = view.filter_events([base, made, unknown], 'NBA', result='missed')
        self.assertEqual(filtered, [base])
        self.assertEqual(view.filter_events([base, made], 'NBA', period='5'), [made])
        half = view.court_figure([base, made, unknown])
        full = view.court_figure([base, made, unknown], full=True)
        self.assertEqual(sum(len(t.x) for t in half.data), 1)
        self.assertEqual(sum(len(t.x) for t in full.data), 2)
        self.assertGreater(len(full.layout.shapes), len(half.layout.shapes))
        self.assertEqual(view.clock_label('PT03M09.00S'), '03:09')
        self.assertEqual(view.period_label(5), 'OT1')

    def test_nfl_filters_use_both_passer_and_receiver_and_real_yards(self):
        run = nfl_play(play())
        passed = nfl_play(play(play_id='70', play_type='pass', passer_player_name='QB', receiver_player_name='WR',
                               pass_location='left', drive='2', yards_gained='0'))
        self.assertEqual(view.filter_events([run, passed], 'NFL', player='WR', drive='2', kind='pass'), [passed])
        figure = view.football_figure([run, passed])
        pass_trace = next(t for t in figure.data if t.name == 'Pass / sack')
        self.assertEqual(list(pass_trace.x), [22])
        self.assertEqual(list(pass_trace.y), [view.LANES['left']])
        columns, table = view.event_table([passed], 'NFL')
        self.assertEqual(table[0]['yards'], 0)
        self.assertIn('receiver', {c['id'] for c in columns})
        self.assertTrue(any(s.type == 'rect' and s.x0 == -10 and s.x1 == 110 for s in figure.layout.shapes))
        self.assertTrue(any(s.type == 'line' and s.x0 == 1 and abs((s.y0 + s.y1) / 2 - 23.5833) < .001 for s in figure.layout.shapes))

    def test_unsafe_game_ids_and_wrong_seasons_are_rejected(self):
        self.assertFalse(valid_game('NBA', '../file'))
        self.assertFalse(valid_game('NFL', '2025_01_ARI_NO/../../file'))
        with self.assertRaises(ValueError): self.service.game('NFL', 2024, NFL_ID)
        with self.assertRaises(ValueError): self.service.game('NBA', 2024, NBA_ID)
        with self.assertRaises(ValueError): self.service.catalog('NCAAF', 2025)

    def test_dash_http_loads_a_saved_game_renders_and_preserves_on_page_exit(self):
        write_csv(self.nba, [shot(), shot(action_number='8', order_index='2', shot_result='Made')])
        write_csv(self.root / 'models/nba/team_games_2025_regular.csv', [dict(game_id=NBA_ID, team='HOU',
            opponent='OKC', is_home='0', event_date='2025-10-21', season_type='regular')])
        app = create_app(in_game_service=self.service)
        client = app.server.test_client()

        def post(name, values, changed):
            key, meta = next((key, meta) for key, meta in app.callback_map.items()
                if meta.get('callback') and meta['callback'].__wrapped__.__name__ == name)
            payload = dict(output=key, outputs=[dict(id=o.component_id, property=o.component_property) for o in meta['output']],
                inputs=[dict(**item, value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item, value=wire_value(item,values)) for item in meta['state']], changedPropIds=[changed])
            return client.post('/_dash-update-component', json=payload)

        values = {'workspace-page': 'in-game', 'sport': 'NBA', 'in-game-season': 2025,
                  'in-game-phase': 'regular', 'in-game-reload-list': 0, 'in-game-catalog': None}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            response = post('in_game_catalog', values, 'workspace-page.value')
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            values.update({'in-game-catalog': response.get_json()['response']['in-game-catalog']['data'],
                           'in-game-game': NBA_ID, 'in-game-refresh': 0, 'in-game-events': None, 'in-game-charting-load': 0})
            response = post('in_game_load', values, 'in-game-game.value')
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            values['in-game-events'] = response.get_json()['response']['in-game-events']['data']
            values.update({'in-game-' + key: view.ALL for key in ['offense', 'player', 'period', 'result', 'kind', 'drive']})
            values.update({'in-game-court': 'half', 'in-game-event-select': '8'})
            response = post('in_game_render', values, 'in-game-event-select.value')
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            result = response.get_json()['response']
            self.assertEqual([r['id'] for r in result['in-game-table']['data']], ['8'])
            self.assertEqual(result['in-game-table']['data'][0]['result'], 'Made')
            self.assertEqual(result['in-game-chart']['figure']['data'][0]['name'], 'Made')
            values['workspace-page'] = 'historical'
            self.assertEqual(post('in_game_load', values, 'workspace-page.value').status_code, 204)
            values['workspace-page'] = 'in-game'
            response = post('in_game_load', values, 'workspace-page.value')
            self.assertNotIn('in-game-events', response.get_json()['response'])
        self.client.json.assert_not_called()

    def test_dashboard_routing_visibility_and_stale_chart_guard(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            app = create_app(in_game_service=self.service)
            client = app.server.test_client()
            for route in ['/', '/_dash-layout', '/_dash-dependencies', '/assets/in_game.css']:
                with client.get(route) as response:
                    self.assertEqual(response.status_code, 200)
        callbacks = callback_functions(app)
        self.assertEqual(callbacks['restore_game_route']('#nba-in-game-data'), ('NBA', 'in-game'))
        self.assertEqual(callbacks['restore_game_route']('#nfl-in-game-data'), ('NFL', 'in-game'))
        self.assertEqual(callbacks['navigate']('in-game', 'NBA')[0], {'display': 'none'})
        self.assertEqual(callbacks['in_game_visibility']('in-game', 'NFL')[0], {'display': 'grid'})
        self.assertEqual(callbacks['in_game_visibility']('in-game', 'NCAAF')[1], {'display': 'none'})
        self.assertEqual(callbacks['in_game_supported_page']('NCAAF', 'in-game'), 'historical')
        with self.assertRaises(PreventUpdate): callbacks['in_game_supported_page']('NBA', 'in-game')
        with patch('sports_analytics.in_game_callbacks.ctx', SimpleNamespace(triggered_id=None)):
            with self.assertRaises(PreventUpdate):
                callbacks['in_game_catalog']('historical', 'NBA', 2025, 'regular', 0, None)
            with self.assertRaises(PreventUpdate):
                callbacks['in_game_load'](NBA_ID, None, 0, 'historical', 'NBA', None)
            data = dict(sport='NBA', season=2025, game_id=NBA_ID, events=[nba_shot(shot())])
            catalog = dict(sport='NBA', season=2025)
            rendered = callbacks['in_game_render'](data, view.ALL, view.ALL, view.ALL, view.ALL, view.ALL,
                view.ALL, 'half', 'NFL', NFL_ID, catalog)
            self.assertEqual(rendered[-1], [])
        events = [nfl_play(play(defteam='NO')), nfl_play(play(play_id='70', posteam='NO', defteam='ARI', drive='2'))]
        data = dict(sport='NFL', season=2025, game_id=NFL_ID, events=events)
        choices = callbacks['in_game_player_options'](data, view.ALL, view.ALL, None)
        self.assertEqual(choices[-1], 'ARI:1')
        self.assertEqual(callbacks['in_game_player_options'](data, view.ALL, view.ALL, 'NO:2')[-1], 'NO:2')
        self.assertEqual(callbacks['in_game_player_options'](data, view.ALL, view.ALL, view.ALL)[-1], view.ALL)
        clicked = dict(type='in-game-drive-button', drive='NO:2')
        with patch('sports_analytics.in_game_callbacks.ctx', SimpleNamespace(triggered_id=clicked, triggered=[dict(value=1)])):
            self.assertEqual(callbacks['in_game_choose_drive']([0, 1], data, view.ALL), 'NO:2')
            with self.assertRaises(PreventUpdate): callbacks['in_game_choose_drive']([0, 1], data, 'ARI')
        key, meta = next((key, item) for key, item in app.callback_map.items()
            if item.get('callback') and item['callback'].__wrapped__.__name__ == 'in_game_choose_drive')
        response = app.server.test_client().post('/_dash-update-component', json=dict(output=key,
            outputs=dict(id='in-game-drive', property='value'),
            inputs=[dict(**meta['inputs'][0], value=[0, 1])],
            state=[dict(**item, value=data if item['id'] == 'in-game-events' else view.ALL) for item in meta['state']],
            changedPropIds=[json.dumps(clicked, sort_keys=True, separators=(',', ':')) + '.n_clicks']))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['response']['in-game-drive']['value'], 'NO:2')
        key, meta = next((key, item) for key, item in app.callback_map.items()
            if item.get('callback') and item['callback'].__wrapped__.__name__ == 'in_game_load')
        catalog = dict(sport='NFL', season=2025, games=[dict(id=NFL_ID, date='2025-09-07', matchup='ARI @ NO')])
        loaded = {**data, 'version': 2, 'source': 'fixture', 'source_at': '2026-10-22T00:00:00Z'}
        values = {'in-game-game': NFL_ID, 'in-game-catalog': catalog, 'in-game-refresh': 0,
                  'workspace-page': 'in-game', 'sport': 'NFL', 'in-game-charting-load': 1, 'in-game-events': loaded}
        with patch.object(self.service, 'game', return_value=loaded) as load:
            response = app.server.test_client().post('/_dash-update-component', json=dict(output=key,
                outputs=[dict(id=o.component_id, property=o.component_property) for o in meta['output']],
                inputs=[dict(**item, value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item, value=wire_value(item,values)) for item in meta['state']],
                changedPropIds=['in-game-charting-load.n_clicks']))
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            load.assert_called_once_with('NFL', 2025, NFL_ID, refresh=False, charting=True)
            self.assertEqual(response.get_json()['response']['in-game-events']['data']['game_id'], NFL_ID)
        self.assertTrue(callbacks['in_game_nfl_insights'](data, view.ALL, view.ALL, view.ALL, view.ALL,
            'NO:2', view.ALL, 'NFL', NFL_ID))
        self.assertEqual(callbacks['in_game_nfl_insights'](data, view.ALL, view.ALL, view.ALL, view.ALL,
            'NO:2', view.ALL, 'NBA', NBA_ID), [])


if __name__ == '__main__':
    main()
