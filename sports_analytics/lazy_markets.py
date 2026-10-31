"""Bounded server-side row renderers; the browser carries only scoped view IDs."""
from collections import OrderedDict
from threading import RLock
from uuid import uuid4

from dash import Input, Output, State, MATCH, dcc, html
from dash.exceptions import PreventUpdate


class LazyMarkets:
    def __init__(self,capacity=32):
        self.lock=RLock();self.views=OrderedDict();self.capacity=capacity

    def view(self):
        key=uuid4().hex
        with self.lock:
            self.views[key]={}
            while len(self.views)>self.capacity:self.views.popitem(last=False)
        return key

    def exists(self,view):
        with self.lock:return view in self.views

    def update(self,view,section,render):
        with self.lock:
            if view not in self.views or section not in self.views[view]:return None
            old=self.views[view][section]
            self.views[view][section]=(render,*old[1:])
            return old[4]

    def panel(self,view,section,render,total,*,initial=True,size=6,step=50):
        identity=dict(view=view,section=section)
        with self.lock:self.views[view][section]=(render,total,size,step,min(size,total) if initial else 0)
        return html.Div([
            html.Button('',id=dict(type='lazy-market-trigger',**identity),n_clicks=0,
                        style={'display':'none'},tabIndex=-1,**{'aria-hidden':'true'}),
            dcc.Loading(html.Div(render(size) if initial else html.P('Open this market to load its selections.',role='status'),
                id=dict(type='lazy-market-body',**identity),**{'data-ready':'true' if initial else 'false'}),
                delay_show=150,color='#57dbc4'),
            html.Button('Show more selections',id=dict(type='lazy-market-more',**identity),n_clicks=0,
                className='market-load-more',style={} if initial and total>size else {'display':'none'}),
        ],className='lazy-market-body')

    def read(self,identity,pages):
        with self.lock:
            view=self.views.get(identity['view'])
            if view is None or identity['section'] not in view:
                return html.P('This view expired. Reopen the category to reload selections.',role='status'),'true',{'display':'none'},''
            self.views.move_to_end(identity['view'])
            render,total,size,step,previous=view[identity['section']]
            count=min(total,max(previous,size+step*max(0,int(pages or 0))))
            view[identity['section']]=(render,total,size,step,count)
        # A live quote/model update can replace the renderer while rows are being
        # built. Publish from the newest source instead of reinstating old prices.
        for _ in range(3):
            children=render(count)
            with self.lock:
                latest=self.views.get(identity['view'],{}).get(identity['section'])
                if latest is None or (latest[0] is render and latest[4]==count):break
                render,total,size,step,count=latest
        else:
            return html.P('Quotes are updating. Reload the selections to use the latest values.',role='status'),'false',{},'Reload selections'
        return children,'true',{} if count<total else {'display':'none'},f'Show more ({total-count} remaining)'


markets=LazyMarkets()


def register(app):
    pattern=lambda kind:dict(type=kind,view=MATCH,section=MATCH)
    @app.callback(Output(pattern('lazy-market-body'),'children'),Output(pattern('lazy-market-body'),'data-ready'),
                  Output(pattern('lazy-market-more'),'style'),Output(pattern('lazy-market-more'),'children'),
                  Input(pattern('lazy-market-trigger'),'n_clicks'),Input(pattern('lazy-market-more'),'n_clicks'),
                  State(pattern('lazy-market-body'),'id'),prevent_initial_call=True)
    def lazy_market_rows(opened,pages,identity):
        if not opened and not pages:raise PreventUpdate
        return markets.read(identity,pages)
