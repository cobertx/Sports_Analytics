"""Repeat blocked-work HTTP checks; no external requests or browser timing claims."""
import argparse
from contextlib import redirect_stdout,redirect_stderr
import io
import json
import re
from statistics import median
import unittest

from check_progressive_loading import ProgressiveLoadingChecks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=5)
    args=parser.parse_args()
    if args.repeats<1:parser.error('repeats must be positive')
    samples={}
    tests=['test_career_http_is_independent_and_deduplicates_pending_work',
           'test_kalshi_http_returns_pending_without_waiting_and_reuses_exact_completed_result',
           'test_summary_quotes_render_while_models_are_blocked_and_recalculation_retries']
    for _ in range(args.repeats):
        stream=io.StringIO()
        with redirect_stdout(stream),redirect_stderr(stream):
            result=unittest.TextTestRunner(stream=stream).run(unittest.TestSuite(ProgressiveLoadingChecks(name) for name in tests))
        if not result.wasSuccessful():raise AssertionError(stream.getvalue())
        for name,value in re.findall(r'(Career|Kalshi|Market Summary) HTTP submit: ([0-9.]+) ms',stream.getvalue()):
            samples.setdefault(name,[]).append(float(value))
    assert set(samples)=={'Career','Kalshi','Market Summary'} and all(len(values)==args.repeats for values in samples.values())
    print(json.dumps(dict(conditions=dict(repeats=args.repeats,network='blocked/mocked local fixtures',browser_measured=False,
        workload='Real Dash HTTP submission while the calculation/source is held by an Event; no arbitrary delay',
        limitation='Submission latency only; excludes real model fitting, download time and browser painting'),
        submit={name:dict(samples_ms=values,median_ms=median(values)) for name,values in samples.items()},
        correctness='Exact completed-result equivalence, single-flight reuse, independent chart/quote availability checked each repetition'),indent=2))


if __name__=='__main__':main()
