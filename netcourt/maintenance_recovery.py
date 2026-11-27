# -*- coding: utf-8 -*-
"""共享网络设备维护的正式授权、现场事实认回与履约补救。

与 provisional_* 的临时判断不同，本模块只承认逐户合同渠道上的本人（或有效
代理人）签收，并把已经发生的现场物理动作建立在独立现场证据之上：

* 接收人身份、合同渠道、最短提前期、时区、窗口、本人确认逐户计算；入队与
  传输商接单都不是客户签收，退信直接否定送达。
* 工项区分未碰设备、已断开但可安全恢复、不可逆完成、服务实际受影响时段。
* 现场签章与控制端可能因断网/崩溃不同步，认回只看独立现场证据，不靠最后
  一条记录猜进度，也不会自动让另一班组再次拆端子。
* 甲乙共享设备但通知权相互独立，任何一方的确认不能替另一方承担停机。
* 紧急安全例外必须同时具备独立故障事实、授权人、实际事后通知，且可被
  推翻；计划维护失误不得事后改贴“紧急”标签。
* 通知、中断、补偿付款只追加原件，不覆盖、不删除。
"""
from .store import one, rows

# 物理形态（由独立现场证据推得，控制端回执不参与）
STATE_NOT_TOUCHED = "not_touched"
STATE_DISCONNECTED = "disconnected"
STATE_JUMPERED = "jumpered"
STATE_IRREVERSIBLE = "irreversible_complete"
STATE_JUMPER_FAILED = "jumper_failed"

DISCONNECT_STATES = (
    STATE_DISCONNECTED, STATE_JUMPERED, STATE_JUMPER_FAILED, STATE_IRREVERSIBLE
)
# 仍可在安全检查后恢复旧服务的半完成形态
RECOVERABLE_HALF_STATES = (STATE_DISCONNECTED, STATE_JUMPERED)
# 参与推进物理形态判定的证据类别；安全检查与故障告警不改变设备物理形态
STATE_EVIDENCE = (
    "disconnect", "jumper_installed", "jumper_failed", "irreversible_work"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mr_profiles(
  customer TEXT PRIMARY KEY, contract_channel TEXT, recipient TEXT,
  timezone_minutes INTEGER, lead_minutes INTEGER, sla_free_minutes INTEGER,
  tariff_per_minute INTEGER);
CREATE TABLE IF NOT EXISTS mr_delegations(
  id TEXT PRIMARY KEY, customer TEXT, agent TEXT, valid_from INTEGER,
  valid_to INTEGER, revoked INTEGER);
CREATE TABLE IF NOT EXISTS mr_notice_events(
  id TEXT, customer TEXT, revision TEXT, channel TEXT, actor TEXT,
  status TEXT, at INTEGER, window_id TEXT, bounce_of TEXT,
  PRIMARY KEY(id, status, at));
CREATE TABLE IF NOT EXISTS mr_window_consents(
  id TEXT PRIMARY KEY, customer TEXT, revision TEXT, weekday INTEGER,
  start_minute INTEGER, end_minute INTEGER, withdrawn INTEGER);
CREATE TABLE IF NOT EXISTS mr_work_orders(
  id TEXT PRIMARY KEY, revision TEXT, parent TEXT, planned_start INTEGER,
  status TEXT, defect INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS mr_work_items(
  work_order TEXT, item TEXT, customers TEXT, state TEXT,
  PRIMARY KEY(work_order, item));
CREATE TABLE IF NOT EXISTS mr_receipts(
  id TEXT PRIMARY KEY, work_order TEXT, item TEXT, node TEXT, kind TEXT,
  at INTEGER, lost INTEGER);
CREATE TABLE IF NOT EXISTS mr_field_evidence(
  id TEXT PRIMARY KEY, work_order TEXT, item TEXT, kind TEXT, source TEXT,
  at INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS mr_reconciliations(
  work_order TEXT, item TEXT, true_state TEXT, basis TEXT, at INTEGER,
  PRIMARY KEY(work_order, item));
CREATE TABLE IF NOT EXISTS mr_outages(
  id TEXT PRIMARY KEY, work_order TEXT, item TEXT, customer TEXT,
  started INTEGER, restored INTEGER, minutes INTEGER, outage_no INTEGER);
CREATE TABLE IF NOT EXISTS mr_communications(
  id TEXT PRIMARY KEY, work_order TEXT, customer TEXT, kind TEXT, channel TEXT,
  actor TEXT, status TEXT, at INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS mr_emergency_authorizers(
  id TEXT PRIMARY KEY, name TEXT, scope TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS mr_emergency_cases(
  id TEXT PRIMARY KEY, work_order TEXT, fault_evidence TEXT, authorizer TEXT,
  post_notice TEXT, fault_at INTEGER, status TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS mr_site_leads(
  id TEXT PRIMARY KEY, name TEXT, authorization TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS mr_necessity_certs(
  id TEXT PRIMARY KEY, work_order TEXT, item TEXT, lead TEXT, reason TEXT,
  at INTEGER);
CREATE TABLE IF NOT EXISTS mr_compensations(
  id TEXT PRIMARY KEY, work_order TEXT, customer TEXT, outage_id TEXT,
  minutes INTEGER, amount INTEGER, reason TEXT, paid INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS mr_costs(
  work_order TEXT, item TEXT, kind TEXT, amount INTEGER, at INTEGER,
  PRIMARY KEY(work_order, item, kind));
"""


def ensure_schema(db):
    db.executescript(SCHEMA)


# ---------------------------------------------------------------- 时间与窗口
def local_clock(at, timezone_minutes):
    """epoch 秒 -> (星期 0..6, 当地当日分钟)。"""
    day, second = divmod(at + timezone_minutes * 60, 86400)
    return day % 7, second // 60


def _consent_active(db, customer, revision, window_id):
    c = one(db,
        "SELECT * FROM mr_window_consents WHERE id=? AND customer=? AND revision=? AND withdrawn=0",
        (window_id, customer, revision),
    )
    return c is not None


def _delegate_valid(db, customer, agent, at):
    """委托在签收时刻 at 是否有效。

    事后撤销（代理人替换）不追溯否定撤销前已作出的合法签收：只要有效期覆盖
    该时刻即可；撤销状态只对撤销生效之后的新签收起作用。
    """
    return one(db,
        "SELECT id FROM mr_delegations WHERE customer=? AND agent=? "
        "AND valid_from<=? AND valid_to>?",
        (customer, agent, at, at),
    ) is not None


def _bounced_notice_ids(db, customer, revision):
    return {
        r["bounce_of"]
        for r in rows(db,
            "SELECT bounce_of FROM mr_notice_events WHERE customer=? AND revision=? AND status='bounced'",
            (customer, revision),
        )
        if r["bounce_of"]
    }


def _signed_events(db, customer, revision):
    return rows(db,
        "SELECT * FROM mr_notice_events WHERE customer=? AND revision=? AND status='signed' "
        "ORDER BY at",
        (customer, revision),
    )


def evaluate_customer(db, customer, revision, planned_start):
    """逐户核对正式开工权限。入队/接单不算签收，退信直接否定送达。"""
    p = one(db, "SELECT * FROM mr_profiles WHERE customer=?", (customer,))
    if not p:
        return {"customer": customer, "ok": False, "reason": "unknown_customer"}
    bounced = _bounced_notice_ids(db, customer, revision)
    qualifying = None
    for e in _signed_events(db, customer, revision):
        if e["id"] in bounced:
            continue  # 同一份通知最终退信：签收事实被推翻
        if e["channel"] != p["contract_channel"]:
            continue  # 非合同渠道
        if e["actor"] != p["recipient"] and not _delegate_valid(
            db, customer, e["actor"], e["at"]
        ):
            continue  # 非本人且无有效代理
        if not _consent_active(db, customer, revision, e["window_id"]):
            continue  # 窗口已撤回或不存在
        if (planned_start - e["at"]) // 60 < p["lead_minutes"]:
            continue  # 最短提前期不足（档案按分钟，时刻按 epoch 秒）
        weekday, minute = local_clock(planned_start, p["timezone_minutes"])
        w = one(db, "SELECT * FROM mr_window_consents WHERE id=?", (e["window_id"],))
        if not (w["weekday"] == weekday and w["start_minute"] <= minute < w["end_minute"]):
            continue  # 开工时刻落在该户合同窗口之外
        qualifying = e
        break
    if qualifying is None:
        if bounced:
            reason = "bounced"
        elif not _signed_events(db, customer, revision):
            reason = "no_signed_receipt"
        else:
            reason = "notice_terms_not_satisfied"
        return {"customer": customer, "ok": False, "reason": reason}
    return {
        "customer": customer,
        "ok": True,
        "reason": "authorized",
        "notice": qualifying["id"],
        "actor": qualifying["actor"],
    }


def formal_start_authorized(db, work_order_id, revision, customers, planned_start):
    """正式开工权限：逐户独立通过才放行；任一方的确认不能授权另一方。"""
    checks = [evaluate_customer(db, c, revision, planned_start) for c in customers]
    return {
        "authorized": all(c["ok"] for c in checks),
        "checks": checks,
        "work_order": work_order_id,
    }


# ---------------------------------------------------------------- 现场事实认回
def true_item_state(db, work_order_id, item):
    """只依据独立现场证据重建设备真实物理形态，控制端回执不参与。"""
    evs = rows(db,
        "SELECT * FROM mr_field_evidence WHERE work_order=? AND item=? AND kind IN (?,?,?,?) "
        "ORDER BY at, id",
        (work_order_id, item) + STATE_EVIDENCE,
    )
    if not evs:
        return STATE_NOT_TOUCHED, []
    state = STATE_NOT_TOUCHED
    for e in evs:
        if e["kind"] == "disconnect":
            state = STATE_DISCONNECTED
        elif e["kind"] == "jumper_installed":
            state = STATE_JUMPERED
        elif e["kind"] == "jumper_failed":
            state = STATE_JUMPER_FAILED
        elif e["kind"] == "irreversible_work":
            state = STATE_IRREVERSIBLE
    return state, evs


def reconcile(db, x):
    """进程重启后的认回：按独立现场证据认定真实状态并登记，不自动再施工。"""
    ensure_schema(db)
    wo = one(db, "SELECT * FROM mr_work_orders WHERE id=?", (x["work_order"],))
    if not wo:
        return {"decision": "unknown_work_order"}
    results = []
    half_finished = []
    for it in rows(db,
        "SELECT * FROM mr_work_items WHERE work_order=?", (x["work_order"],)
    ):
        state, evs = true_item_state(db, x["work_order"], it["item"])
        if not evs:
            results.append({"item": it["item"], "state": STATE_NOT_TOUCHED, "basis": []})
            continue
        # 不可逆改造需要现场签章/日志类独立证据，不能只凭控制端回执
        if state == STATE_IRREVERSIBLE and not any(
            e["kind"] == "irreversible_work" for e in evs
        ):
            return {"decision": "insufficient_irreversible_evidence", "item": it["item"]}
        basis = sorted({e["id"] for e in evs})
        db.execute(
            "INSERT OR REPLACE INTO mr_reconciliations VALUES(?,?,?,?,?)",
            (x["work_order"], it["item"], state, ",".join(basis), x["at"]),
        )
        db.execute(
            "UPDATE mr_work_items SET state=? WHERE work_order=? AND item=?",
            (state, x["work_order"], it["item"]),
        )
        # 服务实际受影响时段从最早的断开类证据起算，逐户登记
        if state in DISCONNECT_STATES:
            started = min(e["at"] for e in evs if e["kind"] == "disconnect")
            for customer in it["customers"].split(","):
                existing = one(db,
                    "SELECT * FROM mr_outages WHERE work_order=? AND item=? AND customer=? "
                    "AND restored IS NULL ORDER BY outage_no LIMIT 1",
                    (x["work_order"], it["item"], customer),
                )
                if not existing:
                    no = _next_outage_no(db, x["work_order"], it["item"], customer)
                    oid = f"out-{x['work_order']}-{it['item']}-{customer}-{no}"
                    db.execute(
                        "INSERT INTO mr_outages VALUES(?,?,?,?,?,?,?,?)",
                        (oid, x["work_order"], it["item"], customer, started, None, None, no),
                    )
                elif existing["started"] is None:
                    # 重约工单预留的第二次停机：实际断开证据出现后回填开始时刻
                    db.execute(
                        "UPDATE mr_outages SET started=? WHERE id=?", (started, existing["id"])
                    )
        entry = {"item": it["item"], "state": state, "basis": basis}
        results.append(entry)
        if state in RECOVERABLE_HALF_STATES or state == STATE_JUMPER_FAILED:
            half_finished.append(entry)
    # 标记崩溃时未落库的现场回执（只标注，不据此猜状态）：回执 id 在独立
    # 现场证据中找不到对应物，即认定该签章在中央进程崩溃时丢失
    for r in rows(db,
        "SELECT * FROM mr_receipts WHERE work_order=? AND lost=0", (x["work_order"],)
    ):
        if not one(db,
            "SELECT id FROM mr_field_evidence WHERE id=?", (r["id"],)
        ):
            db.execute("UPDATE mr_receipts SET lost=1 WHERE id=?", (r["id"],))
    db.execute(
        "UPDATE mr_work_orders SET status='reconciled' WHERE id=?",
        (x["work_order"],),
    )
    return {
        "decision": "reconciled",
        "items": results,
        "half_finished": half_finished,
        "rebooked_or_redispatched": False,
    }


# ---------------------------------------------------------------- 中断与补偿
def outage_minutes(db, work_order_id, customer):
    total = 0
    for o in rows(db,
        "SELECT * FROM mr_outages WHERE work_order=? AND customer=? ORDER BY outage_no",
        (work_order_id, customer),
    ):
        if o["minutes"] is not None:
            total += o["minutes"]
    return total


def _compensation_due(profile, minutes):
    chargeable = max(0, minutes - profile["sla_free_minutes"])
    return chargeable * profile["tariff_per_minute"], chargeable


def fulfillment_summary(db, work_order_id):
    """汇总两户真实中断次数/时长与应付减免，供两种处置路径对比。"""
    wo = one(db, "SELECT * FROM mr_work_orders WHERE id=?", (work_order_id,))
    customers = sorted(
        {c for it in rows(db, "SELECT * FROM mr_work_items WHERE work_order=?", (work_order_id,))
         for c in it["customers"].split(",")}
    )
    out = {}
    for customer in customers:
        os_ = rows(db,
            "SELECT * FROM mr_outages WHERE work_order=? AND customer=? ORDER BY outage_no",
            (work_order_id, customer),
        )
        p = one(db, "SELECT * FROM mr_profiles WHERE customer=?", (customer,))
        minutes = sum(o["minutes"] or 0 for o in os_)
        due, chargeable = _compensation_due(p, minutes) if p else (0, 0)
        paid = one(db,
            "SELECT COALESCE(SUM(amount),0) AS s FROM mr_compensations WHERE work_order=? AND customer=? AND paid=1",
            (work_order_id, customer),
        )["s"]
        out[customer] = {
            "outage_count": len(os_),
            "minutes": minutes,
            "chargeable_minutes": chargeable,
            "compensation_due": due,
            "compensation_paid": paid,
        }
    return {
        "work_order": work_order_id,
        "status": wo["status"],
        "defect": bool(wo["defect"]),
        "customers": out,
    }


def _close_open_outages(db, work_order_id, item, at, minutes=None):
    for o in rows(db,
        "SELECT * FROM mr_outages WHERE work_order=? AND item=? AND restored IS NULL",
        (work_order_id, item),
    ):
        m = minutes if minutes is not None else (at - o["started"]) // 60
        db.execute(
            "UPDATE mr_outages SET restored=?, minutes=? WHERE id=?", (at, m, o["id"])
        )


def _next_outage_no(db, work_order_id, item, customer):
    row = one(db,
        "SELECT COALESCE(MAX(outage_no),0)+1 AS n FROM mr_outages WHERE work_order=? AND item=? AND customer=?",
        (work_order_id, item, customer),
    )
    return row["n"]


def _add_cost(db, work_order_id, item, kind, amount, at):
    db.execute(
        "INSERT INTO mr_costs VALUES(?,?,?,?,?) ON CONFLICT(work_order,item,kind) "
        "DO UPDATE SET amount=excluded.amount, at=excluded.at",
        (work_order_id, item, kind, amount, at),
    )


def restore_and_rebook(db, x):
    """路径一：安全检查 + 临时跨接恢复旧服务，再重新预约（产生第二次停机）。"""
    ensure_schema(db)
    state, _ = true_item_state(db, x["work_order"], x["item"])
    if state not in RECOVERABLE_HALF_STATES:
        return {"decision": "not_safely_recoverable", "state": state}
    check = one(db,
        "SELECT * FROM mr_field_evidence WHERE work_order=? AND item=? AND kind='safety_check' AND at<=?",
        (x["work_order"], x["item"], x["at"]),
    )
    if not check:
        return {"decision": "safety_check_required"}
    if state == STATE_JUMPERED and not one(db,
        "SELECT id FROM mr_field_evidence WHERE work_order=? AND item=? AND kind='jumper_installed'",
        (x["work_order"], x["item"]),
    ):
        return {"decision": "jumper_evidence_missing"}
    # 恢复旧服务：关闭第一段真实中断
    _close_open_outages(db, x["work_order"], x["item"], x["at"])
    db.execute(
        "UPDATE mr_work_items SET state=? WHERE work_order=? AND item=?",
        (STATE_NOT_TOUCHED, x["work_order"], x["item"]),
    )
    db.execute(
        "INSERT OR REPLACE INTO mr_reconciliations VALUES(?,?,?,?,?)",
        (x["work_order"], x["item"], STATE_NOT_TOUCHED, f"safety:{check['id']}", x["at"]),
    )
    _add_cost(db, x["work_order"], x["item"], "restore_labor", x["labor_cost"], x["at"])
    # 重新预约：新工单独立存在，必须重新逐户通知与授权，绝不复用旧签收
    child = x["child_work_order"]
    if one(db, "SELECT id FROM mr_work_orders WHERE id=?", (child,)):
        return {"decision": "rebook_identity_conflict"}
    item = one(db,
        "SELECT * FROM mr_work_items WHERE work_order=? AND item=?",
        (x["work_order"], x["item"]),
    )
    db.execute(
        "INSERT INTO mr_work_orders VALUES(?,?,?,?,?,?,?)",
        (child, x["revision"], x["work_order"], x["planned_start"], "booked", 0, x["at"]),
    )
    db.execute(
        "INSERT INTO mr_work_items VALUES(?,?,?,?)",
        (child, item["item"], item["customers"], STATE_NOT_TOUCHED),
    )
    # 第二次停机的受影响时段占位：重新施工实际断开时再填开始时刻
    for customer in item["customers"].split(","):
        no = _next_outage_no(db, x["work_order"], item["item"], customer)
        oid = f"out-{child}-{item['item']}-{customer}-{no}"
        db.execute(
            "INSERT INTO mr_outages VALUES(?,?,?,?,?,?,?,?)",
            (oid, child, item["item"], customer, None, None, None, no),
        )
    db.execute(
        "UPDATE mr_work_orders SET status='restored_rebooked' WHERE id=?",
        (x["work_order"],),
    )
    return {
        "decision": "restored_and_rebooked",
        "child_work_order": child,
        "state": STATE_NOT_TOUCHED,
        "second_outage_reserved": True,
    }


def authorize_continue(db, x):
    """路径二前置：经授权的现场负责人确认继续施工是避免进一步损坏的必要处置。

    半完成（已断开/已跨接）或跨接失败（恢复旧服务已不安全）时适用。
    """
    ensure_schema(db)
    state, evs = true_item_state(db, x["work_order"], x["item"])
    if state not in RECOVERABLE_HALF_STATES + (STATE_JUMPER_FAILED,):
        return {"decision": "not_half_finished", "state": state}
    lead = one(db, "SELECT * FROM mr_site_leads WHERE id=?", (x["lead"],))
    if not lead or lead["authorization"] != "continue_work":
        return {"decision": "lead_not_authorized"}
    if one(db,
        "SELECT id FROM mr_necessity_certs WHERE work_order=? AND item=?",
        (x["work_order"], x["item"]),
    ):
        return {"decision": "necessity_cert_exists"}
    db.execute(
        "INSERT INTO mr_necessity_certs VALUES(?,?,?,?,?,?)",
        (x["cert_id"], x["work_order"], x["item"], x["lead"], x["reason"], x["at"]),
    )
    # 计划失误事实保留：正式授权缺失不被现场必要性处置洗白
    db.execute(
        "UPDATE mr_work_orders SET defect=1, status='continue_authorized' WHERE id=?",
        (x["work_order"],),
    )
    return {"decision": "continue_authorized", "state": state, "lead": x["lead"]}


def complete_continue(db, x):
    """路径二收尾：完成不可逆改造，固化中断与真实记录。"""
    ensure_schema(db)
    cert = one(db,
        "SELECT * FROM mr_necessity_certs WHERE work_order=? AND item=?",
        (x["work_order"], x["item"]),
    )
    if not cert:
        return {"decision": "necessity_cert_missing"}
    state, evs = true_item_state(db, x["work_order"], x["item"])
    if state != STATE_IRREVERSIBLE:
        return {"decision": "irreversible_evidence_missing", "state": state}
    _close_open_outages(db, x["work_order"], x["item"], x["at"])
    db.execute(
        "UPDATE mr_work_items SET state=? WHERE work_order=? AND item=?",
        (STATE_IRREVERSIBLE, x["work_order"], x["item"]),
    )
    db.execute(
        "INSERT OR REPLACE INTO mr_reconciliations VALUES(?,?,?,?,?)",
        (x["work_order"], x["item"], STATE_IRREVERSIBLE, f"cert:{cert['id']}", x["at"]),
    )
    _add_cost(db, x["work_order"], x["item"], "completion_labor", x["labor_cost"], x["at"])
    db.execute(
        "UPDATE mr_work_orders SET status='completed_with_defect' WHERE id=?",
        (x["work_order"],),
    )
    return {"decision": "completed", "state": STATE_IRREVERSIBLE}


# ---------------------------------------------------------------- 紧急安全例外
def file_emergency(db, x):
    """登记紧急安全例外：独立故障事实 + 授权人 + 实际事后通知，三要素缺一不可。"""
    ensure_schema(db)
    if one(db, "SELECT id FROM mr_emergency_cases WHERE id=?", (x["id"],)):
        return {"decision": "emergency_case_exists"}
    fault = one(db,
        "SELECT * FROM mr_field_evidence WHERE work_order=? AND kind='fault_alarm' AND id=?",
        (x["work_order"], x["fault_evidence"]),
    )
    if not fault:
        return {"decision": "independent_fault_fact_missing"}
    if not one(db,
        "SELECT id FROM mr_emergency_authorizers WHERE id=?", (x["authorizer"],)
    ):
        return {"decision": "authorizer_missing"}
    notice = one(db,
        "SELECT * FROM mr_communications WHERE id=? AND status='delivered'",
        (x["post_notice"],),
    )
    if not notice:
        return {"decision": "post_hoc_notice_missing"}
    # 故障必须发生在被覆盖的现场动作之前；计划维护失误不得事后改贴紧急标签
    first_disconnect = one(db,
        "SELECT MIN(at) AS at FROM mr_field_evidence WHERE work_order=? AND kind='disconnect'",
        (x["work_order"],),
    )["at"]
    if first_disconnect is not None and fault["at"] >= first_disconnect:
        return {"decision": "retroactive_emergency_rejected"}
    db.execute(
        "INSERT INTO mr_emergency_cases VALUES(?,?,?,?,?,?,?,?)",
        (x["id"], x["work_order"], x["fault_evidence"], x["authorizer"],
         x["post_notice"], fault["at"], "valid", x["at"]),
    )
    return {"decision": "emergency_filed"}


def overturn_emergency(db, x):
    """紧急证据后来被推翻：原案保留并标记，停机重新按计划失误计赔。"""
    ensure_schema(db)
    case = one(db, "SELECT * FROM mr_emergency_cases WHERE id=?", (x["id"],))
    if not case:
        return {"decision": "unknown_emergency_case"}
    if case["status"] != "valid":
        return {"decision": "case_not_valid"}
    db.execute(
        "UPDATE mr_emergency_cases SET status='overturned' WHERE id=?", (x["id"],)
    )
    return {"decision": "emergency_overturned", "work_order": case["work_order"]}


# ---------------------------------------------------------------- 统一入口
def run(db, x):
    op = x["op"]
    if op == "profile":
        ensure_schema(db)
        old = one(db, "SELECT customer FROM mr_profiles WHERE customer=?", (x["customer"],))
        db.execute(
            "INSERT INTO mr_profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(customer) DO UPDATE SET "
            "contract_channel=excluded.contract_channel,recipient=excluded.recipient,"
            "timezone_minutes=excluded.timezone_minutes,lead_minutes=excluded.lead_minutes,"
            "sla_free_minutes=excluded.sla_free_minutes,tariff_per_minute=excluded.tariff_per_minute",
            (x["customer"], x["contract_channel"], x["recipient"], x["timezone_minutes"],
             x["lead_minutes"], x["sla_free_minutes"], x["tariff_per_minute"]),
        )
        return {"decision": "profile_replaced" if old else "profile_registered"}
    if op == "replace_agent":
        ensure_schema(db)
        # 代理人替换：旧授权撤销，替换前已作出的签收不被追溯否定
        db.execute(
            "UPDATE mr_delegations SET revoked=1 WHERE customer=? AND revoked=0",
            (x["customer"],),
        )
        if one(db, "SELECT id FROM mr_delegations WHERE id=?", (x["id"],)):
            return {"decision": "delegation_identity_conflict"}
        db.execute(
            "INSERT INTO mr_delegations VALUES(?,?,?,?,?,0)",
            (x["id"], x["customer"], x["agent"], x["valid_from"], x["valid_to"]),
        )
        return {"decision": "agent_replaced"}
    if op == "notice_event":
        ensure_schema(db)
        if x["status"] not in ("queued", "carrier_accepted", "delivered", "signed", "bounced"):
            return {"decision": "invalid_notice_status"}
        if x["status"] == "bounced":
            target = one(db,
                "SELECT * FROM mr_notice_events WHERE id=? AND customer=? AND revision=?",
                (x["bounce_of"], x["customer"], x["revision"]),
            )
            if not target:
                return {"decision": "bounce_target_missing"}
        else:
            dup = one(db,
                "SELECT id FROM mr_notice_events WHERE id=? AND customer=? AND revision=? AND status=?",
                (x["id"], x["customer"], x["revision"], x["status"]),
            )
            if dup:
                return {"decision": "reused"}
        eid = x["bounce_of"] if x["status"] == "bounced" else x["id"]
        db.execute(
            "INSERT INTO mr_notice_events VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, x["customer"], x["revision"], x["channel"], x.get("actor"),
             x["status"], x["at"], x.get("window_id"), x.get("bounce_of")),
        )
        return {"decision": "recorded"}
    if op == "window_consent":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_window_consents WHERE id=?", (x["id"],)):
            return {"decision": "window_identity_conflict"}
        db.execute(
            "INSERT INTO mr_window_consents VALUES(?,?,?,?,?,?,0)",
            (x["id"], x["customer"], x["revision"], x["weekday"],
             x["start_minute"], x["end_minute"]),
        )
        return {"decision": "consent_recorded"}
    if op == "withdraw_window":
        ensure_schema(db)
        w = one(db, "SELECT * FROM mr_window_consents WHERE id=? AND customer=?", (x["id"], x["customer"]))
        if not w:
            return {"decision": "unknown_window"}
        db.execute("UPDATE mr_window_consents SET withdrawn=1 WHERE id=?", (x["id"],))
        return {"decision": "window_withdrawn"}
    if op == "work_order":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_work_orders WHERE id=?", (x["id"],)):
            # 工单重复下发：幂等拒绝，不产生第二个工项，更不会再派班组拆端子
            return {"decision": "duplicate_dispatch_rejected"}
        db.execute(
            "INSERT INTO mr_work_orders VALUES(?,?,?,?,?,?,?)",
            (x["id"], x["revision"], None, x["planned_start"], "issued", 0, x["at"]),
        )
        for it in x["items"]:
            db.execute(
                "INSERT INTO mr_work_items VALUES(?,?,?,?)",
                (x["id"], it["item"], ",".join(it["customers"]), STATE_NOT_TOUCHED),
            )
        return {"decision": "work_order_issued", "items": len(x["items"])}
    if op == "field_receipt":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_receipts WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO mr_receipts VALUES(?,?,?,?,?,?,0)",
            (x["id"], x["work_order"], x["item"], x["node"], x["kind"], x["at"]),
        )
        return {"decision": "receipt_recorded"}
    if op == "field_evidence":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_field_evidence WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO mr_field_evidence VALUES(?,?,?,?,?,?,?)",
            (x["id"], x["work_order"], x["item"], x["kind"], x["source"], x["at"],
             x.get("detail", "")),
        )
        return {"decision": "evidence_recorded"}
    if op == "reconcile":
        return reconcile(db, x)
    if op == "formal_start":
        ensure_schema(db)
        wo = one(db, "SELECT * FROM mr_work_orders WHERE id=?", (x["work_order"],))
        if not wo:
            return {"decision": "unknown_work_order"}
        customers = sorted(
            {c for it in rows(db, "SELECT customers FROM mr_work_items WHERE work_order=?", (x["work_order"],))
             for c in it["customers"].split(",")}
        )
        result = formal_start_authorized(
            db, x["work_order"], wo["revision"], customers, wo["planned_start"]
        )
        result["decision"] = "start_authorized" if result["authorized"] else "start_denied"
        return result
    if op == "close_outage":
        ensure_schema(db)
        # 以独立现场证据（恢复/完工）关闭仍在持续的中断时段，不猜时间
        if not one(db,
            "SELECT id FROM mr_field_evidence WHERE id=?", (x["evidence_id"],)
        ):
            return {"decision": "evidence_missing"}
        before = db.execute(
            "SELECT COUNT(*) AS c FROM mr_outages WHERE work_order=? AND item=? AND restored IS NULL",
            (x["work_order"], x["item"]),
        ).fetchone()["c"]
        _close_open_outages(db, x["work_order"], x["item"], x["at"])
        return {"decision": "outage_closed", "closed": before}
    if op == "restore_rebook":
        return restore_and_rebook(db, x)
    if op == "authorize_continue":
        return authorize_continue(db, x)
    if op == "complete_continue":
        return complete_continue(db, x)
    if op == "explain_in_person":
        ensure_schema(db)
        # 完成后立即向甲本人说明：须经合同渠道、本人或有效代理实际送达
        p = one(db, "SELECT * FROM mr_profiles WHERE customer=?", (x["customer"],))
        if not p:
            return {"decision": "unknown_customer"}
        if x["channel"] != p["contract_channel"]:
            return {"decision": "non_contract_channel"}
        if x["actor"] != p["recipient"] and not _delegate_valid(db, x["customer"], x["actor"], x["at"]):
            return {"decision": "actor_not_recognized"}
        if one(db, "SELECT id FROM mr_communications WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        status = x.get("status", "delivered")
        if status not in ("delivered", "refused"):
            return {"decision": "invalid_communication_status"}
        db.execute(
            "INSERT INTO mr_communications VALUES(?,?,?,?,?,?,?,?,?)",
            (x["id"], x["work_order"], x["customer"], "post_hoc_explanation",
             x["channel"], x["actor"], status, x["at"], x.get("detail", "")),
        )
        return {"decision": "explained" if status == "delivered" else "refusal_recorded"}
    if op == "deliver_records":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_communications WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        # 交付真实中断记录与合同减免原件，按户独立签收
        db.execute(
            "INSERT INTO mr_communications VALUES(?,?,?,?,?,?,?,?,?)",
            (x["id"], x["work_order"], x["customer"], "outage_and_relief_record",
             x["channel"], x["actor"], x["status"], x["at"], x.get("detail", "")),
        )
        return {"decision": "records_delivered" if x["status"] == "delivered" else "record_status_noted"}
    if op == "emergency_authorizer":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_emergency_authorizers WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO mr_emergency_authorizers VALUES(?,?,?,?)",
            (x["id"], x["name"], x["scope"], x["at"]),
        )
        return {"decision": "authorizer_registered"}
    if op == "file_emergency":
        return file_emergency(db, x)
    if op == "overturn_emergency":
        return overturn_emergency(db, x)
    if op == "site_lead":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_site_leads WHERE id=?", (x["id"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO mr_site_leads VALUES(?,?,?,?)",
            (x["id"], x["name"], x["authorization"], x["at"]),
        )
        return {"decision": "lead_registered"}
    if op == "pay_compensation":
        ensure_schema(db)
        if one(db, "SELECT id FROM mr_compensations WHERE id=?", (x["id"],)):
            # 付款原件不可覆盖：同一支付请求重发被拒绝
            return {"decision": "duplicate_payment_rejected"}
        if one(db,
            "SELECT id FROM mr_compensations WHERE work_order=? AND customer=? AND paid=1",
            (x["work_order"], x["customer"]),
        ):
            # 同一工单同一客户的合同减免只付一次：换单号重复支付同样拒绝，保留第一份原件
            return {"decision": "duplicate_payment_rejected"}
        p = one(db, "SELECT * FROM mr_profiles WHERE customer=?", (x["customer"],))
        minutes = outage_minutes(db, x["work_order"], x["customer"])
        due, chargeable = _compensation_due(p, minutes)
        if x["amount"] != due:
            return {"decision": "amount_mismatch", "due": due, "claimed": x["amount"]}
        db.execute(
            "INSERT INTO mr_compensations VALUES(?,?,?,?,?,?,?,1,?)",
            (x["id"], x["work_order"], x["customer"], x.get("outage_id"),
             minutes, x["amount"], x["reason"], x["at"]),
        )
        return {"decision": "compensation_paid", "amount": due, "minutes": minutes}
    raise ValueError(op)
