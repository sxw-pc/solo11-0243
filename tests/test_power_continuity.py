import unittest

from netcourt.store import connect
from netcourt.ups_sensor import report
from netcourt.runtime_note import provisional_runtime
from netcourt import power_continuity as pc

FLAT = [[100, 0.93], [1000, 0.93]]
SITE = "edge-site"


class PowerContinuityTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        pc.init(self.db)

    def tearDown(self):
        self.db.close()

    def step(self, **x):
        return pc.run(self.db, x)

    def build_site(self, site=SITE, capacity_wh=285.0, with_isolated=True,
                   derate=1.0):
        self.step(op="register_pack", site=site, pack="pack-a",
                  capacity_wh=capacity_wh, soc=0.90, soh=1.0, temp_c=25.0,
                  curve=FLAT, at=0)
        if with_isolated:
            # 现场早已隔离的一组电池：登记但不得计入容量。
            self.step(op="register_pack", site=site, pack="pack-b",
                      capacity_wh=500.0, soc=1.0, soh=1.0, temp_c=25.0,
                      curve=FLAT, isolated=1, at=0)
        self.step(op="register_inverter", site=site, rated_w=800.0,
                  derate=derate, at=0)
        self.db.execute(
            "INSERT INTO pc_loads VALUES(?,?,?,?,?,?,?,?,?,0)",
            (site, "trading", "trading-co", "critical", 300.0, 0, 300.0,
             10.0, 800.0))
        self.db.execute(
            "INSERT INTO pc_loads VALUES(?,?,?,?,?,?,?,?,?,0)",
            (site, "noncritical", "retail-co", "best_effort", 400.0, 1,
             0.0, 10.0, 120.0))

    # —— 起点缺陷：同一 pack 重发估计把 provisional 从 18 顶到 36 ——
    def test_duplicate_estimate_does_not_increase_guaranteed_runtime(self):
        self.build_site()
        before = self.step(op="guarantee", site=SITE, at=0)
        self.assertEqual(before["decision"], "guaranteed")
        self.assertEqual(before["packs_used"], ["pack-a"])
        self.assertEqual(before["isolated_packs"], ["pack-b"])
        self.assertLess(before["low_minutes"], 18)

        report(self.db, "r1", SITE, "pack-a", 18, 10)
        self.assertEqual(provisional_runtime(self.db, SITE), 18)
        first = self.step(op="guarantee", site=SITE, at=0)
        # 物理区间恰好夹住控制器的 18 分钟：下界保守、上界宽裕。
        self.assertLess(first["low_minutes"], 18)
        self.assertGreater(first["high_minutes"], 18)
        # 控制器重发同一 18 分钟估计：暂报翻倍，但正式可保证时长不动。
        report(self.db, "r2", SITE, "pack-a", 18, 20)
        self.assertEqual(provisional_runtime(self.db, SITE), 36)
        second = self.step(op="guarantee", site=SITE, at=0)
        self.assertEqual(
            (first["low_minutes"], first["high_minutes"],
             first["available_wh_low"], first["available_wh_high"]),
            (second["low_minutes"], second["high_minutes"],
             second["available_wh_low"], second["available_wh_high"]))
        self.assertEqual(second["controller_estimates"], {"pack-a": 18})
        self.assertAlmostEqual(second["load_w"], 700.0)

    def test_isolated_pack_never_counts_and_re_isolation_empties_capacity(self):
        self.build_site()
        g = self.step(op="guarantee", site=SITE, at=0)
        self.assertEqual(g["packs_used"], ["pack-a"])
        self.step(op="isolate_pack", site=SITE, pack="pack-a", at=5)
        dead = self.step(op="guarantee", site=SITE, at=6)
        self.assertEqual(dead["decision"], "no_available_pack")
        self.step(op="restore_pack", site=SITE, pack="pack-a", at=7)
        back = self.step(op="guarantee", site=SITE, at=8)
        self.assertEqual(back["decision"], "guaranteed")

    def test_inverter_derate_forces_load_reduction(self):
        self.build_site()
        r = self.step(op="set_inverter_derate", site=SITE, derate=0.5, at=10)
        self.assertAlmostEqual(r["inverter_limit_w"], 400.0)
        g = self.step(op="guarantee", site=SITE, at=11)
        self.assertEqual(g["decision"], "insufficient_inverter")
        self.assertAlmostEqual(g["reduce_w"], 300.0)
        # 未获同意不能削减；且关键负载本身不可降级。
        self.assertEqual(
            self.step(op="shed_load", site=SITE, load="trading",
                      compensation=0, at=12)["decision"],
            "load_not_degradable")
        self.assertEqual(
            self.step(op="shed_load", site=SITE, load="noncritical",
                      compensation=120.0, at=12)["decision"],
            "consent_missing")

    # —— 结果一：削减非关键负载，电池撑过修复窗口，交易客户零中断 ——
    def test_outcome_a_shed_noncritical_and_battery_bridges(self):
        self.build_site()
        self.step(op="gen_dispatch", site=SITE, truck="truck-1", eta=720,
                  rental_cost=5000.0, fuel_w=700.0, at=0)
        self.step(op="meter_reading", site=SITE, at=0, battery_w=700.0)
        initial = self.step(op="guarantee", site=SITE, at=0)
        # 原负载下电池窗口仅略大于发电车 ETA：到场与接线的不确定性无法吸收，
        # 因此需要削减非关键负载，把余量留给交易客户。
        self.assertGreater(initial["low_until"], 720)
        self.assertLess(initial["high_until"], 1200)

        # 客户确认降级后才削减；削减预演须确认交易客户保护路径仍在。
        self.step(op="consent", site=SITE, load="noncritical", granted=1, at=60)
        shed = self.step(op="shed_load", site=SITE, load="noncritical",
                         compensation=120.0, at=60)
        self.assertEqual(shed["decision"], "load_shed")
        self.assertAlmostEqual(shed["plan"]["load_w"], 300.0)
        self.assertGreater(shed["plan"]["low_until"], 1800)

        self.step(op="meter_reading", site=SITE, at=60, battery_w=300.0)
        self.step(op="meter_reading", site=SITE, at=720, battery_w=300.0)
        # 物理表计认回真实电源：电池母线，而非未到场的发电车。
        self.assertEqual(
            self.step(op="reconcile_source", site=SITE, at=120)["decision"],
            "source_recognized")
        declared = self.step(op="declare_backup", site=SITE, at=121)
        self.assertEqual(declared["decision"], "backup_declared")
        self.assertEqual(declared["source"], "battery")

        # 市电恢复，先回切再恢复非关键负载。
        self.step(op="meter_reading", site=SITE, at=1800, battery_w=300.0)
        self.step(op="restore_load", site=SITE, load="noncritical", at=1800)
        self.step(op="meter_reading", site=SITE, at=1800, grid_w=700.0,
                  battery_w=0.0)
        balance = self.step(op="close_balance", site=SITE, window_start=0,
                            window_end=1800, tolerance_wh=0.05)
        self.assertEqual(balance["decision"], "balanced")
        self.assertEqual(balance["interruptions"], [])
        self.assertAlmostEqual(balance["battery_wh"],
                               700.0 * 60 / 3600 + 300.0 * 1740 / 3600,
                               places=4)

        report = self.step(op="interruption_report", site=SITE)
        self.assertEqual(report["interruptions"], [])
        reasons = {(c["customer"], c["reason"]) for c in report["compensations"]}
        self.assertIn(("retail-co", "degraded_service"), reasons)
        self.assertFalse(any(c == "trading-co" for c, _ in reasons))
        # 发电车仅在途：ETA 没有变成已供电。
        gen = pc._one(self.db, "SELECT * FROM pc_gen_state WHERE site=?",
                      (SITE,))
        self.assertIsNone(gen["arrived_at"])
        self.assertIsNone(gen["transferred_at"])

    # —— 结果二：维持原负载，发电车在电池耗尽前带载接管，全员在线 ——
    def test_outcome_b_generator_takeover_with_short_break_and_correction(self):
        self.build_site()
        self.step(op="gen_dispatch", site=SITE, truck="truck-2", eta=720,
                  rental_cost=5000.0, fuel_w=700.0, at=0)
        for t, kw in ((0, 700.0), (720, 700.0), (777, 700.0)):
            self.step(op="meter_reading", site=SITE, at=t, battery_w=kw)

        # 五个环节分别记录；带载未成功前不得转供。
        self.assertEqual(
            self.step(op="gen_arrive", site=SITE, at=700)["decision"],
            "arrived")
        self.assertEqual(
            self.step(op="gen_wire", site=SITE, at=720)["decision"], "wired")
        self.step(op="phase_check", site=SITE, ok=False, at=735)
        self.assertEqual(
            self.step(op="transfer", site=SITE, at=736)["decision"],
            "phase_sequence_failed")
        self.step(op="phase_check", site=SITE, ok=True, at=740)
        self.step(op="load_test", site=SITE, ok=False, test_w=0.0, at=750)
        self.assertEqual(
            self.step(op="transfer", site=SITE, at=751)["decision"],
            "transfer_blocked")
        self.step(op="load_test", site=SITE, ok=True, test_w=700.0, at=760)
        # 转换瞬间电池先脱开（2 秒短断，合同上限 10 秒）。
        self.step(op="meter_reading", site=SITE, at=778,
                  battery_w=0.0, gen_w=0.0)
        transfer = self.step(op="transfer", site=SITE, at=780)
        self.assertEqual(transfer["decision"], "transferred")
        self.step(op="meter_reading", site=SITE, at=780, gen_w=700.0)
        detected = self.step(op="detect_interruptions", site=SITE,
                             window_start=0, window_end=1800)
        breaks = detected["interruptions"]
        self.assertEqual({b["load"] for b in breaks},
                         {"trading", "noncritical"})
        self.assertTrue(all(b["duration_s"] == 2 and b["within_contract"]
                            for b in breaks))

        # 中央进程丢失并联签章：禁止再合闸，未确认前不得宣布备用电。
        self.assertEqual(
            self.step(op="lose_parallel_receipt", site=SITE, at=800)["decision"],
            "receipt_lost")
        self.assertEqual(
            self.step(op="reclose_probe", site=SITE, at=801)["decision"],
            "reclose_forbidden")
        self.assertEqual(
            self.step(op="declare_backup", site=SITE, at=802)["decision"],
            "unconfirmed_backup")
        rec = self.step(op="reconcile_source", site=SITE, at=803)
        self.assertEqual(rec["decision"], "source_recognized")
        self.assertEqual(rec["source"], "generator")
        self.assertEqual(rec["basis"], "physical_meter")
        self.assertEqual(
            self.step(op="declare_backup", site=SITE, at=804)["decision"],
            "backup_declared")

        # 事后独立电表校正：真实短断为 12 秒，超限，产生 SLA 补偿。
        self.step(op="correct_meter", site=SITE, at=780,
                  battery_w=0.0, gen_w=0.0,
                  window_start=0, window_end=1800)
        corrected = self.step(op="correct_meter", site=SITE, at=790,
                              gen_w=700.0, window_start=0,
                              window_end=1800)
        self.assertEqual(len(corrected["interruptions"]), 2)
        self.assertTrue(all(b["duration_s"] == 12 and not b["within_contract"]
                            and b["corrected"] for b in
                            corrected["interruptions"]))
        self.step(op="meter_reading", site=SITE, at=1200, gen_w=700.0)
        self.step(op="meter_reading", site=SITE, at=1800, grid_w=700.0,
                  gen_w=0.0)
        balance = self.step(op="close_balance", site=SITE, window_start=0,
                            window_end=1800, tolerance_wh=0.05)
        self.assertEqual(balance["decision"], "balanced")
        self.assertAlmostEqual(balance["unserved_wh"],
                               700.0 * 12 / 3600, places=4)
        report = self.step(op="interruption_report", site=SITE)
        self.assertTrue(all(i["compensated"] and not i["within_contract"]
                            for i in report["interruptions"]))
        comps = {(c["customer"], c["reason"]) for c in report["compensations"]}
        self.assertEqual(
            comps, {("trading-co", "sla_breach"),
                    ("retail-co", "sla_breach")})

    def test_generator_late_misses_battery_window(self):
        # 小容量 pack：可保证窗口约 3.5 分钟，发电车 15 分钟才到。
        self.build_site(capacity_wh=60.0)
        self.step(op="gen_dispatch", site=SITE, truck="truck-3", eta=720,
                  rental_cost=5000.0, fuel_w=700.0, at=0)
        # 表计沿时间轴记录持续放电：到 600s 时名义能量 49.32Wh 已放完。
        for t in (0, 120, 300, 600):
            self.step(op="meter_reading", site=SITE, at=t, battery_w=700.0)
        arr = self.step(op="gen_arrive", site=SITE, at=900)
        self.assertEqual(arr["decision"], "generator_late")
        self.step(op="gen_wire", site=SITE, at=920)
        self.step(op="phase_check", site=SITE, ok=True, at=940)
        self.step(op="load_test", site=SITE, ok=True, test_w=700.0, at=960)
        blocked = self.step(op="transfer", site=SITE, at=980)
        self.assertEqual(blocked["decision"], "battery_exhausted")
        gen = pc._one(self.db, "SELECT * FROM pc_gen_state WHERE site=?",
                      (SITE,))
        self.assertIsNone(gen["transferred_at"])

    def test_consent_withdrawal_revokes_shed_and_blocks_renewal(self):
        self.build_site()
        self.step(op="consent", site=SITE, load="noncritical", granted=1, at=10)
        self.assertEqual(
            self.step(op="shed_load", site=SITE, load="noncritical",
                      compensation=120.0, at=20)["decision"], "load_shed")
        out = self.step(op="withdraw_consent", site=SITE,
                        load="noncritical", at=120)
        self.assertEqual(out["decision"], "withdrawn_restore_required")
        load = pc._one(self.db, "SELECT * FROM pc_loads WHERE site=? AND load=?",
                       (SITE, "noncritical"))
        self.assertEqual(load["shed"], 0)
        ev = pc._one(
            self.db,
            "SELECT * FROM pc_shed_events WHERE site=? AND load=?",
            (SITE, "noncritical"))
        self.assertEqual(ev["invalid"], 1)
        self.assertEqual(
            self.step(op="shed_load", site=SITE, load="noncritical",
                      compensation=120.0, at=130)["decision"],
            "consent_withdrawn")

    def test_no_backup_claim_without_physical_evidence(self):
        self.build_site()
        self.assertEqual(
            self.step(op="reconcile_source", site=SITE, at=0)["decision"],
            "no_meter_evidence")
        self.assertEqual(
            self.step(op="declare_backup", site=SITE, at=0)["decision"],
            "unconfirmed_backup")
        self.step(op="meter_reading", site=SITE, at=0, battery_w=700.0)
        self.step(op="reconcile_source", site=SITE, at=1)
        self.assertEqual(
            self.step(op="declare_backup", site=SITE, at=2)["decision"],
            "backup_declared")

    def test_temperature_health_and_curve_shape_the_window(self):
        self.build_site(with_isolated=False)
        warm = self.step(op="guarantee", site=SITE, at=0)
        self.step(op="telemetry", site=SITE, pack="pack-a", soc=0.90,
                  soh=0.8, temp_c=0.0, at=10)
        cold = self.step(op="guarantee", site=SITE, at=10)
        # 低温折减 0.8 系数 + SOH 0.8：可用能量缩水至 0.64 以下。
        self.assertLess(cold["available_wh_high"],
                        warm["available_wh_high"] * 0.72)


if __name__ == "__main__":
    unittest.main()
