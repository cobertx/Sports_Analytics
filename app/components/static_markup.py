"""Escaped native markup for read-only tables without a Dash node per cell."""
from html import escape
import re
from urllib.parse import urlsplit

TABLE_TAGS={'a','button','caption','details','div','dl','dt','dd','img','p','small','span','strong','summary','table','tbody','td','th','thead','tr'}


def markup(component):
    if component is None or isinstance(component,bool):return ''
    if isinstance(component,(str,int,float)):return escape(str(component))
    if isinstance(component,(list,tuple)):return ''.join(markup(child) for child in component)
    value=component.to_plotly_json()
    if value['namespace']!='dash_html_components':
        raise TypeError('Static table markup only accepts native HTML components')
    tag=value['type'].lower();props=value['props'];attributes=[]
    if tag not in TABLE_TAGS:raise ValueError('Unsupported static table tag: '+tag)
    for key,value in props.items():
        if key in {'children','key','n_clicks','n_clicks_timestamp'} or value is None:continue
        if key=='id' and isinstance(value,dict):
            raise ValueError('Static table controls must use delegated events, not Dash IDs')
        if key.startswith('on'):raise ValueError('Inline handlers are not supported')
        key={'className':'class','htmlFor':'for'}.get(key,key.lower())
        if key in {'href','src'} and urlsplit(str(value)).scheme.lower() not in {'','http','https'}:
            raise ValueError('Unsupported table link protocol')
        if key=='style':
            value=';'.join(re.sub(r'[A-Z]',lambda m:'-'+m[0].lower(),name)+':'+str(style) for name,style in value.items())
        if isinstance(value,bool):
            if key.startswith('aria-') or key.startswith('data-'):value=str(value).lower()
            elif value:attributes.append(key);continue
            else:continue
        attributes.append(key+'="'+escape(str(value),quote=True)+'"')
    opening='<'+tag+(' '+' '.join(attributes) if attributes else '')+'>'
    if tag in {'img','br','hr','input','wbr'}:return opening[:-1]+'/>'
    return opening+markup(props.get('children'))+'</'+tag+'>'
