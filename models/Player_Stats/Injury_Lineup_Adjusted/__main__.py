"""Run ILA on a saved NBA market snapshot."""

from models.Player_Stats.Playing_Time_Rotation.__main__ import main

from .prediction_service import InjuryLineupMarketService


if __name__ == "__main__":
    main(InjuryLineupMarketService, description=__doc__)
