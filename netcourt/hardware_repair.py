"""Hardware spare identity, field exchange, customer acceptance and warranty settlement.

The customer repair conclusion is derived only from the manufacturing serial
that was actually installed (proven by a one-time credential receipt or by a
corroborated offline vendor-signed exchange / device self-proof) together with
the *current* customer business acceptance.  A shipment, a box label, an
arrival scan or a warehouse dispatch row never makes an order repaired: those
records cannot prove which physical device was bolted into the rack.

Three independent fulfilment ledgers are kept:

* spare allocation / installation evidence,
* customer notices and credits (减免),
* vendor deposit / return / warranty settlement.

Every event and ledger entry is append-only.  Retrying a command after a
coordinator crash is safe: command ids are de-duplicated and natural
obligations (one shipment guard, one credential spend, one deposit payment per
obligation, one refund per deposit, one inbound record per RMA) are unique.
"""

import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_warranty_contracts(
  id TEXT PRIMARY KEY,
  vendor TEXT NOT NULL,
  covered_faults TEXT NOT NULL DEFAULT '["*"]',
  expires_at INTEGER,
  terms TEXT
);
CREATE TABLE IF NOT EXISTS hr_devices(
  serial TEXT PRIMARY KEY,
  hardware_version TEXT NOT NULL,
  firmware_compat TEXT NOT NULL,
  warranty_contract TEXT,
  registered_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hr_node_firmware(
  node TEXT PRIMARY KEY,
  firmware TEXT NOT NULL,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS hr_orders(
  id TEXT PRIMARY KEY,
  node TEXT NOT NULL,
  fault_code TEXT,
  created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hr_reservations(
  id TEXT PRIMARY KEY,
  serial TEXT NOT NULL,
  order_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS hr_resv_active_serial
  ON hr_reservations(serial) WHERE status IN ('held','shipped','consumed');
CREATE TABLE IF NOT EXISTS hr_credentials(
  id TEXT PRIMARY KEY,
  serial TEXT NOT NULL,
  order_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'issued',
  issued_at INTEGER,
  used_at INTEGER,
  receipt_id TEXT
);
CREATE TABLE IF NOT EXISTS hr_shipments(
  id TEXT PRIMARY KEY,
  order_id TEXT,
  node TEXT NOT NULL,
  reservation_id TEXT,
  box_label TEXT,
  carrier TEXT,
  status TEXT NOT NULL DEFAULT 'shipped',
  shipped_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_label_changes(
  id TEXT PRIMARY KEY,
  shipment_id TEXT NOT NULL,
  old_label TEXT,
  new_label TEXT,
  reason TEXT,
  at INTEGER
);
CREATE TABLE IF NOT EXISTS hr_rmas(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  old_serial TEXT NOT NULL,
  new_serial TEXT,
  signature TEXT,
  fault_code TEXT,
  issued_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'authorized'
);
CREATE TABLE IF NOT EXISTS hr_evidence(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE,
  order_id TEXT,
  node TEXT,
  serial TEXT,
  kind TEXT NOT NULL,
  at INTEGER NOT NULL,
  data TEXT
);
CREATE TABLE IF NOT EXISTS hr_notices(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  at INTEGER NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS hr_credits(
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  amount INTEGER NOT NULL,
  reason TEXT,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_deposits(
  id TEXT PRIMARY KEY,
  obligation TEXT NOT NULL UNIQUE,
  order_id TEXT,
  rma_id TEXT,
  vendor TEXT NOT NULL,
  serial TEXT,
  amount INTEGER NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_deposit_refunds(
  id TEXT PRIMARY KEY,
  deposit_id TEXT NOT NULL UNIQUE,
  amount INTEGER NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_old_part_receipts(
  id TEXT PRIMARY KEY,
  rma_id TEXT NOT NULL UNIQUE,
  serial TEXT NOT NULL,
  warehouse TEXT,
  received_by TEXT,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_old_part_losses(
  id TEXT PRIMARY KEY,
  rma_id TEXT NOT NULL UNIQUE,
  detail TEXT,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_appraisals(
  id TEXT PRIMARY KEY,
  rma_id TEXT NOT NULL,
  covered INTEGER NOT NULL,
  reason TEXT,
  appraiser TEXT,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_settlement_adjustments(
  id TEXT PRIMARY KEY,
  deposit_id TEXT NOT NULL,
  rma_id TEXT,
  kind TEXT NOT NULL,
  amount INTEGER NOT NULL,
  appraisal_id TEXT UNIQUE,
  at INTEGER NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS hr_timeline(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT,
  at INTEGER NOT NULL,
  kind TEXT NOT NULL,
  ref_id TEXT,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS hr_commands(
  command_id TEXT PRIMARY KEY,
  op TEXT,
  result TEXT,
  at INTEGER
);
"""


def connect(path=":memory:"):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=3000")
    init_db(db)
    return db


def init_db(db):
    db.executescript(SCHEMA)


@contextmanager
def tx(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def one(db, q, a=()):
    x = db.execute(q, a).fetchone()
    return dict(x) if x else None


def rows(db, q, a=()):
    return [dict(x) for x in db.execute(q, a)]


def _data(row):
    try:
        return json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        return {}


def _timeline(db, order_id, at, kind, ref_id=None, detail=None):
    db.execute(
        "INSERT INTO hr_timeline(order_id,at,kind,ref_id,detail) VALUES(?,?,?,?,?)",
        (order_id, at, kind, ref_id, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)),
    )


def _record_evidence(db, event_id, kind, at, order_id=None, node=None, serial=None, data=None):
    db.execute(
        "INSERT INTO hr_evidence(id,order_id,node,serial,kind,at,data) VALUES(?,?,?,?,?,?,?)",
        (event_id, order_id, node, serial, kind, at,
         json.dumps(data or {}, ensure_ascii=False, sort_keys=True)),
    )


def _transact(db, command_id, op, at, work):
    """Run ``work`` atomically; replay a stored result for a duplicate command id."""
    init_db(db)
    if command_id is not None:
        cached = one(db, "SELECT result FROM hr_commands WHERE command_id=?", (command_id,))
        if cached is not None:
            return json.loads(cached["result"])
    with tx(db):
        result = work()
        if command_id is not None:
            db.execute(
                "INSERT INTO hr_commands(command_id,op,result,at) VALUES(?,?,?,?)",
                (command_id, op,
                 json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), at),
            )
        return result


# ---------------------------------------------------------------------------
# Fixed device identity: manufacturing serial + hardware + firmware + warranty
# ---------------------------------------------------------------------------

def register_warranty_contract(db, contract_id, vendor, covered_faults=None,
                               expires_at=None, terms=None):
    init_db(db)

    def work():
        old = one(db, "SELECT * FROM hr_warranty_contracts WHERE id=?", (contract_id,))
        if old:
            return {"decision": "already_registered", "contract_id": contract_id}
        faults = ["*"] if covered_faults is None else list(covered_faults)
        db.execute(
            "INSERT INTO hr_warranty_contracts VALUES(?,?,?,?,?)",
            (contract_id, vendor, json.dumps(faults), expires_at, terms),
        )
        return {"decision": "registered", "contract_id": contract_id}

    return _transact(db, None, "register_warranty_contract", 0, work)


def register_device(db, serial, hardware_version, firmware_compat,
                    warranty_contract=None, at=0):
    init_db(db)

    def work():
        if warranty_contract and not one(
            db, "SELECT id FROM hr_warranty_contracts WHERE id=?", (warranty_contract,)
        ):
            return {"decision": "unknown_warranty_contract",
                    "warranty_contract": warranty_contract}
        compat = list(firmware_compat or [])
        old = one(db, "SELECT * FROM hr_devices WHERE serial=?", (serial,))
        identity = (hardware_version, json.dumps(compat), warranty_contract)
        if old:
            if (old["hardware_version"], old["firmware_compat"],
                    old["warranty_contract"]) != identity:
                return {"decision": "identity_conflict", "serial": serial}
            return {"decision": "already_registered", "serial": serial}
        db.execute("INSERT INTO hr_devices VALUES(?,?,?,?,?)",
                   (serial, hardware_version, json.dumps(compat),
                    warranty_contract, at))
        return {"decision": "registered", "serial": serial,
                "hardware_version": hardware_version, "firmware_compat": compat}

    return _transact(db, None, "register_device", at, work)


def set_node_firmware(db, node, firmware, at=0):
    init_db(db)

    def work():
        db.execute(
            "INSERT INTO hr_node_firmware(node,firmware,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(node) DO UPDATE SET firmware=excluded.firmware,"
            "updated_at=excluded.updated_at",
            (node, firmware, at),
        )
        return {"decision": "recorded", "node": node, "firmware": firmware}

    return _transact(db, None, "set_node_firmware", at, work)


def contract_covers_fault(db, serial, fault_code, at=None):
    """Whether the fixed identity's warranty contract lists this fault."""
    dev = one(db, "SELECT * FROM hr_devices WHERE serial=?", (serial,))
    if not dev or not dev["warranty_contract"]:
        return False
    c = one(db, "SELECT * FROM hr_warranty_contracts WHERE id=?",
            (dev["warranty_contract"],))
    if not c:
        return False
    if c["expires_at"] is not None and at is not None and at > c["expires_at"]:
        return False
    faults = json.loads(c["covered_faults"])
    return "*" in faults or fault_code in faults


# ---------------------------------------------------------------------------
# Repair orders and central spare reservation / one-time install credentials
# ---------------------------------------------------------------------------

def open_order(db, order_id, node, fault_code=None, at=0):
    init_db(db)

    def work():
        old = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if old:
            if old["node"] != node:
                return {"decision": "order_identity_conflict", "order_id": order_id}
            return {"decision": "already_open", "order_id": order_id}
        db.execute("INSERT INTO hr_orders VALUES(?,?,?,?)",
                   (order_id, node, fault_code, at))
        _timeline(db, order_id, at, "order_opened", order_id,
                  {"node": node, "fault_code": fault_code})
        return {"decision": "opened", "order_id": order_id, "node": node}

    return _transact(db, None, "open_order", at, work)


def reserve_spare(db, reservation_id, order_id, serial, at=0, command_id=None):
    """Centrally reserve the unique manufacturing serial for one order."""
    def work():
        if one(db, "SELECT id FROM hr_reservations WHERE id=?", (reservation_id,)):
            return {"decision": "already_recorded", "reservation_id": reservation_id}
        order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        if not one(db, "SELECT serial FROM hr_devices WHERE serial=?", (serial,)):
            return {"decision": "unknown_device_serial", "serial": serial}
        held = one(
            db,
            "SELECT * FROM hr_reservations WHERE serial=? "
            "AND status IN ('held','shipped','consumed') ORDER BY created_at LIMIT 1",
            (serial,),
        )
        if held:
            if held["order_id"] == order_id:
                return {"decision": "reservation_held",
                        "reservation_id": held["id"], "serial": serial}
            return {"decision": "spare_already_reserved", "serial": serial,
                    "held_by_order": held["order_id"]}
        db.execute("INSERT INTO hr_reservations VALUES(?,?,?,?,?)",
                   (reservation_id, serial, order_id, "held", at))
        _timeline(db, order_id, at, "spare_reserved", reservation_id,
                  {"serial": serial})
        return {"decision": "reserved", "reservation_id": reservation_id,
                "order_id": order_id, "serial": serial}

    return _transact(db, command_id, "reserve_spare", at, work)


def issue_install_credential(db, credential_id, order_id, serial, at=0,
                             command_id=None):
    """Issue the technician a one-time installation credential for this serial."""
    def work():
        old = one(db, "SELECT * FROM hr_credentials WHERE id=?", (credential_id,))
        if old:
            return {"decision": "already_recorded", "credential_id": credential_id}
        order = one(db, "SELECT id FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        resv = one(
            db,
            "SELECT * FROM hr_reservations WHERE order_id=? AND serial=? "
            "AND status IN ('held','shipped','consumed')",
            (order_id, serial),
        )
        if not resv:
            return {"decision": "reservation_required",
                    "order_id": order_id, "serial": serial}
        duplicate = one(
            db,
            "SELECT * FROM hr_credentials WHERE order_id=? AND serial=? "
            "AND status='issued'",
            (order_id, serial),
        )
        if duplicate:
            return {"decision": "credential_already_issued",
                    "credential_id": duplicate["id"]}
        db.execute("INSERT INTO hr_credentials(id,serial,order_id,status,"
                   "issued_at,used_at,receipt_id) VALUES(?,?,?,'issued',?,NULL,NULL)",
                   (credential_id, serial, order_id, at))
        _timeline(db, order_id, at, "install_credential_issued", credential_id,
                  {"serial": serial})
        return {"decision": "credential_issued", "credential_id": credential_id,
                "one_time": True}

    return _transact(db, command_id, "issue_install_credential", at, work)


# ---------------------------------------------------------------------------
# Shipment, box labels and the distinct physical handling events
# ---------------------------------------------------------------------------

def ship_spare(db, shipment_id, at, order_id=None, node=None, box_label=None,
               carrier=None, reservation_id=None, command_id=None):
    init_db(db)

    def work():
        existing = one(db, "SELECT * FROM hr_shipments WHERE id=?", (shipment_id,))
        if existing:
            return {"decision": "already_recorded", "shipment_id": shipment_id}
        order = None
        if order_id:
            order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
            if not order:
                return {"decision": "unknown_order", "order_id": order_id}
        ship_node = node
        if ship_node is None:
            if not order:
                return {"decision": "node_or_order_required"}
            ship_node = order["node"]
        # A retry / duplicate dispatch must never create a second shipment for
        # the same order while the first is live.
        if order_id:
            prior = one(
                db,
                "SELECT id FROM hr_shipments WHERE order_id=? AND status='shipped'",
                (order_id,),
            )
            if prior:
                return {"decision": "spare_already_dispatched",
                        "shipment_id": prior["id"]}
        if reservation_id:
            resv = one(db, "SELECT * FROM hr_reservations WHERE id=?",
                       (reservation_id,))
            if not resv:
                return {"decision": "unknown_reservation",
                        "reservation_id": reservation_id}
            if resv["status"] in ("shipped", "consumed"):
                return {"decision": "spare_already_dispatched",
                        "reservation_id": reservation_id}
            db.execute("UPDATE hr_reservations SET status='shipped' WHERE id=?",
                       (reservation_id,))
        db.execute("INSERT INTO hr_shipments VALUES(?,?,?,?,?,?,?,?)",
                   (shipment_id, order_id, ship_node, reservation_id, box_label,
                    carrier, "shipped", at))
        _timeline(db, order_id, at, "spare_dispatched", shipment_id,
                  {"node": ship_node, "box_label": box_label,
                   "carrier": carrier})
        return {"decision": "dispatched", "shipment_id": shipment_id,
                "node": ship_node, "box_label": box_label}

    return _transact(db, command_id, "ship_spare", at, work)


def replace_box_label(db, change_id, shipment_id, old_label, new_label, at,
                      reason=None, command_id=None):
    """Record a box-label replacement.  Labels are logistics data, not identity."""
    def work():
        if one(db, "SELECT id FROM hr_label_changes WHERE id=?", (change_id,)):
            return {"decision": "already_recorded", "change_id": change_id}
        ship = one(db, "SELECT * FROM hr_shipments WHERE id=?", (shipment_id,))
        if not ship:
            return {"decision": "unknown_shipment", "shipment_id": shipment_id}
        if ship["box_label"] != old_label:
            return {"decision": "box_label_mismatch",
                    "current_label": ship["box_label"]}
        db.execute("UPDATE hr_shipments SET box_label=? WHERE id=?",
                   (new_label, shipment_id))
        db.execute("INSERT INTO hr_label_changes VALUES(?,?,?,?,?,?)",
                   (change_id, shipment_id, old_label, new_label, reason, at))
        _timeline(db, ship["order_id"], at, "box_label_replaced", change_id,
                  {"old_label": old_label, "new_label": new_label,
                   "reason": reason})
        return {"decision": "label_replaced", "shipment_id": shipment_id,
                "box_label": new_label,
                "customer_repaired": False}

    return _transact(db, command_id, "replace_box_label", at, work)


def _shipment_event(db, event_id, shipment_id, kind, at, actor=None,
                    detail=None, command_id=None):
    init_db(db)
    allowed = {"carrier_signed", "site_received", "unsealed"}

    def work():
        if kind not in allowed:
            return {"decision": "invalid_event_kind", "kind": kind}
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (event_id,)):
            return {"decision": "already_recorded", "event_id": event_id}
        ship = one(db, "SELECT * FROM hr_shipments WHERE id=?", (shipment_id,))
        if not ship:
            return {"decision": "unknown_shipment", "shipment_id": shipment_id}
        payload = dict(detail or {})
        payload.update({"shipment_id": shipment_id, "actor": actor})
        _record_evidence(db, event_id, kind, at, order_id=ship["order_id"],
                          node=ship["node"], data=payload)
        _timeline(db, ship["order_id"], at, kind, event_id, payload)
        return {"decision": "recorded", "event_id": event_id, "kind": kind}

    return _transact(db, command_id, kind, at, work)


def carrier_signed(db, receipt_id, shipment_id, at, signed_by=None,
                   command_id=None):
    """承运签收: carrier took / confirmed custody of the consignment."""
    return _shipment_event(db, receipt_id, shipment_id, "carrier_signed", at,
                           actor=signed_by, command_id=command_id)


def site_received(db, receipt_id, shipment_id, at, signed_by=None,
                  command_id=None):
    """现场到货签收 (distinct from dispatch and from unsealing)."""
    return _shipment_event(db, receipt_id, shipment_id, "site_received", at,
                           actor=signed_by, command_id=command_id)


def unseal(db, event_id, shipment_id, at, by=None, note=None, command_id=None):
    """现场拆封."""
    return _shipment_event(db, event_id, shipment_id, "unsealed", at,
                           actor=by, detail={"note": note},
                           command_id=command_id)


def scan_manufacturing_serial(db, scan_id, serial, at, node=None,
                              order_id=None, scanned_by=None, source="rack_scan",
                              command_id=None):
    """Record the manufacturing serial actually scanned in the rack."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (scan_id,)):
            return {"decision": "already_recorded", "scan_id": scan_id}
        dev = one(db, "SELECT * FROM hr_devices WHERE serial=?", (serial,))
        _record_evidence(db, scan_id, "mfg_scan", at, order_id=order_id,
                         node=node, serial=serial,
                         data={"scanned_by": scanned_by, "source": source,
                               "registered": dev is not None})
        _timeline(db, order_id, at, "manufacturing_serial_scanned", scan_id,
                  {"serial": serial, "registered": dev is not None})
        return {"decision": "scan_recorded", "serial": serial,
                "registered": dev is not None}

    return _transact(db, command_id, "scan_manufacturing_serial", at, work)


def record_field_report(db, report_id, order_id, at, recovered_claimed=True,
                        note=None, by=None, command_id=None):
    """An offline technician verbal/field report.  Never proof of recovery."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (report_id,)):
            return {"decision": "already_recorded", "report_id": report_id}
        order = one(db, "SELECT id FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        _record_evidence(db, report_id, "field_report", at, order_id=order_id,
                         data={"recovered_claimed": bool(recovered_claimed),
                               "note": note, "by": by})
        _timeline(db, order_id, at, "field_report_pending_verification",
                  report_id, {"recovered_claimed": recovered_claimed})
        return {"decision": "recorded_pending_verification",
                "report_id": report_id}

    return _transact(db, command_id, "record_field_report", at, work)


# ---------------------------------------------------------------------------
# Path A: one-time credentialed install receipt
# ---------------------------------------------------------------------------

def submit_install_receipt(db, receipt_id, order_id, serial, credential_id,
                           installed_at, uploaded_at=None, by=None,
                           command_id=None):
    init_db(db)
    uploaded_at = installed_at if uploaded_at is None else uploaded_at

    def work():
        existing = one(db, "SELECT * FROM hr_evidence WHERE id=?", (receipt_id,))
        if existing:
            return {"decision": "already_recorded", "receipt_id": receipt_id}
        order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        cred = one(db, "SELECT * FROM hr_credentials WHERE id=?",
                   (credential_id,))
        if not cred:
            _timeline(db, order_id, uploaded_at, "install_rejected", receipt_id,
                      {"reason": "unknown_credential", "serial": serial})
            return {"decision": "unknown_credential",
                    "credential_id": credential_id}
        if cred["order_id"] != order_id:
            _timeline(db, order_id, uploaded_at, "install_rejected", receipt_id,
                      {"reason": "credential_order_mismatch", "serial": serial})
            return {"decision": "credential_order_mismatch"}
        if cred["serial"] != serial:
            _timeline(db, order_id, uploaded_at, "install_rejected", receipt_id,
                      {"reason": "credential_serial_mismatch",
                       "presented": serial, "expected": cred["serial"]})
            return {"decision": "credential_serial_mismatch",
                    "expected_serial": cred["serial"], "presented_serial": serial}
        if cred["status"] == "used":
            if cred["receipt_id"] == receipt_id:
                return {"decision": "already_recorded", "receipt_id": receipt_id}
            _timeline(db, order_id, uploaded_at, "install_rejected", receipt_id,
                      {"reason": "credential_already_used"})
            return {"decision": "credential_already_used",
                    "credential_id": credential_id}
        if not one(db, "SELECT serial FROM hr_devices WHERE serial=?", (serial,)):
            _timeline(db, order_id, uploaded_at, "install_rejected", receipt_id,
                      {"reason": "unknown_device_serial", "serial": serial})
            return {"decision": "unknown_device_serial", "serial": serial}

        _record_evidence(db, receipt_id, "install_receipt", installed_at,
                         order_id=order_id, node=order["node"], serial=serial,
                         data={"credential_id": credential_id, "by": by,
                               "uploaded_at": uploaded_at})
        db.execute(
            "UPDATE hr_credentials SET status='used',used_at=?,receipt_id=? "
            "WHERE id=?", (uploaded_at, receipt_id, credential_id))
        db.execute(
            "UPDATE hr_reservations SET status='consumed' "
            "WHERE order_id=? AND serial=? AND status!='consumed'",
            (order_id, serial))
        _timeline(db, order_id, installed_at, "installed", receipt_id,
                  {"serial": serial, "credential_id": credential_id})
        return {"decision": "install_recorded", "receipt_id": receipt_id,
                "order_id": order_id, "serial": serial,
                "installed_at": installed_at}

    return _transact(db, command_id, "submit_install_receipt",
                     uploaded_at, work)


# ---------------------------------------------------------------------------
# Path B: vendor offline authorization + offline exchange + self proof
# ---------------------------------------------------------------------------

def issue_rma(db, rma_id, order_id, vendor, old_serial, at,
              signature=None, new_serial=None, fault_code=None,
              command_id=None):
    """Create the vendor authorization order (授权单), optionally with an
    offline vendor seal/signature enabling a disconnected exchange."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_rmas WHERE id=?", (rma_id,)):
            return {"decision": "already_recorded", "rma_id": rma_id}
        if not one(db, "SELECT id FROM hr_orders WHERE id=?", (order_id,)):
            return {"decision": "unknown_order", "order_id": order_id}
        db.execute("INSERT INTO hr_rmas VALUES(?,?,?,?,?,?,?,?,?)",
                   (rma_id, order_id, vendor, old_serial, new_serial,
                    signature, fault_code, at,
                    "authorized_offline" if signature else "authorized"))
        _timeline(db, order_id, at, "rma_authorized", rma_id,
                  {"rma_id": rma_id, "vendor": vendor,
                   "offline_signature": bool(signature),
                   "old_serial": old_serial})
        return {"decision": "rma_authorized", "rma_id": rma_id,
                "offline_signature": bool(signature)}

    return _transact(db, command_id, "issue_rma", at, work)


def record_offline_exchange(db, voucher_id, order_id, new_serial, old_serial,
                            rma_id, exchanged_at, recorded_at=None,
                            technician=None, command_id=None):
    """Record an exchange performed offline under the vendor's signed auth."""
    recorded_at = exchanged_at if recorded_at is None else recorded_at

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (voucher_id,)):
            return {"decision": "already_recorded", "voucher_id": voucher_id}
        order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
        if not rma or rma["order_id"] != order_id:
            return {"decision": "unknown_or_mismatched_rma", "rma_id": rma_id}
        if not rma["signature"]:
            return {"decision": "vendor_authorization_required",
                    "rma_id": rma_id}
        if rma["old_serial"] != old_serial:
            return {"decision": "old_serial_mismatch",
                    "authorized_old_serial": rma["old_serial"],
                    "presented_old_serial": old_serial}
        if not one(db, "SELECT serial FROM hr_devices WHERE serial=?",
                   (new_serial,)):
            return {"decision": "unknown_device_serial", "serial": new_serial}
        _record_evidence(db, voucher_id, "offline_exchange", exchanged_at,
                         order_id=order_id, node=order["node"],
                         serial=new_serial,
                         data={"old_serial": old_serial, "rma_id": rma_id,
                               "technician": technician,
                               "recorded_at": recorded_at,
                               "vendor_signature": rma["signature"]})
        _timeline(db, order_id, exchanged_at, "offline_exchange_claimed",
                  voucher_id, {"new_serial": new_serial,
                               "old_serial": old_serial, "rma_id": rma_id})
        return {"decision": "offline_exchange_recorded",
                "voucher_id": voucher_id, "serial": new_serial,
                "status": "awaiting_self_proof_and_customer_probe"}

    return _transact(db, command_id, "record_offline_exchange",
                     recorded_at, work)


def submit_device_self_proof(db, proof_id, serial, at, node=None,
                             order_id=None, hardware_version=None,
                             firmware=None, signature="device-attestation",
                             command_id=None):
    """Device self-attestation of its own manufacturing identity."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (proof_id,)):
            return {"decision": "already_recorded", "proof_id": proof_id}
        dev = one(db, "SELECT * FROM hr_devices WHERE serial=?", (serial,))
        reasons = []
        if not dev:
            reasons.append("unknown_serial")
        else:
            if hardware_version is not None and \
                    hardware_version != dev["hardware_version"]:
                reasons.append("hardware_version_mismatch")
            compat = json.loads(dev["firmware_compat"])
            if firmware is not None and firmware not in compat:
                reasons.append("firmware_not_compatible")
        valid = not reasons
        _record_evidence(db, proof_id, "self_proof", at, order_id=order_id,
                         node=node, serial=serial,
                         data={"valid": valid, "reasons": reasons,
                               "hardware_version": hardware_version,
                               "firmware": firmware, "signature": signature})
        if order_id:
            _timeline(db, order_id, at,
                       "device_self_proof_verified" if valid
                       else "device_self_proof_invalid",
                       proof_id, {"serial": serial, "reasons": reasons})
        return {"decision": "self_proof_verified" if valid
                else "self_proof_invalid",
                "proof_id": proof_id, "valid": valid, "reasons": reasons}

    return _transact(db, command_id, "submit_device_self_proof", at, work)


def record_power_on_self_test(db, event_id, order_id, serial, passed, at,
                              firmware=None, report=None, command_id=None):
    """通电自检, checked against the fixed firmware compatibility list."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (event_id,)):
            return {"decision": "already_recorded", "event_id": event_id}
        order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        dev = one(db, "SELECT * FROM hr_devices WHERE serial=?", (serial,))
        if not dev:
            return {"decision": "unknown_device_serial", "serial": serial}
        fw = firmware
        if fw is None:
            nf = one(db, "SELECT firmware FROM hr_node_firmware WHERE node=?",
                      (order["node"],))
            fw = nf["firmware"] if nf else None
        compat = json.loads(dev["firmware_compat"])
        compatible = fw is not None and fw in compat
        post_passed = bool(passed) and compatible
        _record_evidence(db, event_id, "post", at, order_id=order_id,
                         node=order["node"], serial=serial,
                         data={"passed": bool(passed),
                               "counts_as_passed": post_passed,
                               "compatible": compatible, "firmware": fw,
                               "report": report})
        _timeline(db, order_id, at,
                  "post_passed" if post_passed else "post_failed",
                  event_id, {"serial": serial, "firmware": fw,
                             "compatible": compatible})
        if not compatible:
            return {"decision": "firmware_incompatible", "serial": serial,
                    "firmware": fw, "firmware_compat": compat}
        return {"decision": "post_passed" if post_passed else "post_failed",
                "event_id": event_id}

    return _transact(db, command_id, "record_power_on_self_test", at, work)


def record_business_verification(db, verification_id, order_id, passed, at,
                                 accepted_by=None, metrics=None, probe=None,
                                 command_id=None):
    """Customer business probe / test and optional real business acceptance.

    A passing probe without ``accepted_by`` is not formal acceptance.  The
    latest verification is the current truth, so a later failed retest
    overturns an earlier recovery conclusion without erasing the earlier row.
    """
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?",
               (verification_id,)):
            return {"decision": "already_recorded",
                    "verification_id": verification_id}
        order = one(db, "SELECT id FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        accepted = bool(passed) and bool(accepted_by)
        _record_evidence(db, verification_id, "business_test", at,
                         order_id=order_id,
                         data={"passed": bool(passed), "accepted": accepted,
                               "accepted_by": accepted_by, "metrics": metrics,
                               "probe": probe})
        if not passed:
            kind = "business_retest_failed"
            decision = "retest_failed"
        elif accepted:
            kind = "business_accepted"
            decision = "business_accepted"
        else:
            kind = "business_probe_passed"
            decision = "probe_passed_pending_acceptance"
        _timeline(db, order_id, at, kind, verification_id,
                  {"accepted_by": accepted_by, "metrics": metrics})
        return {"decision": decision, "verification_id": verification_id,
                "accepted": accepted}

    return _transact(db, command_id, "record_business_verification", at, work)


def adjudicate_identity(db, resolution_id, order_id, serial, proof_id, at,
                        installed_at=None, actor=None, command_id=None):
    """Resolve an offline-receipt vs self-proof contradiction using a valid
    device self-proof.  The correction is appended, never overwrites evidence."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (resolution_id,)):
            return {"decision": "already_recorded",
                    "resolution_id": resolution_id}
        order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
        if not order:
            return {"decision": "unknown_order", "order_id": order_id}
        proof = one(db, "SELECT * FROM hr_evidence WHERE id=? AND kind='self_proof'",
                    (proof_id,))
        pdata = _data(proof) if proof else {}
        if not proof or not pdata.get("valid") or proof["serial"] != serial:
            return {"decision": "valid_self_proof_required",
                    "proof_id": proof_id}
        if proof["node"] and proof["node"] != order["node"]:
            return {"decision": "proof_node_mismatch",
                    "proof_node": proof["node"], "order_node": order["node"]}
        payload = {"serial": serial, "proof_id": proof_id, "actor": actor,
                   "installed_at": installed_at}
        _record_evidence(db, resolution_id, "identity_resolution", at,
                         order_id=order_id, node=order["node"], serial=serial,
                         data=payload)
        _timeline(db, order_id, at, "identity_adjudicated_to_self_proof",
                  resolution_id, payload)
        return {"decision": "identity_resolved", "order_id": order_id,
                "serial": serial}

    return _transact(db, command_id, "adjudicate_identity", at, work)


def resolve_serial_contest(db, resolution_id, serial, winning_order,
                           proof_id, at, actor=None, command_id=None):
    """Resolve two repair orders claiming one physical serial.

    Requires a valid self-proof of that serial at the winning order's node and
    a currently passing customer business probe for the winning order.  The
    losing claim stays on record as rejected.
    """
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (resolution_id,)):
            return {"decision": "already_recorded",
                    "resolution_id": resolution_id}
        winner = one(db, "SELECT * FROM hr_orders WHERE id=?", (winning_order,))
        if not winner:
            return {"decision": "unknown_order", "order_id": winning_order}
        proof = one(db, "SELECT * FROM hr_evidence WHERE id=? AND kind='self_proof'",
                    (proof_id,))
        pdata = _data(proof) if proof else {}
        if not proof or not pdata.get("valid") or proof["serial"] != serial:
            return {"decision": "valid_self_proof_required",
                    "proof_id": proof_id}
        if proof["node"] and proof["node"] != winner["node"]:
            return {"decision": "proof_node_mismatch"}
        latest_biz = one(
            db,
            "SELECT * FROM hr_evidence WHERE order_id=? AND kind='business_test' "
            "ORDER BY at DESC, seq DESC LIMIT 1",
            (winning_order,),
        )
        bdata = _data(latest_biz) if latest_biz else {}
        if not latest_biz or not bdata.get("passed"):
            return {"decision": "passing_customer_probe_required"}
        holders = {
            r["order_id"] for r in rows(
                db,
                "SELECT DISTINCT order_id FROM hr_evidence "
                "WHERE serial=? AND kind IN ('install_receipt','offline_exchange')",
                (serial,),
            ) if r["order_id"]
        }
        if len(holders) < 2:
            return {"decision": "no_serial_contest", "serial": serial,
                    "holders": sorted(h for h in holders if h)}
        payload = {"serial": serial, "winning_order": winning_order,
                   "proof_id": proof_id, "actor": actor}
        _record_evidence(db, resolution_id, "serial_contest_resolution", at,
                         order_id=winning_order, serial=serial, data=payload)
        _timeline(db, winning_order, at, "serial_contest_won", resolution_id,
                  payload)
        for loser in sorted(h for h in holders if h != winning_order):
            _timeline(db, loser, at, "serial_claim_rejected", resolution_id,
                      {"serial": serial, "winner": winning_order})
        return {"decision": "contest_resolved", "serial": serial,
                "winning_order": winning_order,
                "rejected_orders": sorted(h for h in holders
                                          if h != winning_order)}

    return _transact(db, command_id, "resolve_serial_contest", at, work)


# ---------------------------------------------------------------------------
# Adjudication: the only place a customer repair conclusion is produced
# ---------------------------------------------------------------------------

def _shipment_seen(db, order_id, node):
    return one(
        db,
        "SELECT id FROM hr_shipments WHERE order_id=? "
        "OR (order_id IS NULL AND node=?) ORDER BY shipped_at LIMIT 1",
        (order_id, node),
    )


def adjudicate(db, order_id):
    init_db(db)
    order = one(db, "SELECT * FROM hr_orders WHERE id=?", (order_id,))
    if not order:
        return {"decision": "unknown_order", "order_id": order_id,
                "customer_repaired": False, "state": "unknown_order",
                "installed_serial": None, "recovered_at": None,
                "blockers": ["unknown_order"], "conflicts": []}
    node = order["node"]
    blockers = []
    conflicts = []

    all_claims = rows(
        db,
        "SELECT order_id,serial,kind,id FROM hr_evidence "
        "WHERE kind IN ('install_receipt','offline_exchange')",
    )
    holders_by_serial = {}
    for c in all_claims:
        holders_by_serial.setdefault(c["serial"], set()).add(c["order_id"])

    contest_winner = {}
    for e in rows(
        db,
        "SELECT serial,data FROM hr_evidence "
        "WHERE kind='serial_contest_resolution' ORDER BY seq",
    ):
        contest_winner[e["serial"]] = _data(e).get("winning_order")

    idres = one(
        db,
        "SELECT * FROM hr_evidence WHERE order_id=? "
        "AND kind='identity_resolution' ORDER BY seq DESC LIMIT 1",
        (order_id,),
    )

    my_claims = rows(
        db,
        "SELECT serial,kind FROM hr_evidence WHERE order_id=? "
        "AND kind IN ('install_receipt','offline_exchange')",
        (order_id,),
    )
    my_serials = {c["serial"] for c in my_claims}
    has_credential_claim = any(c["kind"] == "install_receipt"
                               for c in my_claims)
    has_offline_claim = any(c["kind"] == "offline_exchange"
                            for c in my_claims)

    proof_rows = rows(
        db,
        "SELECT * FROM hr_evidence WHERE kind='self_proof' "
        "AND (order_id=? OR (order_id IS NULL AND node=?))",
        (order_id, node),
    )
    valid_proof_serials = {p["serial"] for p in proof_rows
                           if _data(p).get("valid")}

    candidate = None
    installed_at = None

    resolved_serial = None
    if idres:
        d = _data(idres)
        proof = one(db, "SELECT * FROM hr_evidence WHERE id=? "
                        "AND kind='self_proof'", (d.get("proof_id"),))
        pd = _data(proof) if proof else {}
        if proof and pd.get("valid") and proof["serial"] == d.get("serial"):
            resolved_serial = d.get("serial")
            installed_at = d.get("installed_at") or idres["at"]
        else:
            blockers.append("identity_resolution_evidence_invalid")

    if resolved_serial:
        winners = holders_by_serial.get(resolved_serial, set())
        winner = contest_winner.get(resolved_serial)
        other_holders = {h for h in winners if h and h != order_id}
        if winner and winner != order_id:
            blockers.append("install_claim_rejected")
        elif other_holders and not winner:
            conflicts.append({"code": "serial_double_claim",
                              "serial": resolved_serial,
                              "orders": sorted(winners)})
            blockers.append("serial_double_claim")
        else:
            candidate = resolved_serial
    elif not my_serials:
        blockers.append("no_install_evidence")
    elif len(my_serials) > 1:
        blockers.append("ambiguous_install_serial")
    else:
        serial = next(iter(my_serials))
        holders = holders_by_serial.get(serial, set())
        others = {h for h in holders if h and h != order_id}
        winner = contest_winner.get(serial)
        if others and not winner:
            conflicts.append({"code": "serial_double_claim",
                              "serial": serial,
                              "orders": sorted(holders)})
            blockers.append("serial_double_claim")
        elif winner and winner != order_id:
            blockers.append("install_claim_rejected")
        elif len(valid_proof_serials) > 1:
            blockers.append("self_proof_conflict")
        elif valid_proof_serials and serial not in valid_proof_serials:
            blockers.append("identity_contradiction")
        elif has_offline_claim and not has_credential_claim \
                and serial not in valid_proof_serials:
            blockers.append("awaiting_device_self_proof")
        else:
            candidate = serial
            cr = one(
                db,
                "SELECT at,data FROM hr_evidence WHERE order_id=? "
                "AND kind IN ('install_receipt','offline_exchange') "
                "AND serial=? ORDER BY seq DESC LIMIT 1",
                (order_id, serial),
            )
            if cr:
                cd = _data(cr)
                installed_at = cd.get("exchanged_at") or cr["at"]

    post_ok = False
    if candidate:
        if not one(db, "SELECT serial FROM hr_devices WHERE serial=?",
                   (candidate,)):
            blockers.append("device_identity_missing")
            candidate = None
        else:
            for pe in rows(
                db,
                "SELECT data FROM hr_evidence WHERE order_id=? "
                "AND kind='post' AND serial=?",
                (order_id, candidate),
            ):
                pd = _data(pe)
                if pd.get("counts_as_passed", pd.get("passed")
                          and pd.get("compatible", True)):
                    post_ok = True
                    break
            if not post_ok:
                blockers.append("post_not_passed")

    recovered_at = None
    latest_biz = one(
        db,
        "SELECT * FROM hr_evidence WHERE order_id=? AND kind='business_test' "
        "ORDER BY at DESC, seq DESC LIMIT 1",
        (order_id,),
    )
    biz_accepted = False
    if latest_biz:
        bd = _data(latest_biz)
        if bd.get("passed") and bd.get("accepted_by"):
            biz_accepted = True
            recovered_at = latest_biz["at"]
        elif not bd.get("passed"):
            blockers.append("customer_retest_failed")
        else:
            blockers.append("awaiting_customer_acceptance")
    else:
        blockers.append("business_verification_missing")

    repaired = bool(candidate and post_ok and biz_accepted and not conflicts)

    if repaired:
        state = "customer_recovered"
    elif any(c["code"] == "serial_double_claim" for c in conflicts):
        state = "serial_claim_conflict"
    elif any(b in blockers for b in ("identity_contradiction",
                                     "ambiguous_install_serial",
                                     "self_proof_conflict")):
        state = "identity_conflict"
    elif "install_claim_rejected" in blockers:
        state = "claim_rejected"
    elif candidate is not None:
        state = "recovery_pending_verification"
    elif one(db, "SELECT id FROM hr_evidence WHERE order_id=? "
                  "AND kind='field_report'", (order_id,)):
        state = "recovery_pending_verification"
    elif _shipment_seen(db, order_id, node):
        state = "spare_dispatched_awaiting_install"
    else:
        state = "awaiting_spare"

    return {
        "decision": "adjudicated",
        "order_id": order_id,
        "node": node,
        "customer_repaired": repaired,
        "state": state,
        "installed_serial": candidate if not conflicts else None,
        "claimed_serials": sorted(my_serials),
        "proven_serials": sorted(valid_proof_serials),
        "recovered_at": recovered_at if repaired else None,
        "installed_at": installed_at,
        "post_passed": post_ok,
        "business_accepted": biz_accepted,
        "blockers": blockers,
        "conflicts": conflicts,
    }


def customer_repair_conclusion(db, order_id):
    """The authoritative new conclusion: true only after the matching
    manufacturing serial is installed, POST passes and the customer accepts."""
    return adjudicate(db, order_id)["customer_repaired"]


# Readable aliases.
is_customer_repaired = customer_repair_conclusion
repair_conclusion = customer_repair_conclusion
is_repaired = customer_repair_conclusion
repair_verdict = adjudicate
verdict = adjudicate


def current_customer_notice(db, order_id):
    """Notice the customer is contractually allowed to see right now.

    Until the authoritative conclusion is true the customer never receives a
    formal "repaired" notice.  Once a spare has been reserved/dispatched (the
    non-rollbackable field exchange window, including "installed but receipt
    not yet uploaded" after a crash), the only customer code is
    ``recovery_pending_verification``; the finer adjudication state is carried
    as ``substate`` for operators.
    """
    v = adjudicate(db, order_id)
    if v["customer_repaired"]:
        return {"code": "repaired", "at": v["recovered_at"],
                "order_id": order_id}
    if v["state"] != "awaiting_spare" or _shipment_seen(
            db, order_id, v["node"]):
        return {"code": "recovery_pending_verification",
                "order_id": order_id, "substate": v["state"],
                "blockers": v["blockers"]}
    return {"code": "repair_in_progress", "order_id": order_id,
            "substate": "awaiting_spare"}


def record_sent_notice(db, notice_id, order_id, kind, at, detail=None,
                       command_id=None):
    """Append-only record of a notice that was actually sent.

    A premature legacy "fixed" notice recorded here is evidence and is never
    rewritten; it simply does not determine :func:`customer_repair_conclusion`.
    """
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_notices WHERE id=?", (notice_id,)):
            return {"decision": "already_recorded", "notice_id": notice_id}
        db.execute("INSERT INTO hr_notices VALUES(?,?,?,?,?)",
                   (notice_id, order_id, kind, at,
                    json.dumps(detail or {}, ensure_ascii=False)))
        _timeline(db, order_id, at, "notice_sent", notice_id, {"kind": kind})
        return {"decision": "notice_recorded", "notice_id": notice_id,
                "kind": kind}

    return _transact(db, command_id, "record_sent_notice", at, work)


def sent_notices(db, order_id):
    init_db(db)
    return rows(db, "SELECT * FROM hr_notices WHERE order_id=? ORDER BY at,rowid",
                (order_id,))


def timeline(db, order_id):
    init_db(db)
    return rows(db, "SELECT * FROM hr_timeline WHERE order_id=? "
                    "ORDER BY at,seq", (order_id,))


# ---------------------------------------------------------------------------
# Independent customer credit (减免) ledger
# ---------------------------------------------------------------------------

def issue_credit(db, credit_id, order_id, amount, at, reason=None,
                 command_id=None):
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_credits WHERE id=?", (credit_id,)):
            return {"decision": "already_recorded", "credit_id": credit_id}
        if not one(db, "SELECT id FROM hr_orders WHERE id=?", (order_id,)):
            return {"decision": "unknown_order", "order_id": order_id}
        if amount is None or amount <= 0:
            return {"decision": "invalid_credit_amount"}
        db.execute("INSERT INTO hr_credits VALUES(?,?,?,?,?)",
                   (credit_id, order_id, amount, reason, at))
        _timeline(db, order_id, at, "customer_credit_issued", credit_id,
                  {"amount": amount, "reason": reason})
        return {"decision": "credit_issued", "credit_id": credit_id,
                "amount": amount}

    return _transact(db, command_id, "issue_credit", at, work)


def credit_total(db, order_id):
    init_db(db)
    return db.execute("SELECT COALESCE(SUM(amount),0) FROM hr_credits "
                      "WHERE order_id=?", (order_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# Independent vendor deposit / RMA / return / warranty settlement ledger
# ---------------------------------------------------------------------------

def pay_deposit(db, payment_id, vendor, amount, at, obligation=None,
                order_id=None, rma_id=None, serial=None, command_id=None):
    """Pay a vendor deposit.

    ``obligation`` is the idempotency key for the commercial obligation and
    defaults to ``deposit:<order_id>``.  Retrying with the same command id or
    payment id replays; a brand-new payment id against the same obligation is
    refused so a crash retry can never pay the deposit twice.
    """
    init_db(db)
    if obligation is None:
        if order_id is None:
            raise ValueError("obligation or order_id required")
        obligation = "deposit:%s" % order_id

    def work():
        existing = one(db, "SELECT * FROM hr_deposits WHERE id=?", (payment_id,))
        if existing:
            return {"decision": "deposit_paid", "payment_id": payment_id,
                    "obligation": existing["obligation"], "replayed": True}
        dup = one(db, "SELECT * FROM hr_deposits WHERE obligation=?",
                  (obligation,))
        if dup:
            return {"decision": "deposit_already_paid",
                    "payment_id": dup["id"], "obligation": obligation}
        db.execute("INSERT INTO hr_deposits VALUES(?,?,?,?,?,?,?,?)",
                   (payment_id, obligation, order_id, rma_id, vendor,
                    serial, amount, at))
        if order_id:
            _timeline(db, order_id, at, "vendor_deposit_paid", payment_id,
                      {"amount": amount, "vendor": vendor,
                       "obligation": obligation})
        return {"decision": "deposit_paid", "payment_id": payment_id,
                "obligation": obligation, "amount": amount}

    return _transact(db, command_id, "pay_deposit", at, work)


def ship_old_part(db, event_id, rma_id, at, carrier=None, tracking=None,
                  command_id=None):
    """旧件返厂交运 (distinct from authorised inbound receipt)."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_evidence WHERE id=?", (event_id,)):
            return {"decision": "already_recorded", "event_id": event_id}
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
        if not rma:
            return {"decision": "unknown_rma", "rma_id": rma_id}
        _record_evidence(db, event_id, "old_part_shipped", at,
                         order_id=rma["order_id"], serial=rma["old_serial"],
                         data={"rma_id": rma_id, "carrier": carrier,
                               "tracking": tracking})
        _timeline(db, rma["order_id"], at, "old_part_shipped_back", event_id,
                  {"rma_id": rma_id, "serial": rma["old_serial"]})
        return {"decision": "old_part_shipment_recorded", "event_id": event_id}

    return _transact(db, command_id, "ship_old_part", at, work)


def receive_old_part(db, receipt_id, rma_id, serial, at, warehouse="central",
                     received_by=None, command_id=None):
    """旧件按授权单入库: the inbound condition for deposit refund."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_old_part_receipts WHERE id=?",
               (receipt_id,)):
            return {"decision": "already_recorded", "receipt_id": receipt_id}
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
        if not rma:
            return {"decision": "unknown_rma", "rma_id": rma_id}
        lost = one(db, "SELECT id FROM hr_old_part_losses WHERE rma_id=?",
                   (rma_id,))
        if lost:
            return {"decision": "rma_marked_lost", "rma_id": rma_id}
        prior = one(db, "SELECT * FROM hr_old_part_receipts WHERE rma_id=?",
                    (rma_id,))
        if prior:
            return {"decision": "old_part_already_received",
                    "receipt_id": prior["id"]}
        if serial != rma["old_serial"]:
            _timeline(db, rma["order_id"], at, "old_part_rejected",
                       receipt_id, {"reason": "serial_mismatch",
                                   "presented": serial,
                                   "expected": rma["old_serial"]})
            return {"decision": "old_part_serial_mismatch",
                    "authorized_serial": rma["old_serial"],
                    "presented_serial": serial}
        db.execute("INSERT INTO hr_old_part_receipts VALUES(?,?,?,?,?,?)",
                   (receipt_id, rma_id, serial, warehouse, received_by, at))
        _timeline(db, rma["order_id"], at, "old_part_received_authorised",
                  receipt_id, {"rma_id": rma_id, "serial": serial,
                               "warehouse": warehouse})
        return {"decision": "old_part_received", "receipt_id": receipt_id,
                "rma_id": rma_id}

    return _transact(db, command_id, "receive_old_part", at, work)


def report_old_part_lost(db, loss_id, rma_id, at, detail=None,
                         command_id=None):
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_old_part_losses WHERE id=?", (loss_id,)):
            return {"decision": "already_recorded", "loss_id": loss_id}
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
        if not rma:
            return {"decision": "unknown_rma", "rma_id": rma_id}
        if one(db, "SELECT id FROM hr_old_part_receipts WHERE rma_id=?",
               (rma_id,)):
            return {"decision": "old_part_already_received"}
        db.execute("INSERT INTO hr_old_part_losses VALUES(?,?,?,?)",
                   (loss_id, rma_id, detail, at))
        _timeline(db, rma["order_id"], at, "old_part_lost", loss_id,
                  {"rma_id": rma_id, "detail": detail})
        return {"decision": "old_part_marked_lost", "loss_id": loss_id}

    return _transact(db, command_id, "report_old_part_lost", at, work)


def appraise_warranty(db, appraisal_id, rma_id, covered, at, reason=None,
                      appraiser="vendor", command_id=None):
    """Vendor warranty appraisal.

    Filing a *denied* reappraisal after a deposit was already refunded keeps
    the original payment and refund vouchers untouched and appends a clawback
    adjustment (鉴定翻案).
    """
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_appraisals WHERE id=?", (appraisal_id,)):
            return {"decision": "already_recorded",
                    "appraisal_id": appraisal_id}
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
        if not rma:
            return {"decision": "unknown_rma", "rma_id": rma_id}
        db.execute("INSERT INTO hr_appraisals VALUES(?,?,?,?,?,?)",
                   (appraisal_id, rma_id, 1 if covered else 0, reason,
                    appraiser, at))
        decision = "warranty_appraised_covered" if covered \
            else "warranty_appraised_denied"

        clawback_id = "clawback:%s" % appraisal_id
        if not covered:
            dep = one(db, "SELECT * FROM hr_deposits WHERE rma_id=? "
                          "ORDER BY at LIMIT 1", (rma_id,))
            if not dep:
                dep = one(db, "SELECT * FROM hr_deposits WHERE order_id=? "
                              "ORDER BY at LIMIT 1", (rma["order_id"],))
            refund = dep and one(
                db, "SELECT * FROM hr_deposit_refunds WHERE deposit_id=?",
                (dep["id"],))
            if refund and not one(
                db, "SELECT id FROM hr_settlement_adjustments "
                    "WHERE appraisal_id=?", (appraisal_id,)
            ):
                db.execute(
                    "INSERT INTO hr_settlement_adjustments"
                    "(id,deposit_id,rma_id,kind,amount,appraisal_id,at,reason) "
                    "VALUES(?,?,?,'refund_clawback',?,?,?,?)",
                    (clawback_id, dep["id"], rma_id, refund["amount"],
                     appraisal_id, at, reason))
                _timeline(db, rma["order_id"], at,
                          "deposit_refund_reversed_appraisal", clawback_id,
                          {"deposit_id": dep["id"],
                           "clawback_amount": refund["amount"],
                           "appraisal_id": appraisal_id})
                decision = "warranty_reversal_clawback_recorded"
        _timeline(db, rma["order_id"], at,
                  "warranty_covered" if covered else "warranty_denied",
                  appraisal_id, {"reason": reason})
        return {"decision": decision, "appraisal_id": appraisal_id,
                "covered": bool(covered)}

    return _transact(db, command_id, "appraise_warranty", at, work)


def _refund_blockers(db, deposit, rma):
    blockers = []
    rma_id = deposit["rma_id"] if deposit["rma_id"] else (
        rma["id"] if rma else None)
    if not rma_id:
        rma = one(db, "SELECT * FROM hr_rmas WHERE order_id=? "
                      "ORDER BY issued_at DESC LIMIT 1",
                  (deposit["order_id"],))
        rma_id = rma["id"] if rma else None
    if not rma_id:
        blockers.append("missing_authorisation_order")
        return blockers, None
    rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
    if one(db, "SELECT id FROM hr_old_part_losses WHERE rma_id=?", (rma_id,)):
        blockers.append("old_part_lost")
    elif not one(db, "SELECT id FROM hr_old_part_receipts WHERE rma_id=?",
                 (rma_id,)):
        blockers.append("old_part_not_received_per_authorisation")
    appraisal = one(
        db,
        "SELECT * FROM hr_appraisals WHERE rma_id=? "
        "ORDER BY at DESC, rowid DESC LIMIT 1",
        (rma_id,),
    )
    if not appraisal:
        blockers.append("warranty_appraisal_pending")
    elif not appraisal["covered"]:
        blockers.append("warranty_denied")
    return blockers, rma


def refund_deposit(db, refund_id, at, deposit_id=None, rma_id=None,
                   amount=None, command_id=None):
    """Refund the vendor deposit only after authorised inbound receipt of the
    old part and a current in-warranty appraisal."""
    init_db(db)

    def work():
        if one(db, "SELECT id FROM hr_deposit_refunds WHERE id=?", (refund_id,)):
            return {"decision": "already_recorded", "refund_id": refund_id}
        deposit = None
        if deposit_id:
            deposit = one(db, "SELECT * FROM hr_deposits WHERE id=?",
                          (deposit_id,))
        elif rma_id:
            deposit = one(db, "SELECT * FROM hr_deposits WHERE rma_id=?",
                          (rma_id,))
            if not deposit:
                rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
                if rma:
                    deposit = one(
                        db,
                        "SELECT * FROM hr_deposits WHERE order_id=? "
                        "ORDER BY at LIMIT 1", (rma["order_id"],))
        if not deposit:
            return {"decision": "deposit_not_found"}
        if one(db, "SELECT id FROM hr_deposit_refunds WHERE deposit_id=?",
               (deposit["id"],)):
            return {"decision": "deposit_already_refunded",
                    "deposit_id": deposit["id"]}
        blockers, rma = _refund_blockers(db, deposit,
                                        one(db, "SELECT * FROM hr_rmas WHERE id=?",
                                            (rma_id,)) if rma_id else None)
        if blockers:
            if deposit["order_id"]:
                _timeline(db, deposit["order_id"], at,
                          "deposit_refund_blocked", refund_id,
                          {"blockers": blockers})
            return {"decision": "refund_blocked", "refund_id": refund_id,
                    "deposit_id": deposit["id"], "blockers": blockers}
        refund_amount = deposit["amount"] if amount is None else amount
        db.execute("INSERT INTO hr_deposit_refunds VALUES(?,?,?,?)",
                   (refund_id, deposit["id"], refund_amount, at))
        if deposit["order_id"]:
            _timeline(db, deposit["order_id"], at, "vendor_deposit_refunded",
                      refund_id, {"amount": refund_amount})
        return {"decision": "deposit_refunded", "refund_id": refund_id,
                "deposit_id": deposit["id"], "amount": refund_amount}

    return _transact(db, command_id, "refund_deposit", at, work)


def settlement_status(db, rma_id=None, order_id=None):
    init_db(db)
    rma = None
    if rma_id:
        rma = one(db, "SELECT * FROM hr_rmas WHERE id=?", (rma_id,))
    elif order_id:
        rma = one(db, "SELECT * FROM hr_rmas WHERE order_id=? "
                      "ORDER BY issued_at DESC LIMIT 1", (order_id,))
        rma_id = rma["id"] if rma else None
    if order_id is None and rma:
        order_id = rma["order_id"]

    deposit = None
    if rma_id:
        deposit = one(db, "SELECT * FROM hr_deposits WHERE rma_id=? "
                          "ORDER BY at LIMIT 1", (rma_id,))
    if not deposit and order_id:
        deposit = one(db, "SELECT * FROM hr_deposits WHERE order_id=? "
                          "ORDER BY at LIMIT 1", (order_id,))

    result = {"decision": "settlement", "rma_id": rma_id,
              "order_id": order_id, "deposit_paid": False,
              "deposit_amount": 0, "refunded_amount": 0,
              "clawback_amount": 0, "state": "no_deposit",
              "blockers": []}
    if not deposit:
        if rma_id:
            appraisal = one(db, "SELECT covered FROM hr_appraisals "
                                "WHERE rma_id=? ORDER BY at DESC,rowid DESC LIMIT 1",
                            (rma_id,))
            result["latest_appraisal_covered"] = (
                None if not appraisal else bool(appraisal["covered"]))
        return result

    result["deposit_paid"] = True
    result["deposit_id"] = deposit["id"]
    result["deposit_amount"] = deposit["amount"]

    refund = one(db, "SELECT * FROM hr_deposit_refunds WHERE deposit_id=?",
                 (deposit["id"],))
    adjustments = rows(
        db,
        "SELECT * FROM hr_settlement_adjustments WHERE deposit_id=? "
        "ORDER BY at,rowid", (deposit["id"],))
    clawback = sum(a["amount"] for a in adjustments
                   if a["kind"] == "refund_clawback")
    result["adjustments"] = adjustments
    result["clawback_amount"] = clawback
    if refund:
        result["refund_id"] = refund["id"]
        result["refunded_amount"] = refund["amount"]

    blockers, _ = _refund_blockers(db, deposit, rma)
    result["blockers"] = blockers
    result["refund_allowed"] = not blockers and refund is None

    if refund and clawback:
        result["state"] = "refund_reversed_clawback_due"
        result["net_refunded"] = refund["amount"] - clawback
    elif refund:
        result["state"] = "refunded"
        result["net_refunded"] = refund["amount"]
    elif "old_part_lost" in blockers:
        result["state"] = "retained_old_part_lost"
    elif "warranty_denied" in blockers:
        result["state"] = "retained_warranty_denied"
    elif "old_part_not_received_per_authorisation" in blockers:
        result["state"] = "pending_old_part_return"
    elif "warranty_appraisal_pending" in blockers:
        result["state"] = "pending_warranty_appraisal"
    elif not blockers:
        result["state"] = "refund_due"
    else:
        result["state"] = "settlement_blocked"
    return result


# ---------------------------------------------------------------------------
# House-style op dispatcher (parity with intent/policy/enforce modules)
# ---------------------------------------------------------------------------

def run(db, payload):
    p = dict(payload)
    op = p.pop("op")
    fn = OPS.get(op)
    if fn is None:
        raise ValueError(op)
    return fn(db, **p)


OPS = {
    "register_warranty_contract": register_warranty_contract,
    "register_device": register_device,
    "set_node_firmware": set_node_firmware,
    "open_order": open_order,
    "reserve_spare": reserve_spare,
    "issue_install_credential": issue_install_credential,
    "ship_spare": ship_spare,
    "replace_box_label": replace_box_label,
    "carrier_signed": carrier_signed,
    "site_received": site_received,
    "unseal": unseal,
    "scan_manufacturing_serial": scan_manufacturing_serial,
    "record_field_report": record_field_report,
    "submit_install_receipt": submit_install_receipt,
    "issue_rma": issue_rma,
    "record_offline_exchange": record_offline_exchange,
    "submit_device_self_proof": submit_device_self_proof,
    "record_power_on_self_test": record_power_on_self_test,
    "record_business_verification": record_business_verification,
    "adjudicate_identity": adjudicate_identity,
    "resolve_serial_contest": resolve_serial_contest,
    "record_sent_notice": record_sent_notice,
    "issue_credit": issue_credit,
    "pay_deposit": pay_deposit,
    "ship_old_part": ship_old_part,
    "receive_old_part": receive_old_part,
    "report_old_part_lost": report_old_part_lost,
    "appraise_warranty": appraise_warranty,
    "refund_deposit": refund_deposit,
}
