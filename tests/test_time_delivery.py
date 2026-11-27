import json
import unittest

from netcourt.store import connect
from netcourt.clock_probe import sample
from netcourt.time_note import provisional_accurate
from netcourt import time_delivery as td
from netcourt.store import one, rows


def exchange(offset_us, rtt_us):
    """Build a two-way (t1..t4) exchange with the given offset and RTT."""
    fwd = rtt_us // 2
    rev = rtt_us - fwd
    t1 = 1_000_000
    t2 = t1 + fwd + offset_us
    t3 = t2 + 5
    t4 = t3 + rev - offset_us
    return t1, t2, t3, t4


def path(*segments):
    return [{"id": seg, "shared": seg == "fault-segment"} for seg in segments]


class TimeDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        td.init(self.db)
        self.cal = 0

    def tearDown(self):
        self.db.close()

    def step(self, **x):
        return td.run(self.db, x)

    def source(self, sid, kind="satellite", chain=None, leap="leap-1", detail=None):
        return self.step(
            op="register_source", id=sid, kind=kind,
            auth_chain=chain or ["root-" + kind, sid], leap_table=leap,
            detail=detail or {},
        )

    def measure(self, sid, node, offset, rtt, start, end, asym=10,
                segments=None, leap="leap-1", late=False, received_at=None,
                drift=0.2, budget=500, osc_state=None, noise=0,
                asym_validated=True):
        self.cal += 1
        t1, t2, t3, t4 = exchange(offset, rtt)
        return self.step(
            op="calibrate", id=f"cal-{node}-{self.cal}", node=node, source=sid,
            window_start=start, window_end=end, t1=t1, t2=t2, t3=t3, t4=t4,
            asym_bound_us=asym, noise_us=noise, asym_validated=asym_validated,
            path=path(*(segments or ("seg-" + sid,))), leap_table=leap,
            late=late, received_at=received_at or end,
            osc_state={"state": osc_state or "locked",
                       "drift_us_per_s": drift, "holdover_budget_us": budget},
        )

    def judge(self, node, start, end, at=None, high_limit=10, vid=None):
        return self.step(
            op="adjudicate", id=vid or f"v-{node}-{start}-{end}", node=node,
            window_start=start, window_end=end, at=at or end,
            high_limit_us=high_limit,
        )

    def contract(self, customer, grade, high=10, low=100, at=0):
        return self.step(
            op="register_contract", customer=customer, grade=grade,
            high_limit_us=high, low_limit_us=low, at=at,
        )

    # 1. The legacy provisional monitor cancels opposite offsets; formal
    #    interval adjudication must not repeat that mistake.
    def test_opposite_offsets_cancel_provisionally_but_not_formally(self):
        sample(self.db, "s1", "edge-1", "source-a", 40, 10)
        self.assertFalse(provisional_accurate(self.db, "edge-1", 5))
        sample(self.db, "s2", "edge-1", "source-b", -40, 10)
        self.assertTrue(provisional_accurate(self.db, "edge-1", 5))

        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        r1 = self.measure("sat-a", "edge-1", 40, 120, 0, 100,
                          asym=10, segments=("sat-dish",))
        r2 = self.measure("fib-a", "edge-1", -40, 120, 0, 100,
                          asym=10, segments=("fiber-line",))
        # Half-RTT (60) dominates the asymmetry bound (10); no average to zero.
        self.assertEqual(r1["interval_us"], [30, 50])
        self.assertEqual(r2["interval_us"], [-50, -30])
        verdict = self.judge("edge-1", 0, 100)
        self.assertNotEqual(verdict["decision"], "accurate")
        self.assertEqual(verdict["decision"], "inconsistent")
        # The full span is retained: nothing was averaged to zero.
        self.assertEqual(verdict["bound_us"], 50)
        self.assertEqual(len(verdict["evidence"]), 2)
        self.assertEqual(len(verdict["groups"]), 2)

    # 2. Colluding sources sharing one authentication root move together and
    #    must not count as independent corroboration.
    def test_colluding_satellite_sources_share_one_group(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("sat-b", "satellite", chain=["gnss-root", "sat-b"])
        self.measure("sat-a", "edge-1", 40, 120, 0, 100, segments=("dish-a",))
        self.measure("sat-b", "edge-1", 40, 120, 0, 100, segments=("dish-b",))
        verdict = self.judge("edge-1", 0, 100)
        self.assertEqual(verdict["decision"], "uncorroborated")
        self.assertEqual(len(verdict["groups"]), 1)
        self.assertEqual(verdict["suspect"], verdict["groups"])
        # Even a perfectly self-consistent cluster is not accuracy.
        self.assertNotEqual(verdict["decision"], "accurate")

        # A third independent source disagreeing exposes the lie: the
        # adjudication stays non-accurate and both groups are suspect.
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("fiber-line",))
        verdict = self.judge("edge-1", 0, 100, vid="v-2")
        self.assertEqual(verdict["decision"], "inconsistent")
        self.assertEqual(len(verdict["groups"]), 2)
        self.assertEqual(len(verdict["suspect"]), 2)

    # 3. Satellite leap-second mishandling condemns the source and corrects
    #    proofs already issued against it.
    def test_satellite_leap_second_mishandling_corrects_issued_proofs(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("fiber-line",))
        self.assertEqual(self.judge("edge-1", 0, 100)["decision"], "accurate")
        self.contract("cust-h", "high")
        issued = self.step(
            op="issue_proof", proof_id="p-1", txn="txn-1", customer="cust-h",
            node="edge-1", at=100,
        )
        self.assertEqual(issued["decision"], "issued")
        self.assertEqual(issued["grade"], "high")

        # New measurement arrives with an unacknowledged leap table.
        bad = self.measure("sat-a", "edge-1", 2, 40, 101, 200, leap="leap-2")
        self.assertEqual(bad["decision"], "rejected")
        self.assertEqual(bad["reason"], "leap_table_mismatch")
        condemn = self.step(
            op="condemn_source", source="sat-a",
            reason="leap_second_mishandling", at=250,
        )
        self.assertEqual(condemn["decision"], "condemned")
        self.assertIn("txn-1", condemn["transactions_for_rereview"])
        proof = one(self.db, "SELECT * FROM time_proofs WHERE proof_id='p-1'")
        self.assertEqual(proof["status"], "corrected")
        correction = one(
            self.db,
            "SELECT * FROM time_corrections WHERE proof_id='p-1'",
        )
        self.assertEqual(correction["reason"], "leap_second_mishandling")
        # The original bound and raw samples remain frozen in the proof.
        frozen = rows(self.db, "SELECT frozen FROM time_proofs WHERE proof_id='p-1'")[0]
        self.assertIn("leap-1", frozen["frozen"])
        self.assertEqual(proof["bound_us"], issued["bound_us"])
        # The tainted measurement window is traceable.
        interval = one(
            self.db,
            "SELECT * FROM time_failure_intervals WHERE status='tainted'",
        )
        self.assertEqual(interval["start_at"], 0)
        self.assertEqual(interval["end_at"], 200)

    # 4. A terrestrial reference sharing the fault segment cannot validate
    #    asymmetry; switching to an independent path restores tight bounds.
    def test_ground_path_switch_revalidates_asymmetry(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        # RTT alone, because the fiber rides the same faulty segment as the
        # customer network: the real error cannot be hidden behind stable RTT.
        shared = self.measure(
            "fib-a", "edge-1", 2, 40, 0, 100, asym=10,
            segments=("fault-segment", "fiber-tail"),
        )
        self.assertTrue(shared["path_fault"])
        self.assertEqual(shared["bound_us"], 20)
        verdict = self.judge("edge-1", 0, 100)
        self.assertNotEqual(verdict["decision"], "accurate")
        self.assertEqual(len(verdict["evidence"]), 1)  # shared-path sample excluded

        # Path switch onto an independent ground reference, revalidated.
        self.source("fib-b", "terrestrial", chain=["fiber-root-2", "fib-b"])
        switched = self.measure(
            "fib-b", "edge-1", 2, 40, 0, 100, asym=5,
            segments=("independent-fiber",),
        )
        self.assertFalse(switched["path_fault"])
        verdict = self.judge("edge-1", 0, 100, vid="v-switch")
        self.assertEqual(verdict["decision"], "accurate")
        self.assertLessEqual(verdict["bound_us"], 10)

    # 5. Oscillator aging widens holdover bounds; past the budget no bound is
    #    defensible and low-grade time stops rather than lying.
    def test_oscillator_aging_during_holdover(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",),
                     drift=1.0, budget=300)
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("fiber-line",),
                     drift=1.0, budget=300)
        self.assertEqual(self.judge("edge-1", 0, 100)["decision"], "accurate")
        self.step(op="link_break", id="fb-1", node="edge-1", at=100)

        # Inside holdover the bound grows linearly with the aged drift.
        inside = td._node_interval(self.db, "edge-1", 200)
        self.assertIsNotNone(inside)
        self.assertEqual(inside[2], 8 + 1.0 * 100)

        # Aging raises the drift and shortens the remaining holdover life.
        self.step(
            op="update_oscillator", node="edge-1", state="holdover",
            drift_us_per_s=2.0, holdover_budget_us=300, at=200,
        )
        self.assertEqual(td._node_interval(self.db, "edge-1", 210)[2],
                         8 + 2.0 * 110)
        self.assertIsNone(td._node_interval(self.db, "edge-1", 300))
        self.contract("cust-l", "low", low=100)
        proof = self.step(
            op="issue_proof", proof_id="p-l", txn="txn-l", customer="cust-l",
            node="edge-1", at=300,
        )
        self.assertEqual(proof["decision"], "unavailable")

    # 6. Customer migration preserves the cross-room bound; when holdover is
    #    exhausted the relation stops giving an upper bound.
    def test_customer_migration_keeps_relation_until_holdover_ends(self):
        for node in ("room-a", "room-b"):
            self.source(f"sat-{node}", "satellite",
                        chain=[f"gnss-root-{node}", f"sat-{node}"])
            self.source(f"fib-{node}", "terrestrial",
                        chain=[f"fiber-root-{node}", f"fib-{node}"])
            self.measure(f"sat-{node}", node, 2, 40, 0, 100,
                         segments=(f"dish-{node}",), drift=0.5, budget=200)
            self.measure(f"fib-{node}", node, -2, 40, 0, 100,
                         segments=(f"line-{node}",), drift=0.5, budget=200)
            self.assertEqual(
                self.judge(node, 0, 100, vid=f"v-{node}")["decision"], "accurate"
            )
        rel = self.step(
            op="build_relation", id="rel-1", verdict_a="v-room-a",
            verdict_b="v-room-b", at=100,
        )
        self.assertEqual(rel["decision"], "linked")

        migration = self.step(
            op="migrate_customer", id="mig-1", customer="cust-x",
            from_node="room-a", to_node="room-b", at=150,
        )
        self.assertEqual(migration["decision"], "migrated")
        self.assertGreaterEqual(migration["relation_bound_us"], 3)

        # Within holdover the relation still bounds order.
        ordering = self.step(
            op="order_events", at=150,
            event_a={"node": "room-a", "ts": 1000},
            event_b={"node": "room-b", "ts": 2000},
        )
        self.assertEqual(ordering["decision"], "before")

        # Break the link and age both oscillators past their holdover budget.
        self.step(op="link_break", id="fb-a", node="room-a", at=160)
        self.step(op="link_break", id="fb-b", node="room-b", at=160)
        self.step(op="update_oscillator", node="room-a", state="holdover",
                  drift_us_per_s=2.0, holdover_budget_us=200, at=160)
        self.step(op="update_oscillator", node="room-b", state="holdover",
                  drift_us_per_s=2.0, holdover_budget_us=200, at=160)
        late = self.step(
            op="migrate_customer", id="mig-2", customer="cust-x",
            from_node="room-a", to_node="room-b", at=300,
        )
        self.assertEqual(late["decision"], "unsortable")

    # 7. Transactions whose UTC intervals overlap cannot be force-ordered.
    def test_overlapping_uncertainty_is_unsortable_not_forced(self):
        self.source("sat-a", "satellite", chain=["gnss-root-a", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root-a", "fib-a"])
        self.measure("sat-a", "room-a", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "room-a", -2, 40, 0, 100, segments=("line-a",))
        self.assertEqual(self.judge("room-a", 0, 100)["decision"], "accurate")
        # Same node, timestamps closer together than the 3 us bound.
        result = self.step(
            op="order_events", at=100,
            event_a={"node": "room-a", "ts": 1000},
            event_b={"node": "room-a", "ts": 1002},
        )
        self.assertEqual(result["decision"], "unsortable")
        clear = self.step(
            op="order_events", at=100,
            event_a={"node": "room-a", "ts": 1000},
            event_b={"node": "room-a", "ts": 1100},
        )
        self.assertEqual(clear["decision"], "before")

    # 8. Withdrawing an issued proof emits a correction and marks its txn for
    #    re-review; the frozen evidence is retained.
    def test_withdraw_issued_proof(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("line-a",))
        self.judge("edge-1", 0, 100)
        self.contract("cust-h", "high")
        self.step(op="issue_proof", proof_id="p-w", txn="txn-w",
                  customer="cust-h", node="edge-1", at=100)
        withdrawal = self.step(
            op="withdraw_proof", proof_id="p-w", reason="audit_failure", at=140,
        )
        self.assertEqual(withdrawal["decision"], "withdrawn")
        self.assertEqual(withdrawal["transactions_for_rereview"], ["txn-w"])
        self.assertEqual(
            one(self.db, "SELECT status FROM time_proofs WHERE proof_id='p-w'")[
                "status"
            ],
            "withdrawn",
        )
        correction = one(
            self.db,
            "SELECT * FROM time_corrections WHERE proof_id='p-w'",
        )
        self.assertEqual(correction["reason"], "audit_failure")
        # Idempotent guard.
        again = self.step(op="withdraw_proof", proof_id="p-w", at=150)
        self.assertEqual(again["decision"], "already_withdrawn")

    # 9. Late samples arriving after a link break are forensic only: they
    #    cannot reopen the sealed verdict, but contradictions issue corrections.
    def test_late_sample_after_break_cannot_rewrite_verdict(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("line-a",))
        self.judge("edge-1", 0, 100)
        self.contract("cust-h", "high")
        self.step(op="issue_proof", proof_id="p-late", txn="txn-late",
                  customer="cust-h", node="edge-1", at=100)
        self.step(op="link_break", id="fb-x", node="edge-1", at=110)

        # A late, contradicting sample for the already-sealed window.
        result = self.measure(
            "fib-a", "edge-1", 90, 40, 0, 100, segments=("line-a",),
            late=True, received_at=300,
        )
        self.assertTrue(result["corrections"])
        self.assertEqual(
            one(self.db,
                "SELECT status FROM time_proofs WHERE proof_id='p-late'")[
                "status"
            ],
            "corrected",
        )
        # Re-adjudicating the same window ignores the late sample entirely.
        again = self.judge("edge-1", 0, 100, vid="v-again")
        self.assertEqual(again["decision"], "accurate")
        self.assertEqual(len(again["evidence"]), 2)
        # The failure interval was reassessed but not erased.
        interval = one(
            self.db,
            "SELECT * FROM time_failure_intervals WHERE interval_id='fb-x'",
        )
        self.assertEqual(interval["status"], "reassessed")
        self.assertEqual(interval["start_at"], 110)

    # 10. Option A: seal high precision, hold over on the local oscillator
    #     inside its bound, and serve low-grade continuity only to customers
    #     whose verifiable election allows the downgrade.
    def test_option_a_holdover_and_verifiable_downgrade(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",),
                     drift=0.5, budget=400)
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("line-a",),
                     drift=0.5, budget=400)
        self.assertEqual(self.judge("edge-1", 0, 100)["decision"], "accurate")
        self.contract("cust-h", "high", high=10, low=100)
        self.contract("cust-l", "low", high=10, low=100)
        high_ok = self.step(
            op="issue_proof", proof_id="p-h0", txn="txn-h0",
            customer="cust-h", node="edge-1", at=100,
        )
        self.assertEqual(high_ok["grade"], "high")

        # Suspicious satellite jump plus a fiber segment shared with the
        # customer network: both independent sources lose accuracy claims.
        self.step(op="link_break", id="fb-a1", node="edge-1", at=110)
        self.measure(
            "sat-a", "edge-1", 80, 40, 101, 200, segments=("dish-a",),
            osc_state="holdover", drift=0.5, budget=400,
        )
        self.measure(
            "fib-a", "edge-1", 2, 40, 101, 200,
            segments=("fault-segment", "fiber-tail"),
            osc_state="holdover", drift=0.5, budget=400,
        )
        post = self.judge("edge-1", 101, 200, at=200, vid="v-post")
        self.assertNotEqual(post["decision"], "accurate")

        # High-grade customer without an election: high precision is sealed and
        # the transaction is queued, never silently downgraded.
        queued = self.step(
            op="issue_proof", proof_id="p-h1", txn="txn-h1",
            customer="cust-h", node="edge-1", at=200,
        )
        self.assertEqual(queued["decision"], "high_precision_sealed")
        self.assertEqual(
            one(self.db,
                "SELECT status FROM time_proofs WHERE proof_id='p-h1'")[
                "status"
            ],
            "queued",
        )

        # Low-grade continuous customer keeps receiving time inside holdover.
        low_ok = self.step(
            op="issue_proof", proof_id="p-l1", txn="txn-l1",
            customer="cust-l", node="edge-1", at=200,
        )
        self.assertEqual(low_ok["decision"], "issued")
        self.assertEqual(low_ok["grade"], "low")
        self.assertEqual(low_ok["bound_us"], 8 + 0.5 * 90)

        # The high-grade customer can elect a downgrade; the choice is stored
        # with a digest that anyone can verify.
        election = self.step(
            op="record_election", id="el-1", customer="cust-h",
            choice="accept_low", reason="satellite_suspect_fiber_shared",
            credit=25, at=195, expires_at=500,
        )
        self.assertEqual(election["decision"], "recorded")
        self.assertEqual(
            self.step(op="verify_election", customer="cust-h", at=210)[
                "decision"
            ],
            "valid",
        )
        downgraded = self.step(
            op="issue_proof", proof_id="p-h2", txn="txn-h2",
            customer="cust-h", node="edge-1", at=210,
        )
        self.assertEqual(downgraded["decision"], "downgraded")
        self.assertEqual(downgraded["grade"], "low")
        self.assertEqual(downgraded["compensation_credit"], 25)
        comp = one(self.db,
                   "SELECT * FROM time_reference_costs WHERE cost_id='comp-p-h2'")
        self.assertEqual(comp["amount"], -25)
        # The election is verifiable later, and expires after its window.
        self.assertEqual(
            self.step(op="verify_election", customer="cust-h", at=600)[
                "decision"
            ],
            "invalid",
        )

    # 11. Option B: lease an independent terrestrial reference, revalidate the
    #     path, pay the external fee, and resume high-precision issuance while
    #     queued transactions wait for the new seal.
    def test_option_b_leased_reference_resumes_high_precision(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("line-a",))
        self.judge("edge-1", 0, 100)
        self.contract("cust-h", "high", high=10, low=100)

        # Failure window: high precision seals and the txn is queued.
        self.step(op="link_break", id="fb-b1", node="edge-1", at=110)
        queued = self.step(
            op="issue_proof", proof_id="p-q", txn="txn-q",
            customer="cust-h", node="edge-1", at=150,
        )
        self.assertEqual(queued["decision"], "high_precision_sealed")

        # Lease an independent ground reference on a disjoint path; the
        # measurement window runs through the intended re-issuance instant.
        self.source("leased-1", "leased",
                    chain=["leased-telco-root", "leased-1"])
        self.measure(
            "leased-1", "edge-1", 1, 40, 151, 260, asym=4,
            segments=("leased-dark-fiber",), osc_state="holdover",
        )
        # Keep one independent corroborating source on a separate trust domain.
        self.measure(
            "fib-a", "edge-1", 1, 40, 151, 260,
            segments=("diverse-fiber",), osc_state="holdover",
        )
        verdict = self.judge("edge-1", 151, 260, at=250, vid="v-leased")
        self.assertEqual(verdict["decision"], "accurate")

        fee = self.step(
            op="record_cost", id="cost-1", source="leased-1", amount=750,
            at=250, detail={"unit": "external_reference_fee"},
        )
        self.assertEqual(fee["decision"], "recorded")
        self.assertEqual(fee["amount"], 750)

        # The queued transaction is now stamped at the later issuance point.
        resumed = self.step(
            op="issue_proof", proof_id="p-q", txn="txn-q",
            customer="cust-h", node="edge-1", at=260,
        )
        self.assertEqual(resumed["decision"], "issued")
        self.assertEqual(resumed["grade"], "high")
        self.assertEqual(resumed["delay"], 260 - 150)
        self.assertGreaterEqual(resumed["issued_at"], 260)

    # 12. A closed failure interval keeps its traceable bounds; link
    #     restoration closes the open interval rather than deleting it.
    def test_restore_link_closes_failure_interval_without_erasing_it(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.source("fib-a", "terrestrial", chain=["fiber-root", "fib-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, segments=("dish-a",))
        self.measure("fib-a", "edge-1", -2, 40, 0, 100, segments=("line-a",))
        self.judge("edge-1", 0, 100)
        self.step(op="link_break", id="fb-r", node="edge-1", at=110)
        restored = self.step(op="restore_link", node="edge-1", at=180)
        self.assertEqual(restored["decision"], "restored")
        interval = one(
            self.db,
            "SELECT * FROM time_failure_intervals WHERE interval_id='fb-r'",
        )
        self.assertEqual(interval["status"], "closed")
        self.assertEqual(interval["start_at"], 110)
        self.assertEqual(interval["end_at"], 180)
        # Restoring with nothing open is an explicit no-op.
        again = self.step(op="restore_link", node="edge-1", at=190)
        self.assertEqual(again["decision"], "no_open_failure_interval")

    # 13. Persisted evidence covers every contractually required field.
    def test_each_calibration_persists_full_evidence_chain(self):
        self.source("sat-a", "satellite", chain=["gnss-root", "sat-a"])
        self.measure("sat-a", "edge-1", 2, 40, 0, 100, asym=8,
                     segments=("dish-a",), drift=0.25, budget=300)
        cal = one(self.db, "SELECT * FROM time_calibrations")
        self.assertEqual(cal["rtt_us"], 40)
        self.assertEqual(cal["offset_us"], 2)
        self.assertEqual(cal["asym_bound_us"], 8)
        self.assertEqual(cal["bound_us"], 8)
        self.assertEqual(cal["window_start"], 0)
        self.assertEqual(cal["window_end"], 100)
        self.assertIn("dish-a", cal["path"])
        osc = rows(self.db, "SELECT osc_state FROM time_calibrations")[0]["osc_state"]
        self.assertIn("holdover_budget_us", osc)
        source = one(self.db, "SELECT auth_chain FROM time_sources")
        self.assertEqual(json.loads(source["auth_chain"]),
                         ["gnss-root", "sat-a"])
        self.assertEqual(cal["leap_table"], "leap-1")


if __name__ == "__main__":
    unittest.main()
