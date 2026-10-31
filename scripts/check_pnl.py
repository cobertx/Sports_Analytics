"""Exact USD accounting, statement preview/import, durable records and tab isolation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from sports_analytics import pnl
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from check_kalshi import components


def bet(**changes):
    return dict(record_type='bet',account='FanDuel',sport='NBA',description='Team A -3.5',market='Spread',
                stake='100',american_odds='-110',placed_at='2025-10-01T12:00:00-04:00',status='open',**changes)


class PnlChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.ledger=pnl.Ledger(Path(temporary.name)/'history.sqlite3')
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('Personal history must not fetch an API or place bets'))
        network.start();self.addCleanup(network.stop)

    def write(self,values,key=None):return self.ledger.write('add',values,key or uuid4().hex)

    def sample(self):
        for kind,amount in [('opening',1000),('deposit',200),('withdrawal',50)]:
            self.write(dict(record_type=kind,account='FanDuel',amount=amount,placed_at='2025-09-01'))
        won=bet();won.update(status='won',settled_at='2025-10-02',fees='.91');self.write(won)
        lost=bet();lost.update(stake=50,status='lost',settled_at='2025-10-02',fees='1');self.write(lost)
        open_bet=bet();open_bet['stake']=25;self.write(open_bet)

    def app(self):
        app=create_app(ledger=self.ledger)
        cb=callback_functions(app)
        return app,cb

    def test_balance_equity_realized_profit_and_transfers_reconcile_in_integer_cents(self):
        self.sample();result=pnl.history(self.ledger.records())
        self.assertEqual((result['cash'],result['open_stake'],result['bankroll'],result['pnl']),(116400,2500,118900,3900))
        self.assertEqual(result['roi'],.26);self.assertEqual(result['win_rate'],.5)
        self.assertEqual(sum(e['pnl'] for e in result['timeline'] if e['type'] in {'opening','deposit','withdrawal'}),0)
        self.assertTrue(all(isinstance(e['cash_balance'],int) for e in result['timeline']))
        self.assertEqual(pnl.Ledger(self.ledger.path).records(),self.ledger.records())

    def test_standard_odds_and_push_void_cashout_custom_exchange_payouts(self):
        self.assertEqual(pnl.settlement(10000,'-110','won'),(19091,0))
        self.assertEqual(pnl.settlement(10000,'150','won'),(25000,0))
        for status in ['push','void']:self.assertEqual(pnl.settlement(10000,None,status),(10000,0))
        self.assertEqual(pnl.settlement(4000,None,'settled',100,2),(10000,200))
        self.assertEqual(pnl.settlement(10000,None,'cashed_out',70,1),(7000,100))
        self.assertEqual(pnl.settlement(10000,None,'lost',None,1),(0,100))
        with self.assertRaises(ValueError):pnl.settlement(10000,None,'open',0)
        with self.assertRaises(ValueError):pnl.settlement(10000,'0','won')

    def test_settlement_correction_replaces_the_payout_and_preserves_audit_history(self):
        self.sample();won=next(r for r in self.ledger.records() if r.get('status')=='won')
        self.ledger.write('settle',dict(id=won['id'],status='lost',settled_at='2025-10-02',fees=0),uuid4().hex)
        result=pnl.history(self.ledger.records())
        self.assertEqual((result['cash'],result['pnl']),(97400,-15100))
        self.assertEqual(len(result['bets']),3)
        with self.ledger.connect() as db:
            before=db.execute('SELECT before_json FROM revisions WHERE record_id=? AND before_json IS NOT NULL',(won['id'],)).fetchone()
        self.assertIn('"status": "won"',before[0])

    def test_duplicate_parallel_requests_do_not_double_count(self):
        token=uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests=[pool.submit(self.write,bet(),token) for _ in range(2)]
            self.assertEqual(requests[0].result(),requests[1].result())
        self.assertEqual(len(self.ledger.records()),1)
        self.assertEqual(pnl.history(self.ledger.records())['open_stake'],10000)
        different=bet();different['stake']=200
        with self.assertRaises(ValueError):self.write(different,token)

    def test_export_reimports_idempotently_and_can_restore_another_ledger(self):
        self.sample();records=self.ledger.records();csv=pnl.csv_text(records);rows=pnl.parse_csv(csv)
        result=self.ledger.write('import',rows,uuid4().hex)
        self.assertEqual((result['added'],result['skipped']),(0,len(records)))
        restored=pnl.Ledger(self.ledger.path.with_name('restored.sqlite3'))
        restored.write('import',rows,uuid4().hex)
        for key in ['cash','bankroll','pnl','open_stake']:
            self.assertEqual(pnl.history(restored.records())[key],pnl.history(records)[key])

    def test_invalid_rows_and_conflicting_external_ids_roll_back_the_whole_import(self):
        row=bet();row['external_id']='bet-1'
        self.ledger.write('import',[row],uuid4().hex)
        other=deepcopy(row);other.update(external_id='bet-2')
        conflict=deepcopy(row);conflict['stake']=200
        with self.assertRaises(ValueError):self.ledger.write('import',[other,conflict],uuid4().hex)
        self.assertEqual(len(self.ledger.records()),1)
        bad=deepcopy(other);bad['stake']='1.001'
        with self.assertRaises(ValueError):self.ledger.write('import',[other,bad],uuid4().hex)
        self.assertEqual(len(self.ledger.records()),1)
        self.write(dict(record_type='opening',account='FanDuel',amount=100))
        with self.assertRaises(ValueError):self.write(dict(record_type='opening',account='FanDuel',amount=100))

    def test_statement_columns_negative_debits_and_net_profit_are_explicitly_normalized(self):
        source='Bet ID,Date Placed,Date Settled,Selection,Bet Amount,Odds,Result,Net Profit,Commission\nb1,10/01/2025 12:00,10/02/2025 12:00,Team A,"($100.00)",-110,Win,$89.00,($1.00)\n'
        columns,rows=pnl.statement_rows(source);mapping=pnl.column_mapping(columns)
        normalized=pnl.mapped_statement(rows,mapping,'FanDuel',basis='net',date_format='us-minute',debits=True)
        self.assertEqual(normalized[0]['stake'],'100.00');self.assertEqual(normalized[0]['gross_return'],'190.00')
        self.ledger.write('import',normalized,uuid4().hex)
        self.assertEqual(pnl.history(self.ledger.records())['pnl'],8900)
        with self.assertRaises(ValueError):pnl.mapped_statement(rows,mapping,'FanDuel',basis=None)
        with self.assertRaises(ValueError):pnl.mapped_statement(rows,mapping,'FanDuel',basis='gross',date_format='us-minute',debits=False)

    def test_pending_exchange_fees_debit_cash_immediately_and_zero_profit_is_not_a_payout(self):
        self.write(dict(record_type='opening',account='Kalshi',amount=100,placed_at='2025-01-01'))
        columns,rows=pnl.statement_rows('ID,Date,Description,Cost,Fees,Status,Profit\nx1,2025-10-01,Team A wins,-40,-1.25,Pending,0\n')
        mapped=pnl.mapped_statement(rows,pnl.column_mapping(columns),'Kalshi',basis='net',debits=True,fees_at='placement')
        self.ledger.write('import',mapped,uuid4().hex)
        result=pnl.history(self.ledger.records())
        self.assertEqual((result['cash'],result['bankroll'],result['pnl']),(5875,9875,-125));self.assertIsNone(result['roi'])
        recorded=result['bets'][0]
        self.ledger.write('settle',dict(id=recorded['id'],status='won',settled_at='2025-10-02',gross_return=100,fees=2),uuid4().hex)
        result=pnl.history(self.ledger.records())
        self.assertEqual((result['cash'],result['pnl']),(15675,5675));self.assertEqual(result['roi'],5675/4000)

    def test_validation_rejects_invalid_money_dates_currencies_and_outcomes(self):
        for change in [dict(stake='NaN'),dict(stake='-1'),dict(stake='1.001'),dict(currency='EUR'),dict(american_odds=0),dict(description='')]:
            values=bet();values.update(change)
            with self.assertRaises(ValueError):self.write(values)
        invalid=bet();invalid.update(status='won',settled_at='2025-01-01')
        with self.assertRaises(ValueError):self.write(invalid)
        with self.assertRaises(ValueError):pnl.stamp('2026-11-01T01:30:00')
        self.assertEqual(pnl.stamp('2026-11-01T01:30:00-05:00'),'2026-11-01T06:30:00.000000+00:00')
        self.assertEqual(self.ledger.records(),[])

    def test_tab_order_empty_state_visibility_and_account_scopes(self):
        app,cb=self.app()
        tabs=next(c for c in components(app.layout) if getattr(c,'id',None)=='sport')
        self.assertEqual([c.label for c in tabs.children][-2:],['Strategy & Research','P&L History'])
        self.assertEqual(cb['restore_game_route']('#pnl-history'),('PNL','pnl'))
        self.assertEqual(cb['in_game_supported_page']('NBA','pnl'),'historical')
        for page in ['historical','market','backtesting','information']:
            navigation=cb['navigate'](page,'PNL')
            self.assertEqual(navigation[0],{'display':'none'});self.assertEqual(navigation[1],{'display':'none'});self.assertTrue(navigation[4]);self.assertEqual(navigation[8],{'display':'none'})
        empty=cb['pnl_history']('PNL',None,'__all__',0)
        self.assertEqual(empty[3],[]);self.assertEqual(empty[4],[]);self.assertIn('No history',empty[5])
        self.assertNotIn('$0.00',str(empty[0]))
        with self.assertRaises(PreventUpdate):cb['pnl_history']('NBA',None,'__all__',0)
        self.write(bet());other=bet();other.update(account='Kalshi',stake=40);self.write(other)
        self.assertEqual(pnl.history(self.ledger.records(),'fanduel')['open_stake'],10000)
        self.assertEqual(pnl.history(self.ledger.records(),'kalshi')['open_stake'],4000)

    def test_statement_preview_does_not_save_and_changes_require_a_new_preview(self):
        app,cb=self.app();source=pnl.csv_text([dict(pnl.record(bet()),id='manual-id')])
        contents='data:text/csv;base64,'+base64.b64encode(source.encode()).decode()
        document,controls,message=cb['pnl_statement'](contents,'statement.csv','PNL')
        inputs=[c for c in components(controls) if isinstance(getattr(c,'id',None),dict)]
        with patch('sports_analytics.pnl_view.ctx',SimpleNamespace(triggered_id='pnl-import-preview-button')):
            preview=cb['pnl_preview'](1,document,[c.value for c in inputs],None,'bet','open','gross','iso',[],[c.id for c in inputs],'PNL')
        self.assertFalse(preview[3]);self.assertEqual(len(preview[0]),1);self.assertEqual(self.ledger.records(),[])
        with patch('sports_analytics.pnl_view.ctx',SimpleNamespace(triggered_id='pnl-import-basis')):
            changed=cb['pnl_preview'](1,document,[c.value for c in inputs],None,'bet','open','net','iso',[],[c.id for c in inputs],'PNL')
        self.assertTrue(changed[3]);self.assertIsNone(changed[0])

    def test_http_save_and_history_callbacks_serialize_and_do_not_touch_real_accounts(self):
        app,cb=self.app()
        fields={c.id:c for c in components(app.layout) if isinstance(getattr(c,'id',None),str)}
        values={key:getattr(component,'value',None) for key,component in fields.items()}
        values.update({'sport':'PNL','pnl-token':uuid4().hex,'pnl-cash-save':1,'pnl-cash-account':'FanDuel','pnl-cash-kind':'opening',
            'pnl-cash-amount':1000,'pnl-cash-at':'2025-01-01','pnl-cash-notes':'Starting recorded balance','pnl-add':0,'pnl-settle':0,
            'pnl-import-confirm':0,'pnl-import-preview':None,'pnl-revision':None,'pnl-refresh':0,'pnl-account':'__all__'})
        with app.server.test_client() as client:
            for name,changed in [('pnl_save','pnl-cash-save.n_clicks'),('pnl_save','pnl-import-confirm.n_clicks'),('pnl_history','pnl-revision.data')]:
                if changed=='pnl-import-confirm.n_clicks':
                    imported=bet();imported['external_id']='statement-bet-1'
                    values.update({'pnl-cash-save':0,'pnl-import-confirm':1,'pnl-import-preview':[imported]})
                key,meta=next((key,m) for key,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**i,value=wire_value(i,values)) for i in meta['inputs']],state=[dict(**i,value=wire_value(i,values)) for i in meta['state']],changedPropIds=[changed]))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                result=response.get_json()['response']
                if name=='pnl_save':
                    values['pnl-revision']=result['pnl-revision']['data'];values['pnl-token']=result['pnl-token']['data']
                    self.assertIn('1 records saved',result['pnl-status']['children'])
                else:
                    self.assertIn('$1,000.00',str(result));self.assertEqual(len(result['pnl-events']['data']),2)
                    self.assertEqual(len(result['pnl-bets']['data']),1)


if __name__=='__main__':main()
