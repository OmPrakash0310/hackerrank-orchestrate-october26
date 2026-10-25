"""Hard evaluation suite: synthetic trap scenarios plus metamorphic tests on the real data.

Part 1 builds a small fictional world (users, identity history, evidence files in the
same formats as the dataset) in a temporary folder, runs the full agent on it and
compares every case with a hand-written expected answer, step by step.

Part 2 changes the real dataset in ways that must not change the answer (request
email swapped for a historical one, upper-case emails, shuffled rows, look-alike
decoy users) and checks that output.csv stays identical.

Run from the repo root:

  python3 code/evaluation/hard_cases.py            # both parts
  python3 code/evaluation/hard_cases.py --synthetic
"""

import argparse
import csv
import io
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from agent.classify import is_deletion_request  # noqa: E402
from agent.engine import PHASE, STATE, run  # noqa: E402

csv.field_size_limit(10 ** 9)

# ------------------------------------------------------------------ part 1: synthetic world

USERS = [
    # user_id, name, email, phone
    ("user_901", "Ilse Varga", "ilse.varga@example.test", "+36 30 000 0901"),
    ("user_902", "Tomas Brandt", "tomas.brandt@example.test", "+49 151 000 0902"),
    ("user_903", "Nadia Kerr", "nadia.kerr@example.test", "+44 7700 000903"),
    ("user_904", "Ravi Menon", "ravi.menon@example.test", "+91 90000 00904"),
    ("user_905", "Sofia Lund", "sofia.lund@example.test", "+46 70 000 0905"),
    ("user_906", "Jo Ng", "jo.ng@example.test", "+65 9000 0906"),
    ("user_907", "Jo Ng Jr", "jo.ng.jr@example.test", "+65 9000 0907"),
    ("user_908", "Marta Pinto", "marta.pinto@example.test", "+351 910 000 908"),
    ("user_909", "Kenji Arai", "kenji.arai@example.test", "+81 90 0000 0909"),
    ("user_910", "Lena Fischer", "lena.fischer@example.test", "+49 151 000 0910"),
]
# The subject of H1 changed email: requests and old systems still use the old one.
AUDIT = [("audit_901", "user_901", "2026-01-05T10:00:00Z", "email", "ilse.v.old@example.test", "ilse.varga@example.test")]


def rec(ts, system, rid, rtype, result, ref):
    return {"recorded_at": ts, "system": system, "resource_id": rid, "record_type": rtype,
            "result": result, "subject_reference": ref}


# file_id -> (captured_at, kind, content)
FILES = {
    # H1: request uses the OLD email in upper case; only the old email links the account.
    "ev_h1": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9001", "USER_PROFILE", "FOUND", "ilse.v.old@example.test"),
        rec("2026-03-09T09:00:00Z", "search_service", "search_9001", "SEARCH_INDEX_ENTRY", "NOT_FOUND", "ilse.v.old@example.test"),
    ]),
    # H2: clean before the audit; a file captured AFTER the audit shows the record again.
    "ev_h2a": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9002", "USER_PROFILE", "NOT_FOUND", "tomas.brandt@example.test"),
    ]),
    "ev_h2b": ("2026-03-20T09:00:00Z", "records", [
        rec("2026-03-20T09:00:00Z", "account_service", "account_9002", "USER_PROFILE", "FOUND", "tomas.brandt@example.test"),
    ]),
    # H3: deleted, then recreated by a restore before the audit.
    "ev_h3a": ("2026-03-06T09:00:00Z", "records", [
        rec("2026-03-06T09:00:00Z", "account_service", "account_9003", "USER_PROFILE", "NOT_FOUND", "nadia.kerr@example.test"),
    ]),
    "ev_h3b": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9003", "USER_PROFILE", "FOUND", "nadia.kerr@example.test"),
    ]),
    # H4: replica drift inside one SQL capture: primary redacted the analytics profile,
    # the reporting replica still holds the email.
    "ev_h4": ("2026-03-09T09:00:00Z", "sql", [
        ("primary_records", "analytics_platform", "analytics_profiles",
         {"resource_id": "analytics_9004", "user_id": "user_904", "email": None, "segment": "loyal"}),
        ("reporting_replica_records", "analytics_platform", "analytics_profiles",
         {"resource_id": "analytics_9004", "user_id": "user_904", "email": "ravi.menon@example.test", "segment": "loyal"}),
    ]),
    "ev_h4b": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9004", "USER_PROFILE", "NOT_FOUND", "ravi.menon@example.test"),
    ]),
    # H5: tombstone still carries the email; account gone.
    "ev_h5": ("2026-03-09T09:00:00Z", "plain", [
        {"resource_id": "tombstone_9005", "user_id": "user_905", "email": "sofia.lund@example.test"},
    ]),
    "ev_h5b": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9005", "USER_PROFILE", "NOT_FOUND", "sofia.lund@example.test"),
    ]),
    # H6: look-alike decoy in the same index.
    "ev_h6": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "search_service", "search_9006", "SEARCH_INDEX_ENTRY", "FOUND", "jo.ng@example.test"),
        rec("2026-03-09T09:00:00Z", "search_service", "search_9061", "SEARCH_INDEX_ENTRY", "FOUND", "jo.ng.jr@example.test"),
        rec("2026-03-09T09:00:00Z", "account_service", "account_9006", "USER_PROFILE", "NOT_FOUND", "jo.ng@example.test"),
    ]),
    # H7: everything compliant, including retained legal hold and audit record.
    "ev_h7": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9008", "USER_PROFILE", "NOT_FOUND", "user_908"),
        rec("2026-03-09T09:00:00Z", "legal_service", "legal_hold_9008", "LEGAL_HOLD_RECORD", "FOUND", "user_908"),
        rec("2026-03-09T09:00:00Z", "security_service", "audit_record_9008", "SECURITY_AUDIT_LOG", "FOUND", "user_908"),
        rec("2026-03-09T09:00:00Z", "analytics_platform", "analytics_9008", "ANALYTICS_PROFILE", "ANONYMIZED", "user_908"),
    ]),
    # H8: active sync + restorable backup + support conversation + live account.
    "ev_h8": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9009", "USER_PROFILE", "FOUND", "kenji.arai@example.test"),
        rec("2026-03-09T09:00:00Z", "integration_service", "data_flow_9009", "DATA_FLOW", "ACTIVE", "kenji.arai@example.test"),
        rec("2026-03-09T09:00:00Z", "backup_service", "backup_9009", "BACKUP_ARCHIVE", "ACTIVE", "kenji.arai@example.test"),
        rec("2026-03-09T09:00:00Z", "support_platform", "support_9009", "SUPPORT_MESSAGE", "FOUND", "kenji.arai@example.test"),
    ]),
    # H9: a flow that was disabled and a backup already blocked: clean.
    "ev_h9": ("2026-03-09T09:00:00Z", "records", [
        rec("2026-03-09T09:00:00Z", "account_service", "account_9010", "USER_PROFILE", "NOT_FOUND", "lena.fischer@example.test"),
        rec("2026-03-09T09:00:00Z", "integration_service", "data_flow_9010", "DATA_FLOW", "DISABLED", "lena.fischer@example.test"),
        rec("2026-03-09T09:00:00Z", "backup_service", "backup_9010", "BACKUP_ARCHIVE", "BLOCKED", "lena.fischer@example.test"),
    ]),
}

DEL = "Please permanently delete my account and all personal data."
REQUESTS = [
    # case_id, email, request_text, expected status, expected steps [(op, system, rid, evidence)]
    ("case_901", "ILSE.V.OLD@EXAMPLE.TEST", DEL, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "account_service", "account_9001", "ev_h1"),
        ("DELETE_RECORD", "account_service", "account_9001", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9001", ""),
        ("VERIFY_RECORD_ABSENT", "search_service", "search_9001", ""),
    ], "historical email, upper case"),
    ("case_902", "tomas.brandt@example.test", DEL, "COMPLETE", [
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9002", "ev_h2a"),
    ], "evidence captured after audit_at must be ignored"),
    ("case_903", "nadia.kerr@example.test", DEL, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "account_service", "account_9003", "ev_h3b"),
        ("DELETE_RECORD", "account_service", "account_9003", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9003", ""),
    ], "record recreated after an earlier clean check"),
    ("case_904", "ravi.menon@example.test", DEL, "INCOMPLETE", [
        ("REPORT_EXPOSED_PII", "analytics_platform", "analytics_9004", "ev_h4"),
        ("ANONYMIZE_RECORD", "analytics_platform", "analytics_9004", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9004", ""),
        ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_9004", ""),
    ], "replica drift: primary clean, reporting replica still exposed"),
    ("case_905", "sofia.lund@example.test", DEL, "INCOMPLETE", [
        ("REPORT_EXPOSED_PII", "identity_service", "tombstone_9005", "ev_h5"),
        ("REDACT_RECORD", "identity_service", "tombstone_9005", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9005", ""),
        ("VERIFY_RECORD_REDACTED", "identity_service", "tombstone_9005", ""),
    ], "tombstone kept but still carries the email"),
    ("case_906", "jo.ng@example.test", DEL, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "search_service", "search_9006", "ev_h6"),
        ("DELETE_RECORD", "search_service", "search_9006", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9006", ""),
        ("VERIFY_RECORD_ABSENT", "search_service", "search_9006", ""),
    ], "look-alike decoy email in the same index"),
    ("case_907", "marta.pinto@example.test", DEL, "COMPLETE", [
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9008", "ev_h7"),
        ("VERIFY_RECORD_PRESENT", "legal_service", "legal_hold_9008", "ev_h7"),
        ("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9008", "ev_h7"),
        ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_9008", "ev_h7"),
    ], "retained legal hold and audit record; already anonymized analytics"),
    ("case_908", "kenji.arai@example.test", DEL, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "account_service", "account_9009", "ev_h8"),
        ("REPORT_EXPOSED_PII", "support_platform", "support_9009", "ev_h8"),
        ("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9009", "ev_h8"),
        ("REPORT_RESTORE_RISK", "backup_service", "backup_9009", "ev_h8"),
        ("DISABLE_DATA_FLOW", "integration_service", "data_flow_9009", ""),
        ("BLOCK_RESTORE", "backup_service", "backup_9009", ""),
        ("DELETE_RECORD", "account_service", "account_9009", ""),
        ("REDACT_RECORD", "support_platform", "support_9009", ""),
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9009", ""),
        ("VERIFY_RECORD_REDACTED", "support_platform", "support_9009", ""),
        ("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9009", ""),
        ("VERIFY_RESTORE_BLOCKED", "backup_service", "backup_9009", ""),
    ], "recreation risks first, then records; full ordering"),
    ("case_909", "lena.fischer@example.test", DEL, "COMPLETE", [
        ("VERIFY_RECORD_ABSENT", "account_service", "account_9010", "ev_h9"),
        ("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9010", "ev_h9"),
        ("VERIFY_RESTORE_BLOCKED", "backup_service", "backup_9010", "ev_h9"),
    ], "disabled flow and blocked backup count as compliant"),
    ("case_910", "lena.fischer@example.test",
     "No quiero eliminar mi cuenta. Solo actualicen mi dirección de envío, por favor.", "NOT_APPLICABLE", [],
     "Spanish: says 'eliminar' but asks for an address update"),
    ("case_911", "lena.fischer@example.test",
     "Bitte nichts löschen. Ich möchte nur eine Kopie meiner gespeicherten Daten erhalten.", "NOT_APPLICABLE", [],
     "German: says 'löschen' but asks for a data copy"),
    ("case_912", "lena.fischer@example.test",
     "Merci de corriger l'orthographe de mon nom. Ne supprimez pas mon compte.", "NOT_APPLICABLE", [],
     "French: correction request that says 'supprimez'"),
    ("case_913", "lena.fischer@example.test",
     "Please unsubscribe me from newsletters. I want to keep using my account.", "NOT_APPLICABLE", [],
     "marketing opt-out that keeps the account"),
]
AUDIT_AT = "2026-03-10T12:00:00Z"

# Requests that ARE deletions despite wording that looks like a non-deletion intent.
CLASSIFIER_TRAPS = [
    ("I don't want to keep my account anymore. Please delete it and everything linked to it.", True),
    ("Please stop using my data and erase my profile; I no longer need a copy of anything.", True),
    ("Löschen Sie bitte mein Konto, ich brauche es nicht mehr.", True),
    ("請刪除我的帳戶和所有個人資料。", True),
    ("Por favor, eliminen mi cuenta y todos mis datos personales.", True),
    ("Veuillez supprimer définitivement mon compte.", True),
]


def write_world(d):
    (d / "evidence" / "records").mkdir(parents=True)
    (d / "evidence" / "databases").mkdir(parents=True)

    def write_csv(name, header, rows):
        with open(d / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write_csv("user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
              [(u, n, e, p, f"{i:064x}", "2026-01-01T00:00:00Z") for i, (u, n, e, p) in enumerate(USERS, 1)])
    write_csv("user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], AUDIT)
    write_csv("system_activity.csv", ["activity_id", "event_time", "event_type", "source_system", "target_system",
                                      "resource_id", "reported_result", "details"], [])
    manifest = []
    for fid, (cap, kind, content) in FILES.items():
        if kind == "sql":
            path = f"evidence/databases/{fid}.sql"
            lines = []
            for table in sorted({t for t, *_ in content}):
                lines.append(f'CREATE TABLE "{table}" (origin_system TEXT, collection_name TEXT, record_json TEXT);')
            for table, system, coll, r in content:
                lines.append(f"INSERT INTO \"{table}\" VALUES ('{system}', '{coll}', '{json.dumps(r)}');")
            (d / path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path = f"evidence/records/{fid}.json"
            (d / path).write_text(json.dumps({"collection": "", "records": content}), encoding="utf-8")
        manifest.append((fid, path, "synthetic_test", cap))
    write_csv("evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], manifest)
    header = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
    write_csv("sample_deletion_requests.csv", header, [])
    write_csv("deletion_requests.csv", header,
              [(c, e, "2026-03-01T09:00:00Z", "COMPLETE", "2026-03-03T09:00:00Z", AUDIT_AT, t)
               for c, e, t, *_ in REQUESTS])


def expected_steps(steps):
    return [{"step_no": i, "phase": PHASE[op], "system_id": s, "resource_id": r, "operation": op,
             "state": STATE[op], "evidence_id": e} for i, (op, s, r, e) in enumerate(steps, 1)]


def synthetic():
    passed = total = 0
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_world(d)
        rows = {c: (s, json.loads(j)) for c, s, j in run(d, "deletion_requests.csv", {})}
    print("Part 1: synthetic trap scenarios")
    for cid, _, _, status, steps, why in REQUESTS:
        got_status, got = rows[cid]
        want = expected_steps(steps)
        ok = got_status == status and got == want
        total += 1
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'} {cid} {why}")
        if not ok:
            print(f"       status {got_status} (want {status})")
            gk = [(s["operation"], s["resource_id"], s["evidence_id"]) for s in got]
            wk = [(s["operation"], s["resource_id"], s["evidence_id"]) for s in want]
            for x in wk:
                if x not in gk:
                    print("       missing", x)
            for x in gk:
                if x not in wk:
                    print("       extra  ", x)
            if sorted(gk) == sorted(wk):
                print("       wrong order:", gk)
    for text, want in CLASSIFIER_TRAPS:
        ok = is_deletion_request(text) == want
        total += 1
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'} classifier trap {text[:58]!r} -> {want}")
    return passed, total


# ------------------------------------------------------------------ part 2: metamorphic tests

def run_with(requests_rows, extra_identities=()):
    """Run the agent on a copy of the dataset whose request file (and identities) are changed."""
    src = ROOT / "dataset"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for p in src.iterdir():
            if p.name not in ("deletion_requests.csv", "user_identities.csv"):
                (d / p.name).symlink_to(p)
        shutil.copy(src / "user_identities.csv", d / "user_identities.csv")
        if extra_identities:
            with open(d / "user_identities.csv", "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(extra_identities)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(requests_rows[0].keys()))
        w.writeheader()
        w.writerows(requests_rows)
        (d / "deletion_requests.csv").write_text(buf.getvalue(), encoding="utf-8")
        return {c: (s, j) for c, s, j in run(d, "deletion_requests.csv", load_notes())}


def load_notes():
    return json.loads((ROOT / "code" / "agent" / "image_notes.json").read_text(encoding="utf-8"))


def metamorphic():
    src = ROOT / "dataset"
    base_rows = list(csv.DictReader(open(src / "deletion_requests.csv", encoding="utf-8")))
    base = run_with(base_rows)
    identities = list(csv.DictReader(open(src / "user_identities.csv", encoding="utf-8")))
    current = {u["email"].lower(): u["user_id"] for u in identities}
    by_user = {u["user_id"]: u["email"] for u in identities}
    old = {}
    for a in csv.DictReader(open(src / "user_audit_log.csv", encoding="utf-8")):
        if a["field_name"] == "email":
            old.setdefault(a["user_id"], a["old_value"])

    def compare(name, out):
        diff = [c for c in base if base[c] != out.get(c)]
        print(f"  {'PASS' if not diff else 'FAIL'} {name}: {len(base) - len(diff)}/{len(base)} cases unchanged")
        for c in diff[:5]:
            print("       changed", c)
        return not diff

    print("Part 2: metamorphic tests on the real dataset")
    results = []
    # 1. Swap each request email for the same person's other email (current <-> historical).
    swapped, n = [], 0
    for r in base_rows:
        r = dict(r)
        uid = current.get(r["email"].lower())
        if uid and uid in old:
            r["email"], n = old[uid], n + 1
        else:
            for u, e in old.items():
                if r["email"].lower() == e.lower():
                    r["email"], n = by_user[u], n + 1
                    break
        swapped.append(r)
    print(f"    ({n} requests had their email swapped)")
    results.append(compare("current/historical email swap", run_with(swapped)))
    # 2. Upper-case every request email.
    upper = [dict(r, email=r["email"].upper()) for r in base_rows]
    results.append(compare("upper-case request emails", run_with(upper)))
    # 3. Shuffle request row order.
    shuffled = list(base_rows)
    random.Random(7).shuffle(shuffled)
    results.append(compare("shuffled request order", run_with(shuffled)))
    # 4. Add look-alike decoy users that nothing in the evidence belongs to.
    decoys = [(f"user_99{i}", "Decoy Person", e.replace("@", f".x{i}@"), f"+1 555 010 {i:04d}", f"{i:064x}",
               "2026-01-01T00:00:00Z") for i, e in enumerate(sorted(current)[:25])]
    results.append(compare("look-alike decoy identities added", run_with(base_rows, decoys)))
    return sum(results), len(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="only part 1")
    a = ap.parse_args()
    p1, t1 = synthetic()
    p2 = t2 = 0
    if not a.synthetic:
        p2, t2 = metamorphic()
    print(f"\nsynthetic {p1}/{t1}, metamorphic {p2}/{t2}")
    return 0 if (p1, p2) == (t1, t2) else 1


if __name__ == "__main__":
    sys.exit(main())
