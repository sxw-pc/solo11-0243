import unittest

from netcourt.store import connect
from netcourt.spectrum_scan import sample as note_sample
from netcourt.channel_note import provisional_channel
from netcourt import radio_coordination as rc

TOWER_A = "tower-a"
TOWER_B = "tower-b"
PROTECTED = "neighbor-protected"
REGION_BACKHAUL = "region-backhaul"
REGION_PROTECTED = "region-protected"
RESERVE = "band-z"
DISPUTED = "band-y"
ORIGINAL = "band-x"


class RadioCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        rc.init(self.db)
        self.seq = 0

    def tearDown(self):
        self.db.close()

    def op(self, **payload):
        payload.setdefault("id", f"e{self.seq}")
        self.seq += 1
        return rc.run(self.db, payload)

    def open_case(self, case="case-1"):
        self.op(op="open_case", id=case, tenant="tenant-1",
                site_a=TOWER_A, site_b=TOWER_B, at=0)
        self.op(op="contract", case_id=case, tenant="tenant-1",
                throughput=100, latency=20, available_from=10, available_to=1000)
        # Licenses: the disputed band and the original band at full power,
        # and a narrower spare band with a lower power ceiling.
        self.op(op="license", id=f"{case}-lic-y", case_id=case, channel=DISPUTED,
                region=REGION_BACKHAUL, max_power=30, width=20)
        self.op(op="license", id=f"{case}-lic-x", case_id=case, channel=ORIGINAL,
                region=REGION_BACKHAUL, max_power=30, width=20)
        self.op(op="license", id=f"{case}-lic-z", case_id=case, channel=RESERVE,
                region=REGION_BACKHAUL, max_power=15, width=10)
        self.op(op="protected_object", id=PROTECTED, case_id=case, channel=DISPUTED,
                region=REGION_PROTECTED, noise_limit=5,
                windows=[{"start_at": 10, "end_at": 20, "period": 100}])
        self.op(op="complaint", id=f"{case}-complaint", case_id=case, at=5)

    def calibrate(self, site=TOWER_A, sid=None, at=1):
        sid = sid or f"scan-{site}"
        self.op(op="scanner_calibrate", id=sid, site=site, at=at)
        return sid

    def antenna(self, site, azimuth, backlobe=None, aid=None, at=2):
        aid = aid or f"ant-{site}-1"
        self.op(op="antenna_config", id=aid, site=site, azimuth=azimuth,
                backlobe_region=backlobe, at=at)
        return aid

    def survey_ok(self, site, azimuth, at=3):
        self.op(op="antenna_survey", id=f"sur-{site}-{at}", site=site,
                reported_azimuth=azimuth, measured_azimuth=azimuth, at=at)

    def scan(self, site, channel, noise, at, scanner=None, antenna=None, power=12):
        scanner = scanner or f"scan-{site}"
        antenna = antenna or f"ant-{site}-1"
        self.op(op="scan", id=f"scan-{site}-{channel}-{at}-{self.seq}", site=site,
                channel=channel, noise=noise, at=at, scanner_id=scanner,
                antenna_id=antenna, power=power)

    def reception(self, power, at, channel=DISPUTED):
        self.op(op="reception", id=f"rx-{self.seq}", object_id=PROTECTED,
                channel=channel, power=power, at=at)

    def acceptance(self, throughput=100, latency=15, direction="both",
                   evidence="bidirectional", at=40):
        self.op(op="acceptance", id=f"acc-{self.seq}", case_id="case-1",
                direction=direction, throughput=throughput, latency=latency,
                evidence=evidence, at=at)

    def assess(self, channel, at=50, region=REGION_BACKHAUL, case="case-1"):
        return self.op(op="assess", case_id=case, channel=channel, at=at, region=region)

    # ------------------------------------------------------------------
    def test_provisional_picks_quiet_band_but_formal_judgment_rejects(self):
        """The enhanced scan's extra low-noise sample makes the provisional
        note switch to band-y, while the formal judgment still refuses it."""
        self.open_case()
        sa = self.calibrate(TOWER_A)
        sb = self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 90, backlobe=REGION_PROTECTED)
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        self.survey_ok(TOWER_A, 90)
        self.survey_ok(TOWER_B, 270)

        # Single-station scan ledger used by the provisional note.
        note_sample(self.db, "n1", TOWER_A, ORIGINAL, 19, 10)
        self.assertEqual(provisional_channel(self.db, TOWER_A), ORIGINAL)
        note_sample(self.db, "n2", TOWER_A, DISPUTED, 4, 10)
        self.assertEqual(provisional_channel(self.db, TOWER_A), DISPUTED)

        # Formal evidence: tower-a quiet, the other end hit by the protected
        # station's periodic emission, and the backlobe faces the reserve.
        self.scan(TOWER_A, DISPUTED, 4, 15, sa, aa)
        self.scan(TOWER_B, DISPUTED, 14, 15, sb, ab)
        self.reception(power=9, at=15)
        self.acceptance(throughput=60, at=15)

        verdict = self.assess(DISPUTED, at=15)
        self.assertEqual(verdict["decision"], "rejected")
        reasons = set(verdict["reasons"])
        self.assertIn("protected_periodic_emission", reasons)
        self.assertIn("protected_reception_over_limit", reasons)
        self.assertIn("antenna_backlobe_into_protected_area", reasons)
        self.assertIn("open_neighbor_complaint", reasons)
        self.assertIn("sla_throughput_unmet", reasons)
        # A quiet local end on its own proves neither end-to-end service nor
        # neighbor compliance: tower-a's noise 4 does not silence the far end.
        self.assertIn("local_noise_too_high", reasons)

    def test_scanner_calibration_revocation_invalidates_scan(self):
        self.open_case()
        sa = self.calibrate(TOWER_A, at=1)
        sb = self.calibrate(TOWER_B, at=1)
        aa = self.antenna(TOWER_A, 90, backlobe="region-other")
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        self.survey_ok(TOWER_A, 90)
        self.survey_ok(TOWER_B, 270)
        self.scan(TOWER_A, RESERVE, 5, 12, sa, aa)
        self.scan(TOWER_B, RESERVE, 6, 12, sb, ab)
        self.op(op="scanner_revoke", id=sa)
        verdict = self.assess(RESERVE)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("scanner_calibration_revoked", verdict["reasons"])

    def test_antenna_adjustment_ages_out_old_measurement(self):
        self.open_case()
        sa = self.calibrate(TOWER_A)
        sb = self.calibrate(TOWER_B)
        old = self.antenna(TOWER_A, 90, backlobe="region-other", aid="ant-a-old", at=2)
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        self.survey_ok(TOWER_B, 270)
        self.scan(TOWER_A, RESERVE, 5, 12, sa, old)
        self.scan(TOWER_B, RESERVE, 6, 12, sb, ab)
        # Antenna is realigned; the measurement taken under the old antenna
        # can no longer represent the new state.
        new = self.antenna(TOWER_A, 95, backlobe="region-other", aid="ant-a-new", at=20)
        self.survey_ok(TOWER_A, 95, at=21)
        verdict = self.assess(RESERVE, at=25)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("stale_measurement_after_antenna_change", verdict["reasons"])
        # A fresh scan under the new antenna clears that reason.
        self.scan(TOWER_A, RESERVE, 5, 22, sa, new)
        verdict = self.assess(RESERVE, at=25)
        self.assertNotIn("stale_measurement_after_antenna_change", verdict["reasons"])

    def test_antenna_azimuth_misreport_blocks_judgment(self):
        self.open_case()
        self.calibrate(TOWER_A)
        self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 90, backlobe=REGION_PROTECTED)
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        # Survey contradicts the reported heading: no verified direction.
        self.op(op="antenna_survey", id="sur-a-bad", site=TOWER_A,
                reported_azimuth=90, measured_azimuth=70, at=3)
        self.survey_ok(TOWER_B, 270)
        self.scan(TOWER_A, DISPUTED, 4, 40)
        self.scan(TOWER_B, DISPUTED, 5, 40)
        self.reception(power=2, at=40)
        self.acceptance()
        verdict = self.assess(DISPUTED, at=45)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("antenna_azimuth_unverified", verdict["reasons"])
        self.assertNotIn("antenna_backlobe_into_protected_area", verdict["reasons"])

    def test_beacon_and_one_way_evidence_are_not_acceptance(self):
        self.open_case()
        self.calibrate(TOWER_A)
        self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 90, backlobe="region-other")
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        self.survey_ok(TOWER_A, 90)
        self.survey_ok(TOWER_B, 270)
        self.scan(TOWER_A, RESERVE, 5, 40)
        self.scan(TOWER_B, RESERVE, 5, 40)
        # Device registration and an air-interface beacon never prove SLA.
        self.op(op="acceptance", id="acc-reg", case_id="case-1", direction="both",
                throughput=100, latency=10, evidence="registration", at=40)
        self.op(op="acceptance", id="acc-bea", case_id="case-1", direction="both",
                throughput=100, latency=10, evidence="beacon", at=40)
        verdict = self.assess(RESERVE, at=45)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("beacon_or_registration_not_acceptance", verdict["reasons"])
        self.assertIn("missing_bidirectional_acceptance", verdict["reasons"])
        # A single-direction live test is still insufficient.
        self.op(op="acceptance", id="acc-fwd", case_id="case-1", direction="forward",
                throughput=100, latency=10, evidence="bidirectional", at=41)
        verdict = self.assess(RESERVE, at=45)
        self.assertIn("one_way_acceptance_only", verdict["reasons"])
    def _compliant_spare_evidence(self):
        self.calibrate(TOWER_A)
        self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 100, backlobe="region-other")
        ab = self.antenna(TOWER_B, 280, backlobe="region-other")
        self.survey_ok(TOWER_A, 100)
        self.survey_ok(TOWER_B, 280)
        self.scan(TOWER_A, RESERVE, 5, 40, power=10)
        self.scan(TOWER_B, RESERVE, 6, 40, power=10)
        return aa, ab

    def test_retune_path_restores_service_with_capacity_discount(self):
        self.open_case()
        self._compliant_spare_evidence()
        # Lower but guaranteed throughput on the narrower spare band.
        self.acceptance(throughput=60, latency=18, at=42)
        verdict = self.assess(RESERVE, at=45)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("sla_throughput_unmet", verdict["reasons"])
        self.assertIn("open_neighbor_complaint", verdict["reasons"])

        # Engineering retunes within the license; contract capacity discount.
        self.op(op="construction", id="retune-1", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-1", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60,
                reason="narrower_spare_band")
        # Re-accept against the reduced guarantee and inside the SLA window.
        self.op(op="acceptance", id="acc-z", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        # Independent retest and neighbor retest both pass; the latter closes
        # the original complaint before the formal judgment.
        self.op(op="retest", id="rt-ind", case_id="case-1", kind="independent",
                at=48, passed=True, evidence="spectrum_scan")
        self.op(op="retest", id="rt-nbr", case_id="case-1", kind="neighbor",
                at=49, passed=True, evidence="protected_reception")
        verdict = self.assess(RESERVE, at=50)
        self.assertEqual(verdict["decision"], "usable", verdict)

        receipt = self.op(op="receipt", id="rcp-z", case_id="case-1",
                          kind="retuned", channel=RESERVE, at=51)
        self.assertEqual(receipt["decision"], "receipt_issued")
        self.assertEqual(receipt["status"], "serving_retuned")
        case = rc.one(self.db, "SELECT * FROM radio_cases WHERE id='case-1'")
        self.assertEqual(case["status"], "serving_retuned")
        self.assertEqual(case["restored_at"], 51)
        # The passing neighbor retest closed the original complaint.
        self.assertEqual(
            rc.one(self.db, "SELECT status FROM radio_complaints WHERE id='case-1-complaint'")["status"],
            "closed",
        )

    def test_antenna_remediation_path_restores_full_throughput(self):
        self.open_case()
        sa = self.calibrate(TOWER_A)
        sb = self.calibrate(TOWER_B)
        old = self.antenna(TOWER_A, 90, backlobe=REGION_PROTECTED, aid="ant-a-old")
        ab = self.antenna(TOWER_B, 270, backlobe="region-other")
        self.survey_ok(TOWER_A, 90)
        self.survey_ok(TOWER_B, 270)
        self.scan(TOWER_A, DISPUTED, 4, 15, sa, old)
        self.scan(TOWER_B, DISPUTED, 14, 15, sb, ab)
        self.reception(power=9, at=15)
        before = self.assess(DISPUTED, at=18)
        self.assertEqual(before["decision"], "rejected")
        self.assertIn("antenna_backlobe_into_protected_area", before["reasons"])

        # Keep the band: realign the antenna and add shielding.
        self.op(op="construction", id="work-1", case_id="case-1",
                action="antenna", at=25, cost=4000, new_antenna="ant-a-new")
        self.op(op="construction", id="work-2", case_id="case-1",
                action="shield", at=26, cost=1500)
        new = self.antenna(TOWER_A, 120, backlobe="region-other",
                           aid="ant-a-new", at=27)
        self.survey_ok(TOWER_A, 120, at=28)
        # New state requires new measurements at both ends.
        self.scan(TOWER_A, DISPUTED, 5, 30, sa, new)
        self.scan(TOWER_B, DISPUTED, 6, 30, sb, ab)
        # The protected station's latest reception is now inside the limit.
        self.reception(power=3, at=31)
        self.op(op="acceptance", id="acc-y", case_id="case-1", direction="both",
                throughput=100, latency=15, evidence="bidirectional", at=32)
        # Both ends retested and the protected station's reception verified
        # before the formal judgment restores full throughput.
        self.op(op="retest", id="rt-ind2", case_id="case-1", kind="independent",
                at=33, passed=True)
        self.op(op="retest", id="rt-nbr2", case_id="case-1", kind="neighbor",
                at=34, passed=True)
        verdict = self.assess(DISPUTED, at=35)
        self.assertEqual(verdict["decision"], "usable", verdict)

        receipt = self.op(op="receipt", id="rcp-y", case_id="case-1",
                          kind="remediated", channel=DISPUTED, at=38)
        self.assertEqual(receipt["decision"], "receipt_issued")
        self.assertEqual(receipt["status"], "serving_full")
        # Historical over-limit reception remains on file as evidence.
        self.assertEqual(
            rc.rows(self.db, "SELECT COUNT(*) AS c FROM radio_receptions")[0]["c"], 2
        )

    def test_late_retest_reopens_case_and_compensates_impaired_window(self):
        self.open_case()
        self._compliant_spare_evidence()
        self.op(op="construction", id="retune-late", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-late", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="acceptance", id="acc-late", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        self.op(op="retest", id="rt-ok1", case_id="case-1", kind="independent", at=48, passed=True)
        self.op(op="retest", id="rt-nbr-ok", case_id="case-1",
                kind="neighbor", at=49, passed=True)
        self.assertEqual(self.assess(RESERVE, at=50)["decision"], "usable")
        self.op(op="receipt", id="rcp-late", case_id="case-1",
                kind="retuned", channel=RESERVE, at=51)

        # A valid but late independent measurement fails: the customer must be
        # re-notified and the impaired window compensated.
        late = self.op(op="retest", id="rt-late-bad", case_id="case-1",
                       kind="independent", at=70, arrived_at=75, passed=False,
                       evidence="residual_interference")
        self.assertEqual(late["decision"], "recorded")
        case = rc.one(self.db, "SELECT * FROM radio_cases WHERE id='case-1'")
        self.assertEqual(case["status"], "reopened")
        comp = rc.one(
            self.db,
            "SELECT * FROM radio_discounts WHERE kind='compensation' ORDER BY rowid DESC LIMIT 1",
        )
        self.assertEqual((comp["from_at"], comp["to_at"], comp["amount"]), (51, 75, 24))
        notes = rc.rows(self.db, "SELECT reason FROM radio_notifications WHERE case_id='case-1'")
        self.assertIn("late_failed_retest", [n["reason"] for n in notes])

        # A late passing measurement is communicated but does not compensate.
        before = rc.rows(self.db, "SELECT COUNT(*) AS c FROM radio_notifications WHERE case_id='case-1'")[0]["c"]
        self.op(op="retest", id="rt-late-ok", case_id="case-1", kind="independent",
                at=80, arrived_at=82, passed=True)
        after = rc.rows(self.db, "SELECT COUNT(*) AS c FROM radio_notifications WHERE case_id='case-1'")[0]["c"]
        self.assertEqual(after, before + 1)
        self.assertIn(
            "late_retest_evidence",
            [n["reason"] for n in rc.rows(
                self.db, "SELECT reason FROM radio_notifications WHERE case_id='case-1'")],
        )

    def test_repeat_neighbor_complaint_blocks_and_issued_discount_is_corrected(self):
        self.open_case()
        self._compliant_spare_evidence()
        self.op(op="construction", id="retune-r", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-r", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="acceptance", id="acc-r", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        self.op(op="retest", id="rt-r1", case_id="case-1", kind="independent", at=48, passed=True)
        self.op(op="retest", id="rt-r2", case_id="case-1", kind="neighbor", at=49, passed=True)
        self.assertEqual(self.assess(RESERVE, at=50)["decision"], "usable")
        self.op(op="receipt", id="rcp-r", case_id="case-1",
                kind="retuned", channel=RESERVE, at=51)

        # Neighbor complains again: service is reopened and a new receipt is barred.
        self.op(op="complaint", id="complaint-2", case_id="case-1", at=60, kind="repeat")
        self.assertEqual(
            rc.one(self.db, "SELECT status FROM radio_cases WHERE id='case-1'")["status"],
            "reopened",
        )
        blocked = self.op(op="receipt", id="rcp-r2", case_id="case-1",
                          kind="retuned", channel=RESERVE, at=61)
        self.assertEqual(blocked["decision"], "open_neighbor_complaint")

        # The already-issued discount was miscalculated and is corrected;
        # both rows survive with their own facts.
        corrected = self.op(op="correct_discount", id="disc-r-fix",
                            original_id="disc-r", amount=420,
                            reason="actual_impaired_window_longer")
        self.assertEqual(corrected["decision"], "corrected")
        old = rc.one(self.db, "SELECT * FROM radio_discounts WHERE id='disc-r'")
        self.assertEqual(old["corrected_by"], "disc-r-fix")
        new = rc.one(self.db, "SELECT * FROM radio_discounts WHERE id='disc-r-fix'")
        self.assertEqual(new["amount"], 420)
        self.assertEqual(new["from_at"], old["from_at"])
        self.assertEqual(new["guaranteed"], old["guaranteed"])

    def test_receipt_requires_evidence_chain_and_license_boundaries(self):
        self.open_case()
        self._compliant_spare_evidence()
        self.op(op="construction", id="retune-g", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-g", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="acceptance", id="acc-g", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        # With the complaint still open, the formal judgment cannot pass and
        # the coordinator receipt has no usable decision to certify.
        self.assertEqual(
            set(self.assess(RESERVE, at=48)["reasons"]), {"open_neighbor_complaint"}
        )
        self.assertEqual(
            self.op(op="receipt", id="g0", case_id="case-1", kind="retuned",
                    channel=RESERVE, at=48)["decision"],
            "no_usable_decision",
        )
        # Close the complaint through both retests so the judgment is usable.
        self.op(op="retest", id="g-rt", case_id="case-1", kind="independent", at=48, passed=True)
        self.op(op="retest", id="g-rt2", case_id="case-1", kind="neighbor", at=49, passed=True)
        self.assertEqual(self.assess(RESERVE, at=50)["decision"], "usable")

        # A receipt dated before the independent retest is still short of it.
        self.assertEqual(
            self.op(op="receipt", id="g1", case_id="case-1", kind="retuned",
                    channel=RESERVE, at=47)["decision"],
            "missing_independent_retest",
        )
        # A fresh complaint after the retests keeps the receipt barred.
        self.op(op="complaint", id="g-complaint", case_id="case-1", at=50, kind="repeat")
        self.assertEqual(
            self.op(op="receipt", id="g3", case_id="case-1", kind="retuned",
                    channel=RESERVE, at=51)["decision"],
            "open_neighbor_complaint",
        )
        # A channel outside the licensed region can never be judged usable.
        unlicensed = self.assess("band-w", at=48, region="region-elsewhere")
        self.assertEqual(unlicensed["decision"], "rejected")
        self.assertIn("unlicensed_channel_or_region", unlicensed["reasons"])

    def test_receipt_blocked_without_passing_neighbor_retest(self):
        self.open_case()
        self._compliant_spare_evidence()
        self.op(op="construction", id="retune-n", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-n", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="acceptance", id="acc-n", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        # Only the independent retest passes (which closes the original
        # complaint); the neighbor has not separately confirmed its protected
        # channel is clear, so no receipt can be issued.
        self.op(op="retest", id="rt-n1", case_id="case-1", kind="independent",
                at=48, passed=True)
        self.assertEqual(self.assess(RESERVE, at=50)["decision"], "usable")
        self.assertEqual(
            self.op(op="receipt", id="rcp-n", case_id="case-1", kind="retuned",
                    channel=RESERVE, at=51)["decision"],
            "neighbor_still_interfered",
        )

    def test_power_over_license_and_unlicensed_region_rejected(self):
        self.open_case()
        sa = self.calibrate(TOWER_A)
        sb = self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 100, backlobe="region-other")
        ab = self.antenna(TOWER_B, 280, backlobe="region-other")
        self.survey_ok(TOWER_A, 100)
        self.survey_ok(TOWER_B, 280)
        # Spare band ceiling is 15; transmitting at 22 breaches the license.
        self.scan(TOWER_A, RESERVE, 5, 40, sa, aa, power=22)
        self.scan(TOWER_B, RESERVE, 5, 40, sb, ab, power=10)
        self.acceptance(throughput=60, at=42)
        verdict = self.assess(RESERVE, at=45)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("power_over_license", verdict["reasons"])

    def test_historical_over_limit_reception_does_not_judge_current_state(self):
        self.open_case()
        self._compliant_spare_evidence()
        # The protected station's disputed-band history stays on file, but its
        # latest reception is inside the limit after the field correction.
        self.reception(power=9, at=12)
        self.reception(power=2, at=41)
        self.op(op="construction", id="retune-h", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-h", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="acceptance", id="acc-h", case_id="case-1", direction="both",
                throughput=60, latency=18, evidence="bidirectional", at=47)
        self.op(op="retest", id="rt-h1", case_id="case-1", kind="independent",
                at=48, passed=True)
        self.op(op="retest", id="rt-h2", case_id="case-1", kind="neighbor",
                at=49, passed=True)
        verdict = self.assess(RESERVE, at=50)
        self.assertEqual(verdict["decision"], "usable", verdict)
        self.assertEqual(
            rc.rows(self.db, "SELECT COUNT(*) AS c FROM radio_receptions")[0]["c"], 2
        )

    def test_missing_end_measurement_and_discount_guarantee_govern_sla(self):
        self.open_case()
        sa = self.calibrate(TOWER_A)
        sb = self.calibrate(TOWER_B)
        aa = self.antenna(TOWER_A, 100, backlobe="region-other")
        ab = self.antenna(TOWER_B, 280, backlobe="region-other")
        self.survey_ok(TOWER_A, 100)
        self.survey_ok(TOWER_B, 280)
        # Only tower-a measured: a single-end view cannot decide the channel.
        self.scan(TOWER_A, RESERVE, 5, 40, sa, aa)
        self.acceptance(throughput=60, at=42)
        verdict = self.assess(RESERVE, at=45)
        self.assertEqual(verdict["decision"], "rejected")
        self.assertIn("missing_end_measurement", verdict["reasons"])
        # Before any capacity discount, the full contracted 100 is required.
        self.scan(TOWER_B, RESERVE, 6, 40, sb, ab)
        verdict = self.assess(RESERVE, at=45)
        self.assertIn("sla_throughput_unmet", verdict["reasons"])
        # After the discount, the guaranteed 60 becomes the acceptance bar.
        self.op(op="construction", id="retune-m", case_id="case-1",
                action="retune", at=46, cost=500)
        self.op(op="discount", id="disc-m", case_id="case-1", amount=300,
                kind="capacity", from_at=46, to_at=200, guaranteed=60)
        self.op(op="retest", id="rt-m1", case_id="case-1", kind="independent",
                at=48, passed=True)
        self.op(op="retest", id="rt-m2", case_id="case-1", kind="neighbor",
                at=49, passed=True)
        verdict = self.assess(RESERVE, at=50)
        self.assertEqual(verdict["decision"], "usable", verdict)


if __name__ == "__main__":
    unittest.main()
