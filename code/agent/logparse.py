"""Parse the heterogeneous evidence log formats into uniform event dicts."""

import json
import re

TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
KV = re.compile(r'([A-Za-z_][\w.]*)=("([^"]*)"|(\S+))')
SYSLOG = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")
BRACKET = re.compile(r"^\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w.-]+)\]\s+(\w+):\s*(.*)$")
PIPE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([\w-]+)\s*\|\s*([\w.-]+)\s*\|\s*(\w+)\s*\|\s*(.*)$")


def _kv(text):
    out = {}
    for m in KV.finditer(text):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(4)
    return out


def parse_line(line):
    m = TS.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    ev = {"ts": ts}
    if rest.startswith("{"):
        fields = json.loads(rest)
        fields = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        ev.update(fields)
        ev["service"] = fields.get("service") or fields.get("logger", "")
        ev["trace"] = fields.get("trace_id", "")
        return ev
    rest = rest.replace('\\"', '"')
    if (s := SYSLOG.match(rest)):
        _, service, trace, body = s.groups()
        ev.update(_kv(body))
        ev["service"], ev["trace"] = service, trace
        return ev
    if (b := BRACKET.match(rest)):
        _, service, trace, event, body = b.groups()
        ev.update(_kv(body))
        ev["service"], ev["trace"], ev["event"] = service, trace, event
        return ev
    if (p := PIPE.match(rest)):
        _, service, trace, event, body = p.groups()
        ev.update(_kv(body))
        ev["service"], ev["trace"], ev["event"] = service, trace, event
        return ev
    if rest.startswith("requestId="):
        head, _, msg = rest.partition(" message=")
        ev.update(_kv(head))
        msg = msg.strip()
        if msg.startswith('"') and msg.endswith('"'):
            msg = msg[1:-1]
        ev.update(_kv(msg))
        ev["service"] = ev.get("function", "")
        ev["trace"] = ev.get("requestId", "")
        return ev
    ev.update(_kv(rest))
    ev.setdefault("service", "")
    ev["trace"] = ev.get("trace_id", "")
    return ev


def parse_file(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            e = parse_line(line)
            if e:
                e["line"] = i + 1
                events.append(e)
    return events


def merge_traces(events):
    """Fold the started/completed lines of one trace into a single lookup record."""
    traces = {}
    for e in events:
        t = e.get("trace")
        if not t or t.startswith("collector"):
            continue
        traces.setdefault(t, []).append(e)
    return traces
