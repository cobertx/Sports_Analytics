"""Download NBA team logos as transparent 500x500 PNGs, once or on demand.

Run: python download_nba_logos.py
Optional: --overwrite or --output "another folder"
Requires Pillow: python -m pip install -r requirements-logos.txt
Uses ESPN's public team directory and image CDN; no API key is required.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:
    raise SystemExit("Install Pillow first: python -m pip install -r requirements-logos.txt") from None


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "images" / "NBA" / "Team Logos"
DIRECTORY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=1000"
SIZE = (500, 500)


def download(url):
    request = Request(url, headers={"User-Agent": "SportsAnalyticsLogoDownloader/1.0"})
    with urlopen(request, timeout=25) as response:
        return response.read(20 * 1024 * 1024 + 1)


def team_logos():
    """Return one primary source per current team, with Windows-safe filenames."""
    payload = json.loads(download(DIRECTORY_URL))
    entries = payload["sports"][0]["leagues"][0]["teams"]
    teams = {}
    for entry in entries:
        team = entry["team"]
        name = str(team["displayName"]).strip()
        # Match the canonical full name used by the NBA market page.
        name = {"LA Clippers": "Los Angeles Clippers"}.get(name, name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
        logos = team.get("logos") or []
        primary = next((logo for logo in logos if "primary" in logo.get("rel", [])), logos[0] if logos else {})
        url = primary.get("href", "")
        if not name or not url.startswith("https://a.espncdn.com/"):
            raise ValueError(f"Missing team name or ESPN logo source for team ID {team.get('id', '?')}.")
        if name.casefold() in teams:
            raise ValueError(f"Duplicate NBA team name: {name}.")
        teams[name.casefold()] = (name, url)
    if len(teams) != 30:
        raise ValueError(f"Expected 30 NBA teams, received {len(teams)}; no downloads were started.")
    return sorted(teams.values())


def valid_png(path):
    try:
        with Image.open(path) as picture:
            if picture.format != "PNG" or picture.size != SIZE:
                return False
            picture.verify()
        with Image.open(path) as picture:
            picture.load()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def save_logo(name, url, output, overwrite=False):
    destination = output / f"{name}.png"
    if not overwrite and valid_png(destination):
        return "Already saved", name
    raw = download(url)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("Logo source exceeds the 20 MB limit.")
    with Image.open(BytesIO(raw)) as source:
        source.load()
        logo = ImageOps.contain(source.convert("RGBA"), SIZE, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    canvas.alpha_composite(logo, ((SIZE[0] - logo.width) // 2, (SIZE[1] - logo.height) // 2))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            canvas.save(file, format="PNG", optimize=True)
        if not valid_png(temporary):
            raise ValueError("Saved image failed PNG/dimension validation.")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return "Downloaded", name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output folder (default: images/NBA/Team Logos)")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing valid 500x500 PNGs")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    try:
        teams = team_logos()
        output.mkdir(parents=True, exist_ok=True)
    except (HTTPError, URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"Could not prepare NBA logo downloads: {exc}")
        return 1
    print(f"Saving {len(teams)} NBA team logos to {output}")
    downloaded = skipped = failed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(save_logo, name, url, output, args.overwrite): name for name, url in teams}
        for task in as_completed(tasks):
            name = tasks[task]
            try:
                status, name = task.result()
                downloaded += status == "Downloaded"
                skipped += status == "Already saved"
                print(f"{status}: {output / (name + '.png')}")
            except (HTTPError, URLError, OSError, ValueError, UnidentifiedImageError) as exc:
                failed += 1
                print(f"FAILED: {name}: {exc}")
    print(f"Finished: {downloaded} downloaded, {skipped} already valid, {failed} failed.")
    print(f"{downloaded + skipped} valid 500x500 PNG files ready in: {output}")
    if skipped:
        print("Already saved means the PNG exists and passed validation. Use --overwrite to download it again.")
    if failed:
        print("Rerun to retry failed/missing logos; valid files will be skipped.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
