# -*- coding: utf-8 -*-
import unittest

from netcourt.store import connect, one, rows
from netcourt.notice_dispatch import enqueue
from netcourt.maintenance_start import provisional_start
from netcourt import maintenance_recovery as mr


# 统一时钟：所有时刻为 epoch 秒；窗口按各户时区换算后的当地星期/分钟表达。
# 选定时刻在 UTC+8 为周三 14:00、UTC+9 为周三 15:00，均落在两户白天窗口内。
PLANNED_START = 10 * 86400 + 2 * 3600
JIA = "customer-jia"
YI = "customer-yi"


class MaintenanceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        mr.ensure_schema(self.db)
        self._profiles()
        wd = mr.local_clock(PLANNED_START, 480)[0]
        # 甲：UTC+8 合同门户，本人确认，提前 1440 分钟，09:00-18:00 窗口
        self.step(op="window_consent", id="w-jia", customer=JIA, revision="rev-1",
                 weekday=wd, start_minute=9 * 60, end_minute=18 * 60)
        # 乙：UTC+9 值班电话，本人确认，提前 720 分钟，10:00-17:00 窗口
        self.step(op="window_consent", id="w-yi", customer=YI, revision="rev-1",
                 weekday=wd, start_minute=10 * 60, end_minute=17 * 60)

    def tearDown(self):
        self.db.close()

    def step(self, **x):
        return mr.run(self.db, x)

    def _profiles(self):
        self.step(op="profile", customer=JIA, contract_channel="registered_email",
                 recipient="jia-self", timezone_minutes=480, lead_minutes=1440,
                 sla_free_minutes=30, tariff_per_minute=10)
        self.step(op="profile", customer=YI, contract_channel="duty_phone",
                 recipient="yi-duty", timezone_minutes=540, lead_minutes=720,
                 sla_free_minutes=60, tariff_per_minute=5)

    def _switch_jia_to_portal(self):
        # 甲的邮箱停用后，合同渠道变更为合同门户（旧退信原件保留）
        self.step(op="profile", customer=JIA, contract_channel="registered_portal",
                 recipient="jia-self", timezone_minutes=480, lead_minutes=1440,
                 sla_free_minutes=30, tariff_per_minute=10)

    def _jia_email_bounce(self, notice_id, at):
        self.step(op="notice_event", id=notice_id, customer=JIA, revision="rev-1",
                 channel="registered_email", actor="jia-self", status="signed",
                 at=at, window_id="w-jia")
        return self.step(op="notice_event", id=f"{notice_id}-bounce", customer=JIA,
                        revision="rev-1", channel="registered_email",
                        status="bounced", at=at + 10, bounce_of=notice_id)

    def _issue_order(self, order="wo-1"):
        return self.step(op="work_order", id=order, revision="rev-1",
                        planned_start=PLANNED_START, at=100,
                        items=[{"item": "term-1", "customers": [JIA, YI]}])

    def _formal_start(self, order="wo-1"):
        return self.step(op="formal_start", work_order=order)

    def _crash_field_evidence(self, order="wo-1"):
        """模拟进程崩溃前现场已发生、但控制端只剩一条会丢失的回执。"""
        # 控制端回执：崩溃时丢失，不能作为认回依据
        self.step(op="field_receipt", id="rcpt-lost", work_order=order, item="term-1",
                 node="ctrl-1", kind="jumper_receipt", at=15_000)
        # 独立现场证据：班组设备日志与现场签章照片，独立于中央进程
        self.step(op="field_evidence", id="ev-disconnect", work_order=order,
                 item="term-1", kind="disconnect", source="crew-device-log", at=12_000)
        self.step(op="field_evidence", id="ev-jumper", work_order=order,
                 item="term-1", kind="jumper_installed", source="signed-site-photo",
                 at=15_000)

    # ------------------------------------------------------------ 1. 临时 vs 正式
    def test_provisional_true_but_formal_denied_after_bounce(self):
        # 同一份入队数据：增强副本两户各一条队列记录即 provisional true
        enqueue(self.db, "n-jia", JIA, "rev-1", 10)
        enqueue(self.db, "n-yi", YI, "rev-1", 10)
        self.assertTrue(provisional_start(self.db, [JIA, YI], "rev-1"))

        self._issue_order()
        # 传输商接单也不等于客户签收
        self.step(op="notice_event", id="n-jia", customer=JIA, revision="rev-1",
                 channel="registered_email", status="carrier_accepted", at=20)
        self.step(op="notice_event", id="n-yi", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi")
        denied = self._formal_start()
        self.assertEqual(denied["decision"], "start_denied")
        self.assertFalse(denied["authorized"])
        jia_check = next(c for c in denied["checks"] if c["customer"] == JIA)
        self.assertEqual(jia_check["reason"], "no_signed_receipt")
        yi_check = next(c for c in denied["checks"] if c["customer"] == YI)
        self.assertTrue(yi_check["ok"])
        # 乙的确认不能授权甲承担停机
        self.assertFalse(any(c["ok"] for c in denied["checks"] if c["customer"] == JIA))

        # 甲的邮箱退信：即使曾有签章也被推翻
        self._jia_email_bounce("n-jia-2", at=400)
        denied2 = self._formal_start()
        jia_check2 = next(c for c in denied2["checks"] if c["customer"] == JIA)
        self.assertEqual(jia_check2["reason"], "bounced")
        self.assertFalse(denied2["authorized"])
        # 退信原件保留
        self.assertTrue(one(
            self.db,
            "SELECT id FROM mr_notice_events WHERE status='bounced' AND bounce_of=?",
            ("n-jia-2",)))

        # 甲改合同渠道后经门户由本人重新确认，提前期/窗口逐户重算
        self._switch_jia_to_portal()
        self.step(op="notice_event", id="n-jia-portal", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="jia-self", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-jia")
        granted = self._formal_start()
        self.assertEqual(granted["decision"], "start_authorized")
        self.assertTrue(granted["authorized"])

    def test_lead_time_timezone_and_window_are_per_customer(self):
        self._issue_order()
        # 甲提前期 1440 分钟：只提前 1000 分钟 -> 不足
        self._switch_jia_to_portal()
        self.step(op="notice_event", id="n-jia-late", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="jia-self", status="signed",
                 at=PLANNED_START - 1000 * 60, window_id="w-jia")
        # 乙撤回白天窗口，只保留落在其工作窗口之外的夜间同意
        self.step(op="withdraw_window", id="w-yi", customer=YI)
        wd = mr.local_clock(PLANNED_START, 540)[0]
        self.step(op="window_consent", id="w-yi-night", customer=YI, revision="rev-1",
                 weekday=wd, start_minute=20 * 60, end_minute=23 * 60)
        self.step(op="notice_event", id="n-yi-night", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi-night")
        result = self._formal_start()
        reasons = {c["customer"]: c["reason"] for c in result["checks"]}
        self.assertEqual(reasons[JIA], "notice_terms_not_satisfied")  # 提前期不足
        self.assertEqual(reasons[YI], "notice_terms_not_satisfied")   # 窗口不匹配

        # 各自修正后通过：甲补足提前期；乙重新给出白天窗口与确认
        self.step(op="notice_event", id="n-jia-ok", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="jia-self", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-jia")
        wd2 = mr.local_clock(PLANNED_START, 540)[0]
        self.step(op="window_consent", id="w-yi-day", customer=YI, revision="rev-1",
                 weekday=wd2, start_minute=10 * 60, end_minute=17 * 60)
        self.step(op="notice_event", id="n-yi-ok", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi-day")
        self.assertTrue(self._formal_start()["authorized"])

    # ------------------------------------------------------------ 2. 崩溃认回
    def test_reconcile_uses_independent_evidence_never_lost_receipt(self):
        self._issue_order()
        self._crash_field_evidence()
        rec = self.step(op="reconcile", work_order="wo-1", at=24_000)
        self.assertEqual(rec["decision"], "reconciled")
        item = next(i for i in rec["items"] if i["item"] == "term-1")
        self.assertEqual(item["state"], mr.STATE_JUMPERED)
        self.assertIn("ev-disconnect", item["basis"])
        self.assertIn("ev-jumper", item["basis"])
        # 崩溃回执被标记丢失，且没有被当成进度依据
        self.assertEqual(one(self.db, "SELECT lost FROM mr_receipts WHERE id='rcpt-lost'")["lost"], 1)
        self.assertNotIn("rcpt-lost", item["basis"])
        # 半完成状态必须被显式接管，不能搁置
        self.assertEqual([i["item"] for i in rec["half_finished"]], ["term-1"])
        # 真实中断时段已按最早断开证据为两户起算，恢复时刻尚未知
        for customer in (JIA, YI):
            o = one(self.db, "SELECT * FROM mr_outages WHERE work_order=? AND customer=?",
                    ("wo-1", customer))
            self.assertEqual(o["started"], 12_000)
            self.assertIsNone(o["restored"])
        # 再次认回是幂等的，不会新增中断或再派工
        rec2 = self.step(op="reconcile", work_order="wo-1", at=24_060)
        self.assertEqual(rec2["decision"], "reconciled")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS c FROM mr_outages WHERE work_order='wo-1'").fetchone()["c"],
            2)

    def test_bounce_must_not_leave_device_in_unsafe_half_state(self):
        # 甲退信与现场崩溃同时发生：退信否定的是开工授权，不能据此停工搁置设备
        self._issue_order()
        self._crash_field_evidence()
        self._jia_email_bounce("n-jia", at=18_000)
        # 正式开工权限确实被否定（甲未真正获知）
        self.assertFalse(self._formal_start()["authorized"])
        # 但认回只看独立现场证据，照常认定半完成状态并要求接管，不因退信而中止
        rec = self.step(op="reconcile", work_order="wo-1", at=24_000)
        self.assertEqual(rec["decision"], "reconciled")
        self.assertEqual(rec["half_finished"][0]["state"], mr.STATE_JUMPERED)
        self.assertFalse(rec["rebooked_or_redispatched"])  # 不会自动再派班组拆端子
        # 半完成设备必须走到一条真实处置路径，不能停留：安全恢复重约可行
        self.step(op="field_evidence", id="ev-safe", work_order="wo-1", item="term-1",
                 kind="safety_check", source="safety-officer", at=27_000)
        decision = self.step(op="restore_rebook", work_order="wo-1", item="term-1",
                             revision="rev-1", child_work_order="wo-2",
                             planned_start=PLANNED_START + 7 * 86400,
                             labor_cost=500, at=30_000)
        self.assertEqual(decision["decision"], "restored_and_rebooked")
        # 设备已回到安全的未碰状态，不再有未接管的半完成工项
        self.assertEqual(
            one(self.db, "SELECT state FROM mr_work_items WHERE work_order='wo-1' AND item='term-1'")["state"],
            mr.STATE_NOT_TOUCHED)

    def test_duplicate_dispatch_does_not_reopen_terminal_work(self):
        self._issue_order()
        again = self._issue_order()
        self.assertEqual(again["decision"], "duplicate_dispatch_rejected")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) AS c FROM mr_work_items WHERE work_order='wo-1'").fetchone()["c"],
            1)

    # ------------------------------------------------------------ 3. 路径一：恢复重约
    def test_path_restore_and_rebook_with_second_outage(self):
        self._issue_order()
        self._crash_field_evidence()
        self.step(op="reconcile", work_order="wo-1", at=24_000)
        # 没有安全检查不得恢复
        self.assertEqual(
            self.step(op="restore_rebook", work_order="wo-1", item="term-1",
                     revision="rev-1", child_work_order="wo-2",
                     planned_start=PLANNED_START + 7 * 86400, labor_cost=500,
                     at=30_000)["decision"],
            "safety_check_required")
        self.step(op="field_evidence", id="ev-safe", work_order="wo-1", item="term-1",
                 kind="safety_check", source="safety-officer", at=27_000)
        restored = self.step(op="restore_rebook", work_order="wo-1", item="term-1",
                            revision="rev-1", child_work_order="wo-2",
                            planned_start=PLANNED_START + 7 * 86400,
                            labor_cost=500, at=30_000)
        self.assertEqual(restored["decision"], "restored_and_rebooked")
        self.assertTrue(restored["second_outage_reserved"])
        # 第一段中断在恢复时刻关闭：12000 -> 30000 秒 = 300 分钟，设备回到未碰状态
        for customer in (JIA, YI):
            o = one(self.db, "SELECT * FROM mr_outages WHERE work_order='wo-1' AND customer=?",
                    (customer,))
            self.assertEqual((o["restored"], o["minutes"]), (30_000, 300))
        self.assertEqual(
            one(self.db, "SELECT state FROM mr_work_items WHERE work_order='wo-1' AND item='term-1'")["state"],
            mr.STATE_NOT_TOUCHED)
        # 重约工单必须重新逐户通知，旧签收不能复用
        self.assertFalse(self._formal_start("wo-2")["authorized"])
        self._switch_jia_to_portal()
        s2 = PLANNED_START + 7 * 86400
        # s2 落在另一星期：两户须按各自时区重新给出窗口
        wd_jia2 = mr.local_clock(s2, 480)[0]
        wd_yi2 = mr.local_clock(s2, 540)[0]
        self.step(op="window_consent", id="w2-jia", customer=JIA, revision="rev-1",
                  weekday=wd_jia2, start_minute=9 * 60, end_minute=18 * 60)
        self.step(op="window_consent", id="w2-yi", customer=YI, revision="rev-1",
                  weekday=wd_yi2, start_minute=10 * 60, end_minute=17 * 60)
        self.step(op="notice_event", id="n2-jia", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="jia-self", status="signed",
                 at=s2 - 2 * 86400, window_id="w2-jia")
        self.step(op="notice_event", id="n2-yi", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=s2 - 2 * 86400, window_id="w2-yi")
        self.assertTrue(self._formal_start("wo-2")["authorized"])
        # 第二次施工实际断开（t=s2），50 分钟后恢复：回填预留停机时段
        self.step(op="field_evidence", id="ev2-disconnect", work_order="wo-2",
                 item="term-1", kind="disconnect", source="crew-device-log",
                 at=s2)
        self.step(op="reconcile", work_order="wo-2", at=s2 + 60)
        self.step(op="field_evidence", id="ev2-restore", work_order="wo-2",
                 item="term-1", kind="safety_check", source="crew-device-log",
                 at=s2 + 50 * 60)
        self.step(op="close_outage", work_order="wo-2", item="term-1",
                 evidence_id="ev2-restore", at=s2 + 50 * 60)
        # 两户各经历两次停机；wo-2 下第二次停机恰为 50 分钟
        for customer, free, tariff in ((JIA, 30, 10), (YI, 60, 5)):
            cs = mr.fulfillment_summary(self.db, "wo-2")["customers"][customer]
            self.assertEqual(cs["outage_count"], 1)
            self.assertEqual(cs["minutes"], 50)
            self.assertEqual(cs["compensation_due"], max(0, 50 - free) * tariff)
        # 两工单各自按真实停机计赔（付款原件按工单留存）
        due1 = {c: mr.fulfillment_summary(self.db, "wo-1")["customers"][c]["compensation_due"]
                for c in (JIA, YI)}
        # wo-1：甲 (300-30)*10=2700；乙 (300-60)*5=1200
        self.assertEqual(due1, {JIA: 2700, YI: 1200})
        pay_jia = self.step(op="pay_compensation", id="pay-jia", work_order="wo-1",
                           customer=JIA, amount=2700, reason="sla_relief", at=60_000)
        self.assertEqual(pay_jia["decision"], "compensation_paid")
        self.assertEqual(
            self.step(op="pay_compensation", id="pay-jia-dup", work_order="wo-1",
                     customer=JIA, amount=2700, reason="sla_relief", at=60_060)["decision"],
            "duplicate_payment_rejected")
        self.assertEqual(
            self.step(op="pay_compensation", id="pay-yi-wrong", work_order="wo-1",
                     customer=YI, amount=999, reason="sla_relief", at=60_120)["decision"],
            "amount_mismatch")
        self.step(op="pay_compensation", id="pay-yi", work_order="wo-1", customer=YI,
                 amount=1200, reason="sla_relief", at=60_180)
        # 两段停机合计逐户：均为 350 分钟；合并合同减免 甲3200 / 乙1450
        total = {c: mr.outage_minutes(self.db, "wo-1", c) + mr.outage_minutes(self.db, "wo-2", c)
                 for c in (JIA, YI)}
        self.assertEqual(total, {JIA: 350, YI: 350})
        combined = {
            JIA: max(0, 350 - 30) * 10,
            YI: max(0, 350 - 60) * 5,
        }
        self.assertEqual(combined, {JIA: 3200, YI: 1450})
        # 真实中断记录与减免原件按户交付
        self.assertEqual(
            self.step(op="deliver_records", id="rec-jia", work_order="wo-1",
                     customer=JIA, channel="registered_portal", actor="jia-self",
                     status="delivered", at=61_000)["decision"],
            "records_delivered")

    # ------------------------------------------------------------ 4. 路径二：必要继续
    def test_path_authorized_continue_and_post_hoc_explanation(self):
        self._issue_order()
        self._crash_field_evidence()
        self.step(op="reconcile", work_order="wo-1", at=24_000)
        # 未授权负责人不能签发必要性处置
        self.assertEqual(
            self.step(op="authorize_continue", work_order="wo-1", item="term-1",
                     lead="lead-1", cert_id="cert-1",
                     reason="avoid further damage on exposed terminals", at=25_200)["decision"],
            "lead_not_authorized")
        self.step(op="site_lead", id="lead-1", name="现场负责人",
                 authorization="continue_work", at=24_600)
        auth = self.step(op="authorize_continue", work_order="wo-1", item="term-1",
                        lead="lead-1", cert_id="cert-1",
                        reason="avoid further damage on exposed terminals", at=25_200)
        self.assertEqual(auth["decision"], "continue_authorized")
        # 计划失误事实保留：工单带缺陷，不能洗白成合规开工
        self.assertEqual(one(self.db, "SELECT defect FROM mr_work_orders WHERE id='wo-1'")["defect"], 1)
        # 无不可逆证据不得完工
        self.assertEqual(
            self.step(op="complete_continue", work_order="wo-1", item="term-1",
                     labor_cost=800, at=54_000)["decision"],
            "irreversible_evidence_missing")
        self.step(op="field_evidence", id="ev-irreversible", work_order="wo-1",
                 item="term-1", kind="irreversible_work", source="signed-work-log",
                 at=48_000)
        done = self.step(op="complete_continue", work_order="wo-1", item="term-1",
                        labor_cost=800, at=54_000)
        self.assertEqual(done["decision"], "completed")
        # 完成后立即向甲本人说明；非合同渠道/非本人不认
        self._switch_jia_to_portal()
        self.assertEqual(
            self.step(op="explain_in_person", id="explain-jia-bad", work_order="wo-1",
                     customer=JIA, channel="registered_email", actor="jia-self",
                     at=54_600)["decision"],
            "non_contract_channel")
        explained = self.step(op="explain_in_person", id="explain-jia", work_order="wo-1",
                             customer=JIA, channel="registered_portal", actor="jia-self",
                             at=54_600, detail="真实经过与减免说明")
        self.assertEqual(explained["decision"], "explained")
        # 甲事后拒收通知：拒收是真实送达结果，原件保留，不能改成已读
        refusal = self.step(op="explain_in_person", id="explain-jia-2", work_order="wo-1",
                           customer=JIA, channel="registered_portal", actor="jia-self",
                           status="refused", at=55_200)
        self.assertEqual(refusal["decision"], "refusal_recorded")
        self.assertTrue(one(self.db,
                            "SELECT id FROM mr_communications WHERE id='explain-jia-2' AND status='refused'"))
        # 单一连续停机 700 分钟（12000 -> 54000 秒）：甲 (700-30)*10=6700；乙 (700-60)*5=3200
        summary = mr.fulfillment_summary(self.db, "wo-1")
        self.assertEqual(summary["customers"][JIA]["compensation_due"], 6700)
        self.assertEqual(summary["customers"][YI]["compensation_due"], 3200)
        self.step(op="pay_compensation", id="pay-jia-b", work_order="wo-1", customer=JIA,
                 amount=6700, reason="sla_relief", at=60_000)
        self.step(op="pay_compensation", id="pay-yi-b", work_order="wo-1", customer=YI,
                 amount=3200, reason="sla_relief", at=60_060)
        for customer in (JIA, YI):
            self.step(op="deliver_records", id=f"rec-{customer}", work_order="wo-1",
                     customer=customer, channel="registered_portal",
                     actor="jia-self" if customer == JIA else "yi-duty",
                     status="delivered", at=60_600)
        paid = mr.fulfillment_summary(self.db, "wo-1")["customers"]
        self.assertEqual(paid[JIA]["compensation_paid"], 6700)
        self.assertEqual(paid[YI]["compensation_paid"], 3200)
        # 路径二只有一次停机、恢复时点更晚，但省去恢复重约的二次停机与人工
        self.assertEqual(paid[JIA]["outage_count"], 1)

    def test_jumper_failure_blocks_restore_but_allows_necessary_continue(self):
        self._issue_order()
        self.step(op="field_evidence", id="ev-disconnect", work_order="wo-1",
                 item="term-1", kind="disconnect", source="crew-device-log", at=12_000)
        self.step(op="field_evidence", id="ev-jumper-fail", work_order="wo-1",
                 item="term-1", kind="jumper_failed", source="signed-site-photo",
                 at=15_600)
        rec = self.step(op="reconcile", work_order="wo-1", at=24_000)
        self.assertEqual(rec["items"][0]["state"], mr.STATE_JUMPER_FAILED)
        self.step(op="field_evidence", id="ev-safe", work_order="wo-1", item="term-1",
                 kind="safety_check", source="safety-officer", at=27_000)
        # 跨接失败：安全恢复路径不再可用，设备不能留在不安全的半完成状态
        self.assertEqual(
            self.step(op="restore_rebook", work_order="wo-1", item="term-1",
                     revision="rev-1", child_work_order="wo-2",
                     planned_start=PLANNED_START + 7 * 86400, labor_cost=500,
                     at=30_000)["decision"],
            "not_safely_recoverable")
        self.step(op="site_lead", id="lead-1", name="现场负责人",
                 authorization="continue_work", at=24_600)
        self.assertEqual(
            self.step(op="authorize_continue", work_order="wo-1", item="term-1",
                     lead="lead-1", cert_id="cert-1", reason="jumper failed; continue required",
                     at=25_200)["decision"],
            "continue_authorized")

    # ------------------------------------------------------------ 5. 代理人替换
    def test_agent_replacement_keeps_prior_signature_revokes_new_power(self):
        self._issue_order()
        self._switch_jia_to_portal()
        # 旧代理人在授权有效期内代甲确认（有效期覆盖签收时刻）
        self.step(op="replace_agent", id="del-old", customer=JIA, agent="old-agent",
                 valid_from=PLANNED_START - 3 * 86400, valid_to=PLANNED_START + 10)
        self.step(op="notice_event", id="n-jia-agent", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="old-agent", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-jia")
        self.step(op="notice_event", id="n-yi", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi")
        self.assertTrue(self._formal_start()["authorized"])
        # 替换代理人：旧授权撤销，替换前的签收仍然有效（不追溯否定）
        self.step(op="replace_agent", id="del-new", customer=JIA, agent="new-agent",
                 valid_from=PLANNED_START - 86400, valid_to=PLANNED_START + 10)
        self.assertTrue(self._formal_start()["authorized"])
        # 新代理人无权在替换生效前签字；无有效代理的陌生人更不行
        self.step(op="notice_event", id="n-jia-stranger", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="stranger", status="signed",
                 at=PLANNED_START - 100, window_id="w-jia")
        checks = {c["customer"]: c for c in self._formal_start()["checks"]}
        # 旧签收仍在，甲依旧通过；但陌生人签收不具资格（不会被选中）
        self.assertTrue(checks[JIA]["ok"])
        self.assertNotEqual(checks[JIA].get("actor"), "stranger")
        # 旧代理人在撤销后作出的新签收无效
        self.step(op="window_consent", id="w-jia-b", customer=JIA, revision="rev-1",
                 weekday=mr.local_clock(PLANNED_START, 480)[0],
                 start_minute=9 * 60, end_minute=18 * 60)
        self.step(op="notice_event", id="n-jia-old-revoked", customer=JIA,
                 revision="rev-1", channel="registered_portal", actor="old-agent",
                 status="signed", at=PLANNED_START - 50, window_id="w-jia-b")
        # 撤回原窗口后，只剩两条无效签收，正式授权被否定
        self.step(op="withdraw_window", id="w-jia", customer=JIA)
        denied = self._formal_start()
        self.assertFalse(denied["authorized"])
        self.assertEqual(
            next(c for c in denied["checks"] if c["customer"] == JIA)["reason"],
            "notice_terms_not_satisfied")

    # ------------------------------------------------------------ 6. 乙撤回窗口
    def test_yi_window_withdrawal_denies_start_until_reconsented(self):
        self._issue_order()
        self._switch_jia_to_portal()
        self.step(op="notice_event", id="n-jia", customer=JIA, revision="rev-1",
                 channel="registered_portal", actor="jia-self", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-jia")
        self.step(op="notice_event", id="n-yi", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi")
        self.assertTrue(self._formal_start()["authorized"])
        self.assertEqual(
            self.step(op="withdraw_window", id="w-yi", customer=YI)["decision"],
            "window_withdrawn")
        denied = self._formal_start()
        self.assertFalse(denied["authorized"])
        self.assertEqual(
            next(c for c in denied["checks"] if c["customer"] == YI)["reason"],
            "notice_terms_not_satisfied")
        # 乙重新给出窗口与确认后才恢复；甲的状态不受乙撤回影响
        wd = mr.local_clock(PLANNED_START, 540)[0]
        self.step(op="window_consent", id="w-yi-2", customer=YI, revision="rev-1",
                 weekday=wd, start_minute=10 * 60, end_minute=17 * 60)
        self.step(op="notice_event", id="n-yi-2", customer=YI, revision="rev-1",
                 channel="duty_phone", actor="yi-duty", status="signed",
                 at=PLANNED_START - 2 * 86400, window_id="w-yi-2")
        self.assertTrue(self._formal_start()["authorized"])

    # ------------------------------------------------------------ 7. 紧急例外及其推翻
    def _emergency_order(self):
        # 故障告警发生在任何断开动作之前：独立故障事实
        self._issue_order("wo-em")
        self.step(op="field_evidence", id="ev-fault", work_order="wo-em",
                 item="term-1", kind="fault_alarm", source="independent-monitor",
                 at=6_000)
        self.step(op="field_evidence", id="ev-disconnect", work_order="wo-em",
                 item="term-1", kind="disconnect", source="crew-device-log", at=12_000)
        self.step(op="emergency_authorizer", id="auth-1", name="安全值班授权人",
                 scope="safety_emergency", at=9_000)
        self.step(op="explain_in_person", id="post-jia", work_order="wo-em",
                 customer=JIA, channel="registered_email", actor="jia-self",
                 at=15_600, detail="紧急处置事后通知")
        self.step(op="explain_in_person", id="post-yi", work_order="wo-em",
                 customer=YI, channel="duty_phone", actor="yi-duty",
                 at=15_660, detail="紧急处置事后通知")

    def test_emergency_requires_three_elements_and_can_be_overturned(self):
        self._emergency_order()
        # 缺授权人/缺事后通知/缺故障事实分别拒绝
        self.assertEqual(
            self.step(op="file_emergency", id="em-1", work_order="wo-em",
                     fault_evidence="ev-fault", authorizer="nobody",
                     post_notice="post-jia", at=18_000)["decision"],
            "authorizer_missing")
        self.assertEqual(
            self.step(op="file_emergency", id="em-1", work_order="wo-em",
                     fault_evidence="ev-fault", authorizer="auth-1",
                     post_notice="missing", at=18_000)["decision"],
            "post_hoc_notice_missing")
        self.assertEqual(
            self.step(op="file_emergency", id="em-1", work_order="wo-em",
                     fault_evidence="no-fault", authorizer="auth-1",
                     post_notice="post-jia", at=18_000)["decision"],
            "independent_fault_fact_missing")
        # 三要素齐备才成立
        ok = self.step(op="file_emergency", id="em-1", work_order="wo-em",
                      fault_evidence="ev-fault", authorizer="auth-1",
                      post_notice="post-jia", at=18_000)
        self.assertEqual(ok["decision"], "emergency_filed")
        # 紧急证据后来被推翻：原案保留为 overturned，停机重新按计划失误计赔
        over = self.step(op="overturn_emergency", id="em-1", at=30_000)
        self.assertEqual(over["decision"], "emergency_overturned")
        self.assertEqual(
            one(self.db, "SELECT status FROM mr_emergency_cases WHERE id='em-1'")["status"],
            "overturned")
        self.step(op="reconcile", work_order="wo-em", at=24_000)
        self.step(op="field_evidence", id="ev-irreversible", work_order="wo-em",
                 item="term-1", kind="irreversible_work", source="signed-work-log",
                 at=23_400)
        self.step(op="close_outage", work_order="wo-em", item="term-1",
                 evidence_id="ev-irreversible", at=24_000)
        summary = mr.fulfillment_summary(self.db, "wo-em")
        # 推翻后不再有紧急豁免：200 分钟停机按合同 SLA 实际减免
        self.assertEqual(summary["customers"][JIA]["compensation_due"],
                         max(0, 200 - 30) * 10)
        self.assertEqual(summary["customers"][YI]["compensation_due"],
                         max(0, 200 - 60) * 5)

    def test_planned_mistake_cannot_be_relabelled_emergency(self):
        # 计划维护已经断开端子，之后才补“故障”证据：时间顺序不支持紧急覆盖
        self._issue_order("wo-plan")
        self.step(op="field_evidence", id="ev-disconnect", work_order="wo-plan",
                 item="term-1", kind="disconnect", source="crew-device-log", at=12_000)
        self.step(op="field_evidence", id="ev-late-fault", work_order="wo-plan",
                 item="term-1", kind="fault_alarm", source="monitor", at=15_000)
        self.step(op="emergency_authorizer", id="auth-1", name="安全值班授权人",
                 scope="safety_emergency", at=15_600)
        self.step(op="explain_in_person", id="post-jia", work_order="wo-plan",
                 customer=JIA, channel="registered_email", actor="jia-self", at=16_200)
        rejected = self.step(op="file_emergency", id="em-late", work_order="wo-plan",
                            fault_evidence="ev-late-fault", authorizer="auth-1",
                            post_notice="post-jia", at=18_000)
        self.assertEqual(rejected["decision"], "retroactive_emergency_rejected")
        # 没有任何紧急案件被制造出来，计划失误只能走真实恢复/补偿
        self.assertIsNone(one(self.db, "SELECT id FROM mr_emergency_cases"))

    def test_records_are_append_only_originals(self):
        self._issue_order()
        self._crash_field_evidence()
        self.step(op="reconcile", work_order="wo-1", at=24_000)
        # 证据、通知、通讯、付款原件均不可复用覆盖
        self.assertEqual(
            self.step(op="field_evidence", id="ev-disconnect", work_order="wo-1",
                     item="term-1", kind="disconnect", source="other", at=12_060)["decision"],
            "reused")
        original = one(self.db, "SELECT source FROM mr_field_evidence WHERE id='ev-disconnect'")
        self.assertEqual(original["source"], "crew-device-log")
        self._jia_email_bounce("n-jia", at=24_600)
        # 同一通知同一状态重复登记被拒绝，退信原件不被覆盖
        self.assertEqual(
            self.step(op="notice_event", id="n-jia", customer=JIA, revision="rev-1",
                     channel="registered_email", actor="jia-self", status="signed",
                     at=24_660, window_id="w-jia")["decision"],
            "reused")
        # 原签收与退信两份原件都保留（主键含状态/时刻，追加而非覆盖）
        statuses = {
            r["status"] for r in rows(
                self.db,
                "SELECT status FROM mr_notice_events WHERE id='n-jia'")
        }
        self.assertEqual(statuses, {"signed", "bounced"})


if __name__ == "__main__":
    unittest.main()
