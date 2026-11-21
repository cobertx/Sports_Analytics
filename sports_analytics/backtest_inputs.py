"""Pin the historical files a replay reads before evaluating its first fold."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from .result_cache import atomic_json


def source_files(root, sport, group, key):
    modeling=root/'data/modeling'
    league=modeling/sport.lower()
    patterns=['team_games_*.csv']
    if sport=='NBA' and group=='game-outcome':
        if key=='epa':patterns.extend(['epa/team_games_*.csv','epa/state_statistics_*.npz'])
        if key in {'pace','aode','ff'}:patterns.append('pace/team_games_*.csv')
    if sport=='NFL':
        patterns.append('raw/games.csv')
        if group=='player-stats':patterns.append('raw/snap_counts_*.csv')
        if key=='pge':patterns.append('raw/play_by_play_*.csv')
    if sport=='NCAAF':
        patterns.append('raw/games_*.json')
        if group=='player-stats':
            patterns.extend(['schools.json','player_games_*.csv','player_team_games_*.csv','transfers/*.csv'])
    files={file for pattern in patterns for file in league.glob(pattern) if file.is_file()}
    if group=='player-stats':
        whp=modeling/'player_stats/whp'
        imported=whp/'imports'/f'{sport.lower()}.csv'
        if imported.is_file():files.add(imported)
        if sport=='NCAAF' and key!='whp':files.update(whp.glob('ncaaf/*.json'))
    if key=='ila':
        availability=root/'data/information_variable/availability'
        files.update(availability.glob('nba.json'))
        files.update(availability.glob('archive/nba/**/*.json'))
        files.update((modeling/'player_stats/injury_lineup_adjusted').glob('lineups/**/*.json'))
    return sorted(files)


def backup_database(source, destination):
    if not source.exists():return
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)) as reader, \
            closing(sqlite3.connect(destination)) as writer:
        reader.backup(writer)


def pin_inputs(directory, root, primary_cache, role_cache, sport, group, key, revisions, check_running):
    """Publish a complete snapshot or leave the original data available for retry."""
    inputs=directory/'inputs'
    if inputs.exists():return inputs
    for stale in directory.glob('capture-*'):
        if stale.is_dir() and not stale.is_symlink():shutil.rmtree(stale,ignore_errors=True)
    with TemporaryDirectory(dir=directory,prefix='capture-') as scratch:
        capture=Path(scratch)/'inputs'
        capture.mkdir()
        for source in source_files(root,sport,group,key):
            check_running()
            destination=capture/source.relative_to(root)
            destination.parent.mkdir(parents=True,exist_ok=True)
            # Prepared files are generally replaced atomically. Verify the
            # revision was stable while copying sources written in place too.
            for _ in range(3):
                before=source.stat()
                shutil.copyfile(source,destination)
                after=source.stat()
                if (before.st_size,before.st_mtime_ns,before.st_ino)==(after.st_size,after.st_mtime_ns,after.st_ino):
                    break
            else:
                raise OSError(f'Historical input changed while saving the checkpoint: {source.name}')
            with destination.open('r+b') as copied:
                os.fsync(copied.fileno())
            os.utime(destination,ns=(before.st_atime_ns,before.st_mtime_ns))
        if group=='player-stats':
            backup_database(primary_cache,capture/'primary.sqlite3')
            if role_cache!=primary_cache:backup_database(role_cache,capture/'roles.sqlite3')
        check_running()
        atomic_json(capture/'sources.json',dict(revisions=revisions,separate_role_cache=role_cache!=primary_cache))
        capture.rename(inputs)
    return inputs


def release_inputs(directory):
    # Only the immutable inputs owned by this completed job are disposable.
    inputs=directory/'inputs'
    if inputs.is_dir() and not inputs.is_symlink():
        shutil.rmtree(inputs,ignore_errors=True)
