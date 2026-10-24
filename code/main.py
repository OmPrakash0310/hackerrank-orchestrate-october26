"""Ghost Data Still Exists: deletion-audit agent entry point.

Usage:
  python3 code/main.py                       # audit dataset/deletion_requests.csv -> output.csv
  python3 code/main.py --samples             # audit the 32 samples -> code/evaluation/sample_predictions.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.engine import run  # noqa: E402


def load_image_notes():
    p = Path(__file__).resolve().parent / "agent" / "image_notes.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.samples:
        req, out = "sample_deletion_requests.csv", a.out or str(ROOT / "code" / "evaluation" / "sample_predictions.csv")
    else:
        req, out = "deletion_requests.csv", a.out or str(ROOT / "output.csv")
    rows = run(ROOT / "dataset", req, load_image_notes())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
