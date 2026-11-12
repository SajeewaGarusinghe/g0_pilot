#!/usr/bin/env python3
"""Run RCAEval's reference agent on the Gemini API free tier.

Same agent, prompt and tools as `rcaeval-agent run`; only the model call is
wrapped so free-tier limits do not break the run:

- requests are paced to stay under the per-minute limit (GEMINI_RPM);
- a per-minute 429 waits for the delay Google asks for and retries the same call;
- a per-day 429 stops cleanly. The case in progress is not written, so the
  next `./run_pilot.sh run` (for example tomorrow) redoes it and skips finished ones.

It also repairs RCAEval's tool specs for the Gemini path; see `fix_tool_specs`.

  python gemini_run.py --tasks pilot_tasks/pilot --model gemini/gemini-3.5-flash-lite \
      --out pilot_preds.jsonl
"""
import argparse
import os
import re
import sys
import threading
import time
from copy import deepcopy

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


def fix_tool_specs():
    """Stop LiteLLM's Gemini transform from breaking RCAEval's zero-argument tools.

    RCAEval shares one dict per tool between `react.TOOL_SPECS` (sent to the model)
    and `toolkit.TOOLS` (read by `CaseToolkit.call` to validate arguments). LiteLLM's
    Gemini transform strips empty `properties: {}` from the schema in place, because
    Gemini rejects it. The first Gemini call therefore deletes the key that
    `CaseToolkit.call` needs, and for the rest of the process every `describe_case`
    and `call_graph` call fails with `KeyError: 'properties'`.

    That matters: `describe_case` is the tool the prompt tells the agent to call
    first. Measured here on 2026-11-12 (RCAEval 1.12.0, LiteLLM 1.108.3), leaving it
    broken gave a 69.3% tool-call failure rate, forced 6 of 10 runs to guess at the
    30-step cap, and made repeat runs disagree at temperature 0 in 3 of 4 cases.
    With this copy in place the same 10 cases gave 0.5% and no capped runs.

    Giving the model its own copies leaves `toolkit.TOOLS` intact.
    """
    from RCAEval.agent import react
    from RCAEval.agent.toolkit import TOOLS

    react.TOOL_SPECS = [{"type": "function", "function": deepcopy(t)} for t in TOOLS]


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

    fix_tool_specs()
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
