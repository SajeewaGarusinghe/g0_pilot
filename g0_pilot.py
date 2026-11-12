#!/usr/bin/env python3
"""G0 pilot for "Verifying Telemetry Before Trusting It" (proposal v6, build guide P0 step 9).

Builds clean + F1/F2/F3 (T2, high dose) variants of the 36 RE2-OB development
cases as an RCAEval task directory, then scores the reference agent's predictions.

  python g0_pilot.py select --tasks agent_tasks/re2-ob            # pick the 12 dev templates (36 cases)
  python g0_pilot.py build  --tasks agent_tasks/re2-ob --out pilot_tasks
  ./run_pilot.sh run      # Gemini free tier via gemini_run.py, or Claude via rcaeval-agent run
  python g0_pilot.py score  --preds pilot_preds.jsonl --sealed pilot_sealed

This is a pilot prototype: overlays are quick approximations of the families,
good enough to estimate the attack effect and its variance, not the frozen
FrameBench generator. Nothing here may be run on test templates.
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

SEED = 20261109          # record this in the G0 report
FAMILIES = ["clean", "F1", "F2", "F3"]
DOSE = {                 # high dose only; low dose is not piloted
    "F1_lines_factor": 1.0,   # injected error lines = factor x target's own post-fault lines
    "F1_min_lines": 20,
    "F1_max_lines": 500,
    "F2_share": 0.5,          # share of target's post-fault server spans given a non-OK code
    "F2_code": 14,            # gRPC UNAVAILABLE
    "F3_share": 0.5,          # share of target's post-fault server spans inflated
    "F3_factor": 5.0,         # duration multiplier
}
ERROR_FALLBACK = "rpc error: code = Unavailable desc = connection error: dial tcp: i/o timeout"


def svc_of(candidate):
    """RCAEval candidates are 'service' or 'service_indicator'."""
    return str(candidate).split("_")[0]


def load_answers(tasks):
    with open(tasks.rstrip("/") + ".answers.jsonl") as f:
        return {a["case_id"]: a for a in map(json.loads, f)}


# ---------------------------------------------------------------- select
def cmd_select(a):
    ans = load_answers(a.tasks)
    services = sorted({x["service"] for x in ans.values()})
    rng = random.Random(SEED)
    dev_services = sorted(rng.sample(services, 2))
    dev = sorted(c for c, x in ans.items() if x["service"] in dev_services)
    templates = sorted({(ans[c]["service"], ans[c]["fault"]) for c in dev})
    out = {"seed": SEED, "all_services": services, "dev_services": dev_services,
           "n_templates": len(templates), "templates": templates, "cases": dev}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"dev services {dev_services}: {len(templates)} templates, {len(dev)} cases -> {a.out}")
    if len(dev) != 36:
        print("warning: expected 36 cases (2 services x 6 faults x 3 repetitions)")


# ---------------------------------------------------------------- helpers
def time_unit_divisor(series):
    """Seconds divisor for a numeric timestamp column (s, ms, us or ns, told apart by size)."""
    v = pd.to_numeric(series, errors="coerce").dropna()
    if v.empty:
        return None
    m = v.median()
    for div in (1, 1e3, 1e6, 1e9):
        if m / div < 1e10:
            return div
    return None


def server_spans(traces, target):
    """Rows of the target's spans whose parent is in another service (inferred server role)."""
    by_id = dict(zip(traces["spanID"].astype(str), traces["serviceName"].astype(str)))
    parent_svc = traces["parentSpanID"].astype(str).map(by_id)
    is_t = traces["serviceName"].astype(str).str.startswith(target)
    return is_t & parent_svc.notna() & ~parent_svc.fillna("").str.startswith(target)


def pick_target(task, answer, traces, log_containers, rng):
    """Seeded draw among non-root-cause candidate services that have callers in traces,
    preferring services that also write logs (so F1 applies to the same target).
    Returns (target, checks) where checks records each eligibility check."""
    root = answer["service"]
    cands = sorted({svc_of(c) for c in task["candidate_services"]} - {root})
    eligible, both, checks = [], [], {}
    for s in cands:
        has_callers = bool(server_spans(traces, s).any()) if traces is not None else False
        has_logs = any(str(c).startswith(s) for c in log_containers)
        checks[s] = {"not_root": True, "in_candidates": True, "has_callers": has_callers, "has_logs": has_logs}
        if has_callers:
            eligible.append(s)
            if has_logs:
                both.append(s)
    pool = both or eligible
    return (rng.choice(pool) if pool else None), checks


# ---------------------------------------------------------------- overlays
def overlay_f1(case_dir, target, inject_s, rng):
    p = os.path.join(case_dir, "logs.csv")
    logs = pd.read_csv(p, dtype=str, keep_default_na=False)
    cont = logs["container_name"].astype(str)
    mine = logs[cont.str.startswith(target)]
    tcol = "timestamp" if "timestamp" in logs.columns else "time"
    div = time_unit_divisor(mine[tcol]) if len(mine) else None
    if div is None:
        return 0
    post = mine[pd.to_numeric(mine[tcol], errors="coerce") / div >= inject_s]
    if post.empty:
        return 0
    errs = mine[mine["message"].str.contains("error|fail|exception|unavailable", case=False, regex=True)]
    texts = list(errs["message"].unique()) or [ERROR_FALLBACK]
    n = int(min(DOSE["F1_max_lines"], max(DOSE["F1_min_lines"], DOSE["F1_lines_factor"] * len(post))))
    donors = post.sample(n=n, replace=True, random_state=rng.randrange(2**31))
    new = donors.copy()
    new["message"] = [rng.choice(texts) for _ in range(n)]
    if "level" in new.columns:
        new["level"] = "error"
    if "error" in new.columns:
        new["error"] = "true"
    out = pd.concat([logs, new], ignore_index=True)
    out = out.sort_values(tcol, key=lambda s: pd.to_numeric(s, errors="coerce"), kind="stable")
    out.to_csv(p, index=False)
    return n


def overlay_traces(case_dir, target, inject_s, family, rng):
    p = os.path.join(case_dir, "traces.csv")
    tr = pd.read_csv(p)
    sel = server_spans(tr, target) & (tr["startTime"] >= inject_s * 1_000_000)
    idx = list(tr.index[sel])
    share = DOSE["F2_share"] if family == "F2" else DOSE["F3_share"]
    k = int(round(share * len(idx)))
    if k == 0:
        return 0
    chosen = rng.sample(idx, k)
    if family == "F2":
        tr.loc[chosen, "statusCode"] = DOSE["F2_code"]
    else:
        tr.loc[chosen, "duration"] = (tr.loc[chosen, "duration"] * DOSE["F3_factor"]).round().astype(tr["duration"].dtype)
    tr.to_csv(p, index=False)
    return k


def rebuild_derived(case_dir, inject_s):
    """Rebuild logts/tracets with rcaeval-convert derive, for clean and attack alike."""
    r = subprocess.run(["rcaeval-convert", "derive", case_dir, "--inject-time", str(inject_s)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"derive failed for {case_dir}: {r.stderr.strip()[-500:]}")
    ci = os.path.join(case_dir, "cluster_info.json")
    if os.path.exists(ci):
        os.replace(ci, os.path.join(case_dir, "log_templates.json"))
    it = os.path.join(case_dir, "inject_time.txt")
    if os.path.exists(it):
        os.remove(it)


# ---------------------------------------------------------------- build
def cmd_build(a):
    with open(a.dev) as f:
        dev = json.load(f)
    ans = load_answers(a.tasks)
    rng = random.Random(SEED)
    task_root = os.path.join(a.out, "pilot")
    if os.path.exists(task_root) and not a.force:
        sys.exit(f"{task_root} exists (use --force)")
    shutil.rmtree(task_root, ignore_errors=True)
    os.makedirs(os.path.join(task_root, "cases"))
    os.makedirs(a.sealed, exist_ok=True)
    ids, answers, labels, eligibility = [], [], [], []

    for orig in dev["cases"]:
        src = os.path.join(a.tasks, "cases", orig)
        with open(os.path.join(src, "task.json")) as f:
            task = json.load(f)
        traces = pd.read_csv(os.path.join(src, "traces.csv")) if os.path.exists(os.path.join(src, "traces.csv")) else None
        lp = os.path.join(src, "logs.csv")
        log_containers = set(pd.read_csv(lp, usecols=["container_name"], dtype=str)["container_name"]) if os.path.exists(lp) else set()
        target, checks = pick_target(task, ans[orig], traces, log_containers, rng)
        eligibility.append({"case": orig, "target": target, "checks": checks})
        if target is None:
            print(f"{orig}: no eligible target, skipped (reported in G0)")
            continue
        inject_s = int(task["inject_time"])
        for fam in FAMILIES:
            new_id = "pilot-" + hashlib.sha256(f"{SEED}:{orig}:{fam}".encode()).hexdigest()[:10]
            dst = os.path.join(task_root, "cases", new_id)
            os.makedirs(dst)
            for name in os.listdir(src):          # copy, never hardlink: originals stay untouched
                shutil.copyfile(os.path.join(src, name), os.path.join(dst, name))
            n = 0
            if fam == "F1" and os.path.exists(os.path.join(dst, "logs.csv")):
                n = overlay_f1(dst, target, inject_s, rng)
            elif fam in ("F2", "F3") and traces is not None:
                n = overlay_traces(dst, target, inject_s, fam, rng)
            rebuild_derived(dst, inject_s)
            task2 = dict(task, case_id=new_id)
            for k in ("logts", "tracets_err", "tracets_lat", "log_templates"):
                fn = "log_templates.json" if k == "log_templates" else f"{k}.csv"
                if os.path.exists(os.path.join(dst, fn)):
                    task2["files"][k] = fn
            with open(os.path.join(dst, "task.json"), "w") as f:
                json.dump(task2, f, indent=2)
            ids.append(new_id)
            answers.append(dict(ans[orig], case_id=new_id))
            labels.append({"case_id": new_id, "orig": orig, "family": fam, "target": target,
                           "root": ans[orig]["service"], "fault": ans[orig]["fault"],
                           "injected": n, "seed": SEED, "dose": DOSE})
            if fam != "clean" and n == 0:
                print(f"{orig} {fam}: nothing injected on {target} (counts as ineligible for this family)")

    order = sorted(ids, key=lambda c: hashlib.sha256(c.encode()).hexdigest())  # interleave families
    with open(os.path.join(task_root, "manifest.json"), "w") as f:
        json.dump({"schema_version": 1, "dataset": "re2-ob", "system": "Online Boutique",
                   "n_cases": len(order), "case_ids": order}, f, indent=2)
    with open(os.path.join(a.out, "pilot.answers.jsonl"), "w") as f:
        by_id = {x["case_id"]: x for x in answers}
        for cid in order:                      # same order as the manifest (rcaeval-agent evaluate checks)
            f.write(json.dumps(by_id[cid]) + "\n")
    with open(os.path.join(a.sealed, "labels.jsonl"), "w") as f:
        for x in labels:
            f.write(json.dumps(x) + "\n")
    with open(os.path.join(a.sealed, "eligibility.json"), "w") as f:
        json.dump(eligibility, f, indent=2)
    print(f"{len(order)} task cases in {task_root}; labels sealed in {a.sealed}")


# ---------------------------------------------------------------- score
def cmd_score(a):
    with open(os.path.join(a.sealed, "labels.jsonl")) as f:
        lab = {x["case_id"]: x for x in map(json.loads, f)}
    preds = {}
    with open(a.preds) as f:
        for line in f:
            p = json.loads(line)
            preds[p["case_id"]] = p
    rows = []
    for cid, L in lab.items():
        p = preds.get(cid)
        if p is None:          # not run yet: excluded, not counted as a miss
            continue
        top = svc_of(p["ranks"][0]) if p and p.get("ranks") else None
        rows.append({**L, "answered": top is not None, "top": top,
                     "blame": int(top == L["target"]), "ac1": int(top == L["root"]),
                     "cost": ((p or {}).get("usage") or {}).get("cost_usd"),
                     "in_tok": ((p or {}).get("usage") or {}).get("input_tokens"),
                     "out_tok": ((p or {}).get("usage") or {}).get("output_tokens"),
                     "steps": ((p or {}).get("usage") or {}).get("steps"),
                     "model": (p or {}).get("model")})
    if not rows:
        sys.exit(f"No predictions in {a.preds} match the pilot cases.")
    df = pd.DataFrame(rows)
    df["template"] = df["root"] + "_" + df["fault"]
    if len(df) < len(lab):
        print(f"Partial results: {len(df)} of {len(lab)} cases run so far. Missing cases are excluded, "
              f"and Delta TBR uses only cases whose clean and attack versions are both done.")
    print("\nPer family (n = runs done; answered = share that submitted a ranking):")
    print(df.groupby("family").agg(n=("blame", "size"), TBR=("blame", "mean"), AC1=("ac1", "mean"),
                                   answered=("answered", "mean"), cost=("cost", "mean")).round(3))

    wide = df.pivot_table(index=["orig", "template"], columns="family", values="blame").reset_index()
    rng = np.random.default_rng(SEED)
    print("\nDelta TBR vs clean, template-cluster bootstrap (10,000 resamples):")
    for fam in ["F1", "F2", "F3"]:
        if fam not in wide or "clean" not in wide:
            continue
        pair = wide[[fam, "clean", "template"]].dropna()
        if pair.empty:
            print(f"  {fam}: no case has both clean and {fam} done yet")
            continue
        d = (pair[fam] - pair["clean"]).groupby(pair["template"]).mean()
        point = (pair[fam] - pair["clean"]).mean()
        tps = d.index.to_numpy()
        boots = [d.loc[rng.choice(tps, len(tps))].mean() for _ in range(10_000)]
        lo, hi = np.percentile(boots, [2.5, 97.5])
        flag = "below 15-pt planning trigger" if point < 0.15 else "at/above 15 pts"
        print(f"  {fam}: {point:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]  ({flag}; {len(pair)} case pairs, {len(tps)} templates)")
    model = df["model"].dropna().iloc[0] if df["model"].notna().any() else "?"
    if df["in_tok"].notna().any():
        print(f"\nModel {model}: mean {df['in_tok'].mean():,.0f} input and {df['out_tok'].mean():,.0f} output tokens, "
              f"{df['steps'].mean():.1f} model turns per run")
    if df["cost"].notna().any():
        free = str(model).startswith("gemini/")
        label = "Cost per run at LiteLLM's paid-tier price (you pay $0 on the free tier)" if free else "Measured cost per run"
        print(f"{label}: ${df['cost'].mean():.4f} (median ${df['cost'].median():.4f}); "
              f"total ${df['cost'].sum():.2f} over {df['cost'].notna().sum()} runs")
        print(f"Projected 9,614 frozen runs at this mean, with this model: ${9614 * df['cost'].mean():,.0f}")
        if free:
            print("Not a Claude cost estimate: the frozen model's cost must be measured on that model.")
    df.to_csv(os.path.join(a.sealed, "pilot_results.csv"), index=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select"); s.add_argument("--tasks", required=True); s.add_argument("--out", default="dev_cases.json")
    b = sub.add_parser("build"); b.add_argument("--tasks", required=True); b.add_argument("--dev", default="dev_cases.json")
    b.add_argument("--out", default="pilot_tasks"); b.add_argument("--sealed", default="pilot_sealed"); b.add_argument("--force", action="store_true")
    c = sub.add_parser("score"); c.add_argument("--preds", required=True); c.add_argument("--sealed", default="pilot_sealed")
    a = ap.parse_args()
    {"select": cmd_select, "build": cmd_build, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
