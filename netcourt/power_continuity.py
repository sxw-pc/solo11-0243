"""停电期间备电连续性仲裁。

正式可保证时长只由电池物理事实推出：实际荷电状态(SOC)、健康度(SOH)、
温度、放电曲线、逆变器共享上限与当前真实负载。控制器经 ``ups_sensor``
上报的剩余分钟仅作交叉校验：同一电池组的重发估计按最新一条取用，绝不
相加；已隔离电池组不计入可用容量。

发电车租赁按 承运到场→现场接线→相序核验→带载试验→正式转供 分段记录，
带载试验未成功不得把预计到达时间说成已供电。中央进程丢失并联签章后，
禁止再次闭合开关试探，只能以独立物理表计认回真实电源状态。电池放电、
发电车出力与客户负载沿时间轴积分闭合，转换中真实发生的短断必须按
合同口径报告并在超限时产生补偿。
"""
import json
import sqlite3

RESERVE_SOC = 0.10          # 电池不得放空的保留电量
GUARANTEE_MARGIN = 0.90     # 可保证下限相对名义值的保守系数
ENERGIZED_W = 1.0           # 物理表计判定母线带电的瓦特意值

SCHEMA = """
CREATE TABLE IF NOT EXISTS pc_packs(
  site TEXT, pack TEXT, capacity_wh REAL, soc REAL, soh REAL,
  temp_c REAL, isolated INTEGER, curve TEXT, updated_at INTEGER,
  PRIMARY KEY(site,pack));
CREATE TABLE IF NOT EXISTS pc_inverters(site TEXT PRIMARY KEY, rated_w REAL, derate REAL);
CREATE TABLE IF NOT EXISTS pc_loads(
  site TEXT, load TEXT, customer TEXT, class TEXT, power_w REAL,
  degradable INTEGER, min_power_w REAL, max_break_s REAL, breach_comp REAL,
  shed INTEGER, PRIMARY KEY(site,load));
CREATE TABLE IF NOT EXISTS pc_consents(
  site TEXT, load TEXT, granted INTEGER, at INTEGER, withdrawn INTEGER,
  PRIMARY KEY(site,load));
CREATE TABLE IF NOT EXISTS pc_shed_events(
  id TEXT PRIMARY KEY, site TEXT, load TEXT, at INTEGER,
  restored_at INTEGER, invalid INTEGER);
CREATE TABLE IF NOT EXISTS pc_gen_state(
  site TEXT PRIMARY KEY, truck TEXT, eta INTEGER, rental_cost REAL, fuel_w REAL,
  dispatched_at INTEGER, arrived_at INTEGER, wired_at INTEGER,
  phase_ok INTEGER, phase_at INTEGER, test_ok INTEGER, test_at INTEGER, test_w REAL,
  transferred_at INTEGER, basis TEXT, receipt_lost INTEGER);
CREATE TABLE IF NOT EXISTS pc_source(
  site TEXT PRIMARY KEY, source TEXT, since INTEGER, basis TEXT);
CREATE TABLE IF NOT EXISTS pc_meters(
  site TEXT, at INTEGER, grid_w REAL, battery_w REAL, gen_w REAL, corrected INTEGER,
  PRIMARY KEY(site,at));
CREATE TABLE IF NOT EXISTS pc_interruptions(
  id TEXT PRIMARY KEY, site TEXT, load TEXT, customer TEXT,
  started_at INTEGER, ended_at INTEGER, duration_s REAL,
  kind TEXT, within_contract INTEGER, corrected INTEGER, compensated INTEGER);
CREATE TABLE IF NOT EXISTS pc_compensations(
  id TEXT PRIMARY KEY, site TEXT, customer TEXT, load TEXT,
  amount REAL, reason TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS pc_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, at INTEGER,
  kind TEXT, payload TEXT);
"""


def init(db):
    db.executescript(SCHEMA)


def _log(db, site, at, kind, payload):
    db.execute(
        "INSERT INTO pc_log(site,at,kind,payload) VALUES(?,?,?,?)",
        (site, at, kind, json.dumps(payload, sort_keys=True, ensure_ascii=False)),
    )


def _rows(db, q, a=()):
    return [dict(r) for r in db.execute(q, a)]


def _one(db, q, a=()):
    r = db.execute(q, a).fetchone()
    return dict(r) if r else None


def _temp_factor(temp_c):
    # 10~30°C 满额；越冷越热线性折减，地板 0.5。
    if 10.0 <= temp_c <= 30.0:
        return 1.0
    if temp_c < 10.0:
        return max(0.5, 1.0 - (10.0 - temp_c) * 0.02)
    return max(0.5, 1.0 - (temp_c - 30.0) * 0.03)


def _curve_factor(curve, power_w):
    # 放电曲线：(功率W, 可用系数) 分段线性，端点外钳制。
    pts = sorted((float(w), float(f)) for w, f in curve)
    if power_w <= pts[0][0]:
        return pts[0][1]
    if power_w >= pts[-1][0]:
        return pts[-1][1]
    for (w1, f1), (w2, f2) in zip(pts, pts[1:]):
        if w1 <= power_w <= w2:
            ratio = (power_w - w1) / (w2 - w1) if w2 > w1 else 0.0
            return f1 + (f2 - f1) * ratio
    return pts[-1][1]


def latest_estimates(db, site):
    """每个电池组只取最新一条控制器估计；重发不累计。表不存在时返回空。"""
    try:
        q = """SELECT u.pack, u.minutes FROM ups_reports u
               WHERE u.site=? AND u.at=(
                 SELECT MAX(at) FROM ups_reports WHERE site=? AND pack=u.pack)"""
        return {r["pack"]: r["minutes"] for r in db.execute(q, (site, site))}
    except sqlite3.OperationalError:
        return {}


def _active_loads_at(db, site, at):
    rows = db.execute(
        "SELECT * FROM pc_loads WHERE site=? AND COALESCE(shed,0)=0",
        (site,)).fetchall()
    active = []
    for r in rows:
        load = dict(r)
        paused = _one(
            db,
            "SELECT 1 FROM pc_shed_events WHERE site=? AND load=? AND invalid=0 "
            "AND at<=? AND (restored_at IS NULL OR restored_at>?) LIMIT 1",
            (site, load["load"], at, at),
        )
        if not paused:
            active.append(load)
    return active


def _battery_discharged_wh(db, site, at):
    """沿表计时间轴积分电池已放出的电量（瓦时）。"""
    rows = db.execute(
        "SELECT at,battery_w FROM pc_meters WHERE site=? AND at<=? ORDER BY at",
        (site, at)).fetchall()
    total = 0.0
    for r1, r2 in zip(rows, rows[1:]):
        total += max(0.0, r1["battery_w"]) * (r2["at"] - r1["at"]) / 3600.0
    return total


def guarantee(db, site, at):
    """从物理状态推出可保证供电时间区间（分钟），与控制器上报条数无关。"""
    init(db)
    packs = _rows(
        db, "SELECT * FROM pc_packs WHERE site=? AND isolated=0", (site,))
    isolated = _rows(
        db, "SELECT pack FROM pc_packs WHERE site=? AND isolated=1", (site,))
    loads = _active_loads_at(db, site, at)
    load_w = sum(l["power_w"] for l in loads)
    inv = _one(db, "SELECT * FROM pc_inverters WHERE site=?", (site,))
    limit = inv["rated_w"] * inv["derate"] if inv else 0.0
    discharged = _battery_discharged_wh(db, site, at)
    result = {
        "decision": "guaranteed",
        "site": site,
        "at": at,
        "load_w": round(load_w, 6),
        "inverter_limit_w": round(limit, 6),
        "packs_used": [p["pack"] for p in packs],
        "isolated_packs": [p["pack"] for p in isolated],
        "controller_estimates": latest_estimates(db, site),
        "discharged_wh": round(discharged, 6),
        "available_wh_low": 0.0,
        "available_wh_high": 0.0,
        "low_minutes": None,
        "high_minutes": None,
        "low_until": None,
        "high_until": None,
        "unprotectable": [],
    }
    if not packs:
        result["decision"] = "no_available_pack"
        return result
    cap_sum = sum(p["capacity_wh"] for p in packs)
    energy_low = energy_high = 0.0
    for p in packs:
        share = load_w * p["capacity_wh"] / cap_sum if cap_sum else 0.0
        factor = _temp_factor(p["temp_c"]) * _curve_factor(
            json.loads(p["curve"]), share)
        usable_soc = max(0.0, p["soc"] - RESERVE_SOC)
        nominal = p["capacity_wh"] * usable_soc * p["soh"] * factor
        energy_high += nominal
        energy_low += nominal * GUARANTEE_MARGIN
    # 已沿时间轴放出的电量必须从剩余容量中扣除（按名义能量比例摊到上下界）。
    if energy_high > 0:
        share = min(1.0, discharged / energy_high)
        energy_high = max(0.0, energy_high - discharged)
        energy_low = max(0.0, energy_low - discharged * share)
    result["available_wh_low"] = round(energy_low, 6)
    result["available_wh_high"] = round(energy_high, 6)
    for load in loads:
        if load["min_power_w"] > limit:
            result["unprotectable"].append(load["load"])
    if load_w <= 0:
        result["decision"] = "no_active_load"
        return result
    if load_w > limit:
        result["decision"] = "insufficient_inverter"
        result["reduce_w"] = round(load_w - limit, 6)
        return result
    if energy_high <= 1e-9:
        # 沿时间轴放电后剩余容量已耗尽：此刻没有可保证的剩余时长。
        result["decision"] = "battery_exhausted"
        result["low_minutes"] = 0.0
        result["high_minutes"] = 0.0
        result["low_until"] = at
        result["high_until"] = at
        return result
    high_min = energy_high / load_w * 60.0
    low_min = energy_low / load_w * 60.0
    result["low_minutes"] = round(low_min, 6)
    result["high_minutes"] = round(high_min, 6)
    result["low_until"] = at + round(low_min * 60.0)
    result["high_until"] = at + round(high_min * 60.0)
    return result


def _set_source(db, site, source, at, basis):
    db.execute(
        "INSERT INTO pc_source VALUES(?,?,?,?) "
        "ON CONFLICT(site) DO UPDATE SET source=excluded.source,"
        " since=excluded.since, basis=excluded.basis",
        (site, source, at, basis))


def _add_compensation(db, site, customer, load, amount, reason, at):
    cid = "%s:%s:%s:%s" % (site, load, reason, at)
    db.execute("INSERT OR IGNORE INTO pc_compensations VALUES(?,?,?,?,?,?,?)",
               (cid, site, customer, load, amount, reason, at))
    return cid


def detect_interruptions(db, site, start, end):
    """按物理表计时间轴重推客户短断；表计校正后以校正值为准。"""
    init(db)
    db.execute(
        "DELETE FROM pc_interruptions WHERE site=? AND kind='outage' "
        "AND started_at>=? AND ended_at<=?", (site, start, end))
    readings = _rows(
        db, "SELECT * FROM pc_meters WHERE site=? AND at>=? AND at<=? ORDER BY at",
        (site, start, end))
    found = []
    if len(readings) < 2:
        return found
    open_breaks = {}
    for r1, r2 in zip(readings, readings[1:]):
        t1, t2 = r1["at"], r2["at"]
        supply = r1["grid_w"] + r1["battery_w"] + r1["gen_w"]
        if supply + 1e-9 >= sum(
                l["power_w"] for l in _active_loads_at(db, site, t1)):
            continue
        for load in _active_loads_at(db, site, t1):
            open_breaks.setdefault(load["load"], [t1, t2, load])[1] = t2
    corrected_times = {r["at"] for r in readings if r["corrected"]}
    for load_id, (t1, t2, load) in open_breaks.items():
        duration = float(t2 - t1)
        within = duration <= load["max_break_s"]
        corrected = 1 if any(t1 <= t <= t2 for t in corrected_times) else 0
        iid = "%s:%s:%s" % (site, load_id, t1)
        compensated = 0
        if not within:
            _add_compensation(
                db, site, load["customer"], load_id, load["breach_comp"],
                "sla_breach", t2)
            compensated = 1
        db.execute(
            "INSERT INTO pc_interruptions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (iid, site, load_id, load["customer"], t1, t2, duration,
             "outage", int(within), corrected, compensated))
        found.append({
            "load": load_id, "started_at": t1, "ended_at": t2,
            "duration_s": duration, "within_contract": bool(within),
            "corrected": bool(corrected)})
    return found


def close_balance(db, site, start, end, tolerance_wh):
    """电池放电+发电车出力+市电=客户负载，沿时间轴分段积分闭合。"""
    init(db)
    breaks = detect_interruptions(db, site, start, end)
    readings = _rows(
        db, "SELECT * FROM pc_meters WHERE site=? AND at>=? AND at<=? ORDER BY at",
        (site, start, end))
    grid_wh = battery_wh = gen_wh = load_wh = 0.0
    for r1, r2 in zip(readings, readings[1:]):
        hours = (r2["at"] - r1["at"]) / 3600.0
        grid_wh += r1["grid_w"] * hours
        battery_wh += r1["battery_w"] * hours
        gen_wh += r1["gen_w"] * hours
        load_wh += sum(l["power_w"] for l in
                       _active_loads_at(db, site, r1["at"])) * hours
    supply_wh = grid_wh + battery_wh + gen_wh
    gap = supply_wh - load_wh
    unserved = sum(b["duration_s"] / 3600.0 *
                   next(l["power_w"] for l in _rows(
                       db, "SELECT * FROM pc_loads WHERE site=? AND load=?",
                       (site, b["load"])))
                   for b in breaks)
    # 供电缺口必须恰好等于表计认定的短断未供电量，时间轴才闭合。
    balanced = abs(gap + unserved) <= tolerance_wh + 1e-9
    return {
        "decision": "balanced" if balanced else "energy_gap",
        "site": site, "start": start, "end": end,
        "grid_wh": round(grid_wh, 6), "battery_wh": round(battery_wh, 6),
        "generator_wh": round(gen_wh, 6), "load_wh": round(load_wh, 6),
        "gap_wh": round(gap, 6), "unserved_wh": round(unserved, 6),
        "interruptions": breaks}


def run(db, x):
    init(db)
    op = x["op"]
    site = x["site"]
    at = x.get("at", 0)

    if op == "register_pack":
        db.execute(
            "INSERT INTO pc_packs VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(site,pack) DO UPDATE SET capacity_wh=excluded.capacity_wh,"
            "soc=excluded.soc,soh=excluded.soh,temp_c=excluded.temp_c,"
            "isolated=excluded.isolated,curve=excluded.curve,updated_at=excluded.updated_at",
            (site, x["pack"], x["capacity_wh"], x["soc"], x["soh"],
             x["temp_c"], int(x.get("isolated", 0)),
             json.dumps(x["curve"]), at))
        _log(db, site, at, op, x)
        return {"decision": "pack_registered", "pack": x["pack"]}

    if op == "telemetry":
        old = _one(db, "SELECT * FROM pc_packs WHERE site=? AND pack=?",
                   (site, x["pack"]))
        if not old:
            return {"decision": "unknown_pack"}
        db.execute(
            "UPDATE pc_packs SET soc=?,soh=?,temp_c=?,updated_at=? "
            "WHERE site=? AND pack=?",
            (x.get("soc", old["soc"]), x.get("soh", old["soh"]),
             x.get("temp_c", old["temp_c"]), at, site, x["pack"]))
        _log(db, site, at, op, x)
        return {"decision": "telemetry_recorded", "pack": x["pack"]}

    if op in ("isolate_pack", "restore_pack"):
        old = _one(db, "SELECT * FROM pc_packs WHERE site=? AND pack=?",
                   (site, x["pack"]))
        if not old:
            return {"decision": "unknown_pack"}
        isolated = 1 if op == "isolate_pack" else 0
        db.execute("UPDATE pc_packs SET isolated=?,updated_at=? WHERE site=? AND pack=?",
                   (isolated, at, site, x["pack"]))
        _log(db, site, at, op, x)
        return {"decision": "pack_isolated" if isolated else "pack_restored",
                "pack": x["pack"]}

    if op == "register_inverter":
        db.execute(
            "INSERT INTO pc_inverters VALUES(?,?,?) "
            "ON CONFLICT(site) DO UPDATE SET rated_w=excluded.rated_w,"
            "derate=excluded.derate",
            (site, x["rated_w"], x.get("derate", 1.0)))
        _log(db, site, at, op, x)
        return {"decision": "inverter_registered"}

    if op == "set_inverter_derate":
        cur = _one(db, "SELECT * FROM pc_inverters WHERE site=?", (site,))
        if not cur:
            return {"decision": "unknown_inverter"}
        if not 0.0 < x["derate"] <= 1.0:
            return {"decision": "invalid_derate"}
        db.execute("UPDATE pc_inverters SET derate=? WHERE site=?",
                   (x["derate"], site))
        _log(db, site, at, op, x)
        return {"decision": "derated",
                "inverter_limit_w": cur["rated_w"] * x["derate"]}

    if op == "register_load":
        db.execute(
            "INSERT INTO pc_loads VALUES(?,?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(site,load) DO UPDATE SET customer=excluded.customer,"
            "class=excluded.class,power_w=excluded.power_w,"
            "degradable=excluded.degradable,min_power_w=excluded.min_power_w,"
            "max_break_s=excluded.max_break_s,breach_comp=excluded.breach_comp",
            (site, x["load"], x["customer"], x["class"], x["power_w"],
             int(x.get("degradable", 0)), x["min_power_w"],
             x["max_break_s"], x["breach_comp"]))
        _log(db, site, at, op, x)
        return {"decision": "load_registered", "load": x["load"]}

    if op == "consent":
        db.execute(
            "INSERT INTO pc_consents(site,load,granted,at,withdrawn) "
            "VALUES(?,?,?,?,0) "
            "ON CONFLICT(site,load) DO UPDATE SET granted=excluded.granted,"
            "at=excluded.at,withdrawn=0",
            (site, x["load"], int(x.get("granted", 1)), at))
        _log(db, site, at, op, x)
        return {"decision": "consent_recorded", "load": x["load"]}

    if op == "withdraw_consent":
        con = _one(db, "SELECT * FROM pc_consents WHERE site=? AND load=?",
                   (site, x["load"]))
        if not con or not con["granted"]:
            return {"decision": "consent_missing"}
        db.execute("UPDATE pc_consents SET withdrawn=1 WHERE site=? AND load=?",
                   (site, x["load"]))
        load = _one(db, "SELECT * FROM pc_loads WHERE site=? AND load=?",
                    (site, x["load"]))
        if load and load["shed"]:
            # 同意撤回：削减立即失效，负载回到容量计算，保护路径不得悬空。
            db.execute("UPDATE pc_loads SET shed=0 WHERE site=? AND load=?",
                       (site, x["load"]))
            db.execute(
                "UPDATE pc_shed_events SET invalid=1,restored_at=? "
                "WHERE site=? AND load=? AND invalid=0 AND restored_at IS NULL",
                (at, site, x["load"]))
            decision = "withdrawn_restore_required"
        else:
            decision = "consent_withdrawn"
        _log(db, site, at, op, x)
        return {"decision": decision, "load": x["load"]}

    if op == "shed_load":
        load = _one(db, "SELECT * FROM pc_loads WHERE site=? AND load=?",
                    (site, x["load"]))
        if not load:
            return {"decision": "unknown_load"}
        if not load["degradable"]:
            return {"decision": "load_not_degradable"}
        con = _one(db, "SELECT * FROM pc_consents WHERE site=? AND load=?",
                   (site, x["load"]))
        if not con or not con["granted"] or con["withdrawn"]:
            return {"decision": "consent_withdrawn" if con else "consent_missing"}
        if load["shed"]:
            return {"decision": "already_shed"}
        # 削减前预演：其余受保护负载的最低供电必须仍被逆变器覆盖且有电池可用。
        db.execute("UPDATE pc_loads SET shed=1 WHERE site=? AND load=?",
                   (site, x["load"]))
        plan = guarantee(db, site, at)
        protected = [l for l in _active_loads_at(db, site, at)
                     if not l["degradable"]]
        inv = _one(db, "SELECT * FROM pc_inverters WHERE site=?", (site,))
        limit = inv["rated_w"] * inv["derate"] if inv else 0.0
        breach = plan["decision"] != "guaranteed" or any(
            l["min_power_w"] > limit for l in protected)
        if breach:
            db.execute("UPDATE pc_loads SET shed=0 WHERE site=? AND load=?",
                       (site, x["load"]))
            return {"decision": "shed_would_break_protected_path",
                    "plan": plan}
        sid = "%s:%s:%s" % (site, x["load"], at)
        db.execute("INSERT INTO pc_shed_events VALUES(?,?,?,?,?,0)",
                   (sid, site, x["load"], at, None))
        _add_compensation(db, site, load["customer"], x["load"],
                          x["compensation"], "degraded_service", at)
        _log(db, site, at, op, x)
        return {"decision": "load_shed", "load": x["load"], "plan": plan}

    if op == "restore_load":
        load = _one(db, "SELECT * FROM pc_loads WHERE site=? AND load=?",
                    (site, x["load"]))
        if not load or not load["shed"]:
            return {"decision": "not_shed"}
        db.execute("UPDATE pc_loads SET shed=0 WHERE site=? AND load=?",
                   (site, x["load"]))
        db.execute(
            "UPDATE pc_shed_events SET restored_at=? "
            "WHERE site=? AND load=? AND invalid=0 AND restored_at IS NULL",
            (at, site, x["load"]))
        _log(db, site, at, op, x)
        return {"decision": "load_restored", "load": x["load"]}

    if op == "guarantee":
        return guarantee(db, site, at)

    if op == "gen_dispatch":
        if _one(db, "SELECT 1 FROM pc_gen_state WHERE site=?", (site,)):
            return {"decision": "generator_identity_conflict"}
        db.execute(
            "INSERT INTO pc_gen_state(site,truck,eta,rental_cost,fuel_w,"
            "dispatched_at,receipt_lost) VALUES(?,?,?,?,?,?,0)",
            (site, x["truck"], x["eta"], x["rental_cost"],
             x.get("fuel_w", 0.0), at))
        _log(db, site, at, op, x)
        return {"decision": "dispatched", "eta": x["eta"]}

    if op in ("gen_arrive", "gen_wire", "phase_check", "load_test", "transfer"):
        g = _one(db, "SELECT * FROM pc_gen_state WHERE site=?", (site,))
        if not g:
            return {"decision": "generator_not_dispatched"}
        if op == "gen_arrive":
            if g["arrived_at"] is not None:
                return {"decision": "already_arrived"}
            db.execute("UPDATE pc_gen_state SET arrived_at=? WHERE site=?",
                       (at, site))
            _log(db, site, at, op, x)
            if at > g["eta"]:
                return {"decision": "generator_late",
                        "eta": g["eta"], "arrived_at": at}
            return {"decision": "arrived"}
        if not g["arrived_at"]:
            return {"decision": "generator_not_arrived"}
        if op == "gen_wire":
            if g["wired_at"] is not None:
                return {"decision": "already_wired"}
            db.execute("UPDATE pc_gen_state SET wired_at=? WHERE site=?",
                       (at, site))
            _log(db, site, at, op, x)
            return {"decision": "wired"}
        if not g["wired_at"]:
            return {"decision": "not_wired"}
        if op == "phase_check":
            db.execute(
                "UPDATE pc_gen_state SET phase_ok=?,phase_at=? WHERE site=?",
                (int(bool(x["ok"])), at, site))
            _log(db, site, at, op, x)
            if not x["ok"]:
                return {"decision": "phase_sequence_failed"}
            return {"decision": "phase_verified"}
        if not g["phase_ok"]:
            return {"decision": "phase_sequence_failed"}
        if op == "load_test":
            db.execute(
                "UPDATE pc_gen_state SET test_ok=?,test_at=?,test_w=? WHERE site=?",
                (int(bool(x["ok"])), at, x.get("test_w", 0.0), site))
            _log(db, site, at, op, x)
            if not x["ok"]:
                return {"decision": "load_test_failed"}
            return {"decision": "load_test_passed", "test_w": x.get("test_w", 0.0)}
        if not g["test_ok"]:
            # 带载未成功：ETA 与接线都不能被宣布成已供电。
            return {"decision": "transfer_blocked"}
        plan = guarantee(db, site, at)
        if plan["decision"] != "guaranteed" or plan["low_until"] <= at:
            # 电池窗口已耗尽才到场接管，客户已断；不能当作无缝转供。
            return {"decision": "battery_exhausted", "plan": plan}
        db.execute(
            "UPDATE pc_gen_state SET transferred_at=?,basis=? WHERE site=?",
            (at, "field_receipt", site))
        _set_source(db, site, "generator", at, "field_receipt")
        _log(db, site, at, op, x)
        return {"decision": "transferred", "at": at}

    if op == "lose_parallel_receipt":
        g = _one(db, "SELECT * FROM pc_gen_state WHERE site=?", (site,))
        if not g or not g["transferred_at"]:
            return {"decision": "no_transfer_to_lose"}
        db.execute("UPDATE pc_gen_state SET receipt_lost=1,basis=NULL WHERE site=?",
                   (site,))
        _set_source(db, site, "unknown", at, None)
        _log(db, site, at, op, x)
        return {"decision": "receipt_lost"}

    if op == "reclose_probe":
        g = _one(db, "SELECT * FROM pc_gen_state WHERE site=?", (site,))
        if g and g["receipt_lost"]:
            # 重启后禁止再次合闸试探，只能靠独立物理表计认回。
            return {"decision": "reclose_forbidden"}
        return {"decision": "reclose_unnecessary"}

    if op == "meter_reading":
        db.execute(
            "INSERT INTO pc_meters VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(site,at) DO UPDATE SET grid_w=excluded.grid_w,"
            "battery_w=excluded.battery_w,gen_w=excluded.gen_w,"
            "corrected=excluded.corrected",
            (site, at, x.get("grid_w", 0.0), x.get("battery_w", 0.0),
             x.get("gen_w", 0.0), int(x.get("corrected", 0))))
        return {"decision": "meter_recorded", "at": at}

    if op == "correct_meter":
        db.execute(
            "INSERT INTO pc_meters VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(site,at) DO UPDATE SET grid_w=excluded.grid_w,"
            "battery_w=excluded.battery_w,gen_w=excluded.gen_w,corrected=1",
            (site, at, x.get("grid_w", 0.0), x.get("battery_w", 0.0),
             x.get("gen_w", 0.0)))
        _log(db, site, at, op, x)
        breaks = detect_interruptions(db, site, x["window_start"], x["window_end"])
        return {"decision": "meter_corrected", "interruptions": breaks}

    if op == "reconcile_source":
        row = _one(
            db, "SELECT * FROM pc_meters WHERE site=? ORDER BY at DESC LIMIT 1",
            (site,))
        if not row:
            return {"decision": "no_meter_evidence"}
        if row["gen_w"] >= ENERGIZED_W:
            source = "generator"
        elif row["battery_w"] >= ENERGIZED_W:
            source = "battery"
        elif row["grid_w"] >= ENERGIZED_W:
            source = "grid"
        else:
            _set_source(db, site, "unknown", at, None)
            return {"decision": "source_unconfirmed"}
        _set_source(db, site, source, at, "physical_meter")
        _log(db, site, at, op, {"source": source})
        return {"decision": "source_recognized", "source": source,
                "basis": "physical_meter"}

    if op == "source_status":
        return _one(db, "SELECT * FROM pc_source WHERE site=?", (site,)) or \
            {"decision": "source_unknown", "site": site, "source": "unknown",
             "since": None, "basis": None}

    if op == "declare_backup":
        src = _one(db, "SELECT * FROM pc_source WHERE site=?", (site,))
        if not src or src["source"] == "unknown" or not src["basis"]:
            return {"decision": "unconfirmed_backup"}
        plan = guarantee(db, site, at)
        if plan["decision"] != "guaranteed":
            return {"decision": "backup_declaration_refused", "plan": plan}
        meter = _one(
            db, "SELECT * FROM pc_meters WHERE site=? ORDER BY at DESC LIMIT 1",
            (site,))
        if meter:
            supply = meter["grid_w"] + meter["battery_w"] + meter["gen_w"]
            if supply + 1e-9 < plan["load_w"]:
                return {"decision": "backup_declaration_refused",
                        "reason": "meter_supply_below_load"}
        return {"decision": "backup_declared", "source": src["source"],
                "basis": src["basis"], "plan": plan}

    if op == "detect_interruptions":
        return {"decision": "detected",
                "interruptions": detect_interruptions(
                    db, site, x["window_start"], x["window_end"])}

    if op == "close_balance":
        return close_balance(
            db, site, x["window_start"], x["window_end"], x["tolerance_wh"])

    if op == "interruption_report":
        q = "SELECT * FROM pc_interruptions WHERE site=?"
        a = [site]
        if x.get("customer"):
            q += " AND customer=?"
            a.append(x["customer"])
        items = _rows(db, q + " ORDER BY started_at", a)
        comps = _rows(
            db, "SELECT * FROM pc_compensations WHERE site=? ORDER BY at", (site,))
        return {"decision": "report", "site": site,
                "interruptions": items, "compensations": comps}

    raise ValueError(op)
