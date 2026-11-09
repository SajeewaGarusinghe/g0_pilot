#!/usr/bin/env python3
"""Run RCAEval's reference agent on the Gemini API free tier.

Same agent, prompt and tools as `rcaeval-agent run`; only the model call is
wrapped so free-tier limits do not break the run:

- requests are paced to stay under the per-minute limit (GEMINI_RPM);
- a per-minute 429 waits for the delay Google asks for and retries the same call;
- a per-day 429 stops cleanly. The case in progress is not written, so the
  next `./run_pilot.sh run` (for example tomorrow) redoes it and skips finished ones.

  python gemini_run.py --tasks pilot_tasks/pilot --model gemini/gemini-3.5-flash-lite \
      --out pilot_preds.jsonl
"""
import argparse
import os
import re
import sys
import threading
import time

EXIT_DAILY_QUOTA = 75   # run_pilot.sh reads this to print "rerun tomorrow"


class DailyQuotaReached(Exception):
    pass


def is_daily(msg):
    return bool(re.search(r"PerDay|per day|daily", msg, re.I))


def retry_delay(msg, default):
    m = re.search(r'retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', msg) or re.search(r"retry in (\d+(?:\.\d+)?)\s*s", msg, re.I)
    return float(m.group(1)) + 1 if m else default


def paced_completion(base, rpm, max_retries=8, sleep=time.sleep, clock=time.monotonic):
    """Wrap a LiteLLM-style completion with pacing, per-minute retry and daily-quota stop."""
    gap = 60.0 / max(rpm, 1)
    lock = threading.Lock()
    last = [float("-inf")]

    def call(**kwargs):
        for attempt in range(max_retries + 1):
            with lock:
                wait = last[0] + gap - clock()
                if wait > 0:
                    sleep(wait)
                last[0] = clock()
            try:
                return base(**kwargs)
            except Exception as e:  # LiteLLM maps Google 429s to RateLimitError
                msg = str(e)
                name = type(e).__name__
                limited = name == "RateLimitError" or "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if not limited:
                    if name in ("ServiceUnavailableError", "InternalServerError", "APIConnectionError", "Timeout") \
                            and attempt < max_retries:
                        sleep(min(60, 5 * 2 ** attempt))
                        continue
                    raise
                if is_daily(msg):
                    raise DailyQuotaReached(msg[:300]) from e
                if attempt == max_retries:
                    raise
                sleep(retry_delay(msg, default=min(90, 10 * 2 ** attempt)))
        raise RuntimeError("unreachable")

    return call


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--prompt-version", default="2", choices=["1", "2"])
    ap.add_argument("--rpm", type=float, default=float(os.environ.get("GEMINI_RPM", 12)))
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()

    if a.model.startswith("gemini/") and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("GEMINI_API_KEY is not set. Put it in .env (see .env.example).")

    from RCAEval.agent.react import litellm_completion, run_tasks

    completion = paced_completion(litellm_completion(), a.rpm)
    try:
        run_tasks(a.tasks, a.model, a.out, cases=a.cases or None, workers=a.workers,
                  completion=completion, max_steps=a.max_steps, max_cost_usd=None,
                  temperature=a.temperature, prompt_version=a.prompt_version)
    except DailyQuotaReached as e:
        print(f"\nDaily free-tier quota reached: {e}\nFinished cases are saved in {a.out}. "
              f"Rerun the same command after the quota resets (midnight Pacific time).", file=sys.stderr)
        sys.exit(EXIT_DAILY_QUOTA)


if __name__ == "__main__":
    main()
