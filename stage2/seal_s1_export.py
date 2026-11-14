"""Hash-seal the exported RE2-OB task directories (known issue K10).

`rcaeval-agent export` wrote blind task directories for all 90 RE2-OB cases before the
development draw, so 54 test-stratum (S1) case directories sit on this VM. Proposal v9 permits
that -- a test incident may be on disk but is "never opened, inspected, built, run or summarised"
before G3 -- and Appendix B.9 asks for "the hash seal of the exported S1 directories" as the
evidence that the rule was kept.

This takes that seal. It reads file *bytes* to hash them and keeps nothing else: no content is
printed, stored or returned, and no field of any case file is parsed. The one exception is each
case's stratum, which comes from `dev_cases.json` and the export's own answers file, not from the
case data.

Re-run it at the G3 freeze and compare the root hash. A changed root hash means something under
`agent_tasks/` was altered after this date; an unchanged one is the evidence that it was not.

Usage:
  python stage2/seal_s1_export.py --out stage2/out/seal-<date>
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(REPO, "agent_tasks", "re2-ob")
ANSWERS = os.path.join(REPO, "agent_tasks", "re2-ob.answers.jsonl")
DEV_CASES = os.path.join(REPO, "dev_cases.json")
BLOCK = 1 << 20


def sha256_file(path):
    """The file's SHA-256. Bytes are hashed and discarded; nothing is retained."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(BLOCK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def strata():
    """case_id -> (template, 'development' | 'S1 (test)'), from the draw and the answers file."""
    with open(DEV_CASES) as f:
        dev = json.load(f)
    # dev_cases.json holds `templates` as [service, fault] pairs and `cases` as a list of ids;
    # the export's directory names are "<service>_<fault>"
    dev_templates = {f"{service}_{fault}" for service, fault in dev["templates"]}
    dev_case_ids = set(dev["cases"])
    if len(dev_templates) != dev["n_templates"] or len(dev_case_ids) != 36:
        raise SystemExit(f"dev_cases.json: expected {dev['n_templates']} templates and 36 cases, "
                         f"got {len(dev_templates)} and {len(dev_case_ids)}")
    out = {}
    with open(ANSWERS) as f:
        for line in f:
            if not line.strip():
                continue
            a = json.loads(line)
            template = a["source"].split("/")[0]
            by_template = template in dev_templates
            by_id = a["case_id"] in dev_case_ids
            if by_template != by_id:
                raise SystemExit(f"{a['case_id']}: dev_cases.json's template list and case list "
                                 f"disagree ({template})")
            out[a["case_id"]] = (template, "development" if by_template else "S1 (test)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    case_stratum = strata()
    cases_dir = os.path.join(EXPORT, "cases")
    case_ids = sorted(d for d in os.listdir(cases_dir)
                      if os.path.isdir(os.path.join(cases_dir, d)))
    print(f"{len(case_ids)} exported case directories under {cases_dir}")

    rows, per_case = [], []
    for case_id in case_ids:
        template, stratum = case_stratum.get(case_id, ("(unknown)", "(unknown)"))
        case_dir = os.path.join(cases_dir, case_id)
        digests = []
        for name in sorted(os.listdir(case_dir)):
            path = os.path.join(case_dir, name)
            if not os.path.isfile(path):
                continue
            digest = sha256_file(path)
            size = os.path.getsize(path)
            digests.append(f"{name}:{digest}")
            rows.append({"case_id": case_id, "stratum": stratum, "template": template,
                         "file": name, "bytes": size, "sha256": digest})
        # a per-case hash over "name:digest" lines, so a renamed or removed file also shows up
        case_hash = hashlib.sha256("\n".join(digests).encode()).hexdigest()
        per_case.append({"case_id": case_id, "stratum": stratum, "template": template,
                         "n_files": len(digests), "case_sha256": case_hash})
        print(f"  {case_id}  {stratum:14s} {len(digests)} files  {case_hash[:16]}…", flush=True)

    # the export's own metadata files, sealed alongside the cases
    extra = []
    for path in (os.path.join(EXPORT, "manifest.json"), ANSWERS):
        if os.path.exists(path):
            extra.append({"file": os.path.relpath(path, REPO), "bytes": os.path.getsize(path),
                          "sha256": sha256_file(path)})

    root = hashlib.sha256(
        "\n".join(f"{c['case_id']}:{c['case_sha256']}" for c in per_case).encode()
        + b"\n" + "\n".join(f"{e['file']}:{e['sha256']}" for e in extra).encode()
    ).hexdigest()

    with open(os.path.join(args.out, "s1_seal_files.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(args.out, "s1_seal_cases.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(per_case.__iter__().__next__().keys()))
        w.writeheader()
        w.writerows(per_case)

    counts = {}
    for c in per_case:
        counts[c["stratum"]] = counts.get(c["stratum"], 0) + 1
    summary = {
        "what": "SHA-256 seal of the exported RE2-OB task directories (known issue K10)",
        "sealed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "export_root": os.path.relpath(EXPORT, REPO),
        "cases": len(per_case),
        "cases_by_stratum": counts,
        "files_sealed": len(rows),
        "bytes_sealed": sum(r["bytes"] for r in rows),
        "export_metadata_files": extra,
        "root_sha256": root,
        "sealed_by": "Claude Opus 5 in Claude Code, at the owner's instruction on 2026-11-14",
        "command": "python stage2/seal_s1_export.py --out " + args.out,
        "repo_commit": subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip(),
        "python": platform.python_version(),
        "note": ("Only file bytes were read, to hash them. No case file was parsed, printed or "
                 "summarised, and no value from any S1 case informed any design decision. Each "
                 "case's hash covers its file names as well as their contents, so a removed or "
                 "renamed file changes it."),
    }
    with open(os.path.join(args.out, "s1_seal_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nsealed {len(rows)} files in {len(per_case)} cases "
          f"({summary['bytes_sealed'] / 2**30:.2f} GiB)")
    print(f"by stratum: {counts}")
    print(f"ROOT SHA-256: {root}")
    print(f"wrote {args.out}/")
    print("ALL_DONE")


if __name__ == "__main__":
    main()
