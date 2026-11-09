"""Synthetic predictions for the self-test: blames the target more often under attack.
Usage: python tests/fake_predictions.py <labels.jsonl> <preds.jsonl>"""
import json
import random
import sys

random.seed(3)
labels = [json.loads(line) for line in open(sys.argv[1])]
with open(sys.argv[2], "w") as f:
    for L in labels:
        p = 0.1 if L["family"] == "clean" else 0.45
        top = L["target"] if random.random() < p else L["root"]
        f.write(json.dumps({"case_id": L["case_id"], "ranks": [top + "_cpu"],
                            "usage": {"cost_usd": 0.03}}) + "\n")
