"""Download the top 100 FBS teams' logos as centered, transparent 500x500 PNGs.

Run: python download_ncaaf_logos.py
Requires requirements/requirements.txt and requirements/requirements-logos.txt.
Ranking: CollegeFootballData SRS (highest rating first), not an AP/CFP poll.
Uses CFBD_API_KEY from this project's .env; never calls The Odds API.
Docs: https://api.collegefootballdata.com/api/ratings
      https://api.collegefootballdata.com/api/teams
"""

import argparse
from datetime import date, datetime, timezone
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / 'images' / 'NCAAF' / 'Team Logos'
SIZE = (500, 500)
API = 'https://api.collegefootballdata.com'


def make_session():
    session = requests.Session()
    session.headers['User-Agent'] = 'SportsAnalytics-TeamLogoDownloader/1.0'
    retry = Retry(total=2, backoff_factor=.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=['GET'], respect_retry_after_header=False)
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def api_rows(session, endpoint, year, key):
    # Credentials apply only to CFBD, never to a third-party image host.
    try:
        response = session.get(API + endpoint, params={'year': year},
                               headers={'Authorization': f'Bearer {key}'}, timeout=(5, 30))
    except requests.RequestException:
        raise RuntimeError('Could not reach CollegeFootballData. Check connectivity and retry.') from None
    if response.status_code in (401, 403):
        raise RuntimeError('CollegeFootballData denied access. Check CFBD_API_KEY and access to SRS/team endpoints.')
    if response.status_code == 429:
        raise RuntimeError('CollegeFootballData request quota reached. Retry when your quota resets.')
    if not response.ok:
        raise RuntimeError(f'CollegeFootballData {endpoint} returned HTTP {response.status_code}.')
    try:
        rows = response.json()
    except ValueError:
        raise RuntimeError('CollegeFootballData returned invalid JSON.') from None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError('CollegeFootballData returned an unexpected response schema.')
    return rows


def top_teams(ratings, teams, count):
    """Join exact school names to the season's FBS directory; never guess IDs."""
    directory = {str(t.get('school', '')).strip().casefold(): t for t in teams}
    candidates = {}
    for row in ratings:
        name = str(row.get('team', '')).strip().casefold()
        try:
            rating = float(row['rating'])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(rating) or name not in directory:
            continue
        candidates.setdefault(name, []).append((rating, row))
    ranked = []
    for name, matches in candidates.items():
        if len(matches) > 1:
            # CFBD can return another same-name school with no FBS affiliation.
            # Use the selected season's conference to resolve that identity.
            conference = str(directory[name].get('conference') or '').casefold()
            matches = [(rating, row) for rating, row in matches
                       if conference and str(row.get('conference') or '').casefold() == conference]
        values = {rating for rating, row in matches}
        if len(values) != 1:
            raise RuntimeError(f'Ambiguous SRS entry for {directory[name]["school"]}; check the provider data.')
        ranked.append((values.pop(), directory[name]))
    ordered = sorted(ranked, key=lambda item: (-item[0], item[1]['school'].casefold()))
    return [dict(team=team, rating=rating, rank=rank)
            for rank, (rating, team) in enumerate(ordered[:count], start=1)]


def choose_season(session, key, year, count):
    # An explicit --year is never substituted with a different season.
    candidates = [year] if year is not None else [date.today().year, date.today().year - 1]
    for candidate in candidates:
        ratings = api_rows(session, '/ratings/srs', candidate, key)
        teams = api_rows(session, '/teams/fbs', candidate, key) if ratings else []
        selected = top_teams(ratings, teams, count)
        if len(selected) == count:
            return candidate, selected
        print(f'{candidate}: only {len(selected)} eligible rated FBS teams; need {count}.', flush=True)
        if candidate != candidates[-1]:
            print('Trying the previous season instead.', flush=True)
    raise RuntimeError('Not enough SRS-rated teams in the requested season(s). Try --year with another season.')


def filename(team):
    name = ' '.join(str(part).strip() for part in (team['school'], team.get('mascot')) if part)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('. ')
    if not name or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                                    *(f'COM{i}' for i in range(1, 10)),
                                                    *(f'LPT{i}' for i in range(1, 10))}:
        raise RuntimeError('The source returned an unusable team filename.')
    return name + '.png'


def valid_png(path):
    try:
        with Image.open(path) as image:
            image.load()
            return (image.format == 'PNG' and image.size == SIZE and image.mode == 'RGBA'
                    and image.getchannel('A').getbbox() is not None)
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def atomic_write(path, content):
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_logo(session, urls, destination):
    for url in dict.fromkeys(url for url in urls if isinstance(url, str) and url.startswith('https://')):
        try:
            response = session.get(url, timeout=(5, 25))
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as source:
                source.load()
                image = ImageOps.exif_transpose(source).convert('RGBA')
            # Crop transparent padding, then fit proportionally without stretching.
            bounds = image.getchannel('A').getbbox()
            if bounds is None:
                continue
            image = image.crop(bounds)
            image.thumbnail(SIZE, Image.Resampling.LANCZOS)
            canvas = Image.new('RGBA', SIZE, (0, 0, 0, 0))
            canvas.alpha_composite(image, ((SIZE[0] - image.width) // 2, (SIZE[1] - image.height) // 2))
            buffer = BytesIO()
            canvas.save(buffer, format='PNG', optimize=True)
            atomic_write(destination, buffer.getvalue())
            return url
        except (requests.RequestException, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
            continue
    raise RuntimeError('No usable image from the team logo URLs.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--year', type=int, help='Exact SRS season; default tries this year, then the previous year')
    parser.add_argument('--count', type=int, default=100, help='Number of top-ranked FBS teams (default: 100)')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='Destination folder')
    parser.add_argument('--overwrite', action='store_true', help='Redownload even valid existing images')
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 200:
        parser.error('--count must be between 1 and 200')
    if args.year is not None and not 1897 <= args.year <= date.today().year:
        parser.error('--year must be between 1897 and the current year')
    load_dotenv(ROOT / '.env')
    key = os.environ.get('CFBD_API_KEY', '').strip()
    if not key:
        print('Set CFBD_API_KEY in the project .env file. Get a key at https://collegefootballdata.com/key.', file=sys.stderr)
        return 1
    try:
        with make_session() as session:
            year, selected = choose_season(session, key, args.year, args.count)
            output = args.output.expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            names = [filename(item['team']) for item in selected]
            if len({name.casefold() for name in names}) != len(names):
                raise RuntimeError('Duplicate team filenames from the provider; no images written.')
            print(f'Saving top {args.count} FBS teams by {year} SRS to {output}', flush=True)
            downloaded = skipped = failed = 0
            manifest = {'ranking_source': API + '/ratings/srs', 'ranking_method': 'SRS rating descending; school name breaks ties',
                        'season': year, 'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'teams': []}
            for item, name in zip(selected, names):
                team = item['team']
                destination = output / name
                record = {'rank': item['rank'], 'rating': item['rating'], 'school': team['school'],
                          'team_id': team.get('id'), 'filename': name}
                if not args.overwrite and valid_png(destination):
                    skipped += 1
                    record['status'] = 'already saved'
                    print(f'Already saved: {destination}', flush=True)
                else:
                    try:
                        record['logo_url'] = save_logo(session, team.get('logos') or [], destination)
                        downloaded += 1
                        record['status'] = 'downloaded'
                        print(f'[{item["rank"]:3}] Downloaded: {name}', flush=True)
                    except (RuntimeError, OSError) as exc:
                        failed += 1
                        record['status'] = 'failed'
                        print(f'[{item["rank"]:3}] Failed: {name} — {exc}', flush=True)
                manifest['teams'].append(record)
            atomic_write(output / '_download_manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8'))
            ready = sum(valid_png(output / name) for name in names)
            print(f'Finished: {downloaded} downloaded, {skipped} already saved, {failed} failed. '
                  f'{ready}/{args.count} selected logos ready in {output}', flush=True)
            return 1 if failed or ready != args.count else 0
    except (RuntimeError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
