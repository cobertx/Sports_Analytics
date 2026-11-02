"""Offline navigation dispatch benchmark; no models or external API requests.

Runs the registered client routing/scoping functions and replays the resulting
requests through real Dash HTTP endpoints with no-op data handlers. This measures
callback scheduling, request bytes and framework dispatch, not browser latency or
model execution. The baseline fixture is the actual pre-change callback graph.
"""
from collections import Counter
import argparse
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import ALL,MATCH,ALLSMALLER,Dash,Input,Output,State,html,no_update
from check_navigation_pipeline import ROOT,graph,node,run_client
import check_market_transitions as fixtures


def component_id(value):
    if not value.startswith('{'):return value
    parsed=json.loads(value)
    return {key:{'ALL':ALL,'MATCH':MATCH,'ALLSMALLER':ALLSMALLER}[v[0]] if isinstance(v,list) else v for key,v in parsed.items()}


def measure(records,steps,label,repeats):
    names={name for step in steps for name in step[label]}
    records=[item for item in records if item['name'] in names]
    app=Dash(__name__);outputs=[];endpoints={};calls=Counter()
    for i,record in enumerate(records):
        identity='navigation-benchmark-'+str(i);outputs.append(html.Div(id=identity))
        def handler(*args,name=record['name']):calls[name]+=1;return no_update
        app.callback(Output(identity,'children'),
            *[Input(component_id(item['id']),item['property']) for item in record['inputs']],
            *[State(component_id(item['id']),item['property']) for item in record['state']])(handler)
        endpoints[record['name']]=(identity,record)
    app.layout=html.Div(outputs)
    samples=[];client=app.server.test_client();client.get('/_dash-layout')
    for repetition in range(repeats+1):
        calls.clear();request_bytes=response_bytes=0;start=perf_counter()
        for step in steps:
            for name in step[label]:
                identity,record=endpoints[name]
                def value(item):return [] if item['id'].startswith('{') else step['values'].get(item['id'])
                body=dict(output=identity+'.children',outputs=dict(id=identity,property='children'),
                    inputs=[dict(**item,value=value(item)) for item in record['inputs']],
                    state=[dict(**item,value=value(item)) for item in record['state']],
                    changedPropIds=[item['id']+'.'+item['property'] for item in record['inputs'] if item['id'] in step['updates']])
                encoded=json.dumps(body,separators=(',',':')).encode();request_bytes+=len(encoded)
                response=client.post('/_dash-update-component',data=encoded,content_type='application/json')
                if response.status_code not in {200,204}:raise AssertionError(response.get_data(as_text=True))
                if response.status_code==200 and response.get_json().get('response'):raise AssertionError('Unexpected data output')
                response_bytes+=len(response.data)
        elapsed=(perf_counter()-start)*1000
        if repetition:samples.append(round(elapsed,3))
    return dict(callback_invocations=sum(calls.values()),request_bytes=request_bytes,response_bytes=response_bytes,
                framework_dispatch_samples_ms=samples,framework_dispatch_median_ms=round(median(samples),3),by_callback=dict(calls))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--exchange-history',action='store_true',help='Also measure six exchange/history/Back events separately')
    args=parser.parse_args()
    if args.repeats<1:parser.error('repeats must be positive')
    if not node():parser.error('Node is required; no dependencies will be installed')
    fixture=fixtures.MarketTransitionChecks();fixture.setUp()
    try:
        before=json.loads((ROOT/'scripts/fixtures/navigation_before.json').read_text())
        after=graph(fixture.app);result=run_client(fixture.app,before=before)
        reference=measure(before,result['steps'],'before',args.repeats)
        updated=measure(after,result['steps'],'after',args.repeats)
        if updated['callback_invocations']>119:raise AssertionError('Original warm sequence exceeded 119 server callbacks')
        reduction=1-updated['callback_invocations']/reference['callback_invocations']
        if reduction<.6:raise AssertionError('Callback invocation reduction below 60%')
        def subscribers(records,identity):return sum(any(i['id']==identity for i in r['inputs']) for r in records)
        report=dict(conditions=dict(repeats=args.repeats,network='blocked by fixture',browser_measured=False,
            workload='11 warm navigation/category events; real client scope functions and Dash HTTP; no-op data handlers',
            limitation='Framework dispatch and control metadata only; excludes model execution, real market payloads and browser rendering'),
            before=reference,after=updated,invocation_reduction=reduction,
            direct_url_subscribers=dict(before=subscribers(before,'market-url'),after=subscribers(after,'market-url')),
            direct_page_subscribers=dict(before=subscribers(before,'workspace-page'),after=subscribers(after,'workspace-page')),
            steps=[{k:v for k,v in step.items() if k!='values'} for step in result['steps']])
        if args.exchange_history:
            steps=[]
            for provider,game in [('kalshi','KXNFLGAME-TEST'),('polymarket','456')]:
                route=f'#nfl-{provider}-game-{game}'
                steps.extend([dict(label=provider+' game',hash=route),
                    dict(label=provider+' history',hash='#historical-player?sport=NFL&name=Player+A&market=player_reception_yds'),
                    dict(label=provider+' Back',hash=route)])
            exchange=run_client(fixture.app,steps=steps)
            report['exchange_history']=measure(after,exchange['steps'],'after',args.repeats)
            report['exchange_history']['events']=len(steps)
            report['exchange_history']['limitation']='Scoped framework dispatch only; prompt restoration and real quotes are exercised separately in offline UX fixtures.'
        print(json.dumps(report,indent=2))
    finally:fixture.doCleanups()


if __name__=='__main__':main()
