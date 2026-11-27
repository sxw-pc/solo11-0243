"""应急预警：公文授权、真实触达等级、跨网络覆盖、对账恢复与更正的交付测试。

运行：
    python -B -m unittest discover -s tests -q

测试从题目指定的反例开始：同一区域在旧队列提交前 ``provisional_warned`` 为 False，
提交后为 True，但运营商实际并未广播——正式结论必须仍是“未送达”。随后分别完成
题目给出的两种有效跨网络覆盖方案，并覆盖边界调整、漫游、重复回执、无人接听、
撤权/到期、错误文案更正以及“提交成功但响应丢失”的重启对账。
"""

import tempfile
import unittest
from pathlib import Path

from netcourt.store import connect
from netcourt.public_alert import submit as queue_submit
from netcourt.reach_note import provisional_warned
from netcourt import public_warning as pw


REGION = "R-WATER"
VERSION = "water-2026-11-27.zh-CN.r1"
AUTHORITY = "市应急管理局"


def issue_water_warning(db, wid="w1", **over):
    args = dict(
        warning_id=wid,
        authority=AUTHORITY,
        doc_number="应急水警〔2026〕7号",
        region=REGION,
        text_id="regional-water-outage",
        version=VERSION,
        language="zh-CN",
        starts_at=100,
        ends_at=400,
        at=100,
        body="今日14时至22时第三水厂检修，本区暂停供水，请储水并关照老人。",
        digest="digest-water-v1",
    )
    args.update(over)
    return pw.issue_warning(db, **args)


def build_world(db):
    """题目现场：东区基站在线（受理后失联，广播范围待对账）；西区基站维修中，
    另有一座可用西站；跨边界住户与盲区住户；有所需本人确认的学校。"""
    pw.register_cell(db, "cell-east-1", REGION, "online", "east")
    pw.register_cell(db, "cell-west-fix", REGION, "maintenance", "west")
    pw.register_cell(db, "cell-west-2", REGION, "online", "west")
    pw.register_household(db, "h-e1", REGION, "cell-east-1", name="东一巷1号")
    pw.register_household(db, "h-e2", REGION, "cell-east-1", name="东一巷2号")
    pw.register_household(db, "h-w2", REGION, "cell-west-2", name="西二巷")
    pw.register_household(db, "h-b1", REGION, "cell-west-fix", name="维修站后院")
    pw.register_household(db, "h-b2", REGION, "cell-west-fix", name="河堤独户")
    pw.register_school(db, "school-1", REGION, name="滨河小学",
                       duty_officer="赵值班", duty_phone="8888-110",
                       satellite_terminal="SAT-01")
    pw.register_device(db, "dev-e1", REGION, household="h-e1", kind="cell-phone")
    return {
        "east": ["h-e1", "h-e2"],
        "west_online": ["h-w2"],
        "blind": ["h-b1", "h-b2"],
        "school": "school-1",
    }


class OpeningTrapTest(unittest.TestCase):
    """题目强制的起点：临时判断为已预警，正式结论仍为未送达。"""

    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_provisional_true_after_queue_submit_but_formal_undelivered(self):
        # 提交前：没有任何队列记录，临时判断也是 False（缺表安全）。
        self.assertFalse(provisional_warned(self.db, REGION))
        self.assertFalse(pw.provisional_warned(self.db, REGION))
        self.assertFalse(pw.formal_warned(self.db, REGION))

        # 旧“隔离增强副本”只写区域、摘要、提交时刻到队列。
        queue_submit(self.db, "alert-1", REGION, "digest-1", 10)

        # 旧逻辑见到提交就判该区已预警。
        self.assertTrue(provisional_warned(self.db, REGION))

        # 但运营商实际没有广播、没有任何等级证据：正式结论必须未送达。
        status = pw.delivery_status(self.db, region=REGION)
        self.assertEqual(status["decision"], "undelivered")
        self.assertFalse(status["delivered"])
        self.assertFalse(status["formally_warned"])
        self.assertTrue(status["provisional_warned"])
        self.assertEqual(status["reason"], "no_authorized_warning")

    def test_legacy_four_arg_submit_is_supported_but_still_not_delivery(self):
        # 通过新模块的 submit 兼容旧位置签名 (db, alert, region, digest, at)。
        result = pw.submit(self.db, "alert-2", "R-OTHER", "d2", 11)
        self.assertEqual(result["decision"], "queued_provisional")
        self.assertTrue(pw.provisional_warned(self.db, "R-OTHER"))
        self.assertFalse(pw.formal_warned(self.db, "R-OTHER"))


class PlanACrossNetworkDeliveryTest(unittest.TestCase):
    """方案一：签名流水认回东区 + 同版补投西区可用基站 + 派员盲区现场通知
    + 值守电话找到值班人本人确认。"""

    def setUp(self):
        self.db = connect(":memory:")
        self.world = build_world(self.db)
        issue_water_warning(self.db)

    def tearDown(self):
        self.db.close()

    def test_plan_a_full_effective_coverage(self):
        # 平台提交后运营商受理即失联，平台不知道东区是否已广播。
        sent = pw.dispatch(self.db, "w1", 101, response_lost=True)
        self.assertEqual(sent["decision"], "submitted")
        self.assertTrue(sent["uncertain"])
        self.assertFalse(pw.formal_warned(self.db, "w1"))

        # 仅凭运营商受理/队列，正式结论仍缺全部对象。
        before = pw.delivery_status(self.db, "w1")
        self.assertEqual(before["decision"], "undelivered")
        self.assertEqual(set(before["missing_households"]),
                         {"h-e1", "h-e2", "h-w2", "h-b1", "h-b2"})
        self.assertEqual(before["missing_schools"], ["school-1"])

        # 凭运营商签名流水认回东区实际广播范围（2 级：基站广播）。
        rec = pw.reconcile(self.db, "CAR-SIG-EAST-7788", "w1", 102,
                           broadcast_cells=["cell-east-1"], carrier="carrier-x")
        self.assertEqual(rec["decision"], "reconciled")
        self.assertEqual(rec["broadcast_cells"], ["cell-east-1"])

        after_east = pw.delivery_status(self.db, "w1")
        self.assertEqual(
            set(after_east["missing_households"]),
            {"h-w2", "h-b1", "h-b2"},
        )
        # 广播不能替代学校值班人本人接收。
        self.assertEqual(after_east["missing_schools"], ["school-1"])

        # 以“同一文案版本”补投西区可用基站（维修中的西站不投）。
        bc = pw.cell_broadcast(self.db, "w1", "cell-west-2", 103,
                               evidence_id="receipt-west2-1")
        self.assertEqual(bc["decision"], "broadcast")
        self.assertEqual(bc["version"], VERSION)
        after_west = pw.delivery_status(self.db, "w1")
        self.assertEqual(set(after_west["missing_households"]), {"h-b1", "h-b2"})
        self.assertEqual(after_west["blind_spots"], ["h-b1", "h-b2"])

        # 派值守人员到盲区，逐户现场通知（5 级）。
        onsite = pw.onsite_notify(
            self.db, "w1", ["h-b1", "h-b2"], "孙派员", 120, "action-blind-1")
        self.assertEqual(onsite["decision"], "onsite_notified")
        self.assertEqual(onsite["count"], 2)

        # 学校独立值守电话：值班人本人确认（4 级）。
        call = pw.call_school(self.db, "w1", "school-1", 125, outcome="answered",
                              person="赵值班")
        self.assertEqual(call["decision"], "personally_confirmed")
        self.assertEqual(call["level"], 4)

        status = pw.delivery_status(self.db, "w1")
        self.assertEqual(status["decision"], "delivered")
        self.assertTrue(status["delivered"])
        self.assertEqual(status["missing_households"], [])
        self.assertEqual(status["missing_schools"], [])
        self.assertEqual(status["blind_spots"], [])

        counts = status["counts"]
        self.assertEqual(counts["cells_broadcast"], 2)   # 东区1 + 西区可用1
        self.assertEqual(counts["households_onsite"], 2)
        self.assertEqual(counts["persons_confirmed"], 1)

    def test_same_event_retry_cannot_change_body(self):
        row = self.db.execute("SELECT body,digest,version,starts_at,ends_at "
                              "FROM pw_warnings WHERE id='w1'").fetchone()
        # 同 ID 重试且正文一致：幂等受理。
        again = issue_water_warning(self.db)
        self.assertEqual(again["decision"], "already_issued")
        # 同 ID 却试图改正文/版本/授权时段：身份冲突，拒绝。
        changed = issue_water_warning(self.db, body="被篡改的新正文", digest="digest-CHANGED")
        self.assertEqual(changed["decision"], "warning_identity_conflict")
        row2 = self.db.execute("SELECT body,digest,version,starts_at,ends_at "
                               "FROM pw_warnings WHERE id='w1'").fetchone()
        self.assertEqual(tuple(row), tuple(row2))


class PlanBSatelliteVehicleTest(unittest.TestCase):
    """方案二：确认运营商实际范围后，用独立卫星短报文 + 社区广播车覆盖盲区，
    学校通过卫星终端由值班人签收。"""

    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_cell(self.db, "cell-east-1", REGION, "online", "east")
        pw.register_household(self.db, "h-e1", REGION, "cell-east-1")
        pw.register_household(self.db, "h-b1", REGION, blind=True, name="盲区甲")
        pw.register_household(self.db, "h-b2", REGION, blind=True, name="盲区乙")
        pw.register_school(self.db, "school-1", REGION, duty_officer="陈值",
                           satellite_terminal="SAT-T1")
        pw.register_device(self.db, "sat-1", REGION, household="h-b1", kind="satellite-sms")
        pw.register_device(self.db, "sat-2", REGION, household="h-b2", kind="satellite-sms")

    def tearDown(self):
        self.db.close()

    def test_plan_b_independent_network_delivery_and_profile(self):
        # 先确认原运营商实际只广播了东区。
        rec = pw.reconcile(self.db, "CAR-SIG-SCOPE", "w1", 11,
                           broadcast_cells=["cell-east-1"])
        self.assertEqual(rec["decision"], "reconciled")

        # 独立卫星短报文（不同故障域），盲区终端逐一确认（3 级）。
        send = pw.satellite_send(self.db, "w1", ["sat-1", "sat-2"], 15)
        self.assertEqual(send["decision"], "satellite_sent")
        self.assertTrue(send["independent_channel"])
        self.assertEqual(pw.device_confirm(self.db, "w1", "sat-1", 16,
                                          channel="satellite")["decision"], "confirmed")
        self.assertEqual(pw.device_confirm(self.db, "w1", "sat-2", 17,
                                          channel="satellite")["decision"], "confirmed")

        # 卫星设备确认只有 3 级，盲区规定要 5 级现场证据：此时仍未送达。
        mid = pw.delivery_status(self.db, "w1")
        self.assertFalse(mid["delivered"])
        self.assertEqual(mid["blind_spots"], ["h-b1", "h-b2"])

        # 社区广播车逐户承担盲区现场广播（5 级）。
        veh = pw.vehicle_broadcast(self.db, "w1", ["h-b1", "h-b2"],
                                   "广播车-01", 30, "veh-run-1")
        self.assertEqual(veh["decision"], "onsite_notified")
        self.assertEqual(veh["channel"], "vehicle")

        # 学校通过卫星接收终端，由值班人本人签收（4 级，而非仅终端自动回执）。
        sign = pw.personal_receipt(self.db, "w1", "school-1", "陈值", 18,
                                   channel="satellite", terminal="SAT-T1")
        self.assertEqual(sign["decision"], "personally_confirmed")

        status = pw.delivery_status(self.db, "w1")
        self.assertEqual(status["decision"], "delivered")
        self.assertEqual(status["blind_spots"], [])
        # 盲区住户先经卫星在 16/17 时收到，现场广播车 30 时补齐等级——不同群体时刻不同。
        views = {v["id"]: v for v in status["subjects"]}
        self.assertEqual(views["h-b1"]["reached_at"], 16)
        self.assertIn("satellite", views["h-b1"]["channels"])
        self.assertIn("vehicle", views["h-b1"]["channels"])

        profile = pw.evaluate_plan(self.db, "w1", cost=18600,
                                   false_alarm_risk="low-but-broadcast-vehicle-audible")
        self.assertTrue(profile["delivered"])
        self.assertEqual(profile["cost"], 18600)
        self.assertIn("low", profile["false_alarm_risk"])


class EvidenceHierarchyTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_cell(self.db, "c1", REGION, "online")
        pw.register_cell(self.db, "cb", REGION, "maintenance")
        pw.register_household(self.db, "h1", REGION, "c1")
        pw.register_household(self.db, "hb", REGION, "cb")  # 维修站 -> 盲区，要求 5 级
        pw.register_school(self.db, "s1", REGION)
        pw.register_device(self.db, "gw", REGION, school="s1", kind="school-gateway")

    def tearDown(self):
        self.db.close()

    def test_lower_level_never_substitutes_higher(self):
        # 1 级：队列提交/运营商受理，不构成广播或送达。
        pw.dispatch(self.db, "w1", 101)
        self.assertFalse(pw.formal_warned(self.db, "w1"))

        # 2 级基站广播满足普通在线基站住户 h1，但不满足学校（要求 4 级）。
        pw.cell_broadcast(self.db, "w1", "c1", 102)
        st = pw.delivery_status(self.db, "w1")
        h1 = next(v for v in st["subjects"] if v["id"] == "h1")
        self.assertTrue(h1["covered"])
        self.assertEqual(st["missing_schools"], ["s1"])

        # 3 级：学校网关设备确认，仍不能替代值班人本人接收。
        pw.device_confirm(self.db, "w1", "gw", 103, channel="carrier")
        st = pw.delivery_status(self.db, "w1")
        s1 = next(v for v in st["subjects"] if v["id"] == "s1")
        self.assertEqual(s1["achieved_level"], 3)
        self.assertFalse(s1["covered"])

        # 没有具名本人的签收无效。
        anon = pw.personal_receipt(self.db, "w1", "s1", "", 104)
        self.assertEqual(anon["decision"], "personal_identity_required")

        # 盲区住户 hb 仅有 3 级设备确认时，不能替代规定的 5 级现场证据。
        pw.register_device(self.db, "db", REGION, household="hb")
        pw.device_confirm(self.db, "w1", "db", 105)
        hb = next(v for v in pw.delivery_status(self.db, "w1")["subjects"]
                  if v["id"] == "hb")
        self.assertEqual(hb["required_level"], 5)
        self.assertEqual(hb["achieved_level"], 3)
        self.assertFalse(hb["covered"])

        # 补齐 4 级本人与 5 级现场后才送达。
        pw.personal_receipt(self.db, "w1", "s1", "赵值班", 106, channel="phone")
        pw.onsite_notify(self.db, "w1", ["hb"], "孙派员", 107)
        self.assertTrue(pw.formal_warned(self.db, "w1"))


class BoundaryAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_cell(self.db, "cA", REGION, "online")
        pw.register_cell(self.db, "cB", REGION, "online")
        pw.register_household(self.db, "h1", REGION, "cA")
        pw.register_household(self.db, "h2", REGION, "cA")

    def tearDown(self):
        self.db.close()

    def test_boundary_survey_moves_households_and_blind_list_follows(self):
        pw.cell_broadcast(self.db, "w1", "cA", 102)
        self.assertTrue(pw.formal_warned(self.db, "w1"))

        # 勘界：h2 实际在区界外（迁出），h3 新划入且由 cB 覆盖。
        adj = pw.adjust_boundary(
            self.db, REGION, at=110,
            exclude=["h2"], include=[{"id": "h3", "cell": "cB", "name": "新划入"}])
        self.assertEqual(adj["decision"], "boundary_adjusted")

        st = pw.delivery_status(self.db, "w1")
        # h2 不再是本区责任住户；h3 成为未覆盖对象，不能靠 cA 的总发送数算它已知晓。
        self.assertNotIn("h2", [v["id"] for v in st["subjects"]])
        self.assertEqual(st["missing_households"], ["h3"])
        self.assertFalse(st["delivered"])

        # 在调整后的边界上补广播 cB，新住户被覆盖，结论恢复送达。
        pw.cell_broadcast(self.db, "w1", "cB", 111)
        self.assertTrue(pw.formal_warned(self.db, "w1"))

        changes = self.db.execute(
            "SELECT household,action FROM pw_boundary_changes WHERE region=? ORDER BY household",
            (REGION,)).fetchall()
        actions = {r[0]: r[1] for r in changes}
        self.assertEqual(actions.get("h2"), "exclude")
        self.assertEqual(actions.get("h3"), "include")

    def test_full_membership_replacement(self):
        # 以一份完整在区住户名单重划边界：名单外的 h1/h2 全部迁出。
        pw.adjust_boundary(self.db, REGION, at=120, households=["h9"])
        ids = {r[0] for r in self.db.execute(
            "SELECT id FROM pw_households WHERE region=? AND status='active'", (REGION,))}
        self.assertEqual(ids, {"h9"})
        st = pw.delivery_status(self.db, "w1")
        self.assertEqual(st["missing_households"], ["h9"])


class RoamingTerminalTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_household(self.db, "h1", REGION, "c1")
        pw.register_household(self.db, "h2", REGION, "c1")  # 始终无确认
        pw.register_device(self.db, "d-home", REGION, household="h1")
        pw.register_device(self.db, "d-visitor", "R-OTHER")

    def tearDown(self):
        self.db.close()

    def test_roaming_terminal_is_attributed_to_registration_region_only(self):
        # 本区住户的终端漫游到外区、被网络计入 R-OTHER：改归本区 h1。
        home = pw.device_confirm(self.db, "w1", "d-home", 105,
                                 network_region="R-OTHER", cell="c-remote")
        self.assertEqual(home["decision"], "confirmed_roaming_reattributed")
        self.assertTrue(home["roaming"])
        self.assertEqual(home["counted_region"], REGION)

        # 外区访客漫游进本区：不得计入本区触达。
        visitor = pw.device_confirm(self.db, "w1", "d-visitor", 106,
                                    network_region=REGION, cell="c1")
        self.assertEqual(visitor["decision"], "roaming_excluded")
        self.assertFalse(visitor["counted"])

        counts = pw.reach_counts(self.db, "w1")
        self.assertEqual(counts["devices_confirmed"], 1)  # 访客不计，归属设备只计一次
        status = pw.delivery_status(self.db, "w1")
        self.assertFalse(status["delivered"])
        self.assertIn("h2", status["missing_households"])  # 不能用漫游数粉饰全区


class DuplicateReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_cell(self.db, "c1", REGION, "online")
        pw.register_cell(self.db, "c2", REGION, "online")
        pw.register_household(self.db, "h1", REGION, "c1")
        pw.register_device(self.db, "d1", REGION, household="h1")

    def tearDown(self):
        self.db.close()

    def test_duplicate_cell_receipts_count_once(self):
        first = pw.cell_broadcast(self.db, "w1", "c1", 102, evidence_id="rc-1")
        replay = pw.cell_broadcast(self.db, "w1", "c1", 103, evidence_id="rc-1")
        other_id = pw.cell_broadcast(self.db, "w1", "c1", 104, evidence_id="rc-2")
        self.assertEqual(first["decision"], "broadcast")
        self.assertEqual(replay["decision"], "reused")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(other_id["decision"], "duplicate")
        self.assertEqual(pw.reach_counts(self.db, "w1")["cells_broadcast"], 1)

    def test_same_device_seen_by_two_cells_counts_once(self):
        a = pw.device_confirm(self.db, "w1", "d1", 105, cell="c1")
        b = pw.device_confirm(self.db, "w1", "d1", 106, cell="c2")
        self.assertEqual(a["decision"], "confirmed")
        self.assertEqual(b["decision"], "duplicate")
        self.assertEqual(pw.reach_counts(self.db, "w1")["devices_confirmed"], 1)
        # 即使灌入大量重复回执，结论与盲区也不被“总接收数”改变。
        for i in range(20):
            pw.run(self.db, {"op": "device_confirm", "warning_id": "w1",
                             "device": "d1", "at": 110 + i, "cell": f"c{i % 2}"})
        self.assertEqual(pw.reach_counts(self.db, "w1")["devices_confirmed"], 1)


class SchoolNoAnswerTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db)
        pw.register_school(self.db, "school-1", REGION, duty_officer="赵值班",
                           duty_phone="8888-110")

    def tearDown(self):
        self.db.close()

    def test_unanswered_phone_never_becomes_personal_receipt(self):
        r1 = pw.call_school(self.db, "w1", "school-1", 201)
        r2 = pw.call_school(self.db, "w1", "school-1", 205, outcome="no_answer")
        self.assertEqual(r1["decision"], "no_answer")
        self.assertEqual(r2["decision"], "no_answer")

        st = pw.delivery_status(self.db, "w1")
        self.assertFalse(st["delivered"])
        self.assertEqual(st["missing_schools"], ["school-1"])
        school_view = next(v for v in st["subjects"] if v["id"] == "school-1")
        self.assertEqual(school_view["no_answer_attempts"], 2)
        self.assertEqual(school_view["achieved_level"], 0)

        # 有人接但无法证明是值班本人，仍不满足。
        ans = pw.call_school(self.db, "w1", "school-1", 210, answered=True)
        self.assertEqual(ans["decision"], "answered_but_identity_required")
        self.assertFalse(pw.formal_warned(self.db, "w1"))

        # 终于由赵值班本人接听确认。
        ok = pw.call_school(self.db, "w1", "school-1", 215, answered=True,
                            person="赵值班")
        self.assertEqual(ok["decision"], "personally_confirmed")
        self.assertTrue(pw.formal_warned(self.db, "w1"))


class RevocationAndExpiryTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_revoked_warning_blocks_later_queue_retry(self):
        issue_water_warning(self.db)
        first = pw.dispatch(self.db, "w1", 101)
        self.assertEqual(first["decision"], "submitted")

        rev = pw.revoke(self.db, "w1", 150, reason="应急响应终止")
        self.assertEqual(rev["decision"], "revoked")
        self.assertTrue(rev["warning_revoked"])

        blocked = pw.dispatch(self.db, "w1", 160)
        self.assertEqual(blocked["decision"], "revoked")
        self.assertTrue(blocked["blocked"])
        # 阻断的重试不得产生运营商流水或新的受理记录。
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM pw_flows WHERE warning_id='w1'").fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM pw_outbox WHERE warning_id='w1'").fetchone()[0], 1)
        blocked_note = self.db.execute(
            "SELECT note FROM pw_dispatch_attempts WHERE warning_id='w1' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("blocked", blocked_note)

    def test_authority_revocation_blocks_all_its_warnings(self):
        issue_water_warning(self.db, wid="wA", authority="机关-A")
        pw.revoke(self.db, authority="机关-A", at=120)
        r = pw.dispatch(self.db, "wA", 130)
        self.assertEqual(r["decision"], "authority_revoked")
        self.assertTrue(r["blocked"])
        # 撤权后该机关不得再签发新公文。
        again = issue_water_warning(self.db, wid="wB", authority="机关-A")
        self.assertEqual(again["decision"], "authority_revoked")

    def test_expired_warning_blocks_retry_but_boundary_inclusive_end(self):
        issue_water_warning(self.db, wid="wE", starts_at=100, ends_at=400)
        at_end = pw.dispatch(self.db, "wE", 400)
        self.assertNotEqual(at_end["decision"], "expired")
        after = pw.dispatch(self.db, "wE", 401)
        self.assertEqual(after["decision"], "expired")
        self.assertTrue(after["blocked"])

    def test_not_yet_effective_blocks_early_send(self):
        issue_water_warning(self.db, wid="wN", starts_at=300, ends_at=400)
        r = pw.dispatch(self.db, "wN", 200)
        self.assertEqual(r["decision"], "not_effective")


class CorrectionAfterWrongTextTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue_water_warning(self.db, version="v1-wrong", body="错误文案：全天停水",
                            digest="digest-wrong")
        pw.register_cell(self.db, "c1", REGION, "online")
        pw.register_household(self.db, "h1", REGION, "c1")
        pw.register_household(self.db, "h2", REGION, "c1")
        pw.register_device(self.db, "d1", REGION, household="h1")
        pw.register_school(self.db, "s1", REGION)

    def tearDown(self):
        self.db.close()

    def test_correction_references_old_version_and_named_renotify_scope(self):
        # 错误旧文案已实际广播并被部分确认。
        pw.cell_broadcast(self.db, "w1", "c1", 102)
        pw.device_confirm(self.db, "w1", "d1", 103)
        pw.personal_receipt(self.db, "w1", "s1", "赵值班", 104)
        old_evidence = self.db.execute(
            "SELECT COUNT(*) FROM pw_evidence WHERE warning_id='w1'").fetchone()[0]
        self.assertGreaterEqual(old_evidence, 3)

        corr = pw.correct_warning(
            self.db, "w1", "w1-fix", "v2-correct", 120,
            body="更正：仅14-18时低压供水", digest="digest-right")
        self.assertEqual(corr["decision"], "correction_issued")
        self.assertEqual(corr["corrects"], "w1")
        self.assertEqual(corr["corrects_version"], "v1-wrong")
        # 重新通知名单必须具体到人/户：h1 有设备确认，h2 在旧广播基站范围，s1 本人签收过。
        keys = {(x["type"], x["id"]) for x in corr["renotify"]}
        self.assertIn(("household", "h1"), keys)
        self.assertIn(("household", "h2"), keys)
        self.assertIn(("school", "s1"), keys)

        row = self.db.execute(
            "SELECT corrects,corrects_version,version FROM pw_warnings WHERE id='w1-fix'"
        ).fetchone()
        self.assertEqual(tuple(row), ("w1", "v1-wrong", "v2-correct"))

        # 已发错误文案不可撤回为未发生：旧证据原样保留，旧公文仅标记 corrected。
        kept = self.db.execute(
            "SELECT COUNT(*) FROM pw_evidence WHERE warning_id='w1'").fetchone()[0]
        self.assertEqual(kept, old_evidence)
        self.assertEqual(self.db.execute(
            "SELECT status FROM pw_warnings WHERE id='w1'").fetchone()[0], "corrected")

        # 旧版重试被 supersede 阻断，不能再发错误文案。
        self.assertEqual(pw.dispatch(self.db, "w1", 121)["decision"], "superseded")

        # 更正公文自身也必须完成真实触达，不能因为“已更正”就视为送达。
        st = pw.delivery_status(self.db, "w1-fix")
        self.assertEqual(st["decision"], "undelivered")
        pw.cell_broadcast(self.db, "w1-fix", "c1", 122)
        pw.device_confirm(self.db, "w1-fix", "d1", 123)
        pw.personal_receipt(self.db, "w1-fix", "s1", "赵值班", 124, channel="phone")
        self.assertTrue(pw.formal_warned(self.db, "w1-fix"))


class LostResponseReconciliationTest(unittest.TestCase):
    """提交成功但响应丢失 / 运营商受理后失联：重启再提交必须对账原流水。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "warning.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_resubmission_reconciles_original_flow(self):
        db = connect(self.path)
        issue_water_warning(db)
        # 平台发出后响应丢失；运营商侧实际已受理并在东区广播（平台此刻不知道）。
        lost = pw.dispatch(db, "w1", 101, response_lost=True)
        self.assertTrue(lost["uncertain"])
        db.close()

        # —— 系统重启，重新打开同一持久化数据库 ——
        db = connect(self.path)
        try:
            # 不带流水的盲目重试：仍是 unknown，且不新建预警。
            again = pw.dispatch(db, "w1", 104, response_lost=True)
            self.assertIn(again["decision"], ("response_unknown", "submitted"))

            # 运营商幂等应答/签名流水带回原流水号与东区广播范围，当场对账。
            rec = pw.dispatch(db, "w1", 105, serial="CAR-SIG-ORIG",
                               executed=True, broadcast_cells=["cell-east-1"])
            self.assertEqual(rec["decision"], "reconciled")
            self.assertEqual(rec["serial"], "CAR-SIG-ORIG")

            # 不能制造“内容相同但缺少原授权时段”的新预警：仍只有原始一行且时段不变。
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM pw_warnings").fetchone()[0], 1)
            row = db.execute(
                "SELECT version,digest,starts_at,ends_at,authority,language "
                "FROM pw_warnings WHERE id='w1'").fetchone()
            self.assertEqual(tuple(row), (VERSION, "digest-water-v1", 100, 400,
                                          AUTHORITY, "zh-CN"))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM pw_outbox").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT attempts FROM pw_outbox WHERE key='w1'").fetchone()[0], 3)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM pw_flows WHERE serial='CAR-SIG-ORIG'").fetchone()[0], 1)

            # 再次重启后用同一签名流水对账：幂等，不重复落广播证据。
            db.close()
            db = connect(self.path)
            repeat = pw.reconcile(db, "CAR-SIG-ORIG", "w1", 106)
            self.assertEqual(repeat["decision"], "already_reconciled")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM pw_evidence WHERE kind='cell_broadcast' "
                "AND cell='cell-east-1'").fetchone()[0], 1)
        finally:
            db.close()

    def test_if_carrier_never_executed_queue_is_not_completion(self):
        db = connect(":memory:")
        issue_water_warning(db)
        pw.dispatch(db, "w1", 101, response_lost=True)
        rec = pw.reconcile(db, "CAR-SIG-NEVER", "w1", 102, executed=False)
        self.assertEqual(rec["decision"], "never_executed")
        self.assertTrue(rec["queue_is_not_delivery"])
        self.assertFalse(pw.formal_warned(db, "w1"))

        # 确认从未执行后，可以用同一固定正文重新投递（而不是被误判 already_executed）。
        resent = pw.dispatch(db, "w1", 103)
        self.assertEqual(resent["decision"], "submitted")
        self.assertTrue(resent.get("resent"))
        db.close()

    def test_invalid_signature_is_rejected(self):
        db = connect(":memory:")
        issue_water_warning(db)
        bad = pw.reconcile(db, "FORGED", "w1", 102, signature_valid=False,
                           broadcast_cells=["cell-east-1"])
        self.assertEqual(bad["decision"], "invalid_signature")
        self.assertFalse(pw.formal_warned(db, "w1"))
        db.close()


class DocumentValidationTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_warning_document_requires_all_fixed_elements(self):
        base = dict(warning_id="w", authority=AUTHORITY, region=REGION,
                    version=VERSION, language="zh-CN", starts_at=1, ends_at=9)
        for missing in ("authority", "region", "version", "language", "ends_at"):
            args = dict(base)
            args.pop(missing)
            result = pw.issue_warning(self.db, **args)
            self.assertEqual(result["decision"], "invalid_warning", missing)
        # 生效时段倒置同样无效。
        bad = pw.issue_warning(self.db, warning_id="w2", authority=AUTHORITY,
                               region=REGION, version=VERSION, language="zh-CN",
                               starts_at=10, ends_at=1)
        self.assertEqual(bad["decision"], "invalid_warning")
        self.assertFalse(pw.formal_warned(self.db, REGION))

    def test_two_positional_query_form_is_tolerated(self):
        queue_submit(self.db, "alert-9", REGION, "d", 1)
        self.assertTrue(pw.provisional_warned(self.db, REGION))
        self.assertFalse(pw.formal_warned(self.db, "alert-9", REGION))


class StoreDispatchIntegrationTest(unittest.TestCase):
    def test_public_warning_registered_in_store_execute(self):
        from netcourt.store import execute
        db = connect(":memory:")
        result = execute(db, "public_warning", {
            "op": "issue", "warning_id": "w1", "authority": AUTHORITY,
            "region": REGION, "version": VERSION, "language": "zh-CN",
            "starts_at": 1, "ends_at": 9,
        })
        self.assertEqual(result["decision"], "issued")
        event = db.execute(
            "SELECT subsystem FROM events WHERE subsystem='public_warning'").fetchone()
        self.assertIsNotNone(event)
        db.close()

    def test_public_warning_events_replay_identically(self):
        from netcourt.store import execute, replay
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "replay.sqlite")
        db = connect(path)
        execute(db, "public_warning", {
            "op": "issue", "warning_id": "w", "authority": AUTHORITY,
            "region": REGION, "version": VERSION, "language": "zh-CN",
            "starts_at": 1, "ends_at": 9})
        execute(db, "public_warning", {
            "op": "school", "school_id": "s", "region": REGION,
            "duty_officer": "李"})
        execute(db, "public_warning", {
            "op": "personal_receipt", "warning_id": "w", "school": "s",
            "person": "李", "at": 3})
        db.close()
        rep = replay(path)
        self.assertTrue(rep["verified"], rep)
        self.assertEqual(rep["decisions"], 3)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
