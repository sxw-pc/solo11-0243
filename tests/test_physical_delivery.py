import unittest

from netcourt.store import connect
from netcourt.physical_route import record
from netcourt.delivery_order import provisional_delivery
from netcourt import physical_delivery as pd

# ---------------------------------------------------------------------------
# fixed field facts
#
# Carrier A's as-built drawing and carrier B's maintenance drawing label the
# same physical duct differently after the splice-box labels were replaced.
# The engineering visa maps both names onto one canonical segment, so merely
# intersecting the submitted duct strings cannot establish the real path.
# ---------------------------------------------------------------------------

TODAY = 20261126

SEG_SHARED = "seg-shared-duct-4"   # both provisional routes actually run here
SEG_SPARE = "seg-spare-core"       # the one genuinely independent spare core
SEG_CONTROL = "seg-control-link"   # customer B's proven original control link
SEG_T1 = "seg-temp-alpha"          # temporary carrier path 1
SEG_T2 = "seg-temp-bravo"          # temporary carrier path 2 (different carrier)
SEG_REBUILD = "seg-new-build"      # follow-up physically independent build

ROUTE_A = "carrier-a-17"
ROUTE_B = "carrier-b-88"
ROUTE_CONTROL = "carrier-b-control-9"
SPARE = "spare-core-1"

# spare core measured inside the safety boundary: 96 units; A peaks at 72,
# B's critical control stream needs 48 -> 72 + 48 > 96, never both.
CAP_SPARE = 96
PEAK_A = 72
NEED_B = 48


class PhysicalDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self._build_world()

    def tearDown(self):
        self.db.close()

    # -- construction helpers -------------------------------------------------

    def step(self, op, **payload):
        payload["op"] = op
        return pd.run(self.db, payload)

    def _build_world(self):
        """Record and adjudicate every route from full evidence bundles."""
        # engineering visas: relabelled splice boxes unify the two drawings;
        # every submitted label (ducts and end manholes) sits on one segment
        self.step(
            "register_visa", id="visa-4", old_name="duct-4",
            new_name="duct-4-renamed", segment=SEG_SHARED, at=TODAY - 30,
        )
        self.step(
            "register_visa", id="visa-well-x", old_name="well-x",
            new_name="well-x-m", segment=SEG_SHARED, at=TODAY - 30,
        )
        self.step(
            "register_visa", id="visa-well-y", old_name="well-y",
            new_name="well-y-m", segment=SEG_SHARED, at=TODAY - 30,
        )
        self.step(
            "register_visa", id="visa-spare", old_name="well-spare",
            new_name="spare-manhole", segment=SEG_SPARE, at=TODAY - 30,
        )
        self.step(
            "register_visa", id="visa-control", old_name="duct-ctrl",
            new_name="duct-ctrl-m", segment=SEG_CONTROL, at=TODAY - 30,
        )

        # right-of-way permits covering each physical segment
        self.step(
            "register_row_permit", id="row-shared", scope=SEG_SHARED,
            valid_from=TODAY - 60, valid_until=TODAY + 400,
        )
        self.step(
            "register_row_permit", id="row-spare", scope=SEG_SPARE,
            valid_from=TODAY - 60, valid_until=TODAY + 800,
        )
        self.step(
            "register_row_permit", id="row-control", scope=SEG_CONTROL,
            valid_from=TODAY - 60, valid_until=TODAY + 800,
        )

        # carrier field reports (the isolated enhanced-copy records)
        record(self.db, "svc-1", ROUTE_A, ["well-x", "duct-4"], 100)
        record(self.db, "svc-1", ROUTE_B, ["duct-4-renamed", "well-y"], 100)
        record(self.db, "svc-2", ROUTE_CONTROL, ["duct-ctrl"], 50)

        # raw OTDR curves identify the physical segment optically
        self.step(
            "register_otdr", route=ROUTE_A, signature="otdr-a-17",
            segment=SEG_SHARED, length=12.5,
        )
        self.step(
            "register_otdr", route=ROUTE_B, signature="otdr-b-88",
            segment=SEG_SHARED, length=12.4,
        )
        self.step(
            "register_otdr", route=ROUTE_CONTROL, signature="otdr-ctrl-9",
            segment=SEG_CONTROL, length=8.0,
        )

        # port photographs from both ends of every route
        for route in (ROUTE_A, ROUTE_B, ROUTE_CONTROL):
            self.step(
                "register_port_photo", id=f"photo-{route}-a", route=route,
                end="A-end", received_at=TODAY - 3,
            )
            self.step(
                "register_port_photo", id=f"photo-{route}-b", route=route,
                end="B-end", received_at=TODAY - 2,
            )

        # one genuinely independent spare core, 96 units measured
        self.step(
            "register_resource", id=SPARE, kind="spare_core",
            measured_capacity=CAP_SPARE, segment=SEG_SPARE,
        )

        # adjudicate entity paths using all four evidence kinds together
        for route, permit in (
            (ROUTE_A, "row-shared"),
            (ROUTE_B, "row-shared"),
            (ROUTE_CONTROL, "row-control"),
        ):
            result = self.step(
                "adjudicate_route", route=route, row_permit=permit, at=TODAY - 1
            )
            self.assertEqual(result["decision"], "verified", result)

        # customers: A peaks at 72; B needs 48 and requires isolation
        self.step(
            "register_customer", id="cust-a", name="customer-jia",
            peak=PEAK_A, isolation_required=False,
            degradation_deadline=TODAY + 30,
        )
        self.step(
            "register_customer", id="cust-b", name="customer-yi",
            peak=NEED_B, isolation_required=True,
            degradation_deadline=TODAY + 14,
        )

    def _spare_closure(self, at=TODAY):
        return pd.capacity_closure(self.db, SPARE, at)

    # -- 1. the original wrong conclusion, now corrected ----------------------

    def test_01_shared_duct_dual_name_was_deliverable_but_is_not_redundant(self):
        # the isolated enhanced copy still returns the wrong verdict ...
        self.assertEqual(
            provisional_delivery(self.db, "svc-1", 150),
            {"deliverable": True, "route_count": 2},
        )
        # ... because the two duct strings share no literal token ...
        # ... yet the visa/OTDR evidence rules them onto one physical segment
        finding = self.step("verify_redundancy", service="svc-1", at=TODAY)
        self.assertEqual(finding["decision"], "shared_failure_domain")
        self.assertEqual(finding["shared_segments"], [SEG_SHARED])
        self.assertEqual(finding["routes"], [ROUTE_A, ROUTE_B])

    def test_02_delivery_order_correction_keeps_recoverable_payment_trail(self):
        # the old erroneous orders were already promised to both customers
        self.step(
            "log_provisional_order", id="order-a-1", customer="cust-a",
            basis="route_name_count_and_nominal_capacity",
            decision="deliverable", at=TODAY - 5,
        )
        self.step(
            "log_provisional_order", id="order-b-1", customer="cust-b",
            basis="route_name_count_and_nominal_capacity",
            decision="deliverable", at=TODAY - 5,
        )
        # final instalments on those orders were already paid
        self.step(
            "register_payment", id="pay-a-1", customer="cust-a",
            order_ref="order-a-1", amount=500, basis="final_instalment",
            state="paid", at=TODAY - 4,
        )
        self.step(
            "register_payment", id="pay-b-1", customer="cust-b",
            order_ref="order-b-1", amount=700, basis="final_instalment",
            state="paid", at=TODAY - 4,
        )

        correction_a = self.step(
            "correct_delivery_order", id="order-a-1", new_id="order-a-2",
            claim_id="claim-a-1", service="svc-1", at=TODAY,
        )
        self.assertEqual(correction_a["decision"], "corrected")
        self.assertEqual(correction_a["finding"], "shared_failure_domain")
        self.assertEqual(correction_a["recoverable_payment"], "pay-a-1")

        # the old order is preserved but marked corrected and linked forward
        old = self.db.execute(
            "SELECT decision,replaced_by FROM pd_delivery_orders WHERE id='order-a-1'"
        ).fetchone()
        self.assertEqual(old[0], "corrected_shared_failure_domain")
        self.assertEqual(old[1], "order-a-2")
        # the recovery claim stays open until money actually comes back
        claim = self.db.execute(
            "SELECT state FROM pd_claims WHERE id='claim-a-1'"
        ).fetchone()
        self.assertEqual(claim[0], "open")

        # refunding closes the claim and flips the payment state
        refund = self.step("refund_payment", id="pay-a-1", at=TODAY + 2)
        self.assertEqual(refund["decision"], "refunded")
        self.assertEqual(refund["claim"], "claim-a-1")
        paid_state = self.db.execute(
            "SELECT state FROM pd_payments WHERE id='pay-a-1'"
        ).fetchone()[0]
        self.assertEqual(paid_state, "refunded")
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM pd_claims WHERE id='claim-a-1'"
            ).fetchone()[0],
            "recovered",
        )
        # a non-recoverable payment cannot be refunded
        self.assertEqual(
            self.step("refund_payment", id="pay-b-1", at=TODAY + 2)["decision"],
            "payment_not_recoverable",
        )

    # -- 2. plan A: A owns the spare core, B stays single-route discounted ----

    def test_03_cutover_spare_to_a_never_overbooks_spare(self):
        # B signs the time-limited single-route discount within its window
        self.step(
            "offer_discount", id="disc-b", customer="cust-b", amount=120,
            signed=True, at=TODAY,
        )
        result = self.step(
            "execute_cutover", id="cut-1", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b",
            window_start=TODAY, window_end=TODAY + 20,
            ledger_a="led-a", rebuild_id="rb-b",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(result["decision"], "cutover_complete")
        self.assertEqual(result["spare_holder"], "cust-a")
        self.assertEqual(result["discounted_customer"], "cust-b")
        self.assertIsNone(result["cutover_fee_customer"])
        self.assertEqual(result["rebuild_priority_customer"], "cust-b")

        # the same 96 units are reported once: 72 committed to A, not 72 + 48
        closure = result["closure_after"]
        self.assertTrue(closure["closed"])
        self.assertEqual(closure["committed"], PEAK_A)
        self.assertEqual(closure["occupied"], 0)
        self.assertEqual(closure["total_allocated"], PEAK_A)
        self.assertEqual(closure["headroom"], CAP_SPARE - PEAK_A)

        states = {row["customer"]: row for row in result["states"]}
        self.assertEqual(states["cust-a"]["state"], "spare_exclusive")
        self.assertEqual(states["cust-b"]["state"], "single_route_discounted")
        self.assertTrue(all(row["isolated"] for row in result["states"]))

    def test_04_rejecting_discount_blocks_plan_a(self):
        self.step(
            "offer_discount", id="disc-b", customer="cust-b", amount=120,
            signed=False, at=TODAY,
        )
        result = self.step(
            "execute_cutover", id="cut-1b", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b",
            window_start=TODAY, window_end=TODAY + 20,
            ledger_a="led-a2", rebuild_id="rb-b2",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(result["decision"], "customer_rejected_discount")
        # nothing was written: the spare core is untouched
        self.assertEqual(self._spare_closure()["total_allocated"], 0)
        self.assertIsNone(
            self.db.execute(
                "SELECT 1 FROM pd_service_states WHERE customer='cust-a'"
            ).fetchone()
        )

    def test_05_discount_after_degradation_deadline_is_rejected(self):
        self.step(
            "offer_discount", id="disc-b-late", customer="cust-b", amount=120,
            signed=True, at=TODAY + 20,
        )
        result = self.step(
            "execute_cutover", id="cut-late", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b-late",
            window_start=TODAY + 20, window_end=TODAY + 40,
            ledger_a="led-late", rebuild_id="rb-late",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY + 20,
        )
        # B's degradation window closes at TODAY+14
        self.assertEqual(result["decision"], "degradation_window_exceeded")

    # -- 3. plan B: B owns the spare core, A shards across two carriers -------

    def _prepare_temp_paths(self, lossless=True, capacities=(40, 40),
                            t1_until=TODAY + 40, t2_until=TODAY + 40):
        self.step(
            "register_row_permit", id="row-t1", scope=SEG_T1,
            valid_from=TODAY - 5, valid_until=t1_until,
        )
        self.step(
            "register_row_permit", id="row-t2", scope=SEG_T2,
            valid_from=TODAY - 5, valid_until=t2_until,
        )
        self.step(
            "register_temp_path", id="temp-1", carrier="carrier-x",
            segment=SEG_T1, capacity=capacities[0], row_permit="row-t1",
            lossless_remerge=lossless, at=TODAY,
        )
        self.step(
            "register_temp_path", id="temp-2", carrier="carrier-y",
            segment=SEG_T2, capacity=capacities[1], row_permit="row-t2",
            lossless_remerge=lossless, at=TODAY,
        )

    def test_06_cutover_spare_to_b_keeps_throughput_and_never_overbooks(self):
        self._prepare_temp_paths()
        result = self.step(
            "execute_cutover", id="cut-2", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-b", rebuild_id="rb-a",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            cutover_fee=90, fee_id="fee-a", at=TODAY,
        )
        self.assertEqual(result["decision"], "cutover_complete")
        self.assertEqual(result["spare_holder"], "cust-b")
        self.assertIsNone(result["discounted_customer"])
        self.assertEqual(result["cutover_fee_customer"], "cust-a")
        self.assertEqual(result["rebuild_priority_customer"], "cust-a")

        # only B's 48 sit on the 96-unit core; A's 72 rides the temp paths
        closure = result["closure_after"]
        self.assertTrue(closure["closed"])
        self.assertEqual(closure["committed"], NEED_B)
        self.assertEqual(closure["headroom"], CAP_SPARE - NEED_B)

        states = {row["customer"]: row for row in result["states"]}
        self.assertEqual(states["cust-a"]["state"], "temporary_sharded")
        self.assertEqual(states["cust-b"]["state"], "spare_exclusive")
        fee = self.db.execute(
            "SELECT basis,state FROM pd_payments WHERE id='fee-a'"
        ).fetchone()
        self.assertEqual(fee[0], "temporary_path_cutover_fee")
        self.assertEqual(fee[1], "accrued")

    def test_07_temp_paths_must_be_different_carriers_and_segments(self):
        # same carrier on both paths is not an acceptable shard
        self.step(
            "register_row_permit", id="row-t1", scope=SEG_T1,
            valid_from=TODAY - 5, valid_until=TODAY + 40,
        )
        self.step(
            "register_row_permit", id="row-t2", scope=SEG_T2,
            valid_from=TODAY - 5, valid_until=TODAY + 40,
        )
        self.step(
            "register_temp_path", id="temp-1", carrier="carrier-x",
            segment=SEG_T1, capacity=40, row_permit="row-t1",
            lossless_remerge=True, at=TODAY,
        )
        self.step(
            "register_temp_path", id="temp-2", carrier="carrier-x",
            segment=SEG_T2, capacity=40, row_permit="row-t2",
            lossless_remerge=True, at=TODAY,
        )
        result = self.step(
            "execute_cutover", id="cut-2bad", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-bad", rebuild_id="rb-bad",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(result["decision"], "temp_paths_same_carrier")
        self.assertEqual(self._spare_closure()["total_allocated"], 0)

    def test_08_lossy_remerge_and_insufficient_throughput_are_rejected(self):
        self._prepare_temp_paths(lossless=False)
        result = self.step(
            "execute_cutover", id="cut-lossy", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-lossy", rebuild_id="rb-lossy",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(result["decision"], "shards_cannot_remerge_losslessly")

        # even with lossless remerge, 30 + 30 < A's peak of 72
        self._prepare_temp_paths(lossless=True, capacities=(30, 30))
        result = self.step(
            "execute_cutover", id="cut-thin", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-thin", rebuild_id="rb-thin",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(result["decision"], "temp_throughput_insufficient")

    # -- 4. the single spare core can never serve both contracts --------------

    def test_09_spare_core_refuses_double_booking_and_priority_only_allocation(self):
        # A takes the spare under plan A ...
        self.step(
            "offer_discount", id="disc-b", customer="cust-b", amount=120,
            signed=True, at=TODAY,
        )
        first = self.step(
            "execute_cutover", id="cut-a", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b",
            window_start=TODAY, window_end=TODAY + 20,
            ledger_a="led-a", rebuild_id="rb-b",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(first["decision"], "cutover_complete")

        # ... a direct second reservation for B (48) must fail: 72 + 48 > 96
        blocked = self.step(
            "reserve_capacity", id="led-b-double", resource=SPARE,
            customer="cust-b", kind="committed", amount=NEED_B,
            start_at=TODAY + 1, end_at=TODAY + 19,
        )
        self.assertEqual(blocked["decision"], "capacity_overbooked")

        # and simply ranking B higher cannot manufacture capacity either
        ranked = self.step(
            "reserve_capacity", id="led-b-rank", resource=SPARE,
            customer="cust-b", kind="committed", amount=NEED_B,
            start_at=TODAY + 1, end_at=TODAY + 19,
        )
        self.assertEqual(ranked["decision"], "capacity_overbooked")

        # the ledger still shows the single truthful allocation of 72
        closure = self._spare_closure(TODAY + 5)
        self.assertEqual(closure["total_allocated"], PEAK_A)
        self.assertEqual(len(closure["entries"]), 1)

    def test_10_construction_hold_also_closes_against_live_commitments(self):
        # fill the spare with A's peak
        self.step(
            "reserve_capacity", id="led-a-fill", resource=SPARE,
            customer="cust-a", kind="committed", amount=PEAK_A,
            start_at=TODAY, end_at=TODAY + 30,
        )
        # a rebuild reservation that fits the 24-unit headroom is allowed ...
        ok = self.step(
            "reserve_capacity", id="hold-small", resource=SPARE,
            customer="cust-b", kind="construction_hold", amount=24,
            start_at=TODAY + 2, end_at=TODAY + 10,
        )
        self.assertEqual(ok["decision"], "reserved")
        self.assertTrue(ok["closure"]["closed"])
        self.assertEqual(
            ok["closure"]["construction_reserved"] + ok["closure"]["committed"],
            CAP_SPARE,
        )
        # ... one unit more breaks the same-window closure
        too_much = self.step(
            "reserve_capacity", id="hold-big", resource=SPARE,
            customer="cust-b", kind="construction_hold", amount=25,
            start_at=TODAY + 2, end_at=TODAY + 10,
        )
        self.assertEqual(too_much["decision"], "capacity_overbooked")

    # -- 5. evidence and timing edge cases ------------------------------------

    def test_11_late_port_photo_blocks_adjudication_until_it_arrives(self):
        db = connect(":memory:")
        def local_step(op, **payload):
            payload["op"] = op
            return pd.run(db, payload)
        local_step(
            "register_visa", id="visa-4", old_name="duct-4",
            new_name="duct-4-renamed", segment=SEG_SHARED, at=TODAY - 30,
        )
        local_step(
            "register_row_permit", id="row-shared", scope=SEG_SHARED,
            valid_from=TODAY - 60, valid_until=TODAY + 400,
        )
        record(db, "svc-9", "route-late", ["duct-4"], 100)
        local_step(
            "register_otdr", route="route-late", signature="otdr-late",
            segment=SEG_SHARED, length=3.0,
        )
        local_step(
            "register_port_photo", id="ph-late-a", route="route-late",
            end="A-end", received_at=TODAY - 4,
        )
        # only one end photographed at adjudication time
        missing = local_step(
            "adjudicate_route", route="route-late", row_permit="row-shared",
            at=TODAY - 1,
        )
        self.assertEqual(missing["decision"], "missing_port_photo")
        self.assertEqual(missing["received_ends"], ["A-end"])

        # the delayed B-end photo arrives; adjudication can now complete
        local_step(
            "register_port_photo", id="ph-late-b", route="route-late",
            end="B-end", received_at=TODAY,
        )
        verified = local_step(
            "adjudicate_route", route="route-late", row_permit="row-shared",
            at=TODAY,
        )
        self.assertEqual(verified["decision"], "verified")
        self.assertEqual(verified["segment"], SEG_SHARED)
        db.close()

    def test_12_maintenance_window_overlap_blocks_cutover_and_rebuild(self):
        self.step(
            "register_maintenance_window", id="mw-spare", scope=SEG_SPARE,
            start_at=TODAY + 5, end_at=TODAY + 12,
        )
        self.step(
            "offer_discount", id="disc-b", customer="cust-b", amount=120,
            signed=True, at=TODAY,
        )
        blocked = self.step(
            "execute_cutover", id="cut-mw", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b",
            window_start=TODAY + 6, window_end=TODAY + 18,
            ledger_a="led-mw", rebuild_id="rb-mw",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(blocked["decision"], "maintenance_window_conflict")

        # rebuild scheduling honours the same window on the spare segment
        self.step(
            "reserve_capacity", id="led-pre", resource=SPARE,
            customer="cust-a", kind="committed", amount=40,
            start_at=TODAY, end_at=TODAY + 30,
        )
        self.step(
            "register_row_permit", id="row-build", scope=SEG_REBUILD,
            valid_from=TODAY - 1, valid_until=TODAY + 200,
        )
        # the rebuild row must exist first (normally created by a cutover)
        self.db.execute(
            "INSERT INTO pd_rebuild_orders VALUES(?,?,?,?,?,?,?,?,?)",
            ("rb-x", "cust-b", SEG_REBUILD, "row-build", 100, "planned",
             TODAY, None, None),
        )
        blocked_build = self.step(
            "schedule_rebuild", id="rb-x", row_permit="row-build",
            target_segment=SEG_REBUILD, spare_resource=SPARE,
            spare_segment=SEG_SPARE, construction_hold=20, hold_id="hold-x",
            start_at=TODAY + 6, end_at=TODAY + 10, at=TODAY,
        )
        self.assertEqual(blocked_build["decision"], "maintenance_window_conflict")

    def test_13_revoked_or_expired_temp_row_blocks_cutover(self):
        self._prepare_temp_paths(t1_until=TODAY + 10)
        # permit for temp-1 is revoked after registration
        self.step("revoke_row_permit", id="row-t1")
        revoked = self.step(
            "execute_cutover", id="cut-rev", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-rev", rebuild_id="rb-rev",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(revoked["decision"], "row_permit_revoked")
        self.assertEqual(self._spare_closure()["total_allocated"], 0)

        # restore the permit but let it lapse before the cutover window ends
        self.db.execute(
            "UPDATE pd_row_permits SET revoked=0 WHERE id='row-t1'"
        )
        expired = self.step(
            "execute_cutover", id="cut-exp", plan="spare_to_b",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_route=ROUTE_CONTROL, a_temp_paths=["temp-1", "temp-2"],
            window_start=TODAY, window_end=TODAY + 20,
            ledger_b="led-exp", rebuild_id="rb-exp",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.assertEqual(expired["decision"], "temp_row_expires_before_window_end")

    def test_14_rebuild_schedules_with_hold_and_final_payment_needs_evidence(self):
        # run plan A so B gets the priority rebuild order
        self.step(
            "offer_discount", id="disc-b", customer="cust-b", amount=120,
            signed=True, at=TODAY,
        )
        self.step(
            "execute_cutover", id="cut-3", plan="spare_to_a",
            customer_a="cust-a", customer_b="cust-b", spare_resource=SPARE,
            b_control_route=ROUTE_CONTROL, b_discount="disc-b",
            window_start=TODAY, window_end=TODAY + 20,
            ledger_a="led-a3", rebuild_id="rb-b3",
            rebuild_segment=SEG_REBUILD, rebuild_row_permit="row-spare",
            at=TODAY,
        )
        self.step(
            "register_row_permit", id="row-build", scope=SEG_REBUILD,
            valid_from=TODAY - 1, valid_until=TODAY + 200,
        )
        # the new physical build earns its own full evidence bundle before
        # any final instalment can fall due
        self.step(
            "register_visa", id="visa-build", old_name="duct-new",
            new_name="duct-new-m", segment=SEG_REBUILD, at=TODAY + 10,
        )
        record(self.db, "svc-build", "route-build", ["duct-new"], 60)
        self.step(
            "register_otdr", route="route-build", signature="otdr-build",
            segment=SEG_REBUILD, length=11.0,
        )
        self.step(
            "register_port_photo", id="ph-build-a", route="route-build",
            end="A-end", received_at=TODAY + 20,
        )
        self.step(
            "register_port_photo", id="ph-build-b", route="route-build",
            end="B-end", received_at=TODAY + 20,
        )
        build_route = self.step(
            "adjudicate_route", route="route-build", row_permit="row-build",
            at=TODAY + 21,
        )
        self.assertEqual(build_route["decision"], "verified")

        scheduled = self.step(
            "schedule_rebuild", id="rb-b3", row_permit="row-build",
            target_segment=SEG_REBUILD, spare_resource=SPARE,
            spare_segment=SEG_SPARE, construction_hold=20, hold_id="hold-b3",
            start_at=TODAY + 5, end_at=TODAY + 12, at=TODAY + 1,
        )
        self.assertEqual(scheduled["decision"], "rebuild_scheduled")
        self.assertEqual(scheduled["priority"], 100)
        # live 72 + hold 20 = 92 <= 96, closed in the same overlapping window
        self.assertTrue(scheduled["closure"]["closed"])
        self.assertEqual(scheduled["closure"]["total_allocated"], 92)
        self.assertEqual(scheduled["closure"]["construction_reserved"], 20)

        # final instalment for the build: no customer-side measurement yet
        self.step(
            "register_payment", id="pay-build", customer="cust-b",
            order_ref="rb-b3", amount=300, basis="rebuild_final_instalment",
            state="accrued", at=TODAY + 30,
        )
        withheld = self.step(
            "certify_payment", id="pay-build", row_permit="row-build",
            route="route-build", customer_measured=False, at=TODAY + 30,
        )
        self.assertEqual(withheld["decision"], "payment_withheld_missing_customer_measurement")

        # an expired ROW also blocks the money independently
        self.step(
            "register_row_permit", id="row-old", scope=SEG_REBUILD,
            valid_from=TODAY - 100, valid_until=TODAY + 5,
        )
        row_blocked = self.step(
            "certify_payment", id="pay-build", row_permit="row-old",
            route="route-build", customer_measured=True, at=TODAY + 30,
        )
        self.assertEqual(row_blocked["decision"], "payment_withheld_row_invalid")

        # valid ROW + customer-side measurement on the verified route: due
        due = self.step(
            "certify_payment", id="pay-build", row_permit="row-build",
            route="route-build", customer_measured=True, at=TODAY + 30,
        )
        self.assertEqual(due["decision"], "payment_due")

    def test_15_otdr_and_visa_disagree_then_route_cannot_be_verified(self):
        # a curve claiming a different segment than the visa must not verify
        record(self.db, "svc-3", "route-contradiction", ["duct-4"], 100)
        self.step(
            "register_otdr", route="route-contradiction",
            signature="otdr-wrong", segment=SEG_SPARE, length=9.0,
        )
        self.step(
            "register_port_photo", id="ph-c-a", route="route-contradiction",
            end="A-end", received_at=TODAY - 2,
        )
        self.step(
            "register_port_photo", id="ph-c-b", route="route-contradiction",
            end="B-end", received_at=TODAY - 2,
        )
        result = self.step(
            "adjudicate_route", route="route-contradiction",
            row_permit="row-shared", at=TODAY,
        )
        self.assertEqual(result["decision"], "otdr_segment_mismatch")
        self.assertEqual(result["otdr_segment"], SEG_SPARE)
        self.assertEqual(result["visa_segment"], SEG_SHARED)


if __name__ == "__main__":
    unittest.main()
