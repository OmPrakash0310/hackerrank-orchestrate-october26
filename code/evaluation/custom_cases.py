"""Hand-built evaluation cases beyond the 32 samples.

Each case was labeled by reading the raw evidence directly (screenshots, logs, SQL
captures) and records what the agent must and must not do. Run from the repo root:

  python3 code/evaluation/custom_cases.py
"""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from agent.classify import is_deletion_request  # noqa: E402
from agent.logparse import parse_line  # noqa: E402

csv.field_size_limit(10 ** 9)

# (case_id, expected status, steps that must appear as (operation, resource_id, evidence_id),
#  resource ids that must not appear, why)
CASES = [
    ("case_46", "COMPLETE",
     [("VERIFY_RECORD_ABSENT", "account_6523", "evidence_67"),
      ("VERIFY_RECORD_REDACTED", "tombstone_17", "evidence_42")],
     ["account_4092", "audit_record_1797", "backup_3245"],
     "Repeat requester: the March request must not borrow records that belong to the same "
     "user's August request (sample case_17)."),
    ("case_35", "INCOMPLETE",
     [("REPORT_REMAINING_RECORD", "search_428", "evidence_09")],
     ["search_1874"],
     "Search inspector screenshot shows the subject and a look-alike email; only the exact "
     "match is the subject's."),
    ("case_39", "INCOMPLETE",
     [("REPORT_ACTIVE_DATA_FLOW", "data_flow_1121", "evidence_14"),
      ("DISABLE_DATA_FLOW", "data_flow_1121", "")],
     ["data_flow_9846"],
     "Connector console screenshot lists two active syncs; only one carries the subject's email."),
    ("case_34", "INCOMPLETE",
     [("REPORT_REMAINING_RECORD", "account_5762", "evidence_04"),
      ("REPORT_EXPOSED_PII", "support_9538", "evidence_04")],
     [],
     "Admin console screenshot shows the profile still live and a linked support case."),
    ("case_46", "COMPLETE", [], [],
     "Tombstone screenshot holds only the internal user_id, so it is already compliant (REDACTED)."),
    ("case_55", "INCOMPLETE",
     [("REPORT_RESTORE_RISK", "backup_9287", "evidence_32"), ("BLOCK_RESTORE", "backup_9287", "")],
     [],
     "Backup console screenshot shows the archive restorable; backups are retained but blocked."),
]

CLASSIFIER = [
    ("Please delete my account and everything you hold about me.", True),
    ("Bitte löschen Sie mein Konto und alle meine Daten.", True),
    ("Je souhaite la suppression définitive de mon compte.", True),
    ("Please remove me from your marketing lists only. Do not close my account.", False),
    ("Can you send me a copy of all the personal data you hold about me?", False),
    ("My surname is misspelled on my profile, please correct it.", False),
]

LOG_LINES = [
    "2026-03-10T06:18:01Z level=INFO service=search_service trace_id=trace-1 event=index_lookup_completed resource_id=search_1 result=NOT_FOUND",
    '2026-03-10T06:18:01Z <15>1 node-1 search_service trace-1 - - level=INFO event="index_lookup_completed" resource_id="search_1" result="NOT_FOUND"',
    '2026-03-10T06:18:01Z {"severity":"INFO","service":"search_service","trace_id":"trace-1","event":"index_lookup_completed","resource_id":"search_1","result":"NOT_FOUND"}',
    '2026-03-10T06:18:01Z requestId=trace-1 level=INFO function=search_service message="event=\\"index_lookup_completed\\" resource_id=\\"search_1\\" result=\\"NOT_FOUND\\""',
    "2026-03-10T06:18:01Z [INFO] [search_service] [trace-1] index_lookup_completed: resource_id=search_1 result=NOT_FOUND",
    "2026-03-10T06:18:01Z | INFO | search_service | trace-1 | index_lookup_completed | resource_id=search_1 result=NOT_FOUND",
]


def main():
    out = {r["case_id"]: r for r in csv.DictReader(open(ROOT / "output.csv", encoding="utf-8"))}
    passed = total = 0
    for cid, status, must, must_not, why in CASES:
        steps = json.loads(out[cid]["steps_json"])
        have = {(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps}
        rids = {s["resource_id"] for s in steps}
        problems = []
        if out[cid]["deletion_status"] != status:
            problems.append(f"status {out[cid]['deletion_status']} != {status}")
        problems += [f"missing {m}" for m in must if m not in have]
        problems += [f"unexpected {r}" for r in must_not if r in rids]
        total += 1
        passed += not problems
        print(("PASS" if not problems else "FAIL"), cid, "-", why)
        for p in problems:
            print("     ", p)
    for text, want in CLASSIFIER:
        total += 1
        ok = is_deletion_request(text) == want
        passed += ok
        print("PASS" if ok else "FAIL", "classifier", repr(text[:60]), "->", want)
    parsed = [parse_line(line) for line in LOG_LINES]
    for line, p in zip(LOG_LINES, parsed):
        total += 1
        ok = p and p.get("resource_id") == "search_1" and p.get("result") == "NOT_FOUND"
        passed += bool(ok)
        print("PASS" if ok else "FAIL", "log format", line.split()[1][:12])
    print(f"\n{passed}/{total} custom checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
