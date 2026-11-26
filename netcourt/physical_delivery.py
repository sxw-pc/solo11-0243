"""Unified physical route delivery workflow.

This module repairs the provisional delivery in ``delivery_order.py``, which
only counted carrier-submitted route names and nominal capacity and therefore
treated two routes sharing one physical duct as a redundant pair.

A physical path is only established when four pieces of field evidence are
adjudicated together:

1. the engineering visa recording splice-box label replacements, which maps
   the different duct names used on carrier A's as-built drawing and carrier
   B's maintenance drawing onto one canonical physical segment;
2. port photographs from both ends of the route;
3. the raw optical time-domain reflectometer (OTDR) curve;
4. a right-of-way permit that is still valid at adjudication time.

The same evidence keys drive right-of-way expiry and revocation, maintenance
windows, a single capacity ledger, customer service state, construction
payment and the follow-up rebuild order. Every committed, occupied and
construction-reserved amount is closed for the same time window, so the 96
units of the one genuinely independent spare core can never be reported as
both customer A's 72 and customer B's 48.
"""
import json

from .store import one, rows

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pd_visas(
    id TEXT PRIMARY KEY,
    old_name TEXT,
    new_name TEXT,
    segment TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_otdr(
    route TEXT PRIMARY KEY,
    signature TEXT,
    segment TEXT,
    length REAL
);
CREATE TABLE IF NOT EXISTS pd_port_photos(
    id TEXT PRIMARY KEY,
    route TEXT,
    end TEXT,
    received_at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_row_permits(
    id TEXT PRIMARY KEY,
    scope TEXT,
    valid_from INTEGER,
    valid_until INTEGER,
    revoked INTEGER
);
CREATE TABLE IF NOT EXISTS pd_resources(
    id TEXT PRIMARY KEY,
    kind TEXT,
    measured_capacity INTEGER,
    segment TEXT
);
CREATE TABLE IF NOT EXISTS pd_maintenance_windows(
    id TEXT PRIMARY KEY,
    scope TEXT,
    start_at INTEGER,
    end_at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_verified_routes(
    route TEXT PRIMARY KEY,
    service TEXT,
    segment TEXT,
    otdr_signature TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_customers(
    id TEXT PRIMARY KEY,
    name TEXT,
    peak INTEGER,
    isolation_required INTEGER,
    degradation_deadline INTEGER
);
CREATE TABLE IF NOT EXISTS pd_discounts(
    id TEXT PRIMARY KEY,
    customer TEXT,
    amount INTEGER,
    signed INTEGER,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_temp_paths(
    id TEXT PRIMARY KEY,
    carrier TEXT,
    segment TEXT,
    capacity INTEGER,
    row_permit TEXT,
    lossless_remerge INTEGER
);
CREATE TABLE IF NOT EXISTS pd_service_states(
    customer TEXT PRIMARY KEY,
    state TEXT,
    plan TEXT,
    route TEXT,
    spare_resource TEXT,
    isolated INTEGER,
    since INTEGER
);
CREATE TABLE IF NOT EXISTS pd_capacity_ledger(
    id TEXT PRIMARY KEY,
    resource TEXT,
    customer TEXT,
    kind TEXT,
    amount INTEGER,
    start_at INTEGER,
    end_at INTEGER,
    status TEXT,
    ref TEXT
);
CREATE TABLE IF NOT EXISTS pd_delivery_orders(
    id TEXT PRIMARY KEY,
    customer TEXT,
    basis TEXT,
    decision TEXT,
    replaced_by TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_rebuild_orders(
    id TEXT PRIMARY KEY,
    customer TEXT,
    target_segment TEXT,
    row_permit TEXT,
    priority INTEGER,
    status TEXT,
    at INTEGER,
    start_at INTEGER,
    end_at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_payments(
    id TEXT PRIMARY KEY,
    customer TEXT,
    order_ref TEXT,
    amount INTEGER,
    basis TEXT,
    state TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_claims(
    id TEXT PRIMARY KEY,
    customer TEXT,
    order_ref TEXT,
    payment_ref TEXT,
    reason TEXT,
    state TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS pd_audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op TEXT,
    payload TEXT,
    result TEXT,
    at INTEGER
);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _canonical_segment(db, duct_name):
    """Resolve a drawing duct label to the adjudicated physical segment."""
    visa = one(db,
        "SELECT segment FROM pd_visas WHERE old_name=? OR new_name=?",
        (duct_name, duct_name),
    )
    return visa["segment"] if visa else None


def _row_valid(db, permit_id, at):
    permit = one(db, "SELECT * FROM pd_row_permits WHERE id=?", (permit_id,))
    if not permit:
        return None, "unknown_row_permit"
    if permit["revoked"]:
        return permit, "row_permit_revoked"
    if at < permit["valid_from"] or at > permit["valid_until"]:
        return permit, "row_permit_expired"
    return permit, None


def _window_overlaps(db, scope, start_at, end_at):
    for window in rows(db,
        "SELECT * FROM pd_maintenance_windows WHERE scope=? "
        "AND NOT (end_at < ? OR start_at > ?)",
        (scope, start_at, end_at),
    ):
        return window
    return None


def _ledger_open_amounts(db, resource, at):
    return rows(db,
        "SELECT * FROM pd_capacity_ledger WHERE resource=? AND status='active' "
        "AND start_at <= ? AND end_at >= ?",
        (resource, at, at),
    )


def capacity_closure(db, resource, at):
    """Close every committed, occupied and reserved amount for one window."""
    resource_row = one(db, "SELECT * FROM pd_resources WHERE id=?", (resource,))
    if not resource_row:
        return None
    entries = _ledger_open_amounts(db, resource, at)
    total = sum(entry["amount"] for entry in entries)
    return {
        "resource": resource,
        "measured_capacity": resource_row["measured_capacity"],
        "committed": sum(e["amount"] for e in entries if e["kind"] == "committed"),
        "occupied": sum(e["amount"] for e in entries if e["kind"] == "occupied"),
        "construction_reserved": sum(
            e["amount"] for e in entries if e["kind"] == "construction_hold"
        ),
        "total_allocated": total,
        "headroom": resource_row["measured_capacity"] - total,
        "closed": total <= resource_row["measured_capacity"],
        "entries": [
            {
                "id": e["id"],
                "customer": e["customer"],
                "kind": e["kind"],
                "amount": e["amount"],
                "ref": e["ref"],
            }
            for e in entries
        ],
    }


def _would_overbook(db, resource, amount, start_at, end_at):
    """Intervals overlap; the closure is checked over the full new window."""
    resource_row = one(db, "SELECT * FROM pd_resources WHERE id=?", (resource,))
    if not resource_row:
        return True, "unknown_resource"
    moments = {start_at, end_at}
    for entry in rows(db,
        "SELECT * FROM pd_capacity_ledger WHERE resource=? AND status='active'",
        (resource,),
    ):
        moments.add(max(start_at, min(end_at, entry["start_at"])))
        moments.add(max(start_at, min(end_at, entry["end_at"])))
    for moment in moments:
        if moment < start_at or moment > end_at:
            continue
        used = sum(
            e["amount"]
            for e in _ledger_open_amounts(db, resource, moment)
        )
        if used + amount > resource_row["measured_capacity"]:
            return True, "capacity_overbooked"
    return False, None


# ---------------------------------------------------------------------------
# evidence registration
# ---------------------------------------------------------------------------

def register_visa(db, x):
    old = one(db, "SELECT id FROM pd_visas WHERE id=?", (x["id"],))
    if old:
        return {"decision": "visa_identity_conflict"}
    db.execute(
        "INSERT INTO pd_visas VALUES(?,?,?,?,?)",
        (x["id"], x["old_name"], x["new_name"], x["segment"], x["at"]),
    )
    return {"decision": "recorded", "segment": x["segment"]}


def register_otdr(db, x):
    db.execute(
        "INSERT INTO pd_otdr VALUES(?,?,?,?) ON CONFLICT(route) DO UPDATE SET "
        "signature=excluded.signature,segment=excluded.segment,length=excluded.length",
        (x["route"], x["signature"], x["segment"], x["length"]),
    )
    return {"decision": "recorded", "route": x["route"]}


def register_port_photo(db, x):
    old = one(db, "SELECT id FROM pd_port_photos WHERE id=?", (x["id"],))
    if old:
        return {"decision": "photo_identity_conflict"}
    db.execute(
        "INSERT INTO pd_port_photos VALUES(?,?,?,?)",
        (x["id"], x["route"], x["end"], x["received_at"]),
    )
    return {"decision": "recorded", "route": x["route"], "end": x["end"]}


def register_row_permit(db, x):
    old = one(db, "SELECT id FROM pd_row_permits WHERE id=?", (x["id"],))
    if old:
        return {"decision": "permit_identity_conflict"}
    if x["valid_until"] <= x["valid_from"]:
        return {"decision": "invalid_permit_window"}
    db.execute(
        "INSERT INTO pd_row_permits VALUES(?,?,?,?,0)",
        (x["id"], x["scope"], x["valid_from"], x["valid_until"]),
    )
    return {"decision": "recorded", "scope": x["scope"]}


def revoke_row_permit(db, x):
    permit = one(db, "SELECT * FROM pd_row_permits WHERE id=?", (x["id"],))
    if not permit:
        return {"decision": "unknown_row_permit"}
    db.execute("UPDATE pd_row_permits SET revoked=1 WHERE id=?", (x["id"],))
    return {"decision": "revoked", "id": x["id"]}


def register_resource(db, x):
    db.execute(
        "INSERT INTO pd_resources VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "kind=excluded.kind,measured_capacity=excluded.measured_capacity,"
        "segment=excluded.segment",
        (x["id"], x["kind"], x["measured_capacity"], x["segment"]),
    )
    return {
        "decision": "registered",
        "id": x["id"],
        "measured_capacity": x["measured_capacity"],
    }


def register_maintenance_window(db, x):
    old = one(db, "SELECT id FROM pd_maintenance_windows WHERE id=?", (x["id"],))
    if old:
        return {"decision": "window_identity_conflict"}
    if x["end_at"] <= x["start_at"]:
        return {"decision": "invalid_maintenance_window"}
    db.execute(
        "INSERT INTO pd_maintenance_windows VALUES(?,?,?,?)",
        (x["id"], x["scope"], x["start_at"], x["end_at"]),
    )
    return {"decision": "recorded", "scope": x["scope"]}


# ---------------------------------------------------------------------------
# physical route adjudication
# ---------------------------------------------------------------------------

def adjudicate_route(db, x):
    """Rule on the *entity* path from all four evidence bundles."""
    at = x["at"]
    field = one(db, "SELECT * FROM field_routes WHERE route=?", (x["route"],))
    if not field:
        return {"decision": "unknown_field_route"}
    ducts = json.loads(field["ducts"])

    otdr = one(db, "SELECT * FROM pd_otdr WHERE route=?", (x["route"],))
    if not otdr:
        return {"decision": "missing_otdr_curve"}

    permit, error = _row_valid(db, x["row_permit"], at)
    if error:
        return {"decision": error}

    photos = rows(db,
        "SELECT DISTINCT end FROM pd_port_photos WHERE route=? AND received_at<=?",
        (x["route"], at),
    )
    if len(photos) < 2:
        return {
            "decision": "missing_port_photo",
            "received_ends": [p["end"] for p in photos],
        }

    # The visa must explain every carrier-submitted duct label on this route;
    # intersecting the submitted duct strings alone cannot do this because the
    # same segment was relabelled in the field.
    canonical = set()
    for duct in ducts:
        segment = _canonical_segment(db, duct)
        if not segment:
            return {"decision": "unresolved_duct_label", "duct": duct}
        canonical.add(segment)
    if len(canonical) != 1:
        return {"decision": "ambiguous_physical_segment", "segments": sorted(canonical)}
    segment = next(iter(canonical))

    if otdr["segment"] != segment:
        return {
            "decision": "otdr_segment_mismatch",
            "otdr_segment": otdr["segment"],
            "visa_segment": segment,
        }
    if permit["scope"] != segment:
        return {
            "decision": "row_scope_mismatch",
            "permit_scope": permit["scope"],
            "segment": segment,
        }

    db.execute(
        "INSERT INTO pd_verified_routes VALUES(?,?,?,?,?) "
        "ON CONFLICT(route) DO UPDATE SET service=excluded.service,"
        "segment=excluded.segment,otdr_signature=excluded.otdr_signature,at=excluded.at",
        (x["route"], field["service"], segment, otdr["signature"], at),
    )
    return {
        "decision": "verified",
        "route": x["route"],
        "segment": segment,
        "duct_names": ducts,
    }


def verify_redundancy(db, x):
    """Two routes are redundant only when the evidence proves distinct ducts."""
    at = x["at"]
    routes = rows(db,
        "SELECT * FROM pd_verified_routes WHERE service=? ORDER BY route", (x["service"],)
    )
    if len(routes) < 2:
        return {
            "decision": "not_redundant",
            "reason": "fewer_than_two_verified_routes",
            "route_count": len(routes),
        }
    segments = {route["segment"] for route in routes}
    if len(segments) < len(routes):
        shared = sorted(
            segment
            for segment in segments
            if sum(1 for route in routes if route["segment"] == segment) > 1
        )
        return {
            "decision": "shared_failure_domain",
            "shared_segments": shared,
            "routes": [route["route"] for route in routes],
        }
    for route in routes:
        permit = one(db,
            "SELECT * FROM pd_row_permits WHERE scope=? AND revoked=0 "
            "AND valid_from<=? AND valid_until>=?",
            (route["segment"], at, at),
        )
        if not permit:
            return {
                "decision": "row_invalid",
                "route": route["route"],
                "segment": route["segment"],
            }
    return {
        "decision": "physically_redundant",
        "service": x["service"],
        "segments": sorted(segments),
        "routes": [route["route"] for route in routes],
    }


# ---------------------------------------------------------------------------
# customers, discounts and temporary paths
# ---------------------------------------------------------------------------

def register_customer(db, x):
    old = one(db, "SELECT id FROM pd_customers WHERE id=?", (x["id"],))
    if old:
        return {"decision": "customer_identity_conflict"}
    db.execute(
        "INSERT INTO pd_customers VALUES(?,?,?,?,?)",
        (
            x["id"],
            x["name"],
            x["peak"],
            int(x["isolation_required"]),
            x["degradation_deadline"],
        ),
    )
    return {"decision": "registered", "id": x["id"], "peak": x["peak"]}


def offer_discount(db, x):
    customer = one(db, "SELECT * FROM pd_customers WHERE id=?", (x["customer"],))
    if not customer:
        return {"decision": "unknown_customer"}
    db.execute(
        "INSERT INTO pd_discounts VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET amount=excluded.amount,signed=excluded.signed,at=excluded.at",
        (x["id"], x["customer"], x["amount"], int(x["signed"]), x["at"]),
    )
    return {
        "decision": "signed" if x["signed"] else "awaiting_signature",
        "id": x["id"],
        "customer": x["customer"],
    }


def register_temp_path(db, x):
    permit, error = _row_valid(db, x["row_permit"], x["at"])
    if error:
        return {"decision": error}
    if permit["scope"] != x["segment"]:
        return {"decision": "row_scope_mismatch"}
    db.execute(
        "INSERT INTO pd_temp_paths VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET carrier=excluded.carrier,"
        "segment=excluded.segment,capacity=excluded.capacity,"
        "row_permit=excluded.row_permit,lossless_remerge=excluded.lossless_remerge",
        (
            x["id"],
            x["carrier"],
            x["segment"],
            x["capacity"],
            x["row_permit"],
            int(x["lossless_remerge"]),
        ),
    )
    return {"decision": "recorded", "id": x["id"], "segment": x["segment"]}


# ---------------------------------------------------------------------------
# capacity ledger
# ---------------------------------------------------------------------------

def reserve_capacity(db, x):
    customer = one(db, "SELECT * FROM pd_customers WHERE id=?", (x["customer"],))
    if not customer:
        return {"decision": "unknown_customer"}
    if x["end_at"] <= x["start_at"] or x["amount"] <= 0:
        return {"decision": "invalid_reservation"}
    if one(db, "SELECT id FROM pd_capacity_ledger WHERE id=?", (x["id"],)):
        return {"decision": "ledger_identity_conflict"}
    overbooked, error = _would_overbook(
        db, x["resource"], x["amount"], x["start_at"], x["end_at"]
    )
    if error:
        return {"decision": error}
    db.execute(
        "INSERT INTO pd_capacity_ledger VALUES(?,?,?,?,?,?,?,?,?)",
        (
            x["id"],
            x["resource"],
            x["customer"],
            x["kind"],
            x["amount"],
            x["start_at"],
            x["end_at"],
            "active",
            x.get("ref"),
        ),
    )
    return {
        "decision": "reserved",
        "id": x["id"],
        "closure": capacity_closure(db, x["resource"], x["start_at"]),
    }


def release_capacity(db, x):
    entry = one(db, "SELECT * FROM pd_capacity_ledger WHERE id=?", (x["id"],))
    if not entry:
        return {"decision": "unknown_ledger_entry"}
    if entry["status"] != "active":
        return {"decision": "entry_not_active"}
    db.execute("UPDATE pd_capacity_ledger SET status='released' WHERE id=?", (x["id"],))
    return {"decision": "released", "id": x["id"], "resource": entry["resource"]}


# ---------------------------------------------------------------------------
# delivery orders, payments and recovery
# ---------------------------------------------------------------------------

def log_provisional_order(db, x):
    if one(db, "SELECT id FROM pd_delivery_orders WHERE id=?", (x["id"],)):
        return {"decision": "order_identity_conflict"}
    db.execute(
        "INSERT INTO pd_delivery_orders VALUES(?,?,?,?,?,?)",
        (x["id"], x["customer"], x["basis"], x["decision"], None, x["at"]),
    )
    return {"decision": "recorded", "id": x["id"], "basis": x["basis"]}


def correct_delivery_order(db, x):
    """Re-adjudicate an order already promised to a customer and keep the trail."""
    order = one(db, "SELECT * FROM pd_delivery_orders WHERE id=?", (x["id"],))
    if not order:
        return {"decision": "unknown_delivery_order"}
    finding = verify_redundancy(
        db, {"service": x["service"], "at": x["at"]}
    )
    if finding["decision"] == "physically_redundant":
        return {"decision": "order_still_valid", "id": x["id"]}

    new_id = x["new_id"]
    if one(db, "SELECT id FROM pd_delivery_orders WHERE id=?", (new_id,)):
        return {"decision": "order_identity_conflict"}
    db.execute(
        "INSERT INTO pd_delivery_orders VALUES(?,?,?,?,?,?)",
        (
            new_id,
            order["customer"],
            "adjudicated_physical_evidence",
            "not_deliverable",
            None,
            x["at"],
        ),
    )
    db.execute(
        "UPDATE pd_delivery_orders SET decision=?, replaced_by=? WHERE id=?",
        ("corrected_shared_failure_domain", new_id, x["id"]),
    )

    payment = one(db,
        "SELECT * FROM pd_payments WHERE order_ref=? AND state='paid'", (x["id"],)
    )
    claim = {
        "decision": "corrected",
        "id": x["id"],
        "replaced_by": new_id,
        "finding": finding["decision"],
    }
    if payment:
        db.execute("UPDATE pd_payments SET state='recoverable' WHERE id=?", (payment["id"],))
        claim_id = x["claim_id"]
        if not one(db, "SELECT id FROM pd_claims WHERE id=?", (claim_id,)):
            db.execute(
                "INSERT INTO pd_claims VALUES(?,?,?,?,?,?,?)",
                (
                    claim_id,
                    order["customer"],
                    x["id"],
                    payment["id"],
                    finding["decision"],
                    "open",
                    x["at"],
                ),
            )
        claim["recoverable_payment"] = payment["id"]
        claim["claim"] = claim_id
    return claim


def register_payment(db, x):
    if one(db, "SELECT id FROM pd_payments WHERE id=?", (x["id"],)):
        return {"decision": "payment_identity_conflict"}
    db.execute(
        "INSERT INTO pd_payments VALUES(?,?,?,?,?,?,?)",
        (
            x["id"],
            x["customer"],
            x["order_ref"],
            x["amount"],
            x["basis"],
            x["state"],
            x["at"],
        ),
    )
    return {"decision": "recorded", "id": x["id"], "state": x["state"]}


def certify_payment(db, x):
    """Final instalment falls due only on measured acceptance and valid ROW."""
    payment = one(db, "SELECT * FROM pd_payments WHERE id=?", (x["id"],))
    if not payment:
        return {"decision": "unknown_payment"}
    permit, error = _row_valid(db, x["row_permit"], x["at"])
    if error:
        return {"decision": "payment_withheld_row_invalid"}
    verified = one(db,
        "SELECT * FROM pd_verified_routes WHERE route=? AND segment=?",
        (x["route"], permit["scope"]),
    )
    if not verified:
        return {"decision": "payment_withheld_missing_customer_measurement"}
    if not x.get("customer_measured"):
        return {"decision": "payment_withheld_missing_customer_measurement"}
    db.execute("UPDATE pd_payments SET state='paid' WHERE id=?", (x["id"],))
    return {"decision": "payment_due", "id": x["id"], "route": x["route"]}


def refund_payment(db, x):
    payment = one(db, "SELECT * FROM pd_payments WHERE id=?", (x["id"],))
    if not payment:
        return {"decision": "unknown_payment"}
    if payment["state"] != "recoverable":
        return {"decision": "payment_not_recoverable"}
    claim = one(db,
        "SELECT * FROM pd_claims WHERE payment_ref=? AND state='open'", (x["id"],)
    )
    if not claim:
        return {"decision": "missing_recovery_claim"}
    db.execute("UPDATE pd_payments SET state='refunded' WHERE id=?", (x["id"],))
    db.execute("UPDATE pd_claims SET state='recovered' WHERE id=?", (claim["id"],))
    return {
        "decision": "refunded",
        "id": x["id"],
        "claim": claim["id"],
        "customer": payment["customer"],
    }


# ---------------------------------------------------------------------------
# cutover orchestration
# ---------------------------------------------------------------------------

def _plan_spare_to_a(db, x, at, customer_a, customer_b, spare):
    # B stays on the proven control link under a time-limited single-route
    # discount that B must sign inside its permitted degradation window.
    control = one(db,
        "SELECT * FROM pd_verified_routes WHERE route=?", (x["b_control_route"],)
    )
    if not control:
        return {"decision": "control_route_not_verified"}
    discount = one(db, "SELECT * FROM pd_discounts WHERE id=?", (x["b_discount"],))
    if not discount or discount["customer"] != customer_b["id"]:
        return {"decision": "missing_b_discount"}
    if not discount["signed"]:
        return {"decision": "customer_rejected_discount"}
    if at > customer_b["degradation_deadline"]:
        return {"decision": "degradation_window_exceeded"}
    # B's contract forbids sharing a failure domain with A's load. A now owns
    # the spare core, so B's control link must terminate on another segment.
    if customer_b["isolation_required"] and control["segment"] == spare["segment"]:
        return {"decision": "isolation_contract_violated"}

    overbooked, error = _would_overbook(
        db, spare["id"], customer_a["peak"], x["window_start"], x["window_end"]
    )
    if error:
        return {"decision": error}

    def write():
        db.execute(
            "INSERT INTO pd_capacity_ledger VALUES(?,?,?,?,?,?,?,?,?)",
            (
                x["ledger_a"], spare["id"], customer_a["id"], "committed",
                customer_a["peak"], x["window_start"], x["window_end"],
                "active", x["id"],
            ),
        )
        db.execute(
            "INSERT INTO pd_service_states VALUES(?,?,?,?,?,?,?)",
            (
                customer_a["id"], "spare_exclusive", x["plan"], x.get("a_route"),
                spare["id"], 1, at,
            ),
        )
        db.execute(
            "INSERT INTO pd_service_states VALUES(?,?,?,?,?,?,?)",
            (
                customer_b["id"], "single_route_discounted", x["plan"],
                x["b_control_route"], None, 1, at,
            ),
        )
        db.execute(
            "INSERT INTO pd_rebuild_orders VALUES(?,?,?,?,?,?,?,?,?)",
            (
                x["rebuild_id"], customer_b["id"], x["rebuild_segment"],
                x["rebuild_row_permit"], 100, "planned", at, None, None,
            ),
        )

    return {
        "decision": "cutover_complete",
        "plan": x["plan"],
        "write": write,
        "spare_holder": customer_a["id"],
        "discounted_customer": customer_b["id"],
        "cutover_fee_customer": None,
        "rebuild_priority_customer": customer_b["id"],
        "closure_at": at,
    }


def _plan_spare_to_b(db, x, at, customer_a, customer_b, spare):
    # A shards splittable transactions across two different carriers; total
    # throughput must hold and the shards must merge back without loss.
    paths = rows(db,
        "SELECT * FROM pd_temp_paths WHERE id IN (?,?) ORDER BY id",
        (x["a_temp_paths"][0], x["a_temp_paths"][1]),
    )
    if len(paths) != 2:
        return {"decision": "missing_temp_path"}
    if paths[0]["carrier"] == paths[1]["carrier"]:
        return {"decision": "temp_paths_same_carrier"}
    if paths[0]["segment"] == paths[1]["segment"]:
        return {"decision": "temp_paths_share_segment"}
    for path in paths:
        permit, error = _row_valid(db, path["row_permit"], at)
        if error:
            return {"decision": error}
        if permit["valid_until"] < x["window_end"]:
            return {"decision": "temp_row_expires_before_window_end"}
        if not path["lossless_remerge"]:
            return {"decision": "shards_cannot_remerge_losslessly"}
    if sum(path["capacity"] for path in paths) < customer_a["peak"]:
        return {"decision": "temp_throughput_insufficient"}

    control = one(db,
        "SELECT * FROM pd_verified_routes WHERE route=?", (x["b_route"],)
    )
    if not control:
        return {"decision": "b_route_not_verified"}
    if control["segment"] == paths[0]["segment"] or \
            control["segment"] == paths[1]["segment"]:
        return {"decision": "isolation_contract_violated"}
    if spare["segment"] in {paths[0]["segment"], paths[1]["segment"]}:
        return {"decision": "isolation_contract_violated"}

    overbooked, error = _would_overbook(
        db, spare["id"], customer_b["peak"], x["window_start"], x["window_end"]
    )
    if error:
        return {"decision": error}

    def write():
        db.execute(
            "INSERT INTO pd_capacity_ledger VALUES(?,?,?,?,?,?,?,?,?)",
            (
                x["ledger_b"], spare["id"], customer_b["id"], "committed",
                customer_b["peak"], x["window_start"], x["window_end"],
                "active", x["id"],
            ),
        )
        db.execute(
            "INSERT INTO pd_service_states VALUES(?,?,?,?,?,?,?)",
            (
                customer_a["id"], "temporary_sharded", x["plan"],
                ",".join(x["a_temp_paths"]), None, 1, at,
            ),
        )
        db.execute(
            "INSERT INTO pd_service_states VALUES(?,?,?,?,?,?,?)",
            (
                customer_b["id"], "spare_exclusive", x["plan"], x["b_route"],
                spare["id"], 1, at,
            ),
        )
        db.execute(
            "INSERT INTO pd_rebuild_orders VALUES(?,?,?,?,?,?,?,?,?)",
            (
                x["rebuild_id"], customer_a["id"], x["rebuild_segment"],
                x["rebuild_row_permit"], 100, "planned", at, None, None,
            ),
        )
        if x.get("cutover_fee") is not None:
            db.execute(
                "INSERT INTO pd_payments(id,customer,order_ref,amount,basis,state,at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    x["fee_id"], customer_a["id"], x["id"], x["cutover_fee"],
                    "temporary_path_cutover_fee", "accrued", at,
                ),
            )

    return {
        "decision": "cutover_complete",
        "plan": x["plan"],
        "write": write,
        "spare_holder": customer_b["id"],
        "discounted_customer": None,
        "cutover_fee_customer": customer_a["id"],
        "rebuild_priority_customer": customer_a["id"],
        "closure_at": at,
    }


def execute_cutover(db, x):
    at = x["at"]
    customer_a = one(db, "SELECT * FROM pd_customers WHERE id=?", (x["customer_a"],))
    customer_b = one(db, "SELECT * FROM pd_customers WHERE id=?", (x["customer_b"],))
    if not customer_a or not customer_b:
        return {"decision": "unknown_customer"}
    if x["plan"] not in ("spare_to_a", "spare_to_b"):
        return {"decision": "unknown_cutover_plan"}
    spare = one(db, "SELECT * FROM pd_resources WHERE id=?", (x["spare_resource"],))
    if not spare or spare["kind"] != "spare_core":
        return {"decision": "unknown_spare_resource"}

    if _window_overlaps(db, spare["segment"], x["window_start"], x["window_end"]):
        return {"decision": "maintenance_window_conflict"}
    if x["window_end"] <= x["window_start"]:
        return {"decision": "invalid_window"}
    if at > x["window_start"]:
        return {"decision": "cutover_window_missed"}

    if x["plan"] == "spare_to_a":
        outcome = _plan_spare_to_a(db, x, at, customer_a, customer_b, spare)
    else:
        outcome = _plan_spare_to_b(db, x, at, customer_a, customer_b, spare)
    if outcome["decision"] != "cutover_complete":
        return {k: v for k, v in outcome.items() if k != "write"}

    closure_before = capacity_closure(db, spare["id"], at)
    outcome["write"]()
    closure_after = capacity_closure(db, spare["id"], at)
    if not closure_after["closed"]:
        raise RuntimeError("capacity ledger must never overbook")
    return {
        "decision": "cutover_complete",
        "plan": x["plan"],
        "spare_holder": outcome["spare_holder"],
        "discounted_customer": outcome["discounted_customer"],
        "cutover_fee_customer": outcome["cutover_fee_customer"],
        "rebuild_priority_customer": outcome["rebuild_priority_customer"],
        "states": rows(db, "SELECT customer,state,plan,isolated FROM pd_service_states ORDER BY customer"),
        "closure_before": closure_before,
        "closure_after": closure_after,
    }


# ---------------------------------------------------------------------------
# follow-up rebuild
# ---------------------------------------------------------------------------

def schedule_rebuild(db, x):
    order = one(db, "SELECT * FROM pd_rebuild_orders WHERE id=?", (x["id"],))
    if not order:
        return {"decision": "unknown_rebuild_order"}
    if order["status"] != "planned":
        return {"decision": "rebuild_not_plannable"}
    permit, error = _row_valid(db, x["row_permit"], x["at"])
    if error:
        return {"decision": error}
    if permit["scope"] != x["target_segment"]:
        return {"decision": "row_scope_mismatch"}
    for scope in {x["target_segment"], x.get("spare_segment")}:
        if scope and _window_overlaps(db, scope, x["start_at"], x["end_at"]):
            return {"decision": "maintenance_window_conflict"}

    overbooked, error = _would_overbook(
        db, x["spare_resource"], x["construction_hold"], x["start_at"], x["end_at"]
    )
    if error:
        return {"decision": error}

    db.execute(
        "INSERT INTO pd_capacity_ledger VALUES(?,?,?,?,?,?,?,?,?)",
        (
            x["hold_id"], x["spare_resource"], order["customer"],
            "construction_hold", x["construction_hold"], x["start_at"],
            x["end_at"], "active", x["id"],
        ),
    )
    db.execute(
        "UPDATE pd_rebuild_orders SET status='scheduled',row_permit=?,"
        "target_segment=?,start_at=?,end_at=? WHERE id=?",
        (x["row_permit"], x["target_segment"], x["start_at"], x["end_at"], x["id"]),
    )
    return {
        "decision": "rebuild_scheduled",
        "id": x["id"],
        "customer": order["customer"],
        "priority": order["priority"],
        "closure": capacity_closure(db, x["spare_resource"], x["start_at"]),
    }


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------

_HANDLERS = {
    "register_visa": register_visa,
    "register_otdr": register_otdr,
    "register_port_photo": register_port_photo,
    "register_row_permit": register_row_permit,
    "revoke_row_permit": revoke_row_permit,
    "register_resource": register_resource,
    "register_maintenance_window": register_maintenance_window,
    "adjudicate_route": adjudicate_route,
    "verify_redundancy": verify_redundancy,
    "register_customer": register_customer,
    "offer_discount": offer_discount,
    "register_temp_path": register_temp_path,
    "reserve_capacity": reserve_capacity,
    "release_capacity": release_capacity,
    "log_provisional_order": log_provisional_order,
    "correct_delivery_order": correct_delivery_order,
    "register_payment": register_payment,
    "certify_payment": certify_payment,
    "refund_payment": refund_payment,
    "execute_cutover": execute_cutover,
    "schedule_rebuild": schedule_rebuild,
}


# decisions that mean "accepted and applied"; every other decision is a
# rejection, whose transaction is rolled back and which leaves no audit row.
_ACCEPTED_DECISIONS = {
    "recorded", "registered", "verified", "physically_redundant", "revoked",
    "signed", "awaiting_signature", "reserved", "released", "corrected",
    "order_still_valid", "payment_due", "refunded", "cutover_complete",
    "rebuild_scheduled",
}


def run(db, x):
    ensure_schema(db)
    handler = _HANDLERS.get(x["op"])
    if not handler:
        raise ValueError(x["op"])
    db.execute("BEGIN IMMEDIATE")
    try:
        result = handler(db, x)
        if result["decision"] not in _ACCEPTED_DECISIONS:
            # a rejected adjudication commits nothing and leaves no audit row
            db.execute("ROLLBACK")
            return result
        db.execute(
            "INSERT INTO pd_audit(op,payload,result,at) VALUES(?,?,?,?)",
            (
                x["op"],
                json.dumps(x, sort_keys=True, ensure_ascii=False),
                json.dumps(result, sort_keys=True, ensure_ascii=False),
                x.get("at"),
            ),
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return result
