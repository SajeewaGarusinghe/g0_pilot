#!/usr/bin/env python3
"""G0 stage 2, step 9a follow-up: do ANY Online Boutique services emit genuine non-INFO log lines?

Step 9a (09a-distinguishability.md) found that 0 of 36 drawn targets emit a single genuine
error or warning line in the post-fault window, so the F1 overlay's forged lines are separable
on `level` alone and symptom-matching has nothing to match. It left the design fork open:
either F1's target rule additionally requires genuine error lines to imitate - which shrinks
the eligible-target pool - or F1 is accepted as a family identifiable at the record level.

The fork cannot be decided on the targets alone. If NO service in the deployment emits
non-INFO lines, requiring them would empty the pool and the fork has only one branch.
This script measures that, per service, over the whole post-fault window of all 36
development cases, and also over the pre-fault window for contrast.

  python stage2/log_levels.py --out stage2/out/<dated>

Hygiene: reuses audit_traces.guard, so only the 12 development templates can be opened;
every file read is logged with its size and SHA-256.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_traces import cases_re2_ob_dev, dev_templates, guard, sha256  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    allowed, _ = dev_templates()
    cases = cases_re2_ob_dev(allowed)

    read_log = []
    # service -> window -> Counter(level)
    per_service = defaultdict(lambda: defaultdict(Counter))
    per_case_rows = []

    for cid, case_dir, template in cases:
        lpath = guard("re2-ob-dev", os.path.join(case_dir, "logs.csv"), allowed)
        read_log.append({"case_id": cid, "path": lpath,
                         "bytes": os.path.getsize(lpath), "sha256": sha256(lpath)})
        df = pd.read_csv(lpath, low_memory=False)
        cols = {c.lower(): c for c in df.columns}
        # RE2-OB logs.csv: time, timestamp, container_name, message, level, req_path, error.
        # `container_name` is the service; `timestamp` is nanoseconds (as distinguishability.py uses).
        svc_col = (cols.get("servicename") or cols.get("service") or cols.get("container_name"))
        lvl_col = cols.get("level") or cols.get("severity")
        ts_col = cols.get("timestamp") or cols.get("time")
        if svc_col is None or lvl_col is None:
            sys.exit(f"{cid}: logs.csv lacks a service or level column: {list(df.columns)}")

        # fault onset: the inject_time in task.json if present, else the window midpoint
        onset = None
        tj = os.path.join(case_dir, "inject_time.txt")
        if os.path.exists(tj):
            tj = guard("re2-ob-dev", tj, allowed)
            read_log.append({"case_id": cid, "path": tj, "bytes": os.path.getsize(tj),
                             "sha256": sha256(tj)})
            onset = float(open(tj).read().strip())

        ts = pd.to_numeric(df[ts_col], errors="coerce") if ts_col else None
        if ts is not None and onset is not None:
            # pick the unit from the magnitude: ns (1e18), us (1e15), ms (1e12) or s
            med = float(ts.dropna().median())
            scale = 1e9 if med > 1e17 else 1e6 if med > 1e14 else 1e3 if med > 1e11 else 1.0
            post = (ts / scale) >= onset
        else:
            post = pd.Series([True] * len(df))

        for window, mask in (("pre", ~post), ("post", post)):
            sub = df[mask.fillna(False).to_numpy()]
            # pandas 3 keeps NA as NaN under the new str dtype, so fill before stripping
            svcs = sub[svc_col].astype(str).fillna("")
            lvls = sub[lvl_col].astype(str).fillna("")
            for svc, lvl in zip(svcs, lvls):
                per_service[str(svc)][window][str(lvl).strip().upper()] += 1

        nonlog = df[post.fillna(False).to_numpy()]
        lv = nonlog[lvl_col].astype(str).fillna("").str.strip().str.upper()
        per_case_rows.append({
            "case_id": cid, "template": template,
            "post_lines": int(len(nonlog)),
            "post_non_info": int((~lv.isin(["INFO", "INFORMATION", ""])).sum()),
            "levels_seen": "|".join(sorted(set(lv))),
        })

    # ------------------------------------------------------------------ outputs
    svc_rows = []
    for svc in sorted(per_service):
        pre, post = per_service[svc]["pre"], per_service[svc]["post"]
        non_info = lambda c: sum(v for k, v in c.items() if k not in ("INFO", "INFORMATION", ""))
        svc_rows.append({
            "service": svc,
            "pre_lines": sum(pre.values()), "pre_non_info": non_info(pre),
            "post_lines": sum(post.values()), "post_non_info": non_info(post),
            "levels_pre": "|".join(f"{k}:{v}" for k, v in sorted(pre.items())),
            "levels_post": "|".join(f"{k}:{v}" for k, v in sorted(post.items())),
        })
    pd.DataFrame(svc_rows).to_csv(os.path.join(args.out, "log_levels_by_service.csv"), index=False)
    pd.DataFrame(per_case_rows).to_csv(os.path.join(args.out, "log_levels_by_case.csv"), index=False)
    pd.DataFrame(read_log).to_csv(os.path.join(args.out, "log_levels_read_log.csv"), index=False)

    tot_post = sum(r["post_lines"] for r in svc_rows)
    tot_non = sum(r["post_non_info"] for r in svc_rows)
    print(f"services: {len(svc_rows)}   post-fault log lines: {tot_post:,}   non-INFO: {tot_non:,}")
    print(f"{'service':<24}{'post_lines':>12}{'post_non_INFO':>15}  levels")
    for r in svc_rows:
        print(f"{r['service']:<24}{r['post_lines']:>12,}{r['post_non_info']:>15,}  {r['levels_post'][:70]}")
    with_any = [r["service"] for r in svc_rows if r["post_non_info"] > 0]
    print("\nservices with >=1 genuine non-INFO post-fault line:",
          len(with_any), with_any if with_any else "(none)")
    print("ALL_DONE")


if __name__ == "__main__":
    main()
