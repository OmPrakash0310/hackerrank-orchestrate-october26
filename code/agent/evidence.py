"""Load every evidence file into normalized observations.

An observation says: in file F (captured at T), resource R in system S was seen in
state X, linked to identifiers I. The case engine filters these by the subject's
identifiers and by the case's audit_at cutoff.
"""

import csv
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .logparse import parse_file

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_ID = re.compile(r"\buser_\d+\b")
HASH = re.compile(r"\b[0-9a-f]{64}\b")
PHONE = re.compile(r"\+\d[\d ()-]{6,}\d")

# States
PRESENT = "PRESENT"        # record exists and carries direct identifiers
CLEAN = "CLEAN"            # record exists, direct identifiers removed (redacted/anonymized)
ABSENT = "ABSENT"          # record not found
ACTIVE = "ACTIVE"          # data flow enabled / restore allowed
DISABLED = "DISABLED"
BLOCKED = "BLOCKED"
RETAINED = "RETAINED"      # policy-retained record observed present

DIRECT_FIELDS = ("email", "phone", "email_hash", "full_name", "name", "display_name",
                 "requester_email", "requester_name", "requester_phone", "owner_email",
                 "owner_phone", "customer_email", "uploaded_by")
REF_FIELDS = ("user_id", "customer_reference", "subject_reference", "subject_selector",
              "subject_ref", "lookup_value")

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "cache_service": "CACHE_ENTRY",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "object_storage": "SUPPORT_ATTACHMENT",
}


@dataclass
class Obs:
    file_id: str
    captured_at: str
    system: str
    resource_id: str
    state: str
    category: str
    idents: set = field(default_factory=set)
    source: str = ""
    detail: dict = field(default_factory=dict)


def norm(v):
    if v is None:
        return None
    v = str(v).strip()
    if v.lower() in ("", "null", "none", "[redacted]", "no direct identifier"):
        return None
    return v.lower()


def idents_from_text(text):
    out = set()
    for rx in (EMAIL, USER_ID, HASH, PHONE):
        out.update(m.lower() for m in rx.findall(text or ""))
    return out


def idents_from_record(rec):
    out = set()
    for k, v in rec.items():
        if isinstance(v, dict):
            out |= idents_from_record(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    out |= idents_from_record(x)
        elif k in DIRECT_FIELDS or k in REF_FIELDS:
            n = norm(v)
            if n:
                out.add(n)
        elif isinstance(v, str):
            out |= idents_from_text(v)
    return out


def has_direct(rec):
    for k, v in rec.items():
        if isinstance(v, dict) and has_direct(v):
            return True
        if k in DIRECT_FIELDS and norm(v):
            return True
        if k == "message_text" and norm(v) and idents_from_text(v) - {x for x in idents_from_text(v) if x.startswith("user_")}:
            return True
    return False


def system_of(resource_id, default=""):
    prefix = resource_id.rsplit("_", 1)[0]
    return {
        "account": "account_service", "search": "search_service", "analytics": "analytics_platform",
        "support": "support_platform", "export": "export_service", "data_flow": "integration_service",
        "backup": "backup_service", "audit_record": "security_service", "legal_hold": "legal_service",
        "tombstone": "identity_service", "cache": "cache_service", "marketing": "marketing_platform",
        "email_delivery": "email_delivery_service", "object": "object_storage",
    }.get(prefix, default)


# Generic "record_type/result" rows (manifests, ledgers, handover, record_lookup logs).
RESULT_STATE = {
    "FOUND": PRESENT, "NOT_FOUND": ABSENT, "IDENTIFIABLE": PRESENT, "ANONYMIZED": CLEAN,
    "REDACTED": CLEAN, "ACTIVE": ACTIVE, "DISABLED": DISABLED, "BLOCKED": BLOCKED,
}


def obs_from_result_row(fid, cap, row, source):
    rtype = row.get("record_type", "")
    system = row.get("system") or system_of(row["resource_id"])
    state = RESULT_STATE.get(str(row.get("result", "")).upper(), PRESENT)
    ref = row.get("subject_reference") or row.get("lookup_value") or ""
    idents = {norm(ref)} if norm(ref) else set()
    return Obs(fid, cap, system, row["resource_id"], state, rtype or SYSTEM_CATEGORY.get(system, ""),
               idents, source, dict(row))


def obs_from_record(fid, cap, system, collection, rec, source):
    """A full data record (snapshot bundle, SQL record_json, inventory row, service export)."""
    rid = rec.get("resource_id")
    system = system or system_of(rid)
    category = SYSTEM_CATEGORY.get(system, "")
    idents = idents_from_record(rec)
    direct = has_direct(rec)
    state = PRESENT if direct else CLEAN
    if collection == "ticket_attachments":
        category = "SUPPORT_ATTACHMENT"
    if system == "integration_service":
        state = ACTIVE if truthy(rec.get("enabled")) else DISABLED
    elif system == "backup_service":
        state = ACTIVE if truthy(rec.get("restore_allowed")) else BLOCKED
    elif system in ("security_service", "legal_service"):
        state = RETAINED
    elif system == "account_service" and "account_status" in rec:
        state = PRESENT
    return Obs(fid, cap, system, rid, state, category, idents, source, dict(rec))


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "enabled")


# ---------------------------------------------------------------- JSON files

def load_json(fid, cap, path, coverage=None):
    d = json.load(open(path, encoding="utf-8"))
    out = []
    if coverage is not None and "snapshots" in d:
        for s in d["snapshots"]:
            if s.get("collection") == "customer_profiles":
                coverage.setdefault(fid, {})[s["data_store"]] = s.get("captured_at", cap)
    if "service_exports" in d:
        for system, groups in d["service_exports"].items():
            for group, items in groups.items():
                for it in items:
                    out.extend(obs_from_service_export(fid, cap, system, group, it))
    elif "snapshots" in d:
        for s in d["snapshots"]:
            for r in s["records"]:
                if "record_type" in r:
                    out.append(obs_from_result_row(fid, cap, r, s["collection"]))
                else:
                    out.append(obs_from_record(fid, cap, s["data_store"], s["collection"], r, s["collection"]))
    elif "records" in d:
        coll = d.get("collection", "")
        for r in d["records"]:
            if "record_type" in r:
                out.append(obs_from_result_row(fid, cap, r, coll or d.get("origin", "")))
            else:
                system = system_of(r["resource_id"])
                o = obs_from_record(fid, cap, system, coll, r, coll)
                if system == "export_service" and "download_available" in r:
                    o.state = PRESENT if truthy(r["download_available"]) else ABSENT
                out.append(o)
    return out


def obs_from_service_export(fid, cap, system, group, it):
    rid = it["resource_id"]
    if group == "profile_lookups":
        prof = it.get("profile") or {}
        state = PRESENT if it.get("status_code") == 200 else ABSENT
        return [Obs(fid, cap, system, rid, state, "USER_PROFILE", idents_from_record(prof), group, dict(it))]
    if group == "index_queries":
        docs = (it.get("hits") or {}).get("documents") or []
        out = []
        for doc in docs:
            idents = idents_from_record(doc) | ({norm(it.get("query"))} if norm(it.get("query")) else set())
            out.append(Obs(fid, cap, system, doc["resource_id"], PRESENT, "SEARCH_INDEX_ENTRY", idents, group, dict(it)))
        if not docs:
            q = norm(it.get("query"))
            out.append(Obs(fid, cap, system, rid, ABSENT, "SEARCH_INDEX_ENTRY", {q} if q else set(), group, dict(it)))
        return out
    rec = {k: v for k, v in it.items() if k != "observed_at"}
    return [obs_from_record(fid, cap, system, group, rec, group)]


# ---------------------------------------------------------------- SQL files

def load_sql(fid, cap, path):
    db = sqlite3.connect(":memory:")
    db.executescript(open(path, encoding="utf-8").read())
    out = []
    tables = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
    for t in tables:
        cols = [c[1] for c in db.execute(f'pragma table_info("{t}")')]
        rows = [dict(zip(cols, r)) for r in db.execute(f'select * from "{t}"')]
        replica = t
        if "record_json" in cols:
            for r in rows:
                rec = json.loads(r["record_json"])
                o = obs_from_record(fid, cap, r["origin_system"], r["collection_name"], rec, replica)
                out.append(o)
        elif t == "snapshot_scope":
            continue
        else:
            for r in rows:
                system = system_of(r["resource_id"])
                if t.startswith("customer_profiles"):
                    system = "account_service"
                o = obs_from_record(fid, cap, system, t, r, replica)
                if t == "customer_search_documents" or t.startswith("search_documents"):
                    o.state = PRESENT
                out.append(o)
    # Inventory snapshots list every in-scope system: a subject's resource that is
    # missing from the table is absent. That is resolved later from scope + activity.
    scope = {}
    if "snapshot_scope" in tables:
        scope = dict(db.execute("select origin_system, captured_at from snapshot_scope"))
    return out, scope


# ---------------------------------------------------------------- logs

LOOKUP_KEYS = ("lookup_key", "match_key", "subject_key", "owner_key", "scan_key", "subject_ref",
               "correlation_key", "matched_value")


def load_log(fid, cap, path):
    events = parse_file(path)
    out = []
    traces = {}
    for e in events:
        traces.setdefault(e.get("trace") or f"line{e['line']}", []).append(e)
    for trace, evs in traces.items():
        keys = set()
        for e in evs:
            for k in LOOKUP_KEYS:
                n = norm(e.get(k))
                if n:
                    keys.add(n)
        for e in evs:
            o = obs_from_log_event(fid, cap, e, keys)
            if o:
                out.append(o)
    return out


def obs_from_log_event(fid, cap, e, keys):
    ev = e.get("event", "")
    rid = e.get("resource_id")
    if ev == "record_lookup":
        return obs_from_result_row(fid, cap, e, e.get("service", ""))
    if not rid:
        return None
    system = system_of(rid, e.get("service", ""))
    idents = set(keys) | idents_from_record({k: v for k, v in e.items() if k in DIRECT_FIELDS + REF_FIELDS})
    d = dict(e)

    def mk(state, category=None):
        return Obs(fid, cap, system, rid, state, category or SYSTEM_CATEGORY.get(system, ""), idents, ev, d)

    if ev == "profile_lookup_completed":
        ok = e.get("http_status") == "200" or e.get("rows") == "1"
        return mk(PRESENT if ok else ABSENT)
    if ev == "index_query_completed":
        return mk(PRESENT if e.get("result") == "hit" else ABSENT)
    if ev in ("analytics_profile_read_completed", "analytics_profile_read"):
        direct = has_direct(e) or e.get("direct_identifiers") not in (None, "0")
        return mk(PRESENT if direct else CLEAN)
    if ev == "analytics_identity_scan":
        return mk(PRESENT if e.get("direct_identifiers") != "0" else CLEAN)
    if ev == "contact_lookup_completed":
        cat = "MARKETING_PROFILE" if truthy(e.get("email_opt_in")) else "ANALYTICS_PROFILE"
        return mk(PRESENT if e.get("rows") != "0" else ABSENT, cat)
    if ev == "audit_record_read_completed":
        return mk(RETAINED if e.get("rows") != "0" else ABSENT)
    if ev in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
        active = truthy(e.get("enabled")) and e.get("scheduler_registered", "true") != "false"
        return mk(ACTIVE if active else DISABLED)
    if ev == "restore_policy_read_completed":
        return mk(ACTIVE if truthy(e.get("restore_allowed")) else BLOCKED)
    if ev == "restore_candidate_evaluated":
        active = truthy(e.get("eligible")) and not truthy(e.get("deletion_block"))
        return mk(ACTIVE if active else BLOCKED)
    if ev == "restore_authorization_completed":
        return mk(BLOCKED if e.get("decision") == "deny" else ACTIVE)
    if ev in ("export_metadata_read_completed", "export_download_check_completed"):
        return mk(PRESENT if truthy(e.get("available")) else ABSENT)
    if ev == "support_ticket_read_completed":
        return mk(PRESENT if has_direct(e) else CLEAN)
    if ev == "message_pii_scan":
        return mk(PRESENT, "SUPPORT_MESSAGE")
    if ev == "attachment_pii_scan":
        return mk(PRESENT, "SUPPORT_ATTACHMENT")
    if ev == "message_read":
        return mk(CLEAN if not norm(e.get("message_text")) else PRESENT, "SUPPORT_MESSAGE")
    if ev == "contact_query_completed":
        # A legacy contact-index copy of the subject: a secondary identifier, deleted outright.
        return mk(PRESENT if e.get("rows") != "0" else ABSENT, "SECONDARY_IDENTIFIER")
    if ev == "retained_order_loaded":
        return mk(RETAINED)
    return None


# ---------------------------------------------------------------- text / PDF / images

TEXT_ROW = re.compile(r"^-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+)$")


def load_text(fid, cap, text, source):
    out = []
    for line in text.splitlines():
        m = TEXT_ROW.match(line.strip())
        if m:
            ts, system, rid, rtype, result, ref = m.groups()
            row = {"recorded_at": ts, "system": system, "resource_id": rid, "record_type": rtype,
                   "result": result, "subject_reference": ref.strip()}
            out.append(obs_from_result_row(fid, cap, row, source))
    return out


def pdf_text(path):
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                              text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


RESOURCE = re.compile(r"\b((?:account|search|analytics|support|export|data_flow|backup|audit_record|legal_hold|"
                      r"tombstone|marketing|object)_\d+)\b")
DOC_REF = re.compile(r"\bNST-\d{4}-\d{4}-\d{4}\b")


def load_document(fid, cap, text):
    """A business document (PDF) is itself a copy of whatever records it references."""
    out = []
    idents = idents_from_text(text)
    for rid in sorted(set(RESOURCE.findall(text))):
        system = system_of(rid)
        state = RETAINED if system in ("security_service", "legal_service") else PRESENT
        cat = "SUPPORT_ATTACHMENT" if system == "support_platform" and "attachment" in text.lower() else SYSTEM_CATEGORY.get(system, "")
        out.append(Obs(fid, cap, system, rid, state, cat, set(idents), "document", {}))
    for ref in sorted(set(DOC_REF.findall(text))):
        out.append(Obs(fid, cap, "", ref, PRESENT, "", set(idents), "document", {"doc_ref": ref}))
    return out


# ---------------------------------------------------------------- entry point

def load_all(dataset_dir, image_notes=None):
    """Return (observations, manifest, inventory_scopes)."""
    dataset_dir = Path(dataset_dir)
    manifest = list(csv.DictReader(open(dataset_dir / "evidence_files.csv", encoding="utf-8")))
    obs, scopes, texts = [], {}, {}
    for m in manifest:
        fid, cap, path = m["file_id"], m["captured_at"], dataset_dir / m["file_path"]
        suffix = path.suffix.lower()
        if suffix == ".json":
            obs += load_json(fid, cap, path, scopes)
        elif suffix == ".sql":
            o, scope = load_sql(fid, cap, path)
            obs += o
            if scope:
                scopes[fid] = scope
        elif suffix == ".log":
            obs += load_log(fid, cap, path)
        elif suffix == ".txt":
            t = path.read_text(encoding="utf-8")
            texts[fid] = t
            obs += load_text(fid, cap, t, "text")
        elif suffix == ".pdf":
            texts[fid] = pdf_text(path)
            obs += load_document(fid, cap, texts[fid])
        elif suffix == ".png" and image_notes and m["file_path"] in image_notes:
            for r in image_notes[m["file_path"]]:
                obs.append(Obs(fid, cap, r["system"], r["resource_id"], r["state"], r["category"],
                               {i.lower() for i in r["idents"]}, "screenshot", dict(r)))
    return obs, manifest, scopes, texts
