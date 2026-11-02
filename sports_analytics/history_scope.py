"""Small presentation identities. No data reads, numerical work, or cache policy."""


def analysis_scope(dataset, entity, metric, window, team, route):
    dataset = dataset or {}
    return dict(v=2, route=route or '', key=dataset.get('key'),
                revision=dataset.get('revision') or dataset.get('fetched_at'),
                sport=dataset.get('sport'), kind=dataset.get('kind'),
                season=dataset.get('season'), phase=dataset.get('phase'),
                week=dataset.get('week'), source_team=dataset.get('team') or '',
                entity=entity, metric=metric, window=window, team=team)


def career_scope(dataset, entity, metric, route):
    dataset = dataset or {}
    return dict(route=route or '', sport=dataset.get('sport'), kind=dataset.get('kind'),
                phase=dataset.get('phase'), entity=entity, metric=metric)
