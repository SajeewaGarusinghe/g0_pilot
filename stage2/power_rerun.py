"""G0 stage 2, step 12: the G3 power simulation, rerun on what stage 2 actually measured.

The pilot's simulation (G0-POWER, 12 Nov) assumed S3 had **5 clusters of 6 cases**, giving 13
clusters in all. Step 11a read RE3-OB's case index and found **4** root-cause services over 10
templates and 30 cases, and they are **unequal**: emailservice 15 cases, adservice 9,
cartservice 3, currencyservice 3. Section 14 gives every cluster in a stratum equal weight, so a
3-case cluster now carries the same weight as a 15-case one, and the confirmatory tests rest on
**12** clusters, not 13.

This reruns both planning simulations under the corrected structure and reports the old and new
side by side, so the change is visible rather than silently absorbed. It also applies step 8's
rule-coverage ceiling to H2b, where it belongs: the defence cannot rescue a blamed run whose
forged span had no matched, eligible caller span, so the ceiling is an upper bound on the share
it can remove.

Everything here is a *planning* simulation on assumed effects. The pilot's measured effects are
quoted as the starting point, but v9 replaced its fixed-fraction doses with symptom-matched ones,
so the effects are assumptions and the output is a power curve, not a prediction.

Usage:
  python stage2/power_rerun.py --out stage2/out/step12-<date>
"""
import argparse
import csv
import json
import os

import numpy as np

SEED = 20261114
B = 999                      # bootstrap resamples, as in the pilot
WEBB = np.array([-np.sqrt(1.5), -1, -np.sqrt(.5), np.sqrt(.5), 1, np.sqrt(1.5)])

# (name, [cases per cluster] per stratum). S1: 3 root-cause services x 18 cases;
# S2: 5 x 18; S3 as measured, or as the pilot assumed.
STRATA_MEASURED = {"S1": [18, 18, 18], "S2": [18, 18, 18, 18, 18], "S3": [15, 9, 3, 3]}
STRATA_PILOT = {"S1": [18, 18, 18], "S2": [18, 18, 18, 18, 18], "S3": [6, 6, 6, 6, 6]}
# What the three-path decision implies for H2b. R1's client timing boundary and R2's status
# condition both hold on the Online Boutique strata and on neither count for Train Ticket
# (NA_BOUNDARY, NA_STATUS_LOSSY), so a confirmatory test of a rule-dependent hypothesis can only
# use S1 and S3: 7 clusters and 84 of the 174 cases, not 12 and 174.
STRATA_OB_ONLY = {"S1": [18, 18, 18], "S3": [15, 9, 3, 3]}
# The 7 clusters above are only 6 distinct services: `emailservice` is an S1 root cause *and*
# S3's largest, so treating the two as independent is a dependence the wild bootstrap-t does not
# model, and dependence biases it anti-conservatively -- the opposite direction to the 0.026 size
# the 7-cluster run reports. Merging them gives the honest floor: one cluster of 18 + 15 = 33
# cases, then productcatalogservice 18, recommendationservice 18, adservice 9, cartservice 3,
# currencyservice 3. Equal weight per cluster, 84 cases, 6 clusters.
STRATA_MERGED = {"S1+S3, emailservice merged": [33, 18, 18, 9, 3, 3]}

# step 8's rule-coverage ceiling: the share of forged records a rule can reach at all
CEILING = {"FS (status and duration)": 1.0, "F2 (status only)": 1.0,
           "F3, sibling target": 0.4984, "F3, callee target": 0.0055}

# the pilot's measured blame effects, for orientation only (fixed-fraction doses)
PILOT_BLAME = {"F1": 0.25, "F2": 0.111, "F3": 0.056}


def layout(strata):
    """(weights, case counts) with each stratum equally weighted and clusters equal within it."""
    weights, sizes = [], []
    s = len(strata)
    for cases in strata.values():
        k = len(cases)
        weights.extend([1.0 / (s * k)] * k)
        sizes.extend(cases)
    return np.array(weights), np.array(sizes)


def make_test(weights, rng):
    """One-sided wild cluster bootstrap-t with Webb weights and the null imposed."""
    g = len(weights)
    draws = rng.choice(WEBB, size=(B, g))

    def test(y, alpha):
        theta = (weights * y).sum()
        se = np.sqrt((weights ** 2 * (y - theta) ** 2).sum() * g / (g - 1))
        t = theta / se if se > 0 else 0.0
        resid = y - theta
        yb = draws * resid
        tb_num = (yb * weights).sum(1)
        tb_se = np.sqrt(((weights ** 2) * (yb - tb_num[:, None]) ** 2).sum(1) * g / (g - 1))
        tb = tb_num / np.where(tb_se > 0, tb_se, np.inf)
        return (1 + (tb >= t).sum()) / (B + 1) < alpha

    return test


def sim_h1(strata, mu, sd_b, rng, test, nsim, alpha, base=0.01):
    """H1: does the attack raise target blame above clean? Effect mu per cluster."""
    weights, sizes = layout(strata)
    hits = 0
    for _ in range(nsim):
        pc = np.clip(rng.normal(mu + base, sd_b, len(sizes)), 0, 1)
        attacked = rng.binomial(sizes, pc) / sizes
        clean = rng.binomial(sizes, base) / sizes
        hits += test(attacked - clean, alpha)
    return hits / nsim


def sim_h2b(strata, eff, rescued, sd_b, rng, test, nsim, alpha, base=0.01):
    """H2b: does the defence reduce the attack effect? `rescued` is the share of blamed runs it
    removes, which step 8's ceiling bounds from above."""
    weights, sizes = layout(strata)
    hits = 0
    for _ in range(nsim):
        e = np.clip(rng.normal(eff, sd_b, len(sizes)), 0, 1 - base)
        blamed_d0 = rng.binomial(sizes, base + e)
        # paired on the same cases: of the runs blamed without a defence, a share is rescued
        blamed_d4 = np.array([rng.binomial(int(a), 1 - rescued) for a in blamed_d0])
        clean = rng.binomial(sizes, base)
        y = -((blamed_d4 - clean) - (blamed_d0 - clean)) / sizes
        hits += test(y, alpha)
    return hits / nsim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--nsim-h1", type=int, default=1000)
    ap.add_argument("--nsim-h2b", type=int, default=800)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    rows = []
    for label, strata in (("measured (12 clusters)", STRATA_MEASURED),
                          ("pilot assumption (13 clusters)", STRATA_PILOT),
                          ("Online Boutique strata only (7 clusters)", STRATA_OB_ONLY),
                          ("OB strata, emailservice merged (6 clusters)", STRATA_MERGED)):
        weights, sizes = layout(strata)
        rng = np.random.default_rng(SEED)
        test = make_test(weights, rng)
        g = len(weights)
        print(f"\n=== {label}: {g} clusters, {sizes.sum()} cases, "
              f"cluster sizes {list(sizes)}", flush=True)

        # the test's own size under an exact null, which must stay near alpha
        rng_size = np.random.default_rng(SEED + 1)
        null_hits = 0
        for _ in range(2000):
            y = (rng_size.binomial(sizes, 0.02) - rng_size.binomial(sizes, 0.02)) / sizes
            null_hits += test(y, 0.05)
        size = null_hits / 2000
        print(f"  size at alpha 0.05 under an exact null: {size:.3f}")
        rows.append({"structure": label, "clusters": g, "cases": int(sizes.sum()),
                     "hypothesis": "size check", "parameter": "exact null",
                     "sd_between": "", "alpha": 0.05, "power": size})

        for sd in (0.06, 0.12):
            for mu in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30):
                p = sim_h1(strata, mu, sd, rng, test, args.nsim_h1, 0.0125)
                rows.append({"structure": label, "clusters": g, "cases": int(sizes.sum()),
                             "hypothesis": "H1 (blame above clean)", "parameter": f"effect {mu}",
                             "sd_between": sd, "alpha": 0.0125, "power": p})
                print(f"  H1  sd={sd}  effect={mu:.2f}  power={p:.3f}", flush=True)

        for eff in (0.10, 0.20, 0.30):
            for rescued in (0.25, 0.50, 0.75, 1.00):
                p = sim_h2b(strata, eff, rescued, 0.08, rng, test, args.nsim_h2b, 0.0125)
                rows.append({"structure": label, "clusters": g, "cases": int(sizes.sum()),
                             "hypothesis": "H2b (defence reduces the effect)",
                             "parameter": f"effect {eff}, rescued {rescued}",
                             "sd_between": 0.08, "alpha": 0.0125, "power": p})
                print(f"  H2b effect={eff:.2f}  rescued={rescued:.2f}  power={p:.3f}", flush=True)

    path = os.path.join(args.out, "power_rerun.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # what step 8's ceiling does to the share the defence can rescue
    ceiling_rows = []
    for family, ceiling in CEILING.items():
        best = [r for r in rows if r["structure"].startswith("Online Boutique strata only")
                and r["hypothesis"].startswith("H2b")]
        reachable = [r for r in best
                     if float(r["parameter"].split("rescued ")[1]) <= ceiling + 1e-9]
        ceiling_rows.append({
            "family": family, "coverage_ceiling": ceiling,
            "max_rescued_share_reachable": max(
                (float(r["parameter"].split("rescued ")[1]) for r in reachable), default=0.0),
            "best_power_at_effect_0.20": max(
                (r["power"] for r in reachable if r["parameter"].startswith("effect 0.2")),
                default=0.0),
            "meets_g3_condition_0.70": max(
                (r["power"] for r in reachable if r["parameter"].startswith("effect 0.2")),
                default=0.0) >= 0.70,
        })
    with open(os.path.join(args.out, "ceiling_vs_power.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ceiling_rows[0].keys()))
        w.writeheader()
        w.writerows(ceiling_rows)

    meta = {"seed": SEED, "bootstrap_resamples": B, "nsim_h1": args.nsim_h1,
            "nsim_h2b": args.nsim_h2b, "strata_measured": STRATA_MEASURED,
            "strata_pilot": STRATA_PILOT, "coverage_ceiling": CEILING,
            "pilot_blame_effects_fixed_doses": PILOT_BLAME,
            "note": ("Planning simulation on assumed effects. v9 replaced the pilot's "
                     "fixed-fraction doses with symptom-matched ones, so the pilot's measured "
                     "effects orient the range but do not predict the test-time effect.")}
    with open(os.path.join(args.out, "power_rerun.env.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print("\n=== step 8's ceiling against H2b, on the strata H2b can actually use (S1 + S3)")
    for r in ceiling_rows:
        print(f"  {r['family']:26s} ceiling {r['coverage_ceiling']:.4f}  "
              f"best power at a 20-pt effect: {r['best_power_at_effect_0.20']:.3f}  "
              f"meets >=0.70: {r['meets_g3_condition_0.70']}")
    print(f"\nwrote {path}")
    print("ALL_DONE")


if __name__ == "__main__":
    main()
