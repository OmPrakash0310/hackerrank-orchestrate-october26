"""Case engine: resolve the subject, link resources, reconstruct state at audit_at, apply policy, emit steps."""

import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import evidence as ev
from .classify import is_deletion_request

OP_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
PHASE = {op: ("FINDING" if i < 4 else "REMEDIATION" if i < 9 else "VERIFICATION") for i, op in enumerate(OP_ORDER)}
STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# Policy treatment per data category (deletion_rules.csv).
TREATMENT = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
    "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
    "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "BLOCK", "ACCOUNT_TOMBSTONE": "REDACT",
    "DATA_FLOW": "DISABLE",
}

# Activity windows around a case's reported_at in which the deletion worker acts for it,
# by kind of worker message (measured on the whole activity stream).
WINDOWS = {
    "submitted": (timedelta(hours=1.3), timedelta(hours=1.0)),   # account submission
    "policy": (timedelta(hours=0.6), timedelta(hours=0.05)),     # policy action completed
    "received": (timedelta(hours=0.9), timedelta(hours=2.4)),    # final result from target
    "reported": (timedelta(hours=0.2), timedelta(hours=0.2)),
    "stopped": (timedelta(hours=0.2), timedelta(hours=0.3)),
    "check": (timedelta(hours=0.5), timedelta(hours=2.4)),
}
WORKER_SOURCES = {"deletion_worker"}


def worker_kind(a):
    d = a["details"]
    if d.startswith("Policy action completed"):
        return "policy"
    if "submitted primary profile" in d:
        return "submitted"
    if "received the final result" in d:
        return "received"
    if "reported the requested operation" in d:
        return "reported"
    if "stopped after" in d or "could not confirm" in d:
        return "stopped"
    return "check"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def num(rid):
    m = re.search(r"(\d+)$", rid)
    return int(m.group(1)) if m else 0


class Context:
    def __init__(self, dataset_dir, image_notes=None):
        d = Path(dataset_dir)
        self.identities = {r["user_id"]: r for r in csv.DictReader(open(d / "user_identities.csv", encoding="utf-8"))}
        self.audit = list(csv.DictReader(open(d / "user_audit_log.csv", encoding="utf-8")))
        self.activity = list(csv.DictReader(open(d / "system_activity.csv", encoding="utf-8")))
        self.obs, self.manifest, self.scopes, self.texts = ev.load_all(d, image_notes)
        self.captured = {m["file_id"]: m["captured_at"] for m in self.manifest}

        self.email_index = defaultdict(set)
        for u in self.identities.values():
            self.email_index[u["email"].lower()].add(u["user_id"])
        for a in self.audit:
            if a["field_name"] == "email":
                self.email_index[a["old_value"].lower()].add(a["user_id"])
                self.email_index[a["new_value"].lower()].add(a["user_id"])

        self.user_idents = {uid: self._identifiers(uid) for uid in self.identities}
        self.ident_users = defaultdict(set)
        for uid, ids in self.user_idents.items():
            for i in ids:
                self.ident_users[i].add(uid)

        # Data-catalog links tie a resource (or a document reference) to a subject.
        self.catalog_owner, alias = {}, {}
        for a in self.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"(\S+) \(([^)]+)\) (?:to|belongs only to) (\S+) and (user_\d+)", a["details"])
                if m:
                    rid, ref, _, uid = m.groups()
                    self.catalog_owner[rid] = (uid, a["activity_id"])
                    if ref != rid:
                        alias[ref] = rid
        for o in self.obs:
            if o.resource_id in alias:
                o.resource_id = alias[o.resource_id]
                o.system = ev.system_of(o.resource_id)
                o.category = ev.SYSTEM_CATEGORY.get(o.system, "")

        self.by_resource = defaultdict(list)
        self.res_owners = defaultdict(set)
        for rid, (uid, _) in self.catalog_owner.items():
            self.res_owners[rid].add(uid)
        for o in self.obs:
            self.by_resource[o.resource_id].append(o)
            for i in o.idents:
                self.res_owners[o.resource_id] |= self.ident_users.get(i, set())
        self.user_resources = defaultdict(set)
        for rid, users in self.res_owners.items():
            for u in users:
                self.user_resources[u].add(rid)

        # Files that capture a full system table: absence of a resource there is evidence.
        self.coverage = defaultdict(list)  # system -> [(captured_at, file_id)]
        for fid, scope in self.scopes.items():
            for system in scope:
                self.coverage[system].append((self.captured[fid], fid))
        for m in self.manifest:
            if "failover_replica" in m["file_path"] and "accounts" in m["file_path"]:
                self.coverage["account_service"].append((m["captured_at"], m["file_id"]))
        for v in self.coverage.values():
            v.sort()

    def _identifiers(self, user_id):
        u = self.identities[user_id]
        ids = {user_id.lower()}
        for k in ("email", "phone", "email_hash"):
            if u.get(k):
                ids.add(u[k].lower())
        for a in self.audit:
            if a["user_id"] == user_id:
                for v in (a["old_value"], a["new_value"]):
                    if v:
                        ids.add(v.lower())
        return ids

    def resolve(self, email):
        return sorted(self.email_index.get(email.strip().lower(), set()))

    # ------------------------------------------------------------ activity linking

    def link_activity(self, requests):
        """Assign activity-only resources to cases.

        The deletion worker acts on a case's resources shortly before its reported_at.
        Several cases share a batch, so ownership is resolved by elimination: a resource
        whose observations name a user belongs to that user's case; each case has one
        account; leftovers are matched in case order.
        """
        cases = []
        for r in requests:
            if not is_deletion_request(r["request_text"]):
                continue
            users = self.resolve(r["email"])
            if users:
                cases.append((r["case_id"], users[0], ts(r["reported_at"])))
        cand = defaultdict(set)    # resource -> case ids
        system_of, event_time = {}, {}
        for a in self.activity:
            if a["source_system"] not in WORKER_SOURCES:
                continue
            t = ts(a["event_time"])
            before, after = WINDOWS[worker_kind(a)]
            for cid, uid, rep in cases:
                if rep - before <= t <= rep + after:
                    cand[a["resource_id"]].add(cid)
                    system_of[a["resource_id"]] = a["target_system"]
                    event_time[a["resource_id"]] = a["event_time"]
        case_user = {cid: uid for cid, uid, _ in cases}
        assigned = defaultdict(set)  # case -> resources
        open_res = {}
        for rid, cids in cand.items():
            owners = self.res_owners.get(rid)
            if owners:
                for cid in cids:
                    if case_user[cid] in owners:
                        assigned[cid].add(rid)
            else:
                open_res[rid] = set(cids)

        def has_system(cid, system):
            return any(system_of.get(x) == system or ev.system_of(x) == system for x in assigned[cid])

        # Accounts: one per case. Propagate until stable.
        changed = True
        while changed:
            changed = False
            for rid, cids in list(open_res.items()):
                if system_of[rid] != "account_service":
                    continue
                live = {c for c in cids if not has_system(c, "account_service")}
                if len(live) == 1:
                    assigned[live.pop()].add(rid)
                    del open_res[rid]
                    changed = True
                elif live != cids:
                    open_res[rid] = live
        # Remaining: per system, match in order (case number vs worker event time).
        by_sys = defaultdict(list)
        for rid, cids in open_res.items():
            by_sys[system_of[rid]].append(rid)
        for system, rids in by_sys.items():
            pool = sorted({c for r in rids for c in open_res[r] if not has_system(c, system)},
                          key=lambda c: int(c.split("_")[1]))
            for rid in sorted(rids, key=lambda r: event_time[r]):
                options = [c for c in pool if c in open_res[rid]]
                if options:
                    assigned[options[0]].add(rid)
                    pool.remove(options[0])
        self.activity_links = assigned
        self.activity_system = system_of


def step(op, system, rid, evidence=""):
    return {"phase": PHASE[op], "system_id": system, "resource_id": rid, "operation": op,
            "state": STATE[op], "evidence_id": evidence}


def finalize(steps):
    seen, uniq = set(), []
    for s in steps:
        k = (s["operation"], s["system_id"], s["resource_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    uniq.sort(key=lambda s: (OP_ORDER.index(s["operation"]), s["system_id"], s["resource_id"]))
    return [{"step_no": i, **s} for i, s in enumerate(uniq, 1)]


def epoch_start(ctx, uid, req, all_requests):
    """For a user with several requests, evidence before the previous request's audit belongs to it."""
    prev = [r["audit_at"] for r in all_requests
            if r["case_id"] != req["case_id"] and ctx.resolve(r["email"])[:1] == [uid]
            and r["audit_at"] < req["requested_at"]]
    return max(prev) if prev else ""


def audit_case(ctx, req, all_requests):
    if not is_deletion_request(req["request_text"]):
        return "NOT_APPLICABLE", []
    users = ctx.resolve(req["email"])
    if not users:
        return "COMPLETE", []
    uid = users[0]
    audit_at = req["audit_at"]
    start = epoch_start(ctx, uid, req, all_requests)

    resources = {r for r in ctx.user_resources[uid]
                 if any(start < o.captured_at for o in ctx.by_resource[r])}
    resources |= ctx.activity_links.get(req["case_id"], set())

    # One account per case: keep the one the worker touched for this case if there is a choice.
    accounts = sorted(r for r in resources if ev.system_of(r) == "account_service")
    if len(accounts) > 1:
        linked = [r for r in accounts if r in ctx.activity_links.get(req["case_id"], set())]
        if linked:
            resources -= set(accounts) - set(linked)

    findings, remediation, verification = [], [], []
    for rid in sorted(resources):
        obs = [o for o in ctx.by_resource[rid] if o.captured_at > start]
        system = ev.system_of(rid) or (obs[0].system if obs else ctx.activity_system.get(rid, ""))
        current = [o for o in obs if o.captured_at <= audit_at]
        latest = max(current, key=lambda o: (o.captured_at, o.file_id)) if current else None
        cats = [o.category for o in obs if o.category]
        cat = latest.category if latest and latest.category else (cats[0] if cats else ev.SYSTEM_CATEGORY.get(system, ""))
        treat = TREATMENT.get(cat, "DELETE")
        state = latest.state if latest else None
        f = latest.file_id if latest else ""
        if latest is None and system == "account_service":
            cov = [fid for cap, fid in ctx.coverage.get(system, []) if cap <= audit_at]
            state, f = ev.ABSENT, (cov[-1] if cov else "")

        if treat == "DISABLE":
            if state == ev.ACTIVE:
                findings.append(step("REPORT_ACTIVE_DATA_FLOW", system, rid, f))
                remediation.append(step("DISABLE_DATA_FLOW", system, rid))
            verification.append(step("VERIFY_DATA_FLOW_DISABLED", system, rid, f))
        elif treat == "BLOCK":
            if state == ev.ACTIVE:
                findings.append(step("REPORT_RESTORE_RISK", system, rid, f))
                remediation.append(step("BLOCK_RESTORE", system, rid))
            verification.append(step("VERIFY_RESTORE_BLOCKED", system, rid, f))
        elif treat == "RETAIN":
            if state != ev.ABSENT:
                verification.append(step("VERIFY_RECORD_PRESENT", system, rid, f))
        elif treat == "DELETE":
            if state in (ev.PRESENT, ev.CLEAN):
                findings.append(step("REPORT_REMAINING_RECORD", system, rid, f))
                remediation.append(step("DELETE_RECORD", system, rid))
            verification.append(step("VERIFY_RECORD_ABSENT", system, rid, f))
        elif treat == "REDACT":
            if state == ev.PRESENT:
                findings.append(step("REPORT_EXPOSED_PII", system, rid, f))
                remediation.append(step("REDACT_RECORD", system, rid))
            verification.append(step("VERIFY_RECORD_REDACTED", system, rid, f))
        elif treat == "ANONYMIZE":
            if state == ev.PRESENT:
                findings.append(step("REPORT_EXPOSED_PII", system, rid, f))
                remediation.append(step("ANONYMIZE_RECORD", system, rid))
            verification.append(step("VERIFY_RECORD_ANONYMIZED", system, rid, f))

    if findings:
        for s in verification:
            s["evidence_id"] = ""
        return "INCOMPLETE", finalize(findings + remediation + verification)
    return "COMPLETE", finalize(verification)


def run(dataset_dir, requests_file, image_notes=None):
    d = Path(dataset_dir)
    ctx = Context(d, image_notes)
    everything = []
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        everything += list(csv.DictReader(open(d / name, encoding="utf-8")))
    ctx.link_activity(everything)
    rows = list(csv.DictReader(open(d / requests_file, encoding="utf-8")))
    out = []
    for r in rows:
        status, steps = audit_case(ctx, r, everything)
        out.append((r["case_id"], status, json.dumps(steps, separators=(",", ":"))))
    out.sort(key=lambda x: int(x[0].split("_")[1]))
    return out
