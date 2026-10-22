"""Typed, validated research settings shared by the editor and replay engine."""
from dataclasses import asdict, fields, is_dataclass, replace
from importlib import import_module
import math


def player_config(sport,key):
    folder,module,name={
        'whp':('Weighted_Historical_Performance','core','WHPConfig'),
        'uo':('Usage_Opportunity','config','UsageOpportunityConfig'),
        'oam':('Opponent_Adjusted_Matchup','config','MatchupConfig'),
        'pge':('Pace_Game_Environment','config','PaceConfig'),
    }[key]
    return getattr(import_module(f'models.Player_Stats.{folder}.{module}'),name).for_sport(sport)


def default_config(sport,group,key):
    if sport not in {'NBA','NFL','NCAAF'}:raise ValueError('Choose NBA, NFL or NCAAF.')
    if group=='player-stats':return player_config(sport,key)
    if group!='game-outcome':raise ValueError('Choose a supported strategy group.')
    from .backtesting_service import game_model
    return game_model(sport,key).config


def configured(default,overrides=None,prefix=''):
    """Rebuild frozen/nested dataclasses and apply their original validators."""
    if overrides is None:return default
    if not isinstance(overrides,dict):raise ValueError('Model parameters must be a settings object.')
    definitions={field.name:field for field in fields(default)}
    unknown=set(overrides)-set(definitions)
    if unknown:raise ValueError('Unknown model parameter: '+', '.join(sorted(unknown)))
    changes={}
    for name,value in overrides.items():
        previous=getattr(default,name);field=definitions[name];label=prefix+name
        if is_dataclass(previous):
            if not isinstance(value,dict):raise ValueError(label+' must contain nested model settings.')
            changes[name]=configured(previous,value,label+'.')
        elif field.type is int:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value:
                raise ValueError(label+' must be a finite whole number.')
            changes[name]=int(value)
        elif field.type is float:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise ValueError(label+' must be a finite number.')
            changes[name]=float(value)
        elif isinstance(previous,str):
            if not isinstance(value,str):raise ValueError(label+' must be a supported text option.')
            if name=='sport' and value!=previous:raise ValueError('Model sport must match the selected research sport.')
            changes[name]=value
        else:raise ValueError('Unsupported parameter type: '+label)
    try:return replace(default,**changes)
    except (ValueError,TypeError) as exc:raise ValueError(f'{prefix or type(default).__name__}: {exc}') from None


def parameter_fields(config,prefix=''):
    for field in fields(config):
        value=getattr(config,field.name);path=prefix+field.name
        if is_dataclass(value):yield from parameter_fields(value,path+'.')
        else:yield path,field,value


def form_parameters(sport,group,key,ids,values):
    default=default_config(sport,group,key)
    expected={path for path,_,_ in parameter_fields(default)}
    if len(ids)!=len(values) or len(ids)!=len(expected):raise ValueError('Wait for all model parameters to load before running.')
    paths=[item.get('path') for item in ids]
    if set(paths)!=expected or len(set(paths))!=len(paths):raise ValueError('The parameter editor does not match the selected strategy. Reset defaults and retry.')
    result={}
    for path,value in zip(paths,values):
        target=result;parts=path.split('.')
        for part in parts[:-1]:target=target.setdefault(part,{})
        target[parts[-1]]=value
    return asdict(configured(default,result))
