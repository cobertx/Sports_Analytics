"""Incremental WHP -> Usage/Opportunity -> Opponent/Matchup evaluation."""
from .backtesting import walk_forward_backtest


def compare_models(services,snapshots,actuals):
    reports={}
    for name,service in services.items():
        try:reports[name]=walk_forward_backtest(service,snapshots,actuals)
        except (ValueError,RuntimeError,OSError) as exc:reports[name]={'unavailable':str(exc)}
    keysets=[{(r['game_id'],r['subject'],r['market'],r['line'],r['side'],r['snapshot_timestamp']) for r in report.get('records',[])}
             for report in reports.values()]
    common=set.intersection(*keysets) if keysets else set()
    # Report coverage before comparing metrics: one model must not look better
    # merely because it skipped difficult player/market observations.
    for report in reports.values():
        report['common_quote_count']=len(common)
        records=[r for r in report.get('records',[]) if (r['game_id'],r['subject'],r['market'],r['line'],r['side'],r['snapshot_timestamp']) in common]
        if records:
            from models.Player_Stats.Usage_Opportunity.calibration import diagnostics
            report['common_quote_metrics']=diagnostics(records)
    return reports
