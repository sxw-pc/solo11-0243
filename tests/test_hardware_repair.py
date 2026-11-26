"""Tests for evidence-bound hardware repair adjudication and settlement.

These tests pin the difference between the provisional warehouse rule
(any shipment row => "repaired") and the new conclusion, which is true only
when the manufacturing serial actually installed is proven and the customer
has accepted the recovered business service.
"""

import os
import tempfile
import unittest

from netcourt import hardware_repair as hr
from netcourt.store import connect as store_connect
from netcourt.spare_stock import dispatch
from netcourt.repair_note import provisional_repaired


def register_identity(db, serial="SN-REAL", hardware_version="HW-2.0",
                      firmware_compat=("fw-1.0", "fw-1.1"),
                      contract="W-VEND", vendor="VendorX",
                      faults=("reboot-loop",)):
    hr.register_warranty_contract(db, contract, vendor, list(faults))
    return hr.register_device(db, serial, hardware_version,
                              list(firmware_compat), contract)


def recovered_credential_order(db, oid="R1", node="edge-1", serial="SN-REAL",
                              accept_at=100, post_firmware="fw-1.1"):
    """Drive the reserve -> credential -> install -> POST -> acceptance path."""
    hr.open_order(db, oid, node, "reboot-loop", at=1)
    hr.reserve_spare(db, "res-" + oid, oid, serial, at=2)
    hr.issue_install_credential(db, "cred-" + oid, oid, serial, at=3)
    hr.ship_spare(db, "ship-" + oid, at=10, order_id=oid,
                  box_label="BOX-" + serial, reservation_id="res-" + oid)
    hr.submit_install_receipt(db, "ir-" + oid, oid, serial,
                              "cred-" + oid, installed_at=20, uploaded_at=21)
    hr.record_power_on_self_test(db, "post-" + oid, oid, serial, True, at=22,
                                 firmware=post_firmware)
    return hr.record_business_verification(
        db, "biz-" + oid, oid, True, at=accept_at, accepted_by="customer-ops")


class VerdictDifferenceTest(unittest.TestCase):
    def setUp(self):
        # Same database as the legacy netcourt modules so the contrast is
        # literal: one shipment table and one authoritative adjudication.
        self.db = store_connect(":memory:")
        hr.init_db(self.db)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS spare_shipments("
            "shipment TEXT PRIMARY KEY,serial TEXT,node TEXT,at INTEGER)")
        register_identity(self.db)

    def tearDown(self):
        self.db.close()

    def test_before_transport_after_label_swap_after_actual_install(self):
        db = self.db

        # --- 1) before transport -----------------------------------------
        hr.open_order(db, "R1", "edge-1", "reboot-loop", at=1)
        v_before = hr.adjudicate(db, "R1")
        self.assertFalse(v_before["customer_repaired"])
        self.assertEqual(v_before["state"], "awaiting_spare")
        self.assertIn("no_install_evidence", v_before["blockers"])
        # Legacy rule: no shipment row yet -> also false.
        self.assertFalse(provisional_repaired(db, "edge-1"))

        # The flawed isolated-copy flow dispatches "serial-a" and immediately
        # reports the node repaired, long before any real installation.
        dispatch(db, "ship-legacy", "serial-a", "edge-1", 10)
        self.assertTrue(provisional_repaired(db, "edge-1"))

        # --- 2) shipped, then box label replaced; nothing installed -------
        hr.reserve_spare(db, "res1", "R1", "SN-REAL", at=8)
        hr.issue_install_credential(db, "cred1", "R1", "SN-REAL", at=8)
        shipped = hr.ship_spare(db, "ship1", at=10, order_id="R1",
                                box_label="serial-a",
                                reservation_id="res1")
        self.assertEqual(shipped["decision"], "dispatched")
        swapped = hr.replace_box_label(
            db, "label-change1", "ship1", old_label="serial-a",
            new_label="serial-b", at=11, reason="torn warehouse label")
        self.assertEqual(swapped["decision"], "label_replaced")
        self.assertFalse(hr.customer_repair_conclusion(db, "R1"))
        v_label = hr.adjudicate(db, "R1")
        self.assertNotEqual(v_label["state"], "customer_recovered")
        # The provisional note has already been true since dispatch and the
        # label replacement cannot change that flaw.
        self.assertTrue(provisional_repaired(db, "edge-1"))

        # A premature "fixed" customer notice was really sent; it is retained
        # as original evidence but does not become the repair conclusion.
        hr.record_sent_notice(db, "notice-early-fixed", "R1", "fixed", at=12,
                              detail={"source": "provisional_repair_note"})

        # Rack scan reveals the manufacturing serial differs from BOTH labels.
        scan = hr.scan_manufacturing_serial(
            db, "scan1", "SN-REAL", at=19, node="edge-1", order_id="R1")
        self.assertEqual(scan["decision"], "scan_recorded")
        self.assertNotEqual("serial-b", "SN-REAL")

        # --- 3) actual installation of the matching manufacturing serial --
        hr.carrier_signed(db, "cs1", "ship1", at=10, signed_by="carrier")
        hr.site_received(db, "sr1", "ship1", at=15, signed_by="site-guard")
        hr.unseal(db, "us1", "ship1", at=18, by="technician")
        # Arrival + unseal + rack scan are still not installation/acceptance:
        # merely adding an arrival field to shipment status cannot close this.
        self.assertFalse(hr.customer_repair_conclusion(db, "R1"))
        self.assertIn("no_install_evidence",
                      hr.adjudicate(db, "R1")["blockers"])

        hr.submit_install_receipt(db, "ir1", "R1", "SN-REAL", "cred1",
                                  installed_at=20, uploaded_at=23)
        hr.record_power_on_self_test(db, "post1", "R1", "SN-REAL", True,
                                     at=21, firmware="fw-1.1")

        # Probe passes but customer has not formally accepted yet.
        hr.record_business_verification(db, "biz-probe", "R1", True, at=30)
        v_probe = hr.adjudicate(db, "R1")
        self.assertFalse(v_probe["customer_repaired"])
        self.assertIn("awaiting_customer_acceptance", v_probe["blockers"])
        self.assertEqual(
            hr.current_customer_notice(db, "R1")["code"],
            "recovery_pending_verification")

        # Real business acceptance fixes the recovery time.
        hr.record_business_verification(
            db, "biz-accept", "R1", True, at=40, accepted_by="customer-ops")
        v_after = hr.adjudicate(db, "R1")
        self.assertTrue(v_after["customer_repaired"])
        self.assertEqual(v_after["installed_serial"], "SN-REAL")
        self.assertEqual(v_after["recovered_at"], 40)
        self.assertEqual(
            hr.current_customer_notice(db, "R1")["code"], "repaired")

        # The old provisional note was true at t=10; the authoritative
        # conclusion only becomes true at t=40.
        self.assertTrue(provisional_repaired(db, "edge-1"))
        notices = hr.sent_notices(db, "R1")
        self.assertTrue(any(n["kind"] == "fixed" for n in notices))
        self.assertTrue(any(n["id"] == "notice-early-fixed" for n in notices))

    def test_wrong_serial_or_credential_mismatch_never_completes(self):
        register_identity(self.db, serial="SN-OTHER",
                          hardware_version="HW-9.0")
        hr.open_order(self.db, "R2", "edge-2", "reboot-loop", at=1)
        hr.reserve_spare(self.db, "res2", "R2", "SN-REAL", at=2)
        hr.issue_install_credential(self.db, "cred2", "R2", "SN-REAL", at=3)

        bad = hr.submit_install_receipt(
            self.db, "ir-bad", "R2", "SN-OTHER", "cred2",
            installed_at=20, uploaded_at=20)
        self.assertEqual(bad["decision"], "credential_serial_mismatch")

        # Even a recorded customer acceptance cannot repair an order with no
        # valid matching installation.
        hr.record_business_verification(
            self.db, "biz2", "R2", True, at=40, accepted_by="customer")
        v = hr.adjudicate(self.db, "R2")
        self.assertFalse(v["customer_repaired"])
        self.assertIn("no_install_evidence", v["blockers"])

        # The rejected attempt is on the audit timeline.
        kinds = [e["kind"] for e in hr.timeline(self.db, "R2")]
        self.assertIn("install_rejected", kinds)

    def test_pending_verification_notice_before_receipt_upload(self):
        hr.open_order(self.db, "R9", "edge-9", "reboot-loop", at=1)
        # No spare movement yet: nothing recovery-shaped to announce.
        self.assertEqual(
            hr.current_customer_notice(self.db, "R9")["code"],
            "repair_in_progress")
        hr.ship_spare(self.db, "ship9", at=5, order_id="R9",
                      box_label="serial-a")
        # Contract path A: from dispatch through missing offline receipt the
        # customer only ever sees "recovery pending verification".
        notice0 = hr.current_customer_notice(self.db, "R9")
        self.assertEqual(notice0["code"], "recovery_pending_verification")
        self.assertEqual(notice0["substate"],
                         "spare_dispatched_awaiting_install")
        # Technician verbally reports recovery while offline.
        hr.record_field_report(self.db, "fr9", "R9", at=20,
                               recovered_claimed=True, by="tech")
        notice = hr.current_customer_notice(self.db, "R9")
        self.assertEqual(notice["code"], "recovery_pending_verification")
        self.assertFalse(hr.customer_repair_conclusion(self.db, "R9"))


class SpareContestAndContradictionTest(unittest.TestCase):
    def setUp(self):
        self.db = hr.connect(":memory:")
        register_identity(self.db, serial="SN-PART")
        register_identity(self.db, serial="SN-X", hardware_version="HW-X")
        register_identity(self.db, serial="SN-Y", hardware_version="HW-Y")

    def tearDown(self):
        self.db.close()

    def test_central_reservation_rejects_second_order(self):
        hr.open_order(self.db, "R1", "edge-1", at=1)
        hr.open_order(self.db, "R2", "edge-1", at=1)
        first = hr.reserve_spare(self.db, "res1", "R1", "SN-PART", at=2)
        second = hr.reserve_spare(self.db, "res2", "R2", "SN-PART", at=3)
        self.assertEqual(first["decision"], "reserved")
        self.assertEqual(second["decision"], "spare_already_reserved")
        self.assertEqual(second["held_by_order"], "R1")

    def test_two_orders_grabbing_one_part_block_both_until_evidence_resolves(self):
        db = self.db
        hr.open_order(db, "R1", "edge-1", "reboot-loop", at=1)
        hr.open_order(db, "R2", "edge-1", "reboot-loop", at=1)
        hr.issue_rma(db, "RMA1", "R1", "VendorX", "OLD-1", at=2,
                     signature="vendor-offline-seal-1")
        hr.issue_rma(db, "RMA2", "R2", "VendorX", "OLD-2", at=2,
                     signature="vendor-offline-seal-2")

        # Both technicians offline-swap in the same manufacturing serial.
        hr.record_offline_exchange(db, "OE1", "R1", "SN-PART", "OLD-1",
                                   "RMA1", exchanged_at=50)
        hr.record_offline_exchange(db, "OE2", "R2", "SN-PART", "OLD-2",
                                   "RMA2", exchanged_at=51)
        # Both even obtain customer acceptance -- still neither may close.
        hr.record_business_verification(db, "B1", "R1", True, at=60,
                                        accepted_by="cust1")
        hr.record_business_verification(db, "B2", "R2", True, at=61,
                                        accepted_by="cust2")

        for oid in ("R1", "R2"):
            v = hr.adjudicate(db, oid)
            self.assertFalse(v["customer_repaired"], oid)
            self.assertIn("serial_double_claim", v["blockers"], oid)
            self.assertEqual(v["state"], "serial_claim_conflict")

        # Device self-proof pins SN-PART to edge-1 and the passing probe of
        # R1 lets the contest be adjudicated to the real installation.
        proof = hr.submit_device_self_proof(
            db, "PF1", "SN-PART", at=55, node="edge-1", order_id="R1",
            hardware_version="HW-2.0", firmware="fw-1.1")
        self.assertTrue(proof["valid"])
        resolved = hr.resolve_serial_contest(
            db, "RC1", "SN-PART", "R1", "PF1", at=70, actor="coordinator")
        self.assertEqual(resolved["decision"], "contest_resolved")
        self.assertEqual(resolved["rejected_orders"], ["R2"])

        # Winner still needs its own POST for the proven serial.
        hr.record_power_on_self_test(db, "P1", "R1", "SN-PART", True, at=56,
                                     firmware="fw-1.1")
        v1 = hr.adjudicate(db, "R1")
        v2 = hr.adjudicate(db, "R2")
        self.assertTrue(v1["customer_repaired"])
        self.assertEqual(v1["installed_serial"], "SN-PART")
        self.assertEqual(v1["recovered_at"], 60)
        self.assertFalse(v2["customer_repaired"])
        self.assertIn("install_claim_rejected", v2["blockers"])

    def test_offline_receipt_contradicts_device_self_proof(self):
        db = self.db
        hr.open_order(db, "R3", "edge-3", "reboot-loop", at=1)
        hr.issue_rma(db, "RMA3", "R3", "VendorX", "OLD-3", at=2,
                     signature="vendor-offline-seal-3")
        # Offline receipt claims SN-X ...
        hr.record_offline_exchange(db, "OE3", "R3", "SN-X", "OLD-3",
                                   "RMA3", exchanged_at=50)
        # ... but the device in the rack proves it is SN-Y.
        pf = hr.submit_device_self_proof(
            db, "PF3", "SN-Y", at=52, node="edge-3", order_id="R3",
            hardware_version="HW-Y", firmware="fw-1.0")
        self.assertEqual(pf["decision"], "self_proof_verified")
        hr.record_business_verification(db, "B3", "R3", True, at=60,
                                        accepted_by="customer")

        v = hr.adjudicate(db, "R3")
        self.assertFalse(v["customer_repaired"])
        self.assertIn("identity_contradiction", v["blockers"])
        self.assertEqual(v["state"], "identity_conflict")

        # An invalid proof (wrong hardware) cannot resolve anything.
        bad_pf = hr.submit_device_self_proof(
            db, "PF3B", "SN-Y", at=53, node="edge-3", order_id="R3",
            hardware_version="HW-FAKE")
        self.assertFalse(bad_pf["valid"])
        bad_res = hr.adjudicate_identity(
            db, "IDR-BAD", "R3", "SN-Y", "PF3B", at=70)
        self.assertEqual(bad_res["decision"], "valid_self_proof_required")

        # Adjudication against the valid proof reverse-aligns warehouse/RMA.
        ok = hr.adjudicate_identity(
            db, "IDR3", "R3", "SN-Y", "PF3", at=71, installed_at=50)
        self.assertEqual(ok["decision"], "identity_resolved")
        hr.record_power_on_self_test(db, "P3", "R3", "SN-Y", True, at=54,
                                     firmware="fw-1.0")
        v2 = hr.adjudicate(db, "R3")
        self.assertTrue(v2["customer_repaired"])
        self.assertEqual(v2["installed_serial"], "SN-Y")
        self.assertEqual(v2["recovered_at"], 60)

    def test_unregistered_offline_serial_rejected(self):
        hr.open_order(self.db, "R4", "edge-4", at=1)
        hr.issue_rma(self.db, "RMA4", "R4", "V", "OLD4", at=2, signature="s")
        res = hr.record_offline_exchange(
            self.db, "OE4", "R4", "SN-GHOST", "OLD4", "RMA4", exchanged_at=3)
        self.assertEqual(res["decision"], "unknown_device_serial")
        self.assertFalse(hr.customer_repair_conclusion(self.db, "R4"))


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "crash.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_crash_before_receipt_path_a_never_redispatch_or_double_pay(self):
        db = hr.connect(self.path)
        register_identity(db)
        hr.open_order(db, "R1", "edge-1", "reboot-loop", at=1)
        hr.reserve_spare(db, "res1", "R1", "SN-REAL", at=2,
                         command_id="cmd-reserve")
        hr.issue_install_credential(db, "cred1", "R1", "SN-REAL", at=3,
                                    command_id="cmd-cred")
        hr.ship_spare(db, "ship1", at=10, order_id="R1", box_label="serial-a",
                      reservation_id="res1", command_id="cmd-ship")
        hr.pay_deposit(db, "PAY1", "VendorX", 500, at=11, order_id="R1",
                       command_id="cmd-pay")
        # Technician installed but the install receipt was never uploaded.
        db.close()  # coordinator process dies

        # ---- restart: brand new connection/process on the same file ------
        db = hr.connect(self.path)
        try:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_deposits").fetchone()[0], 1)
            self.assertFalse(hr.customer_repair_conclusion(db, "R1"))
            self.assertIn("no_install_evidence",
                          hr.adjudicate(db, "R1")["blockers"])
            self.assertEqual(
                hr.one(db, "SELECT status FROM hr_credentials WHERE id=?",
                       ("cred1",))["status"], "issued")

            # Command retries with the same ids replay stored results.
            replay_ship = hr.ship_spare(
                db, "ship1", at=10, order_id="R1", box_label="serial-a",
                reservation_id="res1", command_id="cmd-ship")
            self.assertEqual(replay_ship["decision"], "dispatched")
            replay_pay = hr.pay_deposit(
                db, "PAY1", "VendorX", 500, at=11, order_id="R1",
                command_id="cmd-pay")
            self.assertEqual(replay_pay["decision"], "deposit_paid")
            # Natural primary-key replay also reports the existing payment.
            natural_pay = hr.pay_deposit(db, "PAY1", "VendorX", 500, at=11,
                                         order_id="R1")
            self.assertTrue(natural_pay.get("replayed"))

            # A naïve retry with a NEW payment id but the same obligation is
            # still refused: crash retries never pay the deposit twice.
            pay2 = hr.pay_deposit(db, "PAY2", "VendorX", 500, at=12,
                                  order_id="R1")
            self.assertEqual(pay2["decision"], "deposit_already_paid")

            # Natural idempotency on the shipment id as well.
            again = hr.ship_spare(db, "ship1", at=10, order_id="R1",
                                  box_label="serial-a")
            self.assertEqual(again["decision"], "already_recorded")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_deposits").fetchone()[0], 1)

            # Technician re-submits the same signed receipt once connectivity
            # returns; duplicate submission does not spend credential twice.
            first = hr.submit_install_receipt(
                db, "ir1", "R1", "SN-REAL", "cred1",
                installed_at=20, uploaded_at=30, command_id="cmd-ir")
            self.assertEqual(first["decision"], "install_recorded")
            second = hr.submit_install_receipt(
                db, "ir1", "R1", "SN-REAL", "cred1",
                installed_at=20, uploaded_at=31, command_id="cmd-ir")
            self.assertEqual(second["decision"], "install_recorded")  # cached
            third = hr.submit_install_receipt(
                db, "ir1", "R1", "SN-REAL", "cred1",
                installed_at=20, uploaded_at=31)
            self.assertEqual(third["decision"], "already_recorded")
            self.assertEqual(
                hr.one(db, "SELECT status FROM hr_credentials WHERE id=?",
                       ("cred1",))["status"], "used")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_evidence "
                           "WHERE kind='install_receipt'").fetchone()[0], 1)

            hr.record_power_on_self_test(db, "post1", "R1", "SN-REAL", True,
                                         at=21, firmware="fw-1.1")
            hr.record_business_verification(
                db, "biz1", "R1", True, at=40, accepted_by="customer")
            self.assertTrue(hr.customer_repair_conclusion(db, "R1"))
            # Still exactly one shipment after full recovery.
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)
        finally:
            db.close()

    def test_restart_sees_shipment_and_two_orders_then_path_b_reconciles(self):
        db = hr.connect(self.path)
        register_identity(db, serial="SN-REAL")
        hr.open_order(db, "R1", "edge-1", at=1)
        hr.open_order(db, "R2", "edge-1", at=1)
        # Node-level shipment like the old spare_stock.dispatch record.
        hr.ship_spare(db, "ship-node", at=10, node="edge-1",
                      box_label="serial-a", command_id="cmd-ship")
        db.close()

        db = hr.connect(self.path)
        try:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_orders").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)
            for oid in ("R1", "R2"):
                v = hr.adjudicate(db, oid)
                self.assertFalse(v["customer_repaired"])
                self.assertEqual(v["state"],
                                 "spare_dispatched_awaiting_install")

            # Retrying the coordinator dispatch does not ship again.
            hr.ship_spare(db, "ship-node", at=10, node="edge-1",
                          box_label="serial-a", command_id="cmd-ship")
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)

            # Path B: vendor offline seals, two competing exchange claims,
            # then self-proof + customer probe reverse-align reality.
            hr.issue_rma(db, "RMA1", "R1", "VendorX", "OLD1", at=11,
                         signature="seal1")
            hr.issue_rma(db, "RMA2", "R2", "VendorX", "OLD2", at=11,
                         signature="seal2")
            hr.record_offline_exchange(db, "OE1", "R1", "SN-REAL", "OLD1",
                                       "RMA1", exchanged_at=20)
            hr.record_offline_exchange(db, "OE2", "R2", "SN-REAL", "OLD2",
                                       "RMA2", exchanged_at=21)
            hr.submit_device_self_proof(db, "PF1", "SN-REAL", at=22,
                                        node="edge-1", order_id="R1",
                                        hardware_version="HW-2.0",
                                        firmware="fw-1.1")
            hr.record_business_verification(db, "B1", "R1", True, at=23,
                                           accepted_by="cust")
            hr.resolve_serial_contest(db, "RC1", "SN-REAL", "R1", "PF1", at=30)
            hr.record_power_on_self_test(db, "P1", "R1", "SN-REAL", True,
                                         at=24, firmware="fw-1.1")
            self.assertTrue(hr.customer_repair_conclusion(db, "R1"))
            self.assertFalse(hr.customer_repair_conclusion(db, "R2"))
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hr_shipments").fetchone()[0], 1)
        finally:
            db.close()


class WarrantySettlementTest(unittest.TestCase):
    def setUp(self):
        self.db = hr.connect(":memory:")
        register_identity(self.db)
        recovered_credential_order(self.db)
        hr.issue_rma(self.db, "RMA1", "R1", "VendorX", "OLD-1", at=15,
                     signature="vendor-offline-seal")
        hr.pay_deposit(self.db, "PAY1", "VendorX", 500, at=16,
                       order_id="R1", rma_id="RMA1")

    def tearDown(self):
        self.db.close()

    def test_vendor_denial_blocks_refund_but_not_recovery_or_credit(self):
        db = self.db
        self.assertTrue(hr.customer_repair_conclusion(db, "R1"))
        hr.ship_old_part(db, "OLD-SHIP1", "RMA1", at=150, carrier="freight")
        hr.receive_old_part(db, "RC1", "RMA1", "OLD-1", at=180,
                            warehouse="vendor-inbound")
        denied = hr.appraise_warranty(
            db, "AP1", "RMA1", covered=False, at=190,
            reason="physical impact damage excluded")
        self.assertEqual(denied["decision"], "warranty_appraised_denied")

        blocked = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertEqual(blocked["decision"], "refund_blocked")
        self.assertIn("warranty_denied", blocked["blockers"])
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM hr_deposit_refunds").fetchone()[0],
            0)

        s = hr.settlement_status(db, rma_id="RMA1")
        self.assertEqual(s["state"], "retained_warranty_denied")
        self.assertFalse(s["refund_allowed"])

        # Customer relief (减免) is an independent obligation: it is issued
        # and remains valid regardless of the vendor warranty outcome.
        credit = hr.issue_credit(db, "CR1", "R1", 120, at=201,
                                 reason="outage goodwill")
        self.assertEqual(credit["decision"], "credit_issued")
        again = hr.issue_credit(db, "CR1", "R1", 120, at=201)
        self.assertEqual(again["decision"], "already_recorded")
        self.assertEqual(hr.credit_total(db, "R1"), 120)

        # Customer recovery stays true; vendor settlement failure does not
        # reopen the customer conclusion.
        self.assertTrue(hr.customer_repair_conclusion(db, "R1"))

    def test_old_part_lost_blocks_deposit_refund(self):
        db = self.db
        lost = hr.report_old_part_lost(
            db, "LOSS1", "RMA1", at=170, detail="carrier warehouse fire")
        self.assertEqual(lost["decision"], "old_part_marked_lost")
        hr.appraise_warranty(db, "AP1", "RMA1", covered=True, at=190,
                             reason="known factory defect")

        blocked = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertEqual(blocked["decision"], "refund_blocked")
        self.assertIn("old_part_lost", blocked["blockers"])
        s = hr.settlement_status(db, rma_id="RMA1")
        self.assertEqual(s["state"], "retained_old_part_lost")

        # A part reported lost cannot later be inbound-received on that RMA.
        recv = hr.receive_old_part(db, "RC1", "RMA1", "OLD-1", at=210)
        self.assertEqual(recv["decision"], "rma_marked_lost")
        self.assertTrue(hr.customer_repair_conclusion(db, "R1"))

    def test_refund_then_reappraisal_denial_keeps_vouchers_and_claws_back(self):
        db = self.db
        hr.receive_old_part(db, "RC1", "RMA1", "OLD-1", at=180)
        hr.appraise_warranty(db, "AP1", "RMA1", covered=True, at=190,
                             reason="covered batch defect")
        refund = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertEqual(refund["decision"], "deposit_refunded")
        self.assertEqual(refund["amount"], 500)
        self.assertEqual(
            hr.settlement_status(db, rma_id="RMA1")["state"], "refunded")

        # 鉴定翻案: later lab appraisal reverses coverage.
        reversal = hr.appraise_warranty(
            db, "AP2", "RMA1", covered=False, at=220,
            reason="deeper inspection found liquid damage")
        self.assertEqual(reversal["decision"],
                         "warranty_reversal_clawback_recorded")

        # Original payment and refund evidence is untouched.
        pay = hr.one(db, "SELECT * FROM hr_deposits WHERE id='PAY1'")
        self.assertEqual(pay["amount"], 500)
        rf = hr.one(db, "SELECT * FROM hr_deposit_refunds WHERE id='RF1'")
        self.assertEqual(rf["amount"], 500)
        appraisals = hr.rows(
            db, "SELECT covered FROM hr_appraisals WHERE rma_id='RMA1' "
                "ORDER BY at")
        self.assertEqual([a["covered"] for a in appraisals], [1, 0])

        s = hr.settlement_status(db, rma_id="RMA1")
        self.assertEqual(s["state"], "refund_reversed_clawback_due")
        self.assertEqual(s["refunded_amount"], 500)   # original voucher kept
        self.assertEqual(s["clawback_amount"], 500)
        self.assertEqual(s["net_refunded"], 0)

        # No second refund can be issued for the same deposit.
        again = hr.refund_deposit(db, "RF2", at=230, rma_id="RMA1")
        self.assertEqual(again["decision"], "deposit_already_refunded")
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM hr_deposit_refunds").fetchone()[0],
            1)

    def test_refund_requires_all_three_conditions(self):
        db = self.db
        # Nothing returned or appraised yet.
        blocked = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertEqual(blocked["decision"], "refund_blocked")
        self.assertIn("old_part_not_received_per_authorisation",
                      blocked["blockers"])
        self.assertIn("warranty_appraisal_pending", blocked["blockers"])

        # Wrong old serial cannot satisfy authorised inbound.
        bad = hr.receive_old_part(db, "RC-BAD", "RMA1", "OLD-WRONG", at=180)
        self.assertEqual(bad["decision"], "old_part_serial_mismatch")

        hr.receive_old_part(db, "RC1", "RMA1", "OLD-1", at=181)
        still = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertIn("warranty_appraisal_pending", still["blockers"])

        hr.appraise_warranty(db, "AP1", "RMA1", covered=True, at=190)
        ok = hr.refund_deposit(db, "RF1", at=200, rma_id="RMA1")
        self.assertEqual(ok["decision"], "deposit_refunded")
        self.assertEqual(
            hr.settlement_status(db, rma_id="RMA1")["state"], "refunded")


class RetestAndIdentityDetailTest(unittest.TestCase):
    def setUp(self):
        self.db = hr.connect(":memory:")
        register_identity(self.db)

    def tearDown(self):
        self.db.close()

    def test_customer_failed_retest_overturns_then_new_acceptance_restores(self):
        recovered_credential_order(self.db, accept_at=100)
        self.assertTrue(hr.customer_repair_conclusion(self.db, "R1"))
        self.assertEqual(
            hr.adjudicate(self.db, "R1")["recovered_at"], 100)

        failed = hr.record_business_verification(
            self.db, "biz-fail", "R1", False, at=110,
            metrics={"packet_loss": 0.4})
        self.assertEqual(failed["decision"], "retest_failed")
        v = hr.adjudicate(self.db, "R1")
        self.assertFalse(v["customer_repaired"])
        self.assertIn("customer_retest_failed", v["blockers"])
        self.assertIsNone(v["recovered_at"])

        # A later passing re-acceptance establishes a new recovery time.
        hr.record_business_verification(
            self.db, "biz-again", "R1", True, at=120,
            accepted_by="customer-sre")
        v2 = hr.adjudicate(self.db, "R1")
        self.assertTrue(v2["customer_repaired"])
        self.assertEqual(v2["recovered_at"], 120)

        rows = hr.rows(
            self.db,
            "SELECT id FROM hr_evidence WHERE order_id='R1' "
            "AND kind='business_test' ORDER BY at")
        self.assertEqual([r["id"] for r in rows],
                         ["biz-R1", "biz-fail", "biz-again"])

    def test_firmware_compatibility_gates_post(self):
        hr.open_order(self.db, "RF", "edge-f", at=1)
        hr.reserve_spare(self.db, "resf", "RF", "SN-REAL", at=2)
        hr.issue_install_credential(self.db, "cred-f", "RF", "SN-REAL", at=3)
        hr.submit_install_receipt(self.db, "irf", "RF", "SN-REAL", "cred-f",
                                  installed_at=20)
        bad = hr.record_power_on_self_test(
            self.db, "post-bad", "RF", "SN-REAL", True, at=21,
            firmware="fw-0.1-unsupported")
        self.assertEqual(bad["decision"], "firmware_incompatible")
        hr.record_business_verification(
            self.db, "bizf", "RF", True, at=30, accepted_by="c")
        v = hr.adjudicate(self.db, "RF")
        self.assertFalse(v["customer_repaired"])
        self.assertIn("post_not_passed", v["blockers"])

        hr.record_power_on_self_test(
            self.db, "post-ok", "RF", "SN-REAL", True, at=22,
            firmware="fw-1.0")
        self.assertTrue(hr.customer_repair_conclusion(self.db, "RF"))

    def test_all_physical_and_return_events_are_distinct(self):
        hr.open_order(self.db, "RE", "edge-e", at=1)
        hr.reserve_spare(self.db, "rese", "RE", "SN-REAL", at=2)
        hr.issue_install_credential(self.db, "crede", "RE", "SN-REAL", at=3)
        hr.ship_spare(self.db, "she", at=4, order_id="RE",
                      reservation_id="rese", box_label="L")
        hr.carrier_signed(self.db, "cs", "she", at=5)
        hr.site_received(self.db, "sr", "she", at=6)
        hr.unseal(self.db, "us", "she", at=7)
        hr.submit_install_receipt(self.db, "ire", "RE", "SN-REAL", "crede",
                                  installed_at=8)
        hr.record_power_on_self_test(self.db, "pe", "RE", "SN-REAL", True,
                                     at=9, firmware="fw-1.1")
        hr.record_business_verification(self.db, "be", "RE", True, at=10,
                                       accepted_by="c")
        hr.issue_rma(self.db, "RMAE", "RE", "V", "OLDE", at=7)
        hr.ship_old_part(self.db, "soe", "RMAE", at=11)
        hr.receive_old_part(self.db, "roe", "RMAE", "OLDE", at=12)

        kinds = {e["kind"] for e in hr.timeline(self.db, "RE")}
        for expected in (
            "carrier_signed",            # 承运签收
            "site_received",             # 现场签收
            "unsealed",                  # 现场拆封
            "installed",                 # 安装
            "post_passed",               # 通电自检
            "business_accepted",         # 客户业务验收
            "old_part_shipped_back",     # 旧件返厂
            "old_part_received_authorised",  # 按授权单入库
        ):
            self.assertIn(expected, kinds)

        # Evidence kinds are separately queryable (events are not collapsed).
        ekinds = {r["kind"] for r in hr.rows(
            self.db, "SELECT DISTINCT kind FROM hr_evidence WHERE order_id='RE'")}
        for expected in ("carrier_signed", "site_received", "unsealed",
                         "install_receipt", "post", "business_test",
                         "old_part_shipped"):
            self.assertIn(expected, ekinds)

    def test_device_identity_is_fixed_by_four_parts(self):
        # Re-registering the same identity is idempotent; changing any fixed
        # part is an identity conflict rather than a silent update.
        same = register_identity(self.db)
        self.assertEqual(same["decision"], "already_registered")
        changed_hw = hr.register_device(
            self.db, "SN-REAL", "HW-3.0-NEW", ["fw-1.1"], "W-VEND")
        self.assertEqual(changed_hw["decision"], "identity_conflict")
        dev = hr.one(self.db, "SELECT * FROM hr_devices WHERE serial='SN-REAL'")
        self.assertEqual(dev["hardware_version"], "HW-2.0")


if __name__ == "__main__":
    unittest.main()
