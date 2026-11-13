# G0 stage 2 — steps 1–3: trace schema audit, role inference, attempt matching

Build-guide `P0` steps 1, 2 and 3 for *Verifying Telemetry Before Trusting It*. All three read
the same `traces.csv`, so they run as one pass per case. Evidence goes to the knowledge base
at `Msc-project/09-results/g0/` (`01-schema-audit.md`, `02-role-inference.md`,
`03-attempt-matching.md`); this folder holds only the code and its raw aggregates.

**This is a prototype.** P3 rewrites the matcher with unit tests (build guide P0, "Do not
polish the P0 scripts").

## Run

```bash
source .venv/bin/activate

# RE3-TT (Train Ticket calibration data) — RE3-OB is test stratum S3 and is deliberately
# NOT downloaded, so download_re3_dataset() must never be used.
python -c "from RCAEval.utility import download_re3tt_dataset; download_re3tt_dataset(local_path='data/RE3')"

python stage2/audit_traces.py --dataset re2-ob-dev --out stage2/out/<date>   # 36 dev cases
python stage2/audit_traces.py --dataset re3-tt     --out stage2/out/<date>   # 30 TT cases
python stage2/summarize.py --out stage2/out/<date>                           # -> out/<date>/agg/
```

Roughly 15 s per RE2-OB case and 5 s per RE3-TT case, serial, one case in memory at a time.
`--limit N` / `--only <case>` audit a subset.

## Data hygiene (AGENTS.md §6; build-guide test P0-T1)

Only the 36 RE2-OB development cases named in `dev_cases.json` and RE3-TT may be read.

- Every candidate path goes through `guard()` **before it is opened**: it must sit under the
  expected data-set root, must not touch `RE2-TT`/`RE3-OB`/`RE3-SS`/`RE2-SS`/`RE1-*`, and for
  RE2-OB its template must be one of the 12 in `dev_cases.json`. Anything else aborts the run.
- `read_log.csv` records the path, size and SHA-256 of every file actually opened, so what was
  read is evidence rather than an assertion.
- Guard test (keep its output with the run):
  ```bash
  python stage2/audit_traces.py --dataset re2-ob-dev --out /tmp/x \
    --force-path data/RE2/RE2-OB/emailservice_cpu/1     # must print REFUSED and exit 1
  ```

## What the outputs are

Per-case, long format, appended as each case finishes (`stage2/out/<date>/`):

| File | Step | Holds |
|---|---|---|
| `read_log.csv` | — | every file opened: path, bytes, SHA-256 |
| `schema_cases.csv` | 1 | per case: rows, traces, duplicate span ids, services/methods/operations, duration percentiles and unit evidence, `startTime`÷`startTimeMillis`, `statusCode` counts, span partition |
| `schema_columns.csv` | 1 | columns present, dtype, null count, whether expected |
| `status_codes.csv` | 1 | `statusCode` × span class × is-client-span |
| `svc_method_op.csv`, `operation_forms.csv` | 1 | service/method/operation combinations; operation-name form per service |
| `service_role_census.csv`, `span_partition.csv` | 1 | per service: spans as server / as client / root / dangling / same-service; the partition sums to the row count |
| `derived_file_headers.csv` | 1 | headers of `tracets_err.csv` and `tracets_lat.csv` (FrameBench rebuilds these) |
| `role_coverage.csv` | 2 | per service pair: cross edges, method/operation agreement, caller-parent outcome, role tier counts |
| `role_reasons.csv` | 2 | role tier / `NA_ROLE_*` reason counts |
| `op_mismatch_taxonomy.csv` | 2 | raw operation-name mismatches by form pair, with examples and whether normalisation explains them |
| `matching_counts.csv` | 3 | `MATCHED_1TO1`, `MATCHED_1TO1_ROOT_CLIENT`, `MATCHED_1TO1_WEAK_ROLE`, `AMB_*`, `NA_ROLE` per service pair |
| `witness_gaps.csv` | 3 | the gaps kept apart: `NO_WITNESS` (dangling parent) vs role-undecidable (same-service parent) vs root |
| `methods_without_server_child.csv` | 3 | outbound methods whose spans have no cross-service child (callee not instrumented) |
| `paired_code_census.csv` | 3 | joint (client `statusCode`, server `statusCode`) over matched pairs — the table R2 needs |

`summarize.py` writes pooled and per-case-spread versions into `out/<date>/agg/`.

## Role tiers, and why there is more than one

RCAEval's export has no span kind, so the client/server role is inferred from the parent link
across a service boundary plus the guide's two checks. The checks do not always resolve, and the
ways they fail mean different things, so they are reported separately rather than merged:

| Tier | Meaning |
|---|---|
| `STRONG` | method and operation agree (after normalisation) **and** the candidate client span has a same-service parent — the pair identifies one operation on both sides |
| `ROOT_CLIENT` | method and operation agree, but the client span is itself a trace root, so the guide's second check **cannot be evaluated** (it does not fail) |
| `WEAK_VERB_ONLY` | roles are inferable and consistent, but the client span names only the HTTP verb (`GET`), so the route is not identified — Train Ticket |
| `NA_ROLE` | a genuine failure: caller's parent is in a third service, or dangling, or method/operation disagree |

Operation-name normalisation is derived from the data, not assumed: on Online Boutique the callee
side carries a leading `/` or a `grpc.` prefix depending on the instrumentation language, and
`op_mismatch_taxonomy.csv` reports what normalisation does **not** explain.

Reason-code note: A3's `AMB_DUPLICATE` covers both a duplicate span id and "more than one
candidate partner". This script splits the second case into `AMB_REUSED_CTX` (several
cross-service children, same method — retries/fan-out) and `AMB_NON_1TO1` (different methods),
so the two are visible separately; the evidence files say so.

## Limits

- Development data only: 12 templates from **2** root-cause services (36 cases) plus RE3-TT.
  Pooled, span-weighted shares let one large case speak for the set, so every share is also
  reported as a per-case min/median/max (the clustering limit recorded as `STATUS.md` K14).
- These are **planning inputs, not findings** (`09-results/README.md` rule 6). Steps 1–3 feed
  the witness-coverage and rule-coverage ceiling computed at step 8; they do not compute it,
  and they decide nothing about R1 or R2.
