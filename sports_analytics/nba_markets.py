"""FanDuel NBA alternate lines, using the shared click-only snapshot pipeline."""

from .nfl_markets import NflMarketService

# Official event-odds market keys. One bookmaker; at most 13 market credits.
# Availability depends on FanDuel and proximity to tipoff. No extra requests
# are sent to discover missing markets or fill gaps in a saved response.
NBA_MARKETS = {
    "alternate_spreads": ("Game", "Alternate spreads"),
    "alternate_totals": ("Game", "Alternate points total"),
    "player_points_alternate": ("Points", "Alternate points"),
    "player_rebounds_alternate": ("Rebounds", "Alternate rebounds"),
    "player_assists_alternate": ("Assists", "Alternate assists"),
    "player_threes_alternate": ("Threes", "Alternate three-pointers"),
    "player_blocks_alternate": ("Defense", "Alternate blocks"),
    "player_steals_alternate": ("Defense", "Alternate steals"),
    "player_turnovers_alternate": ("Turnovers", "Alternate turnovers"),
    "player_points_assists_alternate": ("Combos", "Alternate points + assists"),
    "player_points_rebounds_alternate": ("Combos", "Alternate points + rebounds"),
    "player_rebounds_assists_alternate": ("Combos", "Alternate rebounds + assists"),
    "player_points_rebounds_assists_alternate": ("Combos", "Alternate points + rebounds + assists"),
}
NBA_CATEGORIES = [("Game", "Game Lines"), ("Points", "Points"), ("Rebounds", "Rebounds"),
                  ("Assists", "Assists"), ("Threes", "Threes"), ("Combos", "Combos"),
                  ("Defense", "Defense"), ("Turnovers", "Turnovers")]
NBA_TEAM_CODES = dict(zip([
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls",
    "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons", "Golden State Warriors",
    "Houston Rockets", "Indiana Pacers", "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies",
    "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks",
    "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers",
    "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors", "Utah Jazz", "Washington Wizards"],
    "ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS".split()))
NBA_TEAM_CODES["LA Clippers"] = "LAC"


class NbaMarketService(NflMarketService):
    sport = "NBA"
    sport_key = "basketball_nba"
    markets = NBA_MARKETS
    team_codes = NBA_TEAM_CODES
    table = "nba_snapshots"
