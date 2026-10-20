"""College FanDuel snapshots, using the shared persistence and credit audit."""

from .nfl_markets import NFL_MARKETS, NflMarketService

# College coverage varies by bookmaker/jurisdiction/event. Never send extra
# discovery requests for missing props or substitute another bookmaker.
NCAAF_MARKETS = dict(NFL_MARKETS)


class NcaafMarketService(NflMarketService):
    sport = "NCAAF"
    sport_key = "americanfootball_ncaaf"
    markets = NCAAF_MARKETS
    team_codes = {}
    table = "ncaaf_snapshots"
