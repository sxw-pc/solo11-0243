"""Formal wireless backhaul coordination.

The provisional note in :mod:`netcourt.channel_note` only ranks single-site
noise samples.  A channel that is usable for the customer *and* compliant with
the neighbouring protected station needs the licensed power/region, both
ends of the link, antenna direction, occupied time windows, the protected
object's own reception records, real bidirectional acceptance, neighbour
retests and the coordinator receipt together.

Every fact (complaint, field correction, independent retest, receipt,
discount and its correction) is kept on its own row; judgments are appended
to ``radio_decisions`` and never overwrite the underlying evidence.
"""

from .store import one, rows

LOCAL_NOISE_LIMIT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_cases(id TEXT PRIMARY KEY,tenant TEXT,site_a TEXT,site_b TEXT,status TEXT,opened_at INTEGER,restored_at INTEGER);
CREATE TABLE IF NOT EXISTS radio_licenses(id TEXT PRIMARY KEY,case_id TEXT,channel TEXT,region TEXT,max_power INTEGER,width INTEGER);
CREATE TABLE IF NOT EXISTS radio_contracts(case_id TEXT PRIMARY KEY,tenant TEXT,throughput INTEGER,latency INTEGER,available_from INTEGER,available_to INTEGER);
CREATE TABLE IF NOT EXISTS radio_protected_objects(id TEXT PRIMARY KEY,case_id TEXT,channel TEXT,region TEXT,noise_limit INTEGER);
CREATE TABLE IF NOT EXISTS radio_protected_windows(object_id TEXT,start_at INTEGER,end_at INTEGER,period INTEGER,PRIMARY KEY(object_id,start_at));
CREATE TABLE IF NOT EXISTS radio_scanners(id TEXT PRIMARY KEY,site TEXT,calibrated_at INTEGER,revoked INTEGER);
CREATE TABLE IF NOT EXISTS radio_antenna_configs(id TEXT PRIMARY KEY,site TEXT,azimuth INTEGER,backlobe_region TEXT,changed_at INTEGER,superseded INTEGER);
CREATE TABLE IF NOT EXISTS radio_antenna_surveys(id TEXT PRIMARY KEY,site TEXT,reported_azimuth INTEGER,measured_azimuth INTEGER,at INTEGER,matches INTEGER);
CREATE TABLE IF NOT EXISTS radio_scans(id TEXT PRIMARY KEY,site TEXT,channel TEXT,noise INTEGER,measured_at INTEGER,scanner_id TEXT,antenna_id TEXT,power INTEGER);
CREATE TABLE IF NOT EXISTS radio_receptions(id TEXT PRIMARY KEY,object_id TEXT,channel TEXT,power INTEGER,at INTEGER);
CREATE TABLE IF NOT EXISTS radio_complaints(id TEXT PRIMARY KEY,case_id TEXT,at INTEGER,status TEXT,kind TEXT);
CREATE TABLE IF NOT EXISTS radio_acceptance_tests(id TEXT PRIMARY KEY,case_id TEXT,direction TEXT,throughput INTEGER,latency INTEGER,evidence TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS radio_constructions(id TEXT PRIMARY KEY,case_id TEXT,action TEXT,at INTEGER,cost INTEGER,new_antenna TEXT);
CREATE TABLE IF NOT EXISTS radio_retests(id TEXT PRIMARY KEY,case_id TEXT,kind TEXT,at INTEGER,arrived_at INTEGER,passed INTEGER,evidence TEXT);
CREATE TABLE IF NOT EXISTS radio_receipts(id TEXT PRIMARY KEY,case_id TEXT,kind TEXT,channel TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS radio_discounts(id TEXT PRIMARY KEY,case_id TEXT,amount INTEGER,kind TEXT,from_at INTEGER,to_at INTEGER,guaranteed INTEGER,corrected_by TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS radio_notifications(id TEXT PRIMARY KEY,case_id TEXT,at INTEGER,reason TEXT);
CREATE TABLE IF NOT EXISTS radio_decisions(id TEXT PRIMARY KEY,case_id TEXT,channel TEXT,decision TEXT,at INTEGER,reasons TEXT);
"""


def init(db):
    db.executescript(SCHEMA)


def _window_covers(w, at):
    if at < w["start_at"]:
        return False
    if w["period"] and w["period"] > 0:
        return (at - w["start_at"]) % w["period"] <= (w["end_at"] - w["start_at"])
    return at <= w["end_at"]


def _current_antenna(db, site):
    return one(
        db,
        "SELECT * FROM radio_antenna_configs WHERE site=? AND superseded=0 ORDER BY changed_at DESC,id DESC LIMIT 1",
        (site,),
    )


def _notify(db, case_id, at, reason):
    seq = one(db, "SELECT COUNT(*) AS c FROM radio_notifications WHERE case_id=?", (case_id,))["c"] + 1
    db.execute(
        "INSERT INTO radio_notifications VALUES(?,?,?,?)",
        (f"{case_id}-n{seq}", case_id, at, reason),
    )


def _record_antenna(db, x):
    old = _current_antenna(db, x["site"])
    if old:
        db.execute("UPDATE radio_antenna_configs SET superseded=1 WHERE id=?", (old["id"],))
    db.execute(
        "INSERT INTO radio_antenna_configs VALUES(?,?,?,?,?,0)",
        (x["id"], x["site"], x["azimuth"], x.get("backlobe_region"), x["at"]),
    )
    return {"decision": "recorded", "superseded": old["id"] if old else None}


def _scan_validity(db, scan):
    """A scan only speaks for the current state: revoked calibration or an
    antenna adjustment after the measurement invalidates it."""
    scanner = one(db, "SELECT * FROM radio_scanners WHERE id=?", (scan["scanner_id"],))
    if not scanner or scanner["revoked"] or scanner["calibrated_at"] > scan["measured_at"]:
        return "scanner_calibration_revoked"
    antenna = _current_antenna(db, scan["site"])
    if not antenna or antenna["id"] != scan["antenna_id"]:
        return "stale_measurement_after_antenna_change"
    return None


def _acceptance_reasons(db, case, at):
    tests = rows(db, "SELECT * FROM radio_acceptance_tests WHERE case_id=?", (case["id"],))
    if not tests:
        return ["missing_bidirectional_acceptance"], None
    reasons = []
    if any(t["evidence"] in ("beacon", "registration") for t in tests):
        reasons.append("beacon_or_registration_not_acceptance")
    valid = [t for t in tests if t["evidence"] == "bidirectional"]
    if not valid:
        reasons.append("missing_bidirectional_acceptance")
    else:
        directions = {t["direction"] for t in valid}
        if not (("both" in directions) or {"forward", "reverse"}.issubset(directions)):
            reasons.append("one_way_acceptance_only")
    contract = one(db, "SELECT * FROM radio_contracts WHERE case_id=?", (case["id"],))
    guarantee = one(db, "SELECT * FROM radio_discounts WHERE case_id=? AND kind='capacity' "
        "AND (to_at IS NULL OR (from_at<=? AND to_at>=?)) ORDER BY rowid DESC LIMIT 1",
        (case["id"], at, at),
    )
    required = guarantee["guaranteed"] if guarantee else contract["throughput"]
    if any(t["throughput"] < required for t in valid):
        reasons.append("sla_throughput_unmet")
    if any(t["latency"] > contract["latency"] for t in valid):
        reasons.append("sla_latency_unmet")
    if any(not (contract["available_from"] <= t["at"] <= contract["available_to"]) for t in valid):
        reasons.append("sla_window_not_covered")
    return reasons, required


def assess(db, x):
    """Formal channel judgment. All six evidence families must agree."""
    init(db)
    case = one(db, "SELECT * FROM radio_cases WHERE id=?", (x["case_id"],))
    if not case:
        return {"decision": "unknown_case"}
    at = x["at"]
    channel = x["channel"]
    reasons = []

    license_row = one(db, "SELECT * FROM radio_licenses WHERE case_id=? AND channel=? AND region=?",
        (case["id"], channel, x["region"]),
    )
    if not license_row:
        reasons.append("unlicensed_channel_or_region")

    objects = rows(db, "SELECT * FROM radio_protected_objects WHERE case_id=? AND channel=?",
        (case["id"], channel),
    )

    # Both ends must hold a fresh measurement for this channel.
    for site in (case["site_a"], case["site_b"]):
        scans = rows(db, "SELECT * FROM radio_scans WHERE site=? AND channel=? ORDER BY measured_at DESC",
            (site, channel),
        )
        if not scans:
            reasons.append("missing_end_measurement")
            continue
        latest = scans[0]
        invalid = _scan_validity(db, latest)
        if invalid:
            reasons.append(invalid)
        if license_row and latest["power"] > license_row["max_power"]:
            reasons.append("power_over_license")
        if latest["noise"] > LOCAL_NOISE_LIMIT:
            reasons.append("local_noise_too_high")

    # Protected station's periodic emissions occupy time windows, not bands.
    # Only its latest reception record speaks for the current state; older
    # over-limit records stay on file as the history of the interference.
    for obj in objects:
        windows = rows(db, "SELECT * FROM radio_protected_windows WHERE object_id=?", (obj["id"],))
        for w in windows:
            if _window_covers(w, at):
                reasons.append("protected_periodic_emission")
        latest_rec = one(db, "SELECT * FROM radio_receptions WHERE object_id=? AND channel=? ORDER BY at DESC,id DESC LIMIT 1",
            (obj["id"], channel),
        )
        if latest_rec and any(_window_covers(w, latest_rec["at"]) for w in windows):
            if latest_rec["power"] > obj["noise_limit"]:
                reasons.append("protected_reception_over_limit")

    # Antenna direction: an unverified azimuth is a field misreport; a
    # verified backlobe falling on the protected area is real interference.
    for site in (case["site_a"], case["site_b"]):
        antenna = _current_antenna(db, site)
        if not antenna:
            reasons.append("antenna_azimuth_unverified")
            continue
        hit = [o for o in objects if o["region"] == antenna["backlobe_region"]]
        if not hit:
            continue
        survey = one(db, "SELECT * FROM radio_antenna_surveys WHERE site=? AND at>=? ORDER BY at DESC,id DESC LIMIT 1",
            (site, antenna["changed_at"]),
        )
        if not survey or not survey["matches"]:
            reasons.append("antenna_azimuth_unverified")
        else:
            reasons.append("antenna_backlobe_into_protected_area")

    if one(db, "SELECT id FROM radio_complaints WHERE case_id=? AND status='open' LIMIT 1",
        (case["id"],),
    ):
        reasons.append("open_neighbor_complaint")

    acceptance, _ = _acceptance_reasons(db, case, at)
    reasons.extend(acceptance)

    deduped = []
    for r in reasons:
        if r not in deduped:
            deduped.append(r)
    decision = "usable" if not deduped else "rejected"
    db.execute(
        "INSERT INTO radio_decisions VALUES(?,?,?,?,?,?)",
        (x["id"], case["id"], channel, decision, at, ",".join(deduped)),
    )
    return {"decision": decision, "channel": channel, "reasons": deduped, "decision_id": x["id"]}


def _retest(db, x):
    case = one(db, "SELECT * FROM radio_cases WHERE id=?", (x["case_id"],))
    if not case:
        return {"decision": "unknown_case"}
    arrived = x.get("arrived_at", x["at"])
    db.execute(
        "INSERT INTO radio_retests VALUES(?,?,?,?,?,?,?)",
        (x["id"], case["id"], x["kind"], x["at"], arrived, int(x["passed"]), x.get("evidence")),
    )
    if x["kind"] == "neighbor" and x["passed"]:
        db.execute(
            "UPDATE radio_complaints SET status='closed' WHERE case_id=? AND status='open'",
            (case["id"],),
        )
    elif x["kind"] == "independent" and x["passed"]:
        # Independent evidence can also close a complaint whose field
        # correction has already been verified without a neighbor retest.
        db.execute(
            "UPDATE radio_complaints SET status='closed' WHERE case_id=? AND status='open'",
            (case["id"],),
        )
    # Late, valid evidence after restoration still has to be communicated;
    # a late failure reopens the case and compensates the impaired window.
    if case["restored_at"] is not None and arrived > case["restored_at"]:
        if not x["passed"]:
            db.execute("UPDATE radio_cases SET status='reopened' WHERE id=?", (case["id"],))
            db.execute(
                "INSERT INTO radio_discounts VALUES(?,?,?,?,?,?,?,?,?)",
                (f"{case['id']}-comp-{x['id']}", case["id"], arrived - case["restored_at"],
                 "compensation", case["restored_at"], arrived, None, None, "late_failed_retest"),
            )
            _notify(db, case["id"], arrived, "late_failed_retest")
        else:
            _notify(db, case["id"], arrived, "late_retest_evidence")
    return {"decision": "recorded", "arrived_at": arrived}


def _receipt(db, x):
    case = one(db, "SELECT * FROM radio_cases WHERE id=?", (x["case_id"],))
    if not case:
        return {"decision": "unknown_case"}
    latest = one(db, "SELECT * FROM radio_decisions WHERE case_id=? AND channel=? ORDER BY at DESC,id DESC LIMIT 1",
        (case["id"], x["channel"]),
    )
    if not latest or latest["decision"] != "usable":
        return {"decision": "no_usable_decision"}
    if one(db, "SELECT id FROM radio_complaints WHERE case_id=? AND status='open' LIMIT 1",
        (case["id"],),
    ):
        return {"decision": "open_neighbor_complaint"}
    if not one(db, "SELECT id FROM radio_retests WHERE case_id=? AND kind='independent' AND passed=1 AND arrived_at<=?",
        (case["id"], x["at"]),
    ):
        return {"decision": "missing_independent_retest"}
    if not one(db, "SELECT id FROM radio_retests WHERE case_id=? AND kind='neighbor' AND passed=1 AND arrived_at<=?",
        (case["id"], x["at"]),
    ):
        return {"decision": "neighbor_still_interfered"}
    if x["kind"] == "retuned":
        if not one(db, "SELECT id FROM radio_constructions WHERE case_id=? AND action='retune'", (case["id"],)):
            return {"decision": "missing_retune_record"}
        if not one(db, "SELECT id FROM radio_discounts WHERE case_id=? AND kind='capacity'", (case["id"],)):
            return {"decision": "missing_capacity_discount"}
        status = "serving_retuned"
    elif x["kind"] == "remediated":
        if not one(db, "SELECT id FROM radio_constructions WHERE case_id=? AND action IN ('antenna','shield')",
            (case["id"],),
        ):
            return {"decision": "missing_construction_record"}
        status = "serving_full"
    else:
        return {"decision": "invalid_receipt_kind"}
    db.execute(
        "INSERT INTO radio_receipts VALUES(?,?,?,?,?)",
        (x["id"], case["id"], x["kind"], x["channel"], x["at"]),
    )
    db.execute(
        "UPDATE radio_cases SET status=?,restored_at=? WHERE id=?",
        (status, x["at"], case["id"]),
    )
    _notify(db, case["id"], x["at"], f"coordinator_receipt_{x['kind']}")
    return {"decision": "receipt_issued", "status": status, "channel": x["channel"]}


def run(db, x):
    init(db)
    op = x["op"]
    if op == "open_case":
        db.execute(
            "INSERT INTO radio_cases VALUES(?,?,?,?,?,?,?)",
            (x["id"], x["tenant"], x["site_a"], x["site_b"], "open", x["at"], None),
        )
        return {"decision": "opened"}
    if op == "license":
        db.execute(
            "INSERT INTO radio_licenses VALUES(?,?,?,?,?,?)",
            (x["id"], x["case_id"], x["channel"], x["region"], x["max_power"], x.get("width")),
        )
        return {"decision": "recorded"}
    if op == "contract":
        db.execute(
            "INSERT INTO radio_contracts VALUES(?,?,?,?,?,?)",
            (x["case_id"], x["tenant"], x["throughput"], x["latency"], x["available_from"], x["available_to"]),
        )
        return {"decision": "recorded"}
    if op == "protected_object":
        db.execute(
            "INSERT INTO radio_protected_objects VALUES(?,?,?,?,?)",
            (x["id"], x["case_id"], x["channel"], x["region"], x["noise_limit"]),
        )
        for w in x.get("windows", []):
            db.execute(
                "INSERT INTO radio_protected_windows VALUES(?,?,?,?)",
                (x["id"], w["start_at"], w["end_at"], w.get("period", 0)),
            )
        return {"decision": "recorded"}
    if op == "scanner_calibrate":
        db.execute(
            "INSERT INTO radio_scanners VALUES(?,?,?,0) ON CONFLICT(id) DO UPDATE SET calibrated_at=excluded.calibrated_at,revoked=0",
            (x["id"], x["site"], x["at"]),
        )
        return {"decision": "recorded"}
    if op == "scanner_revoke":
        db.execute("UPDATE radio_scanners SET revoked=1 WHERE id=?", (x["id"],))
        return {"decision": "recorded"}
    if op == "antenna_config":
        return _record_antenna(db, x)
    if op == "antenna_survey":
        matches = int(x["reported_azimuth"] == x["measured_azimuth"])
        db.execute(
            "INSERT INTO radio_antenna_surveys VALUES(?,?,?,?,?,?)",
            (x["id"], x["site"], x["reported_azimuth"], x["measured_azimuth"], x["at"], matches),
        )
        return {"decision": "recorded", "matches": bool(matches)}
    if op == "scan":
        db.execute(
            "INSERT INTO radio_scans VALUES(?,?,?,?,?,?,?,?)",
            (x["id"], x["site"], x["channel"], x["noise"], x["at"], x["scanner_id"], x["antenna_id"], x["power"]),
        )
        return {"decision": "recorded"}
    if op == "reception":
        db.execute(
            "INSERT INTO radio_receptions VALUES(?,?,?,?,?)",
            (x["id"], x["object_id"], x["channel"], x["power"], x["at"]),
        )
        return {"decision": "recorded"}
    if op == "complaint":
        db.execute(
            "INSERT INTO radio_complaints VALUES(?,?,?,?,?)",
            (x["id"], x["case_id"], x["at"], x.get("status", "open"), x.get("kind", "initial")),
        )
        if x.get("kind") == "repeat":
            db.execute(
                "UPDATE radio_cases SET status='reopened' WHERE id=? AND status IN ('serving_retuned','serving_full')",
                (x["case_id"],),
            )
        return {"decision": "recorded"}
    if op == "acceptance":
        db.execute(
            "INSERT INTO radio_acceptance_tests VALUES(?,?,?,?,?,?,?)",
            (x["id"], x["case_id"], x["direction"], x["throughput"], x["latency"], x["evidence"], x["at"]),
        )
        return {"decision": "recorded"}
    if op == "construction":
        db.execute(
            "INSERT INTO radio_constructions VALUES(?,?,?,?,?,?)",
            (x["id"], x["case_id"], x["action"], x["at"], x.get("cost", 0), x.get("new_antenna")),
        )
        return {"decision": "recorded"}
    if op == "retest":
        return _retest(db, x)
    if op == "discount":
        db.execute(
            "INSERT INTO radio_discounts VALUES(?,?,?,?,?,?,?,?,?)",
            (x["id"], x["case_id"], x["amount"], x["kind"], x["from_at"], x.get("to_at"),
             x.get("guaranteed"), None, x.get("reason")),
        )
        return {"decision": "recorded"}
    if op == "correct_discount":
        old = one(db, "SELECT * FROM radio_discounts WHERE id=?", (x["original_id"],))
        if not old:
            return {"decision": "unknown_discount"}
        db.execute(
            "INSERT INTO radio_discounts VALUES(?,?,?,?,?,?,?,?,?)",
            (x["id"], old["case_id"], x["amount"], old["kind"], old["from_at"], old["to_at"],
             old["guaranteed"], None, x["reason"]),
        )
        db.execute("UPDATE radio_discounts SET corrected_by=? WHERE id=?", (x["id"], x["original_id"]))
        return {"decision": "corrected", "original_id": x["original_id"], "amount": x["amount"]}
    if op == "notify":
        _notify(db, x["case_id"], x["at"], x["reason"])
        return {"decision": "recorded"}
    if op == "assess":
        return assess(db, x)
    if op == "receipt":
        return _receipt(db, x)
    raise ValueError(op)
