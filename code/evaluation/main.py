"""Local evaluation for Ghost Data Still Exists.

Two modes:
  score     compare predictions with labeled answers (default: dataset/sample_output.csv)
  validate  check a full output.csv against the submission contract

Examples:
  python3 code/evaluation/main.py score --pred code/evaluation/sample_predictions.csv
  python3 code/evaluation/main.py validate --pred output.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OP_ORDER = {op: i for i, op in enumerate(OPERATIONS)}
STEP_FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


def step_key(s):
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def sort_key(s):
    return (PHASES.index(s["phase"]), OP_ORDER[s["operation"]], s["system_id"], s["resource_id"])


def score(pred_path, gold_path, verbose=True):
    gold = read_rows(gold_path)
    pred = read_rows(pred_path)
    n = len(gold)
    status_ok = exact_ok = seq_ok = 0
    tp = fp = fn = 0
    field_total = field_ok = 0
    failures = []
    for cid, g in sorted(gold.items(), key=lambda kv: int(kv[0].split("_")[1])):
        p = pred.get(cid)
        gsteps = json.loads(g["steps_json"])
        psteps = json.loads(p["steps_json"]) if p else []
        pstatus = p["deletion_status"] if p else "MISSING"
        if pstatus == g["deletion_status"]:
            status_ok += 1
        gk = [step_key(s) for s in gsteps]
        pk = [step_key(s) for s in psteps]
        if gk == pk:
            seq_ok += 1
        gmap = {step_key(s): s for s in gsteps}
        pmap = {step_key(s): s for s in psteps}
        matched = set(gmap) & set(pmap)
        tp += len(matched)
        fp += len(set(pmap) - set(gmap))
        fn += len(set(gmap) - set(pmap))
        for k in matched:
            for f in ("state", "evidence_id", "step_no"):
                field_total += 1
                field_ok += gmap[k][f] == pmap[k][f]
        same = pstatus == g["deletion_status"] and gsteps == psteps
        exact_ok += same
        if not same:
            failures.append((cid, g["deletion_status"], pstatus,
                             sorted(set(gmap) - set(pmap)), sorted(set(pmap) - set(gmap)),
                             [(k, gmap[k]["evidence_id"], pmap[k]["evidence_id"]) for k in sorted(matched)
                              if gmap[k] != pmap[k]]))
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    metrics = {
        "cases": n,
        "status_accuracy": status_ok / n,
        "exact_case_accuracy": exact_ok / n,
        "sequence_accuracy": seq_ok / n,
        "step_precision": prec,
        "step_recall": rec,
        "step_f1": f1,
        "field_accuracy": field_ok / field_total if field_total else 1.0,
    }
    if verbose:
        for k, v in metrics.items():
            print(f"{k:22s} {v:.4f}" if isinstance(v, float) else f"{k:22s} {v}")
        for cid, gs, ps, missing, extra, diff in failures:
            print(f"\n{cid}: gold={gs} pred={ps}")
            for m in missing:
                print("   missing", m)
            for e in extra:
                print("   extra  ", e)
            for d in diff:
                print("   field  ", d)
    return metrics


def validate(pred_path):
    errors = []
    requests = read_rows(DATASET / "deletion_requests.csv")
    evidence = {r["file_id"]: r["captured_at"] for r in csv.DictReader(open(DATASET / "evidence_files.csv"))}
    activity = {r["activity_id"]: r["event_time"] for r in csv.DictReader(open(DATASET / "system_activity.csv"))}
    audit = {r["audit_id"]: r["event_time"] for r in csv.DictReader(open(DATASET / "user_audit_log.csv"))}
    times = {**evidence, **activity, **audit}

    with open(pred_path, newline="", encoding="utf-8") as f:
        header = f.readline().strip()
    if header != "case_id,deletion_status,steps_json":
        errors.append(f"bad header: {header!r}")
    rows = list(csv.DictReader(open(pred_path, newline="", encoding="utf-8")))
    ids = [r["case_id"] for r in rows]
    if len(rows) != len(requests):
        errors.append(f"row count {len(rows)} != {len(requests)}")
    if len(set(ids)) != len(ids):
        errors.append("duplicate case_id")
    if set(ids) != set(requests):
        errors.append(f"case_id mismatch: missing={sorted(set(requests) - set(ids))[:5]} extra={sorted(set(ids) - set(requests))[:5]}")
    if ids != sorted(ids, key=lambda c: int(c.split("_")[1])):
        errors.append("rows not sorted by case_id (numeric order)")

    for r in rows:
        cid, status = r["case_id"], r["deletion_status"]
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError as e:
            errors.append(f"{cid}: invalid JSON ({e})")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{cid}: NOT_APPLICABLE must have []")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{cid}: {status} with no steps")
        if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps):
            errors.append(f"{cid}: COMPLETE has non-verification steps")
        if status == "INCOMPLETE" and not any(s.get("phase") == "FINDING" for s in steps):
            errors.append(f"{cid}: INCOMPLETE without findings")
        audit_at = requests.get(cid, {}).get("audit_at", "")
        for i, s in enumerate(steps, 1):
            if set(s) != set(STEP_FIELDS):
                errors.append(f"{cid} step {i}: fields {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{cid} step {i}: step_no {s['step_no']}")
            if s["operation"] not in OPERATIONS:
                errors.append(f"{cid} step {i}: unknown operation {s['operation']}")
                continue
            phase, state = OPERATIONS[s["operation"]]
            if (s["phase"], s["state"]) != (phase, state):
                errors.append(f"{cid} step {i}: phase/state {s['phase']}/{s['state']} for {s['operation']}")
            ev = s["evidence_id"]
            needs_ev = s["phase"] == "FINDING" or status == "COMPLETE"
            if needs_ev:
                if ev not in times:
                    errors.append(f"{cid} step {i}: unknown evidence {ev!r}")
                elif times[ev] > audit_at:
                    errors.append(f"{cid} step {i}: evidence {ev} captured after audit_at")
            elif ev != "":
                errors.append(f"{cid} step {i}: future step must have empty evidence_id")
        try:
            if [sort_key(s) for s in steps] != sorted(sort_key(s) for s in steps):
                errors.append(f"{cid}: steps not in required order")
        except (KeyError, ValueError):
            pass
        if status == "INCOMPLETE":
            keys = {(s["system_id"], s["resource_id"], s["phase"]) for s in steps}
            for s in steps:
                if s["phase"] == "FINDING":
                    for ph in ("REMEDIATION", "VERIFICATION"):
                        if (s["system_id"], s["resource_id"], ph) not in keys:
                            errors.append(f"{cid}: finding {s['resource_id']} has no {ph.lower()} step")
    for e in errors:
        print("ERROR", e)
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"rows={len(rows)} status_mix={counts} errors={len(errors)}")
    return not errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["score", "validate"])
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", default=str(DATASET / "sample_output.csv"))
    a = ap.parse_args()
    if a.mode == "score":
        score(a.pred, a.gold)
    else:
        sys.exit(0 if validate(a.pred) else 1)


if __name__ == "__main__":
    main()
