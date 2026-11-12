"""Offline tests for the free-tier wrapper in gemini_run.py: pacing, per-minute retry, daily stop.
Run: python tests/test_gemini_run.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_run import DailyQuotaReached, paced_completion  # noqa: E402


class RateLimitError(Exception):
    pass


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(round(s, 2))
        self.t += s

    def now(self):
        return self.t


def test_pacing():
    c = FakeClock()
    call = paced_completion(lambda **k: "ok", rpm=12, sleep=c.sleep, clock=c.now)
    for _ in range(3):
        assert call() == "ok"
    assert c.slept == [5.0, 5.0], c.slept   # 60/12 s between calls


def test_per_minute_retry():
    c = FakeClock()
    n = {"calls": 0}

    def base(**k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RateLimitError('429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier '
                                 '"retryDelay": "17s"')
        return "ok"

    call = paced_completion(base, rpm=60, sleep=c.sleep, clock=c.now)
    assert call() == "ok" and n["calls"] == 2
    assert 18.0 in c.slept, c.slept          # waited for Google's retryDelay + 1 s


def test_daily_stop():
    c = FakeClock()

    def base(**k):
        raise RateLimitError("429 quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    call = paced_completion(base, rpm=60, sleep=c.sleep, clock=c.now)
    try:
        call()
    except DailyQuotaReached:
        return
    raise AssertionError("daily quota should stop the run")


def test_other_errors_raise():
    def base(**k):
        raise ValueError("bad request")

    call = paced_completion(base, rpm=60, sleep=lambda s: None, clock=lambda: 0.0)
    try:
        call()
    except ValueError:
        return
    raise AssertionError("non-rate-limit errors must propagate")


if __name__ == "__main__":
    for t in (test_pacing, test_per_minute_retry, test_daily_stop, test_other_errors_raise):
        t()
        print("ok", t.__name__)
