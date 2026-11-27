"""Traceable network time delivery.

The provisional monitor in :mod:`netcourt.time_note` averages raw offsets, so a
+40 us sample and a -40 us sample cancel into a fake "accurate" verdict.  This
module never averages opposite offsets away.  Every calibration contributes a
closed error interval ``[lo_us, hi_us]`` derived from the two-way exchange and
the independently validated path-asymmetry bound; formal adjudication only
claims accuracy when intervals from *independent* trust domains intersect and
the intersection is tight enough.

Persisted evidence per calibration: source authentication chain, measurement
path, two-way (round-trip) delay, asymmetry error, node oscillator state and
the measurement window.  Proofs freeze the raw samples and the error bound at
issuance time.  A source later condemned for spoofing or leap-second mishandling
produces corrections that point back at the original proofs and list the
transactions a customer must re-review; the frozen evidence itself is never
rewritten.  When no defensible ordering exists, transactions are reported as
``unsortable`` instead of being assigned a precise time.
"""
import hashlib
import json

from .store import one, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS time_sources(
  source_id TEXT PRIMARY KEY, kind TEXT, auth_chain TEXT, leap_table TEXT,
  status TEXT, compromised INTEGER DEFAULT 0, detail TEXT);
CREATE TABLE IF NOT EXISTS time_calibrations(
  cal_id TEXT PRIMARY KEY, node TEXT, source TEXT, window_start INTEGER,
  window_end INTEGER, t1 INTEGER, t2 INTEGER, t3 INTEGER, t4 INTEGER,
  rtt_us INTEGER, offset_us INTEGER, asym_bound_us INTEGER, bound_us INTEGER,
  lo_us INTEGER, hi_us INTEGER, path TEXT, path_fault INTEGER,
  osc_state TEXT, leap_table TEXT, late INTEGER DEFAULT 0, received_at INTEGER,
  valid INTEGER DEFAULT 1, reason TEXT);
CREATE TABLE IF NOT EXISTS time_verdicts(
  verdict_id TEXT PRIMARY KEY, node TEXT, window_start INTEGER,
  window_end INTEGER, decision TEXT, lo_us INTEGER, hi_us INTEGER,
  bound_us INTEGER, evidence TEXT, groups TEXT, suspect TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS time_oscillators(
  node TEXT PRIMARY KEY, state TEXT, drift_us_per_s REAL,
  locked_bound_us INTEGER, holdover_budget_us INTEGER, holdover_from INTEGER,
  locked_lo INTEGER, locked_hi INTEGER, last_at INTEGER);
CREATE TABLE IF NOT EXISTS time_relations(
  rel_id TEXT PRIMARY KEY, node_a TEXT, node_b TEXT, valid_from INTEGER,
  lo_us INTEGER, hi_us INTEGER, drift_a REAL, drift_b REAL, status TEXT,
  evidence TEXT);
CREATE TABLE IF NOT EXISTS time_contracts(
  customer TEXT PRIMARY KEY, grade TEXT, high_limit_us INTEGER,
  low_limit_us INTEGER, status TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS time_elections(
  election_id TEXT PRIMARY KEY, customer TEXT, choice TEXT, reason TEXT,
  digest TEXT, at INTEGER, expires_at INTEGER, credit INTEGER,
  revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS time_proofs(
  proof_id TEXT PRIMARY KEY, txn TEXT, customer TEXT, node TEXT, grade TEXT,
  status TEXT, at INTEGER, lo_us INTEGER, hi_us INTEGER, bound_us INTEGER,
  frozen TEXT, queued_since INTEGER);
CREATE TABLE IF NOT EXISTS time_corrections(
  correction_id TEXT PRIMARY KEY, proof_id TEXT, reason TEXT, at INTEGER,
  transactions TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS time_failure_intervals(
  interval_id TEXT PRIMARY KEY, node TEXT, start_at INTEGER, end_at INTEGER,
  reason TEXT, status TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS time_migrations(
  migration_id TEXT PRIMARY KEY, customer TEXT, from_node TEXT, to_node TEXT,
  at INTEGER, rel_id TEXT, bound_us INTEGER);
CREATE TABLE IF NOT EXISTS time_reference_costs(
  cost_id TEXT PRIMARY KEY, source TEXT, amount INTEGER, at INTEGER,
  detail TEXT);
"""


def init(db):
    db.executescript(SCHEMA)


def _dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _loads(value, default):
    if value is None:
        return default
    return json.loads(value)


def _two_way(t1, t2, t3, t4):
    """NTP-style two-way exchange.

    ``forward = t2-t1`` and ``reverse = t4-t3`` are the one-way delays.  The
    offset estimate is ``(forward-reverse)/2`` and the round-trip delay is
    ``forward+reverse`` (equivalent to ``(t4-t1)-(t3-t2)``).
    """
    forward = t2 - t1
    reverse = t4 - t3
    rtt = forward + reverse
    offset = (forward - reverse) // 2
    return rtt, offset


def _group_key(source, path):
    # Sources sharing an authentication root can be spoofed together, and
    # measurements sharing a fault domain share the same asymmetry failure.
    # Either dependency collapses them into one independence group.
    chain = _loads(source["auth_chain"], [])
    root = chain[0] if chain else source["source_id"]
    shared = tuple(sorted(seg["id"] for seg in path if seg.get("shared")))
    if shared:
        domain = "shared:" + ":".join(shared)
    elif source.get("kind") == "satellite":
        # Every dish reads the same spoofable constellation signals.
        domain = "sat"
    else:
        # Independent terrestrial/leased links are distinguished by path.
        domain = "path:" + ":".join(sorted(seg["id"] for seg in path))
    return root + "|" + domain


def _oscillator_upsert(db, node, state, drift, budget, at):
    old = one(db, "SELECT * FROM time_oscillators WHERE node=?", (node,))
    if old:
        db.execute(
            "UPDATE time_oscillators SET state=?, drift_us_per_s=?, "
            "holdover_budget_us=?, last_at=? WHERE node=?",
            (state, drift, budget, at, node),
        )
    else:
        db.execute(
            "INSERT INTO time_oscillators VALUES(?,?,?,?,?,?,?,?,?)",
            (node, state, drift, 0, budget, None, None, None, at),
        )


def _node_interval(db, node, at):
    """Project the node's UTC offset interval to ``at``.

    Returns ``(lo_us, hi_us, bound_us)`` or ``None`` when holdover is exhausted
    and no defensible bound exists.  In holdover the locked interval grows by
    the oscillator drift on both sides; it can never shrink back.
    """
    osc = one(db, "SELECT * FROM time_oscillators WHERE node=?", (node,))
    if osc is None or osc["locked_lo"] is None:
        return None
    if osc["state"] == "locked":
        return osc["locked_lo"], osc["locked_hi"], osc["locked_bound_us"]
    elapsed = at - osc["holdover_from"]
    growth = osc["drift_us_per_s"] * elapsed
    bound = osc["locked_bound_us"] + growth
    if bound > osc["holdover_budget_us"]:
        return None
    return (
        osc["locked_lo"] - growth,
        osc["locked_hi"] + growth,
        bound,
    )


def _relation_at(db, rel, at):
    """Project a cross-room clock relation to ``at``; ``None`` past holdover."""
    nodes = (rel["node_a"], rel["node_b"])
    for node in nodes:
        if _node_interval(db, node, at) is None:
            return None
    growth = (rel["drift_a"] + rel["drift_b"]) * (at - rel["valid_from"])
    return rel["lo_us"] - growth, rel["hi_us"] + growth


def _find_relation(db, node_a, node_b):
    rel = one(
        db,
        "SELECT * FROM time_relations WHERE node_a=? AND node_b=? "
        "ORDER BY valid_from DESC LIMIT 1",
        (node_a, node_b),
    )
    if rel:
        return rel, 1
    rel = one(
        db,
        "SELECT * FROM time_relations WHERE node_a=? AND node_b=? "
        "ORDER BY valid_from DESC LIMIT 1",
        (node_b, node_a),
    )
    if rel:
        return rel, -1
    return None, 0


def _active_election(db, customer, at):
    election = one(
        db,
        "SELECT * FROM time_elections WHERE customer=? AND revoked=0 "
        "ORDER BY at DESC LIMIT 1",
        (customer,),
    )
    if not election:
        return None
    payload = _dumps(
        {
            "at": election["at"],
            "choice": election["choice"],
            "credit": election["credit"],
            "customer": election["customer"],
            "expires_at": election["expires_at"],
            "reason": election["reason"],
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != election["digest"]:
        return None
    if election["choice"] != "accept_low" or at > election["expires_at"]:
        return None
    return election


def _frozen_uses_source(frozen, source_id):
    return any(sample["source"] == source_id for sample in frozen["samples"])


def _issue_correction(db, correction_id, proof, reason, at, detail):
    db.execute(
        "INSERT INTO time_corrections VALUES(?,?,?,?,?,?)",
        (
            correction_id,
            proof["proof_id"],
            reason,
            at,
            _dumps([proof["txn"]]),
            _dumps(detail),
        ),
    )


def register_source(db, x):
    old = one(db, "SELECT source_id FROM time_sources WHERE source_id=?", (x["id"],))
    if old:
        return {"decision": "source_identity_conflict"}
    if x["kind"] not in ("satellite", "terrestrial", "leased", "oscillator"):
        return {"decision": "invalid_source_kind"}
    db.execute(
        "INSERT INTO time_sources VALUES(?,?,?,?,?,?,?)",
        (
            x["id"],
            x["kind"],
            _dumps(x["auth_chain"]),
            x["leap_table"],
            "trusted",
            0,
            _dumps(x.get("detail", {})),
        ),
    )
    return {"decision": "registered", "source": x["id"]}


def calibrate(db, x):
    source = one(db, "SELECT * FROM time_sources WHERE source_id=?", (x["source"],))
    if not source:
        return {"decision": "unknown_source"}
    path = x["path"]
    path_fault = 1 if any(seg.get("shared") for seg in path) else 0
    rtt, offset = _two_way(x["t1"], x["t2"], x["t3"], x["t4"])
    asym_validated = x.get("asym_validated", True) and not path_fault
    if asym_validated:
        base = min(rtt // 2, x["asym_bound_us"])
    else:
        # Without a validated asymmetry bound the full round-trip half-width is
        # the only defensible bound; a stable RTT cannot hide this uncertainty.
        base = rtt // 2
    bound = base + x.get("noise_us", 0)
    lo, hi = offset - bound, offset + bound
    osc = x["osc_state"]
    _oscillator_upsert(
        db,
        x["node"],
        osc.get("state", "locked"),
        osc["drift_us_per_s"],
        osc["holdover_budget_us"],
        x["received_at"] if x.get("late") else x["window_end"],
    )
    valid = 1
    reason = None
    if source["status"] == "condemned":
        valid, reason = 0, "condemned_source"
    elif source["leap_table"] != x["leap_table"]:
        valid, reason = 0, "leap_table_mismatch"
    db.execute(
        "UPDATE time_sources SET compromised=? WHERE source_id=?",
        (path_fault, x["source"]),
    )
    db.execute(
        "INSERT INTO time_calibrations(cal_id,node,source,window_start,"
        "window_end,t1,t2,t3,t4,rtt_us,offset_us,asym_bound_us,bound_us,"
        "lo_us,hi_us,path,path_fault,osc_state,leap_table,late,received_at,"
        "valid,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            x["id"], x["node"], x["source"], x["window_start"], x["window_end"],
            x["t1"], x["t2"], x["t3"], x["t4"], rtt, offset,
            x["asym_bound_us"], bound, lo, hi, _dumps(path), path_fault,
            _dumps(osc), x["leap_table"], 1 if x.get("late") else 0,
            x.get("received_at", x["window_end"]), valid, reason,
        ),
    )
    result = {
        "decision": "recorded" if valid else "rejected",
        "cal_id": x["id"],
        "offset_us": offset,
        "rtt_us": rtt,
        "bound_us": bound,
        "interval_us": [lo, hi],
        "path_fault": bool(path_fault),
    }
    if reason:
        result["reason"] = reason
    if x.get("late"):
        result["corrections"] = _late_contradictions(db, x, lo, hi)
    return result


def _late_contradictions(db, cal, lo, hi):
    """Late samples are forensic only.

    They never feed a new accuracy verdict for a sealed window, but if one
    contradicts an already issued proof's frozen interval, the proof gets a
    correction.  Failure intervals are reassessed, never erased.
    """
    corrections = []
    proofs = rows(
        db,
        "SELECT * FROM time_proofs WHERE node=? AND status='active'",
        (cal["node"],),
    )
    for proof in proofs:
        in_window = cal["window_start"] <= proof["at"] <= cal["window_end"]
        disjoint = hi < proof["lo_us"] or lo > proof["hi_us"]
        if in_window and disjoint:
            cid = "corr-late-" + proof["proof_id"]
            if not one(db, "SELECT 1 FROM time_corrections WHERE correction_id=?", (cid,)):
                _issue_correction(
                    db, cid, proof, "late_sample_contradiction",
                    cal["received_at"],
                    {"cal_id": cal["id"], "late_interval_us": [lo, hi]},
                )
                db.execute(
                    "UPDATE time_proofs SET status='corrected' WHERE proof_id=?",
                    (proof["proof_id"],),
                )
                corrections.append(cid)
    for interval in rows(
        db,
        "SELECT * FROM time_failure_intervals WHERE node=? AND status='open'",
        (cal["node"],),
    ):
        db.execute(
            "UPDATE time_failure_intervals SET status='reassessed' "
            "WHERE interval_id=?",
            (interval["interval_id"],),
        )
    return corrections


def adjudicate(db, x):
    cal_rows = rows(
        db,
        "SELECT c.*, s.status AS source_status, s.auth_chain, s.kind "
        "FROM time_calibrations c JOIN time_sources s ON s.source_id=c.source "
        "WHERE c.node=? AND c.valid=1 AND c.late=0 AND c.window_start>=? "
        "AND c.window_end<=? ORDER BY c.cal_id",
        (x["node"], x["window_start"], x["window_end"]),
    )
    cal_rows = [c for c in cal_rows if c["source_status"] == "trusted" and not c["path_fault"]]
    groups = {}
    for cal in cal_rows:
        source = {
            "source_id": cal["source"],
            "auth_chain": cal["auth_chain"],
            "kind": cal["kind"],
        }
        key = _group_key(source, _loads(cal["path"], []))
        bucket = groups.setdefault(key, {"lo": cal["lo_us"], "hi": cal["hi_us"], "cals": []})
        bucket["lo"] = min(bucket["lo"], cal["lo_us"])
        bucket["hi"] = max(bucket["hi"], cal["hi_us"])
        bucket["cals"].append(cal["cal_id"])

    evidence = [cal["cal_id"] for cal in cal_rows]
    suspect = []
    if not cal_rows:
        decision, lo, hi, bound = "unavailable", None, None, None
    elif len(groups) < 2:
        # A cluster of perfectly self-consistent samples is one claim, not
        # corroboration: a spoofed constellation shifts every dependent source
        # together.
        only = next(iter(groups.values()))
        decision, lo, hi = "uncorroborated", only["lo"], only["hi"]
        bound = max(abs(lo), abs(hi))
        suspect = sorted(groups)
    else:
        lo = max(g["lo"] for g in groups.values())
        hi = min(g["hi"] for g in groups.values())
        if lo > hi:
            decision = "inconsistent"
            lo = min(g["lo"] for g in groups.values())
            hi = max(g["hi"] for g in groups.values())
            projected = _node_interval(db, x["node"], x["at"])
            reference = projected[:2] if projected else None
            if reference is None:
                # No prior anchored interval exists to arbitrate: every group
                # could be the liar, so none is trusted until new evidence.
                widest = max(groups.values(), key=lambda g: g["hi"] - g["lo"])
                reference = (widest["lo"], widest["hi"])
                suspect = sorted(groups)
            else:
                for key, g in groups.items():
                    if g["hi"] < reference[0] or g["lo"] > reference[1]:
                        suspect.append(key)
        else:
            decision = "intersecting"
        bound = max(abs(lo), abs(hi))
        if decision == "intersecting":
            if bound > x["high_limit_us"]:
                decision = "degraded"
            else:
                # Intersecting, independent evidence inside the high limit is
                # accuracy by definition; it also relocks a recovered
                # oscillator after a holdover episode.
                decision = "accurate"
                db.execute(
                    "UPDATE time_oscillators SET state='locked', "
                    "locked_lo=?, locked_hi=?, locked_bound_us=?, "
                    "holdover_from=NULL, last_at=? WHERE node=?",
                    (lo, hi, bound, x["at"], x["node"]),
                )

    db.execute(
        "INSERT INTO time_verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            x["id"], x["node"], x["window_start"], x["window_end"], decision,
            lo, hi, bound, _dumps(evidence), _dumps(groups), _dumps(suspect),
            x["at"],
        ),
    )
    return {
        "decision": decision,
        "verdict_id": x["id"],
        "interval_us": [lo, hi] if lo is not None else None,
        "bound_us": bound,
        "evidence": evidence,
        "groups": sorted(groups),
        "suspect": suspect,
    }


def update_oscillator(db, x):
    """Record oscillator aging: a higher drift rate shortens holdover life."""
    _oscillator_upsert(
        db, x["node"], x.get("state", "holdover"), x["drift_us_per_s"],
        x["holdover_budget_us"], x["at"],
    )
    return {"decision": "recorded", "node": x["node"]}


def link_break(db, x):
    old = one(db, "SELECT * FROM time_oscillators WHERE node=?", (x["node"],))
    if not old:
        return {"decision": "unknown_oscillator"}
    db.execute(
        "UPDATE time_oscillators SET state='holdover', holdover_from=?, "
        "last_at=? WHERE node=?",
        (x["at"], x["at"], x["node"]),
    )
    db.execute(
        "INSERT INTO time_failure_intervals VALUES(?,?,?,?,?,?,?)",
        (x["id"], x["node"], x["at"], None, "link_break", "open", _dumps({})),
    )
    db.execute(
        "UPDATE time_relations SET status='broken' WHERE node_a=? OR node_b=?",
        (x["node"], x["node"]),
    )
    return {"decision": "holdover", "node": x["node"], "failure_interval": x["id"]}


def restore_link(db, x):
    result = db.execute(
        "UPDATE time_failure_intervals SET end_at=?, status='closed' "
        "WHERE node=? AND status='open'",
        (x["at"], x["node"]),
    )
    if not result.rowcount:
        return {"decision": "no_open_failure_interval"}
    return {"decision": "restored", "node": x["node"]}


def build_relation(db, x):
    va = one(db, "SELECT * FROM time_verdicts WHERE verdict_id=?", (x["verdict_a"],))
    vb = one(db, "SELECT * FROM time_verdicts WHERE verdict_id=?", (x["verdict_b"],))
    if not va or not vb or va["lo_us"] is None or vb["lo_us"] is None:
        return {"decision": "verdict_unavailable"}
    oa = one(db, "SELECT * FROM time_oscillators WHERE node=?", (va["node"],))
    ob = one(db, "SELECT * FROM time_oscillators WHERE node=?", (vb["node"],))
    # Difference (B - A) interval: combine both node offset intervals.
    lo = vb["lo_us"] - va["hi_us"]
    hi = vb["hi_us"] - va["lo_us"]
    db.execute(
        "INSERT INTO time_relations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            x["id"], va["node"], vb["node"], x["at"], lo, hi,
            oa["drift_us_per_s"] if oa else 0,
            ob["drift_us_per_s"] if ob else 0,
            "linked",
            _dumps({"verdict_a": x["verdict_a"], "verdict_b": x["verdict_b"]}),
        ),
    )
    return {
        "decision": "linked",
        "rel_id": x["id"],
        "difference_us": [lo, hi],
    }


def order_events(db, x):
    """Return ``before``/``after`` only when uncertainty cannot flip the order.

    Overlapping UTC intervals mean the order is not provable; the transaction
    pair is reported ``unsortable`` and never receives a forced precise time.
    """
    a_node, a_ts = x["event_a"]["node"], x["event_a"]["ts"]
    b_node, b_ts = x["event_b"]["node"], x["event_b"]["ts"]
    if a_node == b_node:
        interval = _node_interval(db, a_node, x["at"])
        if interval is None:
            return {"decision": "unsortable", "reason": "holdover_exhausted"}
        lo, hi, _ = interval
        a_utc = [a_ts - hi, a_ts - lo]
        b_utc = [b_ts - hi, b_ts - lo]
    else:
        rel, orientation = _find_relation(db, a_node, b_node)
        if not rel:
            return {"decision": "unsortable", "reason": "no_clock_relation"}
        diff = _relation_at(db, rel, x["at"])
        if diff is None:
            return {"decision": "unsortable", "reason": "holdover_exhausted"}
        d_lo, d_hi = (
            (diff[0], diff[1]) if orientation == 1 else (-diff[1], -diff[0])
        )
        # Express event B in node A's clock frame using the (B-A) relation.
        b_in_a = [b_ts - d_hi, b_ts - d_lo]
        a_utc, b_utc = [a_ts, a_ts], b_in_a
    if a_utc[1] < b_utc[0]:
        decision = "before"
    elif a_utc[0] > b_utc[1]:
        decision = "after"
    else:
        decision = "unsortable"
    return {
        "decision": decision,
        "event_a_utc_us": a_utc,
        "event_b_utc_us": b_utc,
    }


def register_contract(db, x):
    db.execute(
        "INSERT INTO time_contracts VALUES(?,?,?,?,?,?)",
        (
            x["customer"], x["grade"], x["high_limit_us"], x["low_limit_us"],
            "active", x["at"],
        ),
    )
    return {"decision": "registered", "customer": x["customer"], "grade": x["grade"]}


def record_election(db, x):
    """A downgrade needs the customer's verifiable choice, not a silent switch."""
    payload = _dumps(
        {
            "at": x["at"],
            "choice": x["choice"],
            "credit": x["credit"],
            "customer": x["customer"],
            "expires_at": x["expires_at"],
            "reason": x["reason"],
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO time_elections VALUES(?,?,?,?,?,?,?,?,?)",
        (
            x["id"], x["customer"], x["choice"], x["reason"], digest, x["at"],
            x["expires_at"], x["credit"], 0,
        ),
    )
    return {"decision": "recorded", "election_id": x["id"], "digest": digest}


def verify_election(db, x):
    election = one(
        db,
        "SELECT * FROM time_elections WHERE customer=? ORDER BY at DESC LIMIT 1",
        (x["customer"],),
    )
    if not election:
        return {"decision": "invalid", "reason": "no_election"}
    payload = _dumps(
        {
            "at": election["at"],
            "choice": election["choice"],
            "credit": election["credit"],
            "customer": election["customer"],
            "expires_at": election["expires_at"],
            "reason": election["reason"],
        }
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != election["digest"] or election["revoked"]:
        return {"decision": "invalid", "reason": "digest_or_revocation"}
    if x["at"] > election["expires_at"]:
        return {"decision": "invalid", "reason": "expired"}
    return {"decision": "valid", "election_id": election["election_id"]}


def _freeze_evidence(db, node, verdict_id, at):
    if verdict_id is None:
        verdict = None
    else:
        verdict = one(db, "SELECT * FROM time_verdicts WHERE verdict_id=?", (verdict_id,))
    sample_ids = _loads(verdict["evidence"], []) if verdict else []
    samples = []
    for cal_id in sample_ids:
        cal = one(db, "SELECT * FROM time_calibrations WHERE cal_id=?", (cal_id,))
        source = one(db, "SELECT * FROM time_sources WHERE source_id=?", (cal["source"],))
        samples.append(
            {
                "cal_id": cal["cal_id"],
                "source": cal["source"],
                "auth_chain": _loads(source["auth_chain"], []),
                "leap_table": cal["leap_table"],
                "path": _loads(cal["path"], []),
                "window": [cal["window_start"], cal["window_end"]],
                "t1": cal["t1"], "t2": cal["t2"], "t3": cal["t3"], "t4": cal["t4"],
                "rtt_us": cal["rtt_us"],
                "offset_us": cal["offset_us"],
                "asym_bound_us": cal["asym_bound_us"],
                "bound_us": cal["bound_us"],
                "osc_state": _loads(cal["osc_state"], {}),
            }
        )
    relations = rows(
        db,
        "SELECT * FROM time_relations WHERE node_a=? OR node_b=?",
        (node, node),
    )
    return {
        "verdict_id": verdict_id,
        "frozen_at": at,
        "samples": samples,
        "relations": [
            {
                "rel_id": r["rel_id"],
                "node_a": r["node_a"],
                "node_b": r["node_b"],
                "difference_us": [r["lo_us"], r["hi_us"]],
                "status": r["status"],
                "evidence": _loads(r["evidence"], {}),
            }
            for r in relations
        ],
    }


def _latest_verdict(db, node):
    return one(
        db,
        "SELECT * FROM time_verdicts WHERE node=? AND lo_us IS NOT NULL "
        "ORDER BY at DESC LIMIT 1",
        (node,),
    )


def issue_proof(db, x):
    contract = one(
        db,
        "SELECT * FROM time_contracts WHERE customer=? AND status='active'",
        (x["customer"],),
    )
    if not contract:
        return {"decision": "unknown_contract"}
    node, at = x["node"], x["at"]
    interval = _node_interval(db, node, at)
    if interval is None:
        return {"decision": "unavailable", "reason": "holdover_exhausted"}
    lo, hi, projected_bound = interval
    existing = one(
        db,
        "SELECT * FROM time_proofs WHERE txn=? AND customer=?",
        (x["txn"], x["customer"]),
    )
    queued_since = existing["queued_since"] if existing else at

    high_ready = False
    verdict = _latest_verdict(db, node)
    osc = one(db, "SELECT * FROM time_oscillators WHERE node=?", (node,))
    if (
        verdict and verdict["decision"] == "accurate"
        and osc and osc["state"] == "locked"
        and verdict["window_start"] <= at <= verdict["window_end"] + 1
        and verdict["bound_us"] <= contract["high_limit_us"]
    ):
        high_ready = True

    want_high = x.get("grade", contract["grade"]) == "high" or contract["grade"] == "high"
    if want_high and high_ready:
        grade, status = "high", "active"
    elif want_high and not high_ready:
        election = _active_election(db, x["customer"], at)
        low_ok = projected_bound <= contract["low_limit_us"]
        if election and low_ok:
            grade, status = "low", "downgraded"
        else:
            # High precision is sealed: the request is queued rather than
            # silently stamped with a weaker bound.
            if existing:
                db.execute(
                    "UPDATE time_proofs SET status='queued' WHERE proof_id=?",
                    (existing["proof_id"],),
                )
                proof_id = existing["proof_id"]
            else:
                proof_id = x["proof_id"]
                db.execute(
                    "INSERT INTO time_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proof_id, x["txn"], x["customer"], node, "high",
                        "queued", at, lo, hi, projected_bound, None, queued_since,
                    ),
                )
            return {
                "decision": "high_precision_sealed",
                "proof_id": proof_id,
                "queued_since": queued_since,
                "bound_us": projected_bound,
            }
    else:
        if projected_bound > contract["low_limit_us"]:
            return {
                "decision": "unavailable",
                "reason": "low_limit_exceeded",
                "bound_us": projected_bound,
            }
        grade, status = "low", "active"

    frozen = _freeze_evidence(db, node, verdict["verdict_id"] if verdict else None, at)
    proof_id = existing["proof_id"] if existing else x["proof_id"]
    if existing:
        db.execute(
            "UPDATE time_proofs SET grade=?, status=?, at=?, lo_us=?, hi_us=?, "
            "bound_us=?, frozen=? WHERE proof_id=?",
            (grade, status, at, lo, hi, projected_bound, _dumps(frozen), proof_id),
        )
    else:
        db.execute(
            "INSERT INTO time_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proof_id, x["txn"], x["customer"], node, grade, status, at,
                lo, hi, projected_bound, _dumps(frozen), queued_since,
            ),
        )
    result = {
        "decision": "issued" if status == "active" else "downgraded",
        "proof_id": proof_id,
        "grade": grade,
        "bound_us": projected_bound,
        "interval_us": [lo, hi],
        "issued_at": at,
    }
    if status == "downgraded":
        election = _active_election(db, x["customer"], at)
        db.execute(
            "INSERT INTO time_reference_costs VALUES(?,?,?,?,?)",
            (
                "comp-" + proof_id, x["customer"], -election["credit"], at,
                _dumps({"type": "downgrade_compensation", "election": election["election_id"]}),
            ),
        )
        result["compensation_credit"] = election["credit"]
        result["election_id"] = election["election_id"]
    if at > queued_since:
        result["delay"] = at - queued_since
    return result


def condemn_source(db, x):
    source = one(db, "SELECT * FROM time_sources WHERE source_id=?", (x["source"],))
    if not source:
        return {"decision": "unknown_source"}
    db.execute(
        "UPDATE time_sources SET status='condemned' WHERE source_id=?",
        (x["source"],),
    )
    corrections, transactions = [], []
    proofs = rows(
        db,
        "SELECT * FROM time_proofs WHERE status IN ('active','downgraded')"
    )
    for proof in proofs:
        frozen = _loads(proof["frozen"], {"samples": []})
        if not _frozen_uses_source(frozen, x["source"]):
            continue
        cid = "corr-" + proof["proof_id"] + "-" + x["reason"]
        if one(db, "SELECT 1 FROM time_corrections WHERE correction_id=?", (cid,)):
            continue
        _issue_correction(
            db, cid, proof, x["reason"], x["at"],
            {"condemned_source": x["source"], "frozen_bound_us": proof["bound_us"]},
        )
        db.execute(
            "UPDATE time_proofs SET status='corrected' WHERE proof_id=?",
            (proof["proof_id"],),
        )
        corrections.append(cid)
        transactions.append(proof["txn"])
    # Every window the condemned source measured becomes a traceable tainted
    # interval so customers know the reach of the failure.
    windows = rows(
        db,
        "SELECT node, MIN(window_start) AS s, MAX(window_end) AS e "
        "FROM time_calibrations WHERE source=? GROUP BY node",
        (x["source"],),
    )
    interval_id = None
    if windows:
        node = windows[0]["node"]
        interval_id = "taint-" + x["source"]
        db.execute(
            "INSERT OR REPLACE INTO time_failure_intervals VALUES(?,?,?,?,?,?,?)",
            (
                interval_id, node,
                min(w["s"] for w in windows), max(w["e"] for w in windows),
                "source_condemned:" + x["reason"], "tainted",
                _dumps({"source": x["source"], "proofs": corrections}),
            ),
        )
    return {
        "decision": "condemned",
        "source": x["source"],
        "corrections": corrections,
        "transactions_for_rereview": transactions,
        "failure_interval": interval_id,
    }


def withdraw_proof(db, x):
    proof = one(db, "SELECT * FROM time_proofs WHERE proof_id=?", (x["proof_id"],))
    if not proof:
        return {"decision": "unknown_proof"}
    if proof["status"] in ("withdrawn", "corrected"):
        return {"decision": "already_" + proof["status"]}
    _issue_correction(
        db, "corr-withdraw-" + proof["proof_id"], proof,
        x.get("reason", "withdrawn"), x["at"],
        {"frozen_bound_us": proof["bound_us"]},
    )
    db.execute(
        "UPDATE time_proofs SET status='withdrawn' WHERE proof_id=?",
        (x["proof_id"],),
    )
    return {
        "decision": "withdrawn",
        "proof_id": x["proof_id"],
        "transactions_for_rereview": [proof["txn"]],
    }


def migrate_customer(db, x):
    rel, orientation = _find_relation(db, x["from_node"], x["to_node"])
    if not rel:
        return {"decision": "unsortable", "reason": "no_clock_relation"}
    diff = _relation_at(db, rel, x["at"])
    if diff is None:
        return {"decision": "unsortable", "reason": "holdover_exhausted"}
    bound = max(abs(diff[0]), abs(diff[1]))
    db.execute(
        "INSERT INTO time_migrations VALUES(?,?,?,?,?,?,?)",
        (
            x["id"], x["customer"], x["from_node"], x["to_node"], x["at"],
            rel["rel_id"], bound,
        ),
    )
    return {
        "decision": "migrated",
        "migration_id": x["id"],
        "relation_bound_us": bound,
    }


def record_cost(db, x):
    db.execute(
        "INSERT INTO time_reference_costs VALUES(?,?,?,?,?)",
        (x["id"], x["source"], x["amount"], x["at"], _dumps(x.get("detail", {}))),
    )
    return {"decision": "recorded", "cost_id": x["id"], "amount": x["amount"]}


def run(db, x):
    init(db)
    op = x["op"]
    if op == "register_source":
        return register_source(db, x)
    if op == "calibrate":
        return calibrate(db, x)
    if op == "adjudicate":
        return adjudicate(db, x)
    if op == "update_oscillator":
        return update_oscillator(db, x)
    if op == "link_break":
        return link_break(db, x)
    if op == "restore_link":
        return restore_link(db, x)
    if op == "build_relation":
        return build_relation(db, x)
    if op == "order_events":
        return order_events(db, x)
    if op == "register_contract":
        return register_contract(db, x)
    if op == "record_election":
        return record_election(db, x)
    if op == "verify_election":
        return verify_election(db, x)
    if op == "issue_proof":
        return issue_proof(db, x)
    if op == "condemn_source":
        return condemn_source(db, x)
    if op == "withdraw_proof":
        return withdraw_proof(db, x)
    if op == "migrate_customer":
        return migrate_customer(db, x)
    if op == "record_cost":
        return record_cost(db, x)
    raise ValueError(op)
