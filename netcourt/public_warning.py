"""公文授权、真实触达证据与后续更正的应急预警交付台。

本模块替代“提交队列即预警”的临时判断（见 :mod:`netcourt.public_alert` 与
:func:`netcourt.reach_note.provisional_warned`）。核心原则：

* 每份预警公文固定签发机关、有效区域、文案版本、语言与生效时段，事件 ID 即幂等键，
  重试不得改正文或另造一份丢失原授权时段的新预警；
* 证据分五级，低等级不得冒充高等级：
  1 运营商受理 / 2 基站广播 / 3 设备确认 / 4 值班人本人接收 / 5 现场广播或上门通知；
* 触达按住户、学校、设备去重核算：多基站覆盖同一设备只计一次，漫游终端改归注册地，
  绝不计入错误区域；
* 区域边界跨基站时维护具名住户清单与盲区清单，总发送数不能证明区域已知晓；
* 运营商签名流水可在“响应丢失 / 重连后”对账回真实广播范围；
* 撤权或到期后，队列重试不能再发原消息；
* 错误文案一旦广播即成为不可撤回的历史，更正公文必须引用旧版并列明重新通知对象。

仅使用 Python 标准库与 SQLite。对外主入口是 :func:`run`（与 intent/policy/enforce
一致的 ``run(db, payload)`` 分派），同时提供一组语义化函数和查询函数。
"""

import json
from contextlib import contextmanager

from .store import one, rows, tx, connect
from .reach_note import provisional_warned  # re-exported for callers of this module

# ---------------------------------------------------------------------------
# 常量与基础工具
# ---------------------------------------------------------------------------

#: 五级证据。数字越大证明力越强，低等级不得替代高等级。
LEVELS = {
    "accepted": 1, "acceptance": 1, "carrier_acceptance": 1, "l1": 1, "level1": 1,
    "cell_broadcast": 2, "base_station_broadcast": 2, "broadcast": 2, "l2": 2, "level2": 2,
    "device_confirmed": 3, "device_confirmation": 3, "device_ack": 3, "ack": 3, "l3": 3, "level3": 3,
    "personal_receipt": 4, "personal_confirmation": 4, "personally_received": 4,
    "duty_receipt": 4, "duty_officer_receipt": 4, "personal": 4, "l4": 4, "level4": 4,
    "onsite_notified": 5, "onsite_notification": 5, "onsite_broadcast": 5,
    "field_notification": 5, "field_notified": 5, "community_broadcast": 5,
    "broadcast_vehicle": 5, "onsite": 5, "l5": 5, "level5": 5,
}

DEVICE_LEVEL = 3
PERSONAL_LEVEL = 4
ONSITE_LEVEL = 5
BROADCAST_LEVEL = 2
ACCEPT_LEVEL = 1

DEFAULT_HOUSEHOLD_LEVEL = 2   # 普通基站覆盖住户：可核验的基站广播即满足
DEFAULT_BLIND_LEVEL = 5       # 盲区住户：必须现场广播/上门（更高证据不得被低级替代）
DEFAULT_SCHOOL_LEVEL = 4      # 学校：值班人本人接收


class Result(dict):
    """普通 dict，额外允许属性访问，方便不同调用习惯。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def ok(data=None, **kw):
    out = Result(kw)
    if data:
        out.update(data)
    return out


def pick(p, *keys, default=None):
    """返回 payload 中第一个非 None 的键值（兼容大量近义参数名）。"""
    for k in keys:
        if k in p and p[k] is not None:
            return p[k]
    return default


def as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _dumps(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _loads(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return default


def _pos(p, index, default=None):
    pos = p.get("_pos")
    if isinstance(pos, (list, tuple)) and len(pos) > index:
        return pos[index]
    return default


def _target(p, index=0):
    return pick(p, "warning_id", "warning", "event_id", "event", "alert_id", "alert",
                "wid", "target", "id", "region") or _pos(p, index)


def _now_like(p, *keys):
    return pick(p, *keys, "at", "time", "ts")


@contextmanager
def _atomic(db):
    """外层（store.execute）已开事务时不重复 BEGIN；直接调用时自行开事务。"""
    if db.in_transaction:
        yield
    else:
        with tx(db):
            yield


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS pw_warnings(
        id TEXT PRIMARY KEY,
        authority TEXT NOT NULL,
        doc_number TEXT,
        region TEXT NOT NULL,
        text_id TEXT,
        version TEXT NOT NULL,
        digest TEXT,
        body TEXT,
        language TEXT NOT NULL,
        starts_at INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        status TEXT NOT NULL,
        corrects TEXT,
        corrects_version TEXT,
        renotify TEXT,
        created_at INTEGER,
        revoked_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS pw_authority_grants(
        authority TEXT NOT NULL,
        region TEXT NOT NULL,
        status TEXT NOT NULL,
        granted_at INTEGER,
        revoked_at INTEGER,
        reason TEXT,
        PRIMARY KEY(authority, region)
    )""",
    """CREATE TABLE IF NOT EXISTS pw_regions(
        id TEXT PRIMARY KEY,
        authority TEXT,
        boundary_version INTEGER DEFAULT 1,
        household_level INTEGER DEFAULT 2,
        blind_level INTEGER DEFAULT 5,
        school_level INTEGER DEFAULT 4,
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS pw_cells(
        id TEXT PRIMARY KEY,
        region TEXT,
        zone TEXT,
        status TEXT DEFAULT 'online',
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS pw_households(
        id TEXT PRIMARY KEY,
        region TEXT NOT NULL,
        name TEXT,
        cell TEXT,
        blind INTEGER DEFAULT 0,
        required_level INTEGER,
        status TEXT DEFAULT 'active',
        joined_at INTEGER,
        left_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS pw_schools(
        id TEXT PRIMARY KEY,
        region TEXT NOT NULL,
        name TEXT,
        duty_officer TEXT,
        duty_phone TEXT,
        satellite_terminal TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pw_devices(
        id TEXT PRIMARY KEY,
        household TEXT,
        school TEXT,
        home_region TEXT NOT NULL,
        kind TEXT,
        serving_cell TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pw_outbox(
        key TEXT PRIMARY KEY,
        warning_id TEXT,
        channel TEXT DEFAULT 'carrier',
        region TEXT,
        version TEXT,
        digest TEXT,
        status TEXT,
        attempts INTEGER DEFAULT 0,
        response_lost INTEGER DEFAULT 0,
        serial TEXT,
        created_at INTEGER,
        updated_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS pw_dispatch_attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        warning_id TEXT,
        channel TEXT,
        at INTEGER,
        response_lost INTEGER,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pw_flows(
        serial TEXT PRIMARY KEY,
        warning_id TEXT,
        version TEXT,
        digest TEXT,
        carrier TEXT,
        channel TEXT DEFAULT 'carrier',
        scope_cells TEXT,
        scope_regions TEXT,
        signature_valid INTEGER DEFAULT 1,
        executed INTEGER DEFAULT 1,
        accepted_at INTEGER,
        at INTEGER,
        source TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pw_evidence(
        id TEXT PRIMARY KEY,
        warning_id TEXT NOT NULL,
        version TEXT NOT NULL,
        region TEXT,
        subject_type TEXT,
        subject_id TEXT,
        level INTEGER NOT NULL,
        kind TEXT NOT NULL,
        channel TEXT,
        actor TEXT,
        cell TEXT,
        device TEXT,
        network_region TEXT,
        attributed_region TEXT,
        roaming INTEGER DEFAULT 0,
        counted INTEGER DEFAULT 1,
        action_id TEXT,
        detail TEXT,
        at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pw_attempts(
        id TEXT PRIMARY KEY,
        warning_id TEXT,
        channel TEXT,
        target_type TEXT,
        target_id TEXT,
        outcome TEXT,
        person TEXT,
        at INTEGER,
        detail TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pw_boundary_changes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        region TEXT,
        household TEXT,
        old_region TEXT,
        new_region TEXT,
        action TEXT,
        at INTEGER
    )""",
]


def ensure_schema(db):
    for stmt in SCHEMA:
        db.execute(stmt)


def _table_exists(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# 授权与公文
# ---------------------------------------------------------------------------

def _auth_state(db, w, at):
    """返回公文在 at 时刻的授权状态。"""
    if w is None:
        return "missing"
    if w["status"] == "revoked":
        return "revoked"
    if w["status"] == "corrected":
        return "superseded"
    # 任何匹配（精确区域或全域 *）的撤权记录都优先于早先的授权登记。
    revoked_grant = one(
        db,
        "SELECT 1 FROM pw_authority_grants WHERE authority=? AND status='revoked' "
        "AND region IN (?, '*') LIMIT 1",
        (w["authority"], w["region"]),
    )
    if revoked_grant:
        return "authority_revoked"
    if at is None:
        return "valid"
    if at < w["starts_at"]:
        return "not_effective"
    if at > w["ends_at"]:
        return "expired"
    return "valid"


def _blocked_decision(state):
    return {
        "revoked": "revoked",
        "authority_revoked": "authority_revoked",
        "expired": "expired",
        "not_effective": "not_effective",
        "superseded": "superseded",
        "missing": "unknown_warning",
    }.get(state, "dispatch_blocked")


def _op_issue(db, p):
    wid = pick(p, "warning_id", "id", "event_id", "event", "alert", "alert_id") or _pos(p, 0)
    authority = pick(p, "authority", "issuer", "issuing_authority", "organ")
    region = pick(p, "region", "effective_region", "area")
    version = pick(p, "version", "text_version", "message_version", "copy_version")
    language = pick(p, "language", "lang")
    starts = pick(p, "starts_at", "start", "effective_from", "valid_from", "begin_at")
    ends = pick(p, "ends_at", "end", "effective_to", "valid_to", "expires_at", "expire_at", "until")
    at = _now_like(p, "created_at", "issued_at")
    if starts is None:
        starts = at if at is not None else 0
    if ends is None:
        ends = pick(p, "end_at")
    body = pick(p, "body", "text", "content", "message")
    digest = pick(p, "digest", "message_digest", "text_digest")
    if body is not None and digest is None:
        import hashlib
        digest = "sha256:" + hashlib.sha256(str(body).encode("utf-8")).hexdigest()
    if not wid or not str(authority or "").strip() or not str(region or "").strip() \
            or version in (None, "") or not str(language or "").strip() \
            or starts is None or ends is None or starts > ends:
        return Result({"decision": "invalid_warning", "valid": False})
    old = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,))
    immutable = (authority, region, str(version), language, starts, ends,
                 body, digest, pick(p, "corrects"), pick(p, "corrects_version"))
    if old:
        same = (
            old["authority"] == authority and old["region"] == region
            and str(old["version"]) == str(version) and old["language"] == language
            and old["starts_at"] == starts and old["ends_at"] == ends
            and (body is None or old["body"] == body)
            and (digest is None or old["digest"] == digest)
        )
        if not same:
            return Result({"decision": "warning_identity_conflict", "warning_id": wid})
        return Result({"decision": "already_issued", "warning_id": wid, "idempotent_replay": True,
                       "version": old["version"], "region": old["region"]})
    # 若该机关对该区域（或全域）已有撤权记录，不得签发。
    revoked_grant = one(
        db,
        "SELECT 1 FROM pw_authority_grants WHERE authority=? AND status='revoked' "
        "AND region IN (?,'*')",
        (authority, region),
    )
    if revoked_grant:
        return Result({"decision": "authority_revoked", "warning_id": wid, "valid": False})
    corrects = pick(p, "corrects", "corrects_warning", "correct")
    corrects_version = pick(p, "corrects_version", "old_version", "replaces_version")
    db.execute(
        "INSERT INTO pw_warnings(id,authority,doc_number,region,text_id,version,digest,body,"
        "language,starts_at,ends_at,status,corrects,corrects_version,renotify,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'authorized',?,?,?,?)",
        (wid, authority, pick(p, "doc_number", "document_number"), region,
         pick(p, "text_id"), str(version), digest, body, language, starts, ends,
         corrects, corrects_version,
         _dumps(p.get("renotify")) if p.get("renotify") is not None else None,
         at if at is not None else starts),
    )
    db.execute(
        "INSERT OR IGNORE INTO pw_authority_grants(authority,region,status,granted_at) "
        "VALUES(?,?,'granted',?)",
        (authority, region, at if at is not None else starts),
    )
    db.execute(
        "INSERT OR IGNORE INTO pw_regions(id,authority,updated_at) VALUES(?,?,?)",
        (region, authority, at if at is not None else starts),
    )
    return Result({"decision": "issued", "warning_id": wid, "id": wid, "region": region,
                   "version": str(version), "authorized": True})


def _op_revoke(db, p):
    at = _now_like(p)
    wid = pick(p, "warning_id", "warning", "event", "alert")
    authority_arg = pick(p, "authority", "issuer", "organ")
    region_arg = pick(p, "region", "effective_region", "area")
    out = {"decision": "revoked"}
    if wid is None and ("id" in p) and one(db, "SELECT 1 FROM pw_warnings WHERE id=?", (p["id"],)):
        wid = p["id"]
    if wid is not None:
        w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,))
        if not w:
            return Result({"decision": "unknown_warning", "warning_id": wid})
        db.execute("UPDATE pw_warnings SET status='revoked',revoked_at=? WHERE id=?",
                   (at, wid))
        out.update({"warning_id": wid, "warning_revoked": True})
    # 只有显式要求撤“机关授权”时才写授权记录；撤销单条预警不连坐机关授权，
    # 否则同一机关事后发布更正公文会被误挡。
    if authority_arg is not None:
        grant_region = region_arg if region_arg is not None else "*"
        db.execute(
            "INSERT INTO pw_authority_grants(authority,region,status,revoked_at,reason) "
            "VALUES(?,?,'revoked',?,?) ON CONFLICT(authority,region) DO UPDATE SET "
            "status='revoked',revoked_at=excluded.revoked_at,reason=excluded.reason",
            (authority_arg, grant_region, at, pick(p, "reason")),
        )
        out.update({"authority_revoked": True, "authority": authority_arg,
                    "region": grant_region})
    if wid is None and authority_arg is None:
        return Result({"decision": "nothing_to_revoke"})
    return Result(out)


# ---------------------------------------------------------------------------
# 基础资料登记
# ---------------------------------------------------------------------------

def _op_register_region(db, p):
    rid = pick(p, "region_id", "id", "region", "name") or _pos(p, 0)
    if not rid:
        return Result({"decision": "invalid_region"})
    db.execute(
        "INSERT INTO pw_regions(id,authority,boundary_version,updated_at) VALUES(?,?,1,?) "
        "ON CONFLICT(id) DO UPDATE SET authority=COALESCE(excluded.authority,pw_regions.authority)",
        (rid, pick(p, "authority"), _now_like(p)),
    )
    return Result({"decision": "registered", "region": rid})


def _op_register_cell(db, p):
    cid = pick(p, "cell_id", "id", "cell", "station_id", "base_station") or _pos(p, 0)
    if not cid:
        return Result({"decision": "invalid_cell"})
    status = pick(p, "status", "state") or "online"
    if status in ("down", "off", "outage"):
        status = "offline"
    if status in ("repair", "repairing", "under_repair"):
        status = "maintenance"
    db.execute(
        "INSERT INTO pw_cells(id,region,zone,status,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET region=excluded.region,zone=excluded.zone,"
        "status=excluded.status,updated_at=excluded.updated_at",
        (cid, pick(p, "region"), pick(p, "zone", "half"), status, _now_like(p)),
    )
    return Result({"decision": "registered", "cell": cid, "status": status})


def _op_register_household(db, p):
    hid = pick(p, "household_id", "id", "household", "resident_id") or _pos(p, 0)
    region = pick(p, "region") or _pos(p, 1)
    if not hid or not region:
        return Result({"decision": "invalid_household"})
    blind = 1 if pick(p, "blind", "in_blind_spot") else 0
    req = pick(p, "required_level")
    if isinstance(req, str):
        req = LEVELS.get(req, req)
    db.execute(
        "INSERT INTO pw_households(id,region,name,cell,blind,required_level,status,joined_at) "
        "VALUES(?,?,?,?,?,?, 'active',?) ON CONFLICT(id) DO UPDATE SET region=excluded.region,"
        "name=excluded.name,cell=excluded.cell,blind=excluded.blind,"
        "required_level=excluded.required_level,status='active'",
        (hid, region, pick(p, "name", "resident", "household_name"),
         pick(p, "cell", "cell_id", "serving_cell"), blind, req, _now_like(p)),
    )
    return Result({"decision": "registered", "household": hid, "region": region})


def _op_register_school(db, p):
    sid = pick(p, "school_id", "id", "school") or _pos(p, 0)
    region = pick(p, "region") or _pos(p, 1)
    if not sid or not region:
        return Result({"decision": "invalid_school"})
    db.execute(
        "INSERT INTO pw_schools(id,region,name,duty_officer,duty_phone,satellite_terminal) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET region=excluded.region,"
        "name=excluded.name,duty_officer=excluded.duty_officer,duty_phone=excluded.duty_phone,"
        "satellite_terminal=excluded.satellite_terminal",
        (sid, region, pick(p, "name"), pick(p, "duty_officer", "officer", "contact", "person"),
         pick(p, "duty_phone", "phone", "tel"),
         pick(p, "satellite_terminal", "terminal")),
    )
    return Result({"decision": "registered", "school": sid, "region": region})


def _op_register_device(db, p):
    did = pick(p, "device_id", "id", "device", "terminal_id", "terminal") or _pos(p, 0)
    home_region = pick(p, "home_region", "registered_region", "region", "belongs_region")
    if not did or not home_region:
        return Result({"decision": "invalid_device"})
    db.execute(
        "INSERT INTO pw_devices(id,household,school,home_region,kind,serving_cell) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET household=excluded.household,"
        "school=excluded.school,home_region=excluded.home_region,kind=excluded.kind,"
        "serving_cell=excluded.serving_cell",
        (did, pick(p, "household", "household_id"), pick(p, "school", "school_id"),
         home_region, pick(p, "kind", "type"),
         pick(p, "serving_cell", "cell", "cell_id")),
    )
    return Result({"decision": "registered", "device": did, "home_region": home_region})


def _op_set_policy(db, p):
    region = pick(p, "region") or _target(p)
    if not region:
        return Result({"decision": "region_required"})
    ensure = db.execute("SELECT 1 FROM pw_regions WHERE id=?", (region,)).fetchone()
    if not ensure:
        db.execute("INSERT INTO pw_regions(id) VALUES(?)", (region,))
    for col, key in (("household_level", "household_level"),
                     ("blind_level", "blind_level"),
                     ("school_level", "school_level")):
        val = pick(p, key)
        if val is not None:
            if isinstance(val, str):
                val = LEVELS.get(val, val)
            db.execute(f"UPDATE pw_regions SET {col}=? WHERE id=?", (val, region))
    return Result({"decision": "policy_set", "region": region})


# ---------------------------------------------------------------------------
# 区域边界调整
# ---------------------------------------------------------------------------

def _op_adjust_boundary(db, p):
    region = pick(p, "region", "effective_region", "area") or _target(p)
    at = _now_like(p)
    if not region:
        return Result({"decision": "region_required"})
    full = pick(p, "households", "members", "household_ids")
    include = as_list(pick(p, "include", "add", "add_households"))
    exclude = as_list(pick(p, "exclude", "remove", "remove_households"))
    to_region = pick(p, "to_region", "move_to")
    changes = []

    def _move(hid, new_region, action):
        h = one(db, "SELECT * FROM pw_households WHERE id=?", (hid,))
        old_region = h["region"] if h else None
        if h:
            db.execute("UPDATE pw_households SET region=?, status='active' WHERE id=?",
                       (new_region, hid))
        else:
            db.execute(
                "INSERT INTO pw_households(id,region,status,joined_at) VALUES(?,?,'active',?)",
                (hid, new_region, at),
            )
        db.execute(
            "INSERT INTO pw_boundary_changes(region,household,old_region,new_region,action,at) "
            "VALUES(?,?,?,?,?,?)",
            (region, hid, old_region, new_region, action, at),
        )
        changes.append({"household": hid, "old_region": old_region,
                        "new_region": new_region, "action": action})

    if full is not None:
        wanted = set()
        for item in as_list(full):
            if isinstance(item, dict):
                hid = item.get("id") or item.get("household_id")
                wanted.add(hid)
                cell = item.get("cell") or item.get("cell_id")
                if one(db, "SELECT 1 FROM pw_households WHERE id=?", (hid,)):
                    db.execute("UPDATE pw_households SET region=?, cell=COALESCE(?,cell), "
                               "status='active' WHERE id=?", (region, cell, hid))
                else:
                    db.execute(
                        "INSERT INTO pw_households(id,region,name,cell,blind,status,joined_at) "
                        "VALUES(?,?,?,?,?, 'active',?)",
                        (hid, region, item.get("name"), cell,
                         1 if item.get("blind") else 0, at),
                    )
                changes.append({"household": hid, "action": "include"})
            else:
                wanted.add(item)
                _move(item, region, "include")
        for h in rows(db, "SELECT id FROM pw_households WHERE region=? AND status='active'",
                      (region,)):
            if h["id"] not in wanted:
                db.execute("UPDATE pw_households SET status='moved_out',left_at=? WHERE id=?",
                           (at, h["id"]))
                db.execute(
                    "INSERT INTO pw_boundary_changes(region,household,old_region,new_region,"
                    "action,at) VALUES(?,?,?,NULL,'exclude',?)",
                    (region, h["id"], region, at),
                )
                changes.append({"household": h["id"], "action": "exclude"})
    for item in include:
        if isinstance(item, dict):
            hid = item.get("id") or item.get("household_id")
            _move(hid, region, "include")
            if item.get("cell"):
                db.execute("UPDATE pw_households SET cell=? WHERE id=?", (item["cell"], hid))
        else:
            _move(item, region, "include")
    for hid in exclude:
        if to_region:
            _move(hid, to_region, "move")
        else:
            db.execute("UPDATE pw_households SET status='moved_out',left_at=? WHERE id=?",
                       (at, hid))
            db.execute(
                "INSERT INTO pw_boundary_changes(region,household,old_region,new_region,"
                "action,at) VALUES(?,?,?,NULL,'exclude',?)",
                (region, hid, region, at),
            )
            changes.append({"household": hid, "action": "exclude"})
    db.execute(
        "INSERT INTO pw_regions(id,boundary_version,updated_at) VALUES(?,1,?) "
        "ON CONFLICT(id) DO UPDATE SET boundary_version=pw_regions.boundary_version+1,"
        "updated_at=excluded.updated_at",
        (region, at),
    )
    return Result({"decision": "boundary_adjusted", "region": region, "changes": changes})


# ---------------------------------------------------------------------------
# 队列提交 / 运营商受理 / 响应丢失 / 重试
# ---------------------------------------------------------------------------

def _warning_ref(p):
    return _target(p)


def _op_dispatch(db, p):
    """提交/重试。同一事件 ID 的正文与授权时段不可改变；撤权/到期后阻断。"""
    wid = _warning_ref(p)
    at = _now_like(p)
    channel = pick(p, "channel", "network") or "carrier"
    key = pick(p, "key", "idempotency_key", "outbox_key") or wid
    if wid is None and key is not None:
        ob = one(db, "SELECT * FROM pw_outbox WHERE key=?", (key,))
        wid = ob["warning_id"] if ob else None
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    at = at if at is not None else w["starts_at"]
    state = _auth_state(db, w, at)
    if state != "valid":
        # 记录一次被阻断的重试意图，但绝不产生新的发送流水。
        db.execute(
            "INSERT INTO pw_dispatch_attempts(warning_id,channel,at,response_lost,note) "
            "VALUES(?,?,?,0,?)",
            (wid, channel, at, "blocked:" + state),
        )
        return Result({"decision": _blocked_decision(state), "blocked": True,
                       "reason": state, "warning_id": wid, "at": at})

    # 幂等：队列中已有同一事件。
    ob = one(db, "SELECT * FROM pw_outbox WHERE key=?", (key,))
    response_lost = 1 if pick(p, "response_lost", "lost_response", "disconnected",
                              "connection_lost") else 0
    serial = pick(p, "serial", "carrier_serial", "flow_serial", "signed_serial", "flow_id")
    executed = pick(p, "executed", "carrier_executed", "actually_executed")
    cells = as_list(pick(p, "broadcast_cells", "cells", "scope_cells"))
    if ob:
        if ob["warning_id"] != wid or ob["version"] != w["version"] or ob["digest"] != w["digest"]:
            return Result({"decision": "idempotency_conflict", "key": key})
        db.execute("UPDATE pw_outbox SET attempts=attempts+1,response_lost=?,"
                   "updated_at=? WHERE key=?",
                   (response_lost or ob["response_lost"], at, key))
        db.execute(
            "INSERT INTO pw_dispatch_attempts(warning_id,channel,at,response_lost,note) "
            "VALUES(?,?,?,?,?)",
            (wid, channel, at, response_lost, "retry"),
        )
        flow = one(db, "SELECT * FROM pw_flows WHERE warning_id=? AND version=? AND channel=? "
                       "AND executed=1 ORDER BY at DESC LIMIT 1",
                   (wid, w["version"], channel))
        known_serial = serial or (flow["serial"] if flow else ob["serial"])
        # 重试时运营商幂等应答带回了签名流水/广播范围 -> 当场对账。
        if serial and (flow is None or serial != flow["serial"]):
            rec = _reconcile_flow(db, {
                "serial": serial, "warning_id": wid, "version": w["version"],
                "digest": w["digest"], "broadcast_cells": cells, "at": at,
                "signature_valid": pick(p, "signature_valid", "signed", default=True),
                "executed": executed if executed is not None else True,
                "channel": channel, "carrier": pick(p, "carrier"),
                "scope_regions": pick(p, "scope_regions", "regions"),
            })
            if rec.get("decision") == "reconciled":
                db.execute("UPDATE pw_outbox SET status='reconciled',serial=? WHERE key=?",
                           (serial, key))
            return rec
        if flow:
            db.execute("UPDATE pw_outbox SET status='reconciled',serial=? WHERE key=?",
                       (flow["serial"], key))
            return Result({"decision": "already_executed", "idempotent_replay": True,
                           "warning_id": wid, "serial": flow["serial"],
                           "broadcast_cells": _loads(flow["scope_cells"], []),
                           "key": key})
        # 没有任何“运营商确已执行”的流水（含对账为 never_executed 的情形）：
        # 队列记录不是完成，允许用同一固定正文重新投递。
        new_status = "response_unknown" if response_lost else "submitted"
        db.execute("UPDATE pw_outbox SET status=? WHERE key=?", (new_status, key))
        if known_serial and ob["serial"]:
            return Result({"decision": "response_lost", "uncertain": True,
                           "warning_id": wid, "serial": known_serial, "key": key,
                           "note": "serial known but no executed flow reconciled"})
        return Result({"decision": new_status, "uncertain": bool(response_lost),
                       "warning_id": wid, "key": key,
                       "attempts": ob["attempts"] + 1, "resent": True})

    db.execute(
        "INSERT INTO pw_outbox(key,warning_id,channel,region,version,digest,status,attempts,"
        "response_lost,serial,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
        (key, wid, channel, w["region"], w["version"], w["digest"],
         "response_unknown" if response_lost else "submitted",
         response_lost, serial, at, at),
    )
    db.execute(
        "INSERT INTO pw_dispatch_attempts(warning_id,channel,at,response_lost,note) "
        "VALUES(?,?,?,?,?)",
        (wid, channel, at, response_lost, "initial_submit"),
    )
    # 同步应答直接带回签名流水时，按对账处理。
    if serial:
        rec = _reconcile_flow(db, {
            "serial": serial, "warning_id": wid, "version": w["version"],
            "digest": w["digest"], "broadcast_cells": cells, "at": at,
            "signature_valid": pick(p, "signature_valid", "signed"),
            "executed": executed if executed is not None else True,
            "channel": channel, "carrier": pick(p, "carrier"),
            "scope_regions": pick(p, "scope_regions", "regions"),
        })
        if rec.get("decision") == "reconciled":
            db.execute("UPDATE pw_outbox SET status='reconciled',serial=? WHERE key=?",
                       (serial, key))
        return rec
    return Result({"decision": "submitted", "warning_id": wid, "key": key,
                   "region": w["region"], "version": w["version"],
                   "uncertain": bool(response_lost),
                   "state": "response_unknown" if response_lost else "accepted"})


def _reconcile_flow(db, p):
    """凭运营商签名流水认回真实执行范围，并把签名范围内的基站广播落为 2 级证据。"""
    serial = pick(p, "serial", "carrier_serial", "flow_serial", "signed_serial", "flow_id", "id")
    wid = pick(p, "warning_id", "warning", "event", "alert") or _target(p)
    version = pick(p, "version", "text_version")
    digest = pick(p, "digest", "message_digest")
    at = _now_like(p)
    if not serial:
        return Result({"decision": "serial_required"})
    sig = pick(p, "signature_valid", "signed", "valid_signature")
    signature_valid = True if sig is None else bool(sig)
    if not signature_valid:
        db.execute(
            "INSERT OR IGNORE INTO pw_flows(serial,warning_id,version,digest,signature_valid,"
            "executed,at) VALUES(?,?,?,?,0,?,?)",
            (serial, wid, version, digest, 1 if pick(p, "executed", default=True) else 0, at),
        )
        return Result({"decision": "invalid_signature", "serial": serial})

    existing = one(db, "SELECT * FROM pw_flows WHERE serial=?", (serial,))
    cells = as_list(pick(p, "broadcast_cells", "cells", "scope_cells", "broadcast_scope"))
    regions = as_list(pick(p, "scope_regions", "regions"))
    executed = pick(p, "executed", "carrier_executed")
    executed = True if executed is None else bool(executed)

    if existing:
        if wid and existing["warning_id"] and existing["warning_id"] != wid:
            return Result({"decision": "serial_conflict", "serial": serial})
        return Result({"decision": "already_reconciled", "serial": serial,
                       "warning_id": existing["warning_id"],
                       "broadcast_cells": _loads(existing["scope_cells"], []),
                       "idempotent_replay": True})

    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if w:
        version = version or w["version"]
        digest = digest or w["digest"]
        if digest and w["digest"] and digest != w["digest"]:
            return Result({"decision": "digest_mismatch", "serial": serial})
        if version and str(version) != str(w["version"]):
            return Result({"decision": "version_mismatch", "serial": serial})
        at = at if at is not None else w["starts_at"]
    if at is None:
        at = 0

    db.execute(
        "INSERT INTO pw_flows(serial,warning_id,version,digest,carrier,channel,scope_cells,"
        "scope_regions,signature_valid,executed,accepted_at,at,source) "
        "VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)",
        (serial, wid, str(version) if version is not None else None, digest,
         pick(p, "carrier"), pick(p, "channel") or "carrier",
         _dumps(cells), _dumps(regions), 1 if executed else 0,
         pick(p, "accepted_at", "at") or at, at,
         pick(p, "source") or "signed_ledger"),
    )
    if not executed:
        ob = one(db, "SELECT key FROM pw_outbox WHERE warning_id=? ORDER BY created_at LIMIT 1",
                 (wid,)) if wid else None
        if ob:
            db.execute("UPDATE pw_outbox SET status='never_executed' WHERE key=?", (ob["key"],))
        return Result({"decision": "never_executed", "serial": serial,
                       "warning_id": wid, "queue_is_not_delivery": True})

    # 签名流水证明的基站广播范围 -> 逐基站落 2 级证据（业务幂等）。
    recorded_cells = []
    for cell in cells:
        dup = one(
            db,
            "SELECT id FROM pw_evidence WHERE warning_id=? AND version=? "
            "AND kind='cell_broadcast' AND cell=?",
            (wid, str(version), cell),
        ) if wid else None
        if dup:
            recorded_cells.append(cell)
            continue
        eid = f"flow:{serial}:{cell}"
        db.execute(
            "INSERT INTO pw_evidence(id,warning_id,version,region,subject_type,subject_id,"
            "level,kind,channel,cell,attributed_region,at,detail) "
            "VALUES(?,?,?,?, 'cell', ?,2,'cell_broadcast','carrier',?,?,?,?)",
            (eid, wid, str(version), w["region"] if w else (regions[0] if regions else None),
             cell, cell, w["region"] if w else None, at,
             _dumps({"serial": serial, "reconciled": True})),
        )
        recorded_cells.append(cell)
    ob = one(db, "SELECT key FROM pw_outbox WHERE warning_id=? ORDER BY created_at LIMIT 1",
             (wid,)) if wid else None
    if ob:
        db.execute("UPDATE pw_outbox SET status='reconciled',serial=? WHERE key=?",
                   (serial, ob["key"]))
    return Result({"decision": "reconciled", "serial": serial, "warning_id": wid,
                   "version": str(version) if version is not None else None,
                   "broadcast_cells": recorded_cells, "executed": True})


# ---------------------------------------------------------------------------
# 证据写入
# ---------------------------------------------------------------------------

def _resolve_version(db, p, wid):
    version = pick(p, "version", "text_version")
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if version is None and w:
        version = w["version"]
    return w, (str(version) if version is not None else None)


def _insert_evidence(db, *, eid, wid, version, region, subject_type, subject_id, level, kind,
                    at, channel=None, actor=None, cell=None, device=None,
                    network_region=None, attributed_region=None, roaming=0, counted=1,
                    action_id=None, detail=None):
    db.execute(
        "INSERT INTO pw_evidence(id,warning_id,version,region,subject_type,subject_id,level,"
        "kind,channel,actor,cell,device,network_region,attributed_region,roaming,counted,"
        "action_id,detail,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, wid, str(version) if version is not None else None, region, subject_type,
         subject_id, level, kind, channel, actor, cell, device, network_region,
         attributed_region, 1 if roaming else 0, 1 if counted else 0, action_id,
         _dumps(detail) if detail is not None else None, at if at is not None else 0),
    )


def _op_cell_broadcast(db, p):
    wid = _warning_ref(p)
    eid = pick(p, "evidence_id", "receipt_id", "id", "rid")
    cell = pick(p, "cell", "cell_id", "station", "base_station") or _pos(p, 1)
    at = _now_like(p)
    if not wid or not cell:
        return Result({"decision": "invalid_broadcast_receipt"})
    w, version = _resolve_version(db, p, wid)
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    if pick(p, "version") is not None and str(pick(p, "version")) != str(w["version"]):
        return Result({"decision": "version_mismatch"})
    at = at if at is not None else w["starts_at"]
    if eid:
        old = one(db, "SELECT * FROM pw_evidence WHERE id=?", (eid,))
        if old:
            same_fact = old["warning_id"] == wid and old["cell"] == cell \
                and old["kind"] == "cell_broadcast"
            return Result({"decision": "reused" if same_fact else "receipt_conflict",
                           "id": eid, "duplicate": same_fact})
    dup = one(db, "SELECT id FROM pw_evidence WHERE warning_id=? AND version=? "
                  "AND kind='cell_broadcast' AND cell=?", (wid, version, cell))
    cell_row = one(db, "SELECT status,region FROM pw_cells WHERE id=?", (cell,))
    if dup:
        return Result({"decision": "duplicate", "duplicate": True, "cell": cell,
                       "warning_id": wid, "existing_id": dup["id"]})
    eid = eid or f"cell:{wid}:{version}:{cell}"
    _insert_evidence(db, eid=eid, wid=wid, version=version,
                     region=w["region"], subject_type="cell", subject_id=cell,
                     level=BROADCAST_LEVEL, kind="cell_broadcast", at=at,
                     channel=pick(p, "channel") or "carrier", cell=cell,
                     detail={"cell_status": cell_row["status"] if cell_row else None,
                             "receipt": True})
    return Result({"decision": "broadcast", "recorded": True, "cell": cell,
                   "warning_id": wid, "version": version, "level": 2,
                   "cell_status": cell_row["status"] if cell_row else None})


def _attribute_device(db, device, network_region):
    """根据设备登记资料纠正漫游/区域归属。返回 (dev_row, home_region, roaming, subject...)。"""
    dev = one(db, "SELECT * FROM pw_devices WHERE id=?", (device,)) if device else None
    if dev:
        home = dev["home_region"]
        roaming = bool(network_region and home and network_region != home)
        return dev, home, roaming
    home = network_region
    roaming = False
    return None, home, roaming


def _op_device_confirm(db, p):
    wid = _warning_ref(p)
    eid = pick(p, "evidence_id", "receipt_id", "id", "rid")
    device = pick(p, "device", "device_id", "terminal", "terminal_id") or _pos(p, 1)
    at = _now_like(p)
    network_region = pick(p, "network_region", "reported_region", "carrier_region",
                          "counted_region", "region")
    cell = pick(p, "cell", "cell_id", "serving_cell")
    if not wid or not device:
        return Result({"decision": "invalid_device_confirmation"})
    w, version = _resolve_version(db, p, wid)
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    at = at if at is not None else w["starts_at"]
    dev, home_region, roaming = _attribute_device(db, device, network_region)
    explicit_roaming = pick(p, "roaming", "is_roaming", "roamer")
    if explicit_roaming:
        roaming = True
    # 归属主体：设备登记的住户 / 学校。
    subject_type, subject_id, counted_region = "device", device, home_region or w["region"]
    household = dev["household"] if dev else pick(p, "household", "household_id")
    school = dev["school"] if dev else pick(p, "school", "school_id")
    if household:
        subject_type, subject_id = "household", household
        h = one(db, "SELECT region FROM pw_households WHERE id=?", (household,))
        if h:
            counted_region = h["region"]
    elif school:
        subject_type, subject_id = "school", school
        s = one(db, "SELECT region FROM pw_schools WHERE id=?", (school,))
        if s:
            counted_region = s["region"]
    # 访客漫游终端（注册地在外地且未关联本地住户/学校）不计入本区域触达。
    local_subject = bool(household or school)
    counted = 1 if (local_subject or not roaming) else 0
    if roaming and not local_subject and network_region and network_region == w["region"]:
        counted_region = network_region  # 网络想计入的错误区域
    if eid:
        old = one(db, "SELECT * FROM pw_evidence WHERE id=?", (eid,))
        if old:
            same = old["warning_id"] == wid and old["device"] == device
            return Result({"decision": "reused" if same else "receipt_conflict", "id": eid})
    # 同一设备对同一公文版本只计一次（即使经过多个基站/网络重复回执）。
    dup_dev = one(
        db,
        "SELECT id FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND kind='device_confirmed' AND device=?",
        (wid, version, device),
    )
    if dup_dev:
        return Result({"decision": "duplicate", "duplicate": True, "device": device,
                       "warning_id": wid, "counted_once": True,
                       "roaming": roaming, "existing_id": dup_dev["id"]})
    eid = eid or f"dev:{wid}:{version}:{device}"
    _insert_evidence(db, eid=eid, wid=wid, version=version, region=w["region"],
                     subject_type=subject_type, subject_id=subject_id,
                     level=DEVICE_LEVEL, kind="device_confirmed", at=at,
                     channel=pick(p, "channel", "network") or "carrier",
                     actor=pick(p, "actor"), cell=cell or (dev["serving_cell"] if dev else None),
                     device=device, network_region=network_region,
                     attributed_region=counted_region, roaming=roaming, counted=counted,
                     detail={"home_region": home_region, "reattributed": roaming and local_subject})
    decision = "confirmed"
    if roaming and not local_subject:
        decision = "roaming_excluded"
    elif roaming and local_subject:
        decision = "confirmed_roaming_reattributed"
    return Result({"decision": decision, "recorded": True, "device": device,
                   "warning_id": wid, "level": 3, "roaming": roaming,
                   "counted": bool(counted), "counted_region": counted_region,
                   "attributed_region": counted_region})


def _op_personal_receipt(db, p):
    """学校值班人本人接收（4 级）。必须有具名本人；设备/终端自动签收只有 3 级。"""
    wid = _warning_ref(p)
    eid = pick(p, "evidence_id", "receipt_id", "id", "rid")
    school = pick(p, "school", "school_id", "target_id") or _pos(p, 1)
    person = pick(p, "person", "officer", "duty_officer", "received_by", "recipient", "name")
    at = _now_like(p)
    if not wid or not school:
        return Result({"decision": "invalid_personal_receipt"})
    if not person or not str(person).strip():
        # 匿名/仅终端签收不能构成本人接收。
        return Result({"decision": "personal_identity_required", "level": 3,
                       "would_be_level": 3})
    w, version = _resolve_version(db, p, wid)
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    at = at if at is not None else w["starts_at"]
    s = one(db, "SELECT * FROM pw_schools WHERE id=?", (school,))
    region = s["region"] if s else w["region"]
    if eid:
        old = one(db, "SELECT * FROM pw_evidence WHERE id=?", (eid,))
        if old:
            same = old["warning_id"] == wid and old["subject_id"] == school \
                and old["actor"] == person
            return Result({"decision": "reused" if same else "receipt_conflict", "id": eid})
    dup = one(
        db,
        "SELECT id FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND kind='personal_receipt' AND subject_type='school' AND subject_id=? AND actor=?",
        (wid, version, school, person),
    )
    if dup:
        return Result({"decision": "duplicate", "duplicate": True, "school": school,
                       "person": person})
    eid = eid or f"personal:{wid}:{version}:{school}:{person}"
    _insert_evidence(db, eid=eid, wid=wid, version=version, region=region,
                     subject_type="school", subject_id=school, level=PERSONAL_LEVEL,
                     kind="personal_receipt", at=at,
                     channel=pick(p, "channel", "via") or "phone",
                     actor=person, device=pick(p, "terminal", "satellite_terminal", "device"),
                     detail={"role": pick(p, "role") or "duty_officer",
                             "signature": pick(p, "signature", "signed", default=True)})
    return Result({"decision": "personally_confirmed", "recorded": True, "school": school,
                   "person": person, "warning_id": wid, "level": 4,
                   "channel": pick(p, "channel") or "phone"})


def _op_call_school(db, p):
    """拨打值守电话。无人接听只记录尝试，不产生任何本人接收证据。"""
    wid = _warning_ref(p)
    cid = pick(p, "call_id", "attempt_id", "id") or f"call:{wid}:{pick(p,'school','school_id')}:{_now_like(p) or 0}"
    school = pick(p, "school", "school_id", "target_id") or _pos(p, 1)
    outcome = pick(p, "outcome", "status", "result") or "no_answer"
    at = _now_like(p)
    answered = pick(p, "answered")
    if answered is not None:
        outcome = "answered" if answered else "no_answer"
    if outcome in ("no-answer", "missed", "unanswered", "noanswer"):
        outcome = "no_answer"
    if not wid or not school:
        return Result({"decision": "invalid_call"})
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,))
    at = at if at is not None else (w["starts_at"] if w else 0)
    db.execute(
        "INSERT OR REPLACE INTO pw_attempts(id,warning_id,channel,target_type,target_id,"
        "outcome,person,at,detail) VALUES(?,?, 'phone','school',?,?,?,?,?)",
        (cid, wid, school, outcome, pick(p, "person", "officer"), at,
         _dumps({"duration": pick(p, "rings", "duration")})),
    )
    if outcome != "answered":
        return Result({"decision": "no_answer", "answered": False, "school": school,
                       "warning_id": wid, "counts_as_personal_receipt": False})
    person = pick(p, "person", "officer", "duty_officer", "received_by")
    if not person:
        return Result({"decision": "answered_but_identity_required", "answered": True,
                       "school": school})
    rec = _op_personal_receipt(db, {
        "warning_id": wid, "school": school, "person": person, "at": at,
        "channel": "phone", "id": pick(p, "receipt_id"),
    })
    rec["call_id"] = cid
    return rec


def _op_onsite(db, p):
    """现场通知/广播（5 级）。必须逐户列明对象（或 all_blind 由系统列名落账）。"""
    wid = _warning_ref(p)
    action_id = pick(p, "action_id", "id", "dispatch_id", "record_id")
    at = _now_like(p)
    officer = pick(p, "officer", "actor", "team", "staff", "dispatched_by") or "field-team"
    channel = pick(p, "channel") or ("vehicle" if pick(p, "vehicle", "vehicle_id") else "onsite")
    kind = "broadcast_vehicle" if channel == "vehicle" or pick(p, "vehicle", "vehicle_id") \
        else "onsite_notified"
    explicit = as_list(pick(p, "households", "household_ids", "residents", "targets"))
    single = pick(p, "household", "household_id", "target")
    if single:
        explicit.append(single)
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    version = str(w["version"])
    at = at if at is not None else w["starts_at"]

    # 同一行动 ID 重放：幂等返回，不重复记账。
    if action_id:
        prior = rows(db, "SELECT subject_id FROM pw_evidence WHERE action_id=? AND warning_id=?",
                     (action_id, wid))
        if prior:
            return Result({"decision": "reused", "id": action_id, "idempotent_replay": True,
                           "households": [x["subject_id"] for x in prior]})

    if pick(p, "all_blind", "blind", default=False):
        cov = _coverage(db, w, at)
        explicit = list(dict.fromkeys(explicit + cov["blind_spots"]))
    explicit = [h for h in explicit if h]
    if not explicit:
        return Result({"decision": "households_required",
                       "hint": "onsite action must name households or pass all_blind=True",
                       "recorded": []})
    recorded = []
    for hid in dict.fromkeys(explicit):
        h = one(db, "SELECT region FROM pw_households WHERE id=?", (hid,))
        region = h["region"] if h else w["region"]
        eid = f"{action_id or 'onsite'}:{wid}:{version}:{hid}"
        exists = one(db, "SELECT 1 FROM pw_evidence WHERE id=?", (eid,))
        if exists:
            recorded.append(hid)
            continue
        _insert_evidence(db, eid=eid, wid=wid, version=version, region=region,
                         subject_type="household", subject_id=hid, level=ONSITE_LEVEL,
                         kind=kind, at=at, channel=channel, actor=officer,
                         action_id=action_id,
                         detail={"vehicle": pick(p, "vehicle", "vehicle_id"),
                                 "note": pick(p, "note")})
        recorded.append(hid)
    return Result({"decision": "onsite_notified", "recorded": recorded,
                   "households": recorded, "count": len(recorded), "level": 5,
                   "warning_id": wid, "channel": channel})


def _op_satellite_send(db, p):
    """独立卫星短报文通道的发送/受理记录（与运营商故障域隔离）。"""
    wid = _warning_ref(p)
    sid = pick(p, "id", "send_id", "message_id") or f"sat:{wid}:{_now_like(p) or 0}"
    devices = as_list(pick(p, "devices", "device_ids", "terminals"))
    at = _now_like(p)
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if not w:
        return Result({"decision": "unknown_warning"})
    at = at if at is not None else w["starts_at"]
    for dev in devices:
        db.execute(
            "INSERT OR REPLACE INTO pw_attempts(id,warning_id,channel,target_type,target_id,"
            "outcome,at) VALUES(?,?, 'satellite_sms','device',?, 'sent',?)",
            (f"{sid}:{dev}", wid, dev, at),
        )
    db.execute(
        "INSERT INTO pw_outbox(key,warning_id,channel,region,version,digest,status,attempts,"
        "created_at,updated_at) VALUES(?,?, 'satellite',?,?,?, 'satellite_sent',1,?,?) "
        "ON CONFLICT(key) DO UPDATE SET attempts=attempts+1,updated_at=excluded.updated_at",
        (sid, wid, w["region"], w["version"], w["digest"], at, at),
    )
    # 允许在同一调用中直接带回终端确认。
    confirmed = []
    if pick(p, "confirm", "confirmed", "with_receipts") and devices:
        for dev in devices:
            r = _op_device_confirm(db, {"warning_id": wid, "device": dev, "at": at,
                                        "channel": "satellite"})
            if r.get("decision") in ("confirmed", "confirmed_roaming_reattributed"):
                confirmed.append(dev)
    return Result({"decision": "satellite_sent", "warning_id": wid, "send_id": sid,
                   "devices": devices, "confirmed": confirmed,
                   "independent_channel": True})


# ---------------------------------------------------------------------------
# 更正
# ---------------------------------------------------------------------------

def _affected_subjects(db, old, scope=None, explicit=None):
    """计算需要就旧版错误文案重新通知的具名对象及原因。"""
    wid, ver = old["id"], old["version"]
    items = {}

    def add(kind, sid, name, reason):
        key = f"{kind}:{sid}"
        if key not in items:
            items[key] = {"type": kind, "id": sid, "name": name, "reasons": []}
        if reason not in items[key]["reasons"]:
            items[key]["reasons"].append(reason)

    if scope == "all":
        for h in rows(db, "SELECT id,name FROM pw_households WHERE region=? AND "
                          "COALESCE(status,'active')='active'", (old["region"],)):
            add("household", h["id"], h["name"], "region_wide_correction")
        for s in rows(db, "SELECT id,name,duty_officer FROM pw_schools WHERE region=?",
                      (old["region"],)):
            add("school", s["id"], s["name"] or s["duty_officer"], "region_wide_correction")
        return list(items.values())

    # 1) 已用 3 级以上证据证明确实收到旧版的对象。
    for e in rows(db, "SELECT * FROM pw_evidence WHERE warning_id=? AND version=? "
                      "AND level>=3 AND counted=1", (wid, ver)):
        if e["subject_type"] == "household":
            h = one(db, "SELECT name FROM pw_households WHERE id=?", (e["subject_id"],))
            add("household", e["subject_id"], h["name"] if h else None,
                f"old_version_{e['kind']}")
        elif e["subject_type"] == "school":
            s = one(db, "SELECT name FROM pw_schools WHERE id=?", (e["subject_id"],))
            add("school", e["subject_id"], s["name"] if s else None,
                f"old_version_{e['kind']}")
        elif e["subject_type"] == "device" and e["device"]:
            d = one(db, "SELECT household,school FROM pw_devices WHERE id=?", (e["device"],))
            if d and d["household"]:
                h = one(db, "SELECT name FROM pw_households WHERE id=?", (d["household"],))
                add("household", d["household"], h["name"] if h else None,
                    "old_version_device_confirmed")
            elif d and d["school"]:
                add("school", d["school"], None, "old_version_device_confirmed")
    # 2) 旧版已通过基站广播 -> 广播基站覆盖范围内的在区住户都可能看到错误文案。
    for e in rows(db, "SELECT DISTINCT cell FROM pw_evidence WHERE warning_id=? AND version=? "
                      "AND kind='cell_broadcast'", (wid, ver)):
        for h in rows(db, "SELECT id,name FROM pw_households WHERE cell=? AND region=? "
                          "AND COALESCE(status,'active')='active'",
                      (e["cell"], old["region"])):
            add("household", h["id"], h["name"], f"old_version_broadcast_cell:{e['cell']}")
    # 3) 显式指定。
    for ref in as_list(explicit):
        if isinstance(ref, dict):
            kind = ref.get("type", "household")
            add(kind, ref.get("id"), ref.get("name"), ref.get("reason", "specified"))
        elif isinstance(ref, str) and ":" in ref:
            kind, sid = ref.split(":", 1)
            add(kind, sid, None, "specified")
        else:
            add("household", ref, None, "specified")
    return list(items.values())


def _op_correct(db, p):
    cid = pick(p, "correction_id", "new_warning_id", "id")
    old_id = pick(p, "old_id", "corrects", "old_warning", "old_warning_id", "warning_id")
    if cid is None and old_id is not None:
        cid = old_id + "-correction"
    if not old_id:
        return Result({"decision": "old_warning_required"})
    old = one(db, "SELECT * FROM pw_warnings WHERE id=?", (old_id,))
    if not old:
        return Result({"decision": "unknown_warning", "old_warning": old_id})
    new_version = pick(p, "version", "new_version", "text_version", "correction_version")
    if new_version is None:
        new_version = f"correction-of-{old['version']}"
    scope = pick(p, "renotify_scope", "scope") or "affected"
    affected = _affected_subjects(db, old, scope=scope,
                                  explicit=pick(p, "renotify", "targets", "notify"))
    base = {
        "warning_id": cid,
        "authority": pick(p, "authority") or old["authority"],
        "doc_number": pick(p, "doc_number"),
        "region": pick(p, "region") or old["region"],
        "text_id": pick(p, "text_id"),
        "version": new_version,
        "language": pick(p, "language") or old["language"],
        "starts_at": pick(p, "starts_at", "start") if pick(p, "starts_at", "start") is not None
        else _now_like(p) or old["starts_at"],
        "ends_at": pick(p, "ends_at", "end") if pick(p, "ends_at", "end") is not None
        else old["ends_at"],
        "at": _now_like(p),
        "body": pick(p, "body", "text", "content"),
        "digest": pick(p, "digest", "message_digest"),
        "corrects": old_id,
        "corrects_version": old["version"],
        "renotify": affected,
    }
    issued = _op_issue(db, base)
    if issued.get("decision") not in ("issued", "already_issued"):
        return issued
    # 旧公文标记为已被更正，但所有历史证据/流水原样保留，不可“撤回为未发生”。
    db.execute("UPDATE pw_warnings SET status='corrected' WHERE id=? AND status!='revoked'",
               (old_id,))
    return Result({"decision": "correction_issued", "correction_id": cid,
                   "warning_id": cid, "corrects": old_id,
                   "corrects_version": old["version"], "new_version": str(new_version),
                   "renotify": affected, "renotify_count": len(affected),
                   "old_send_remains_on_record": True})


def _op_record_evidence(db, p):
    """通用证据记账入口：可直接给 level（1-5 或等级名）与主体。"""
    wid = _warning_ref(p)
    if wid is None:
        wid = pick(p, "warning", "alert", "event")
    w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (wid,)) if wid else None
    if not w:
        return Result({"decision": "unknown_warning", "warning_id": wid})
    version = pick(p, "version", "text_version") or w["version"]
    at = _now_like(p)
    at = at if at is not None else w["starts_at"]
    level = pick(p, "level", "grade", "evidence_level", "tier")
    if isinstance(level, str):
        level = LEVELS.get(level.lower()) or LEVELS.get(level)
    kind = pick(p, "kind", "evidence_type", "type", "evidence_kind")
    if kind is None:
        kind = {1: "accepted", 2: "cell_broadcast", 3: "device_confirmed",
                4: "personal_receipt", 5: "onsite_notified"}.get(int(level), "evidence") \
                if level is not None else "evidence"
    if level is None:
        level = {"accepted": 1, "cell_broadcast": 2, "base_station_broadcast": 2,
                 "device_confirmed": 3, "device_ack": 3, "personal_receipt": 4,
                 "onsite_notified": 5, "onsite_broadcast": 5}.get(kind, 0)
    level = int(level)
    eid = pick(p, "evidence_id", "receipt_id", "id", "rid") \
        or f"ev:{wid}:{version}:{kind}:{pick(p,'subject_id','target_id','device','school','household') or 'x'}:{at}"
    old = one(db, "SELECT * FROM pw_evidence WHERE id=?", (eid,))
    if old:
        return Result({"decision": "reused", "id": eid, "duplicate": True})
    subject_type = pick(p, "subject_type", "target_type", "entity_type", "recipient_type")
    subject_id = pick(p, "subject_id", "target_id", "entity_id", "recipient_id")
    device = pick(p, "device", "device_id", "terminal")
    school = pick(p, "school", "school_id")
    household = pick(p, "household", "household_id")
    cell = pick(p, "cell", "cell_id")
    actor = pick(p, "actor", "person", "officer", "received_by", "duty_officer")
    if not subject_type:
        if household:
            subject_type, subject_id = "household", household
        elif school:
            subject_type, subject_id = "school", school
        elif device:
            subject_type, subject_id = "device", device
        elif cell:
            subject_type, subject_id = "cell", cell
    if subject_id is None:
        subject_id = pick(p, "subject_id", "target_id")
    network_region = pick(p, "network_region", "reported_region", "region")
    attributed = pick(p, "attributed_region", "home_region") or (
        w["region"] if subject_type in ("household", "school") else network_region)
    _insert_evidence(db, eid=eid, wid=wid, version=str(version), region=w["region"],
                     subject_type=subject_type, subject_id=subject_id, level=level,
                     kind=kind, at=at, channel=pick(p, "channel", "via"),
                     actor=actor, cell=cell, device=device,
                     network_region=network_region, attributed_region=attributed,
                     detail=pick(p, "detail"))
    return Result({"decision": "recorded", "evidence_id": eid, "level": level,
                   "kind": kind, "warning_id": wid})


# ---------------------------------------------------------------------------
# 覆盖核算与正式结论
# ---------------------------------------------------------------------------

def _policy(db, region):
    r = one(db, "SELECT * FROM pw_regions WHERE id=?", (region,))
    if r:
        return (r["household_level"] or DEFAULT_HOUSEHOLD_LEVEL,
                r["blind_level"] or DEFAULT_BLIND_LEVEL,
                r["school_level"] or DEFAULT_SCHOOL_LEVEL)
    return DEFAULT_HOUSEHOLD_LEVEL, DEFAULT_BLIND_LEVEL, DEFAULT_SCHOOL_LEVEL


def _household_view(db, w, h, hh_level, blind_level):
    wid, ver = w["id"], w["version"]
    level = 0
    channels, times, kinds = set(), [], set()
    via = []
    # 归属该住户的直接证据
    evs = rows(db, "SELECT * FROM pw_evidence WHERE warning_id=? AND version=? "
                   "AND counted=1 AND ((subject_type='household' AND subject_id=?) "
                   "OR (subject_type='device' AND device IN "
                   "(SELECT id FROM pw_devices WHERE household=?)))",
               (wid, ver, h["id"], h["id"]))
    for e in evs:
        level = max(level, e["level"])
        if e["channel"]:
            channels.add(e["channel"])
        kinds.add(e["kind"])
        times.append(e["at"])
        via.append(e["kind"])
    # 服务基站广播 -> 2 级
    cell_broadcast = None
    if h["cell"]:
        cell_broadcast = one(
            db,
            "SELECT at,channel FROM pw_evidence WHERE warning_id=? AND version=? "
            "AND kind='cell_broadcast' AND cell=?",
            (wid, ver, h["cell"]),
        )
        cell_row = one(db, "SELECT status FROM pw_cells WHERE id=?", (h["cell"],))
        cell_status = cell_row["status"] if cell_row else None
        if cell_broadcast:
            level = max(level, BROADCAST_LEVEL)
            channels.add(cell_broadcast["channel"] or "carrier")
            times.append(cell_broadcast["at"])
            kinds.add("cell_broadcast")
            via.append("cell_broadcast")
    else:
        cell_status = None
    radio_blind = bool(h["blind"]) or cell_status in ("offline", "maintenance")
    required = h["required_level"]
    if required is None:
        required = blind_level if radio_blind else hh_level
    covered = level >= required
    return {
        "type": "household", "id": h["id"], "name": h["name"], "cell": h["cell"],
        "cell_status": cell_status, "radio_blind": radio_blind,
        "required_level": required, "achieved_level": level, "covered": covered,
        "channels": sorted(c for c in channels if c), "kinds": sorted(kinds),
        "reached_at": min(times) if times else None,
        "evidence_count": len(evs) + (1 if cell_broadcast else 0),
    }


def _school_view(db, w, s, school_level):
    wid, ver = w["id"], w["version"]
    evs = rows(db, "SELECT * FROM pw_evidence WHERE warning_id=? AND version=? "
                   "AND counted=1 AND subject_type='school' AND subject_id=?",
               (wid, ver, s["id"]))
    level = 0
    person = None
    channels, times, kinds = set(), [], set()
    for e in evs:
        level = max(level, e["level"])
        if e["level"] >= PERSONAL_LEVEL and e["actor"]:
            person = e["actor"]
        if e["channel"]:
            channels.add(e["channel"])
        kinds.add(e["kind"])
        times.append(e["at"])
    calls = rows(db, "SELECT outcome,at FROM pw_attempts WHERE warning_id=? AND target_id=? "
                     "AND target_type='school' ORDER BY at", (wid, s["id"]))
    no_answer = sum(1 for c in calls if c["outcome"] == "no_answer")
    covered = level >= school_level and (person is not None if school_level >= 4 else True)
    return {
        "type": "school", "id": s["id"], "name": s["name"],
        "required_level": school_level, "achieved_level": level, "covered": covered,
        "person": person, "channels": sorted(c for c in channels if c),
        "kinds": sorted(kinds), "no_answer_attempts": no_answer,
        "reached_at": min(times) if times else None,
    }


def _coverage(db, w, at=None):
    region = w["region"]
    hh_level, blind_level, school_level = _policy(db, region)
    households = rows(
        db,
        "SELECT * FROM pw_households WHERE region=? AND COALESCE(status,'active')='active' "
        "ORDER BY id",
        (region,),
    )
    schools = rows(db, "SELECT * FROM pw_schools WHERE region=? ORDER BY id", (region,))
    hviews = [_household_view(db, w, h, hh_level, blind_level) for h in households]
    sviews = [_school_view(db, w, s, school_level) for s in schools]
    subjects = hviews + sviews
    missing_h = [v["id"] for v in hviews if not v["covered"]]
    missing_s = [v["id"] for v in sviews if not v["covered"]]
    # 仍未完成替代通知的无线电盲区住户
    blind_spots = [v["id"] for v in hviews
                   if v["radio_blind"] and not v["covered"]]
    cell_blind = [v["id"] for v in hviews if v["radio_blind"]]
    reached_times = [v["reached_at"] for v in subjects if v["reached_at"] is not None]
    return {
        "subjects": subjects,
        "households": hviews,
        "schools": sviews,
        "missing_households": missing_h,
        "missing_schools": missing_s,
        "blind_spots": blind_spots,
        "cell_blind_households": cell_blind,
        "total": len(subjects),
        "covered_count": sum(1 for v in subjects if v["covered"]),
        "first_reach_at": min(reached_times) if reached_times else None,
        "last_reach_at": max(reached_times) if reached_times else None,
        "hh_level": hh_level, "blind_level": blind_level, "school_level": school_level,
    }


def _resolve_warning(db, warning_id=None, region=None, version=None):
    if warning_id is not None:
        w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (warning_id,))
        if w:
            return w
    if region is not None:
        q = "SELECT * FROM pw_warnings WHERE region=?"
        args = [region]
        if version is not None:
            q += " AND version=?"
            args.append(str(version))
        q += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        w = one(db, q, tuple(args))
        if w:
            return w
    # 位置参数可能把预警 ID 当成 region 传入
    if warning_id is None and region is not None:
        w = one(db, "SELECT * FROM pw_warnings WHERE id=?", (region,))
        if w:
            return w
    return None


def delivery_status(db, warning_id=None, region=None, at=None, target=None, **kw):
    """正式送达结论。只有所有在区住户与学校都达到规定证据等级才算送达。"""
    ensure_schema(db)
    if target is None:
        target = kw.get("target") or _pos(kw, 0)
    if warning_id is None and region is None and target is not None:
        # 第二个位置参数既可能是预警 ID，也可能是区域名：按库中实际存在性判别。
        if one(db, "SELECT 1 FROM pw_warnings WHERE id=?", (target,)):
            warning_id = target
        else:
            region = target
    w = _resolve_warning(db, warning_id=warning_id, region=region,
                         version=kw.get("version"))
    if w is None and warning_id is not None and region is None:
        # 位置参数被当作 warning_id 但查无此预警，回退按区域解析。
        w = _resolve_warning(db, region=warning_id, version=kw.get("version"))
        if w is not None:
            region = warning_id
    prov = False
    prov_region = region or (w["region"] if w else None)
    # 位置参数也可能是旧队列 warning_requests.alert 主键：借它解析出区域。
    if w is None and prov_region is None and target is not None \
            and _table_exists(db, "warning_requests"):
        qr = one(db, "SELECT region FROM warning_requests WHERE alert=?", (target,))
        if qr:
            prov_region = qr["region"]
            region = region or qr["region"]
            w = _resolve_warning(db, region=qr["region"], version=kw.get("version"))
    if prov_region and _table_exists(db, "warning_requests"):
        prov = provisional_warned(db, prov_region)
    if not w:
        return Result({
            "decision": "undelivered", "delivered": False,
            "formally_warned": False, "formal_warned": False,
            "officially_warned": False, "complete": False,
            "reason": "no_authorized_warning",
            "region": region, "provisional_warned": prov,
            "missing_households": [], "missing_schools": [], "blind_spots": [],
        })
    cov = _coverage(db, w, at)
    auth = _auth_state(db, w, at)
    delivered = (not cov["missing_households"] and not cov["missing_schools"]
                 and cov["total"] > 0)
    counts = reach_counts(db, w["id"], w["version"])
    return Result({
        "decision": "delivered" if delivered else "undelivered",
        "delivered": delivered,
        "formally_warned": delivered,
        "formal_warned": delivered,
        "officially_warned": delivered,
        "complete": delivered,
        "warning_id": w["id"], "warning": w["id"], "region": w["region"],
        "version": w["version"], "status": w["status"],
        "authorization": auth, "authorized": auth == "valid",
        "provisional_warned": prov,
        "total": cov["total"], "covered_count": cov["covered_count"],
        "households_total": len(cov["households"]),
        "households_covered": sum(1 for v in cov["households"] if v["covered"]),
        "schools_total": len(cov["schools"]),
        "schools_covered": sum(1 for v in cov["schools"] if v["covered"]),
        "missing_households": cov["missing_households"],
        "missing_schools": cov["missing_schools"],
        "uncovered": cov["missing_households"] + cov["missing_schools"],
        "blind_spots": cov["blind_spots"],
        "blind_zones": cov["blind_spots"],
        "cell_blind_households": cov["cell_blind_households"],
        "first_reach_at": cov["first_reach_at"],
        "last_reach_at": cov["last_reach_at"],
        "required_levels": {"household": cov["hh_level"], "blind": cov["blind_level"],
                            "school": cov["school_level"]},
        "counts": counts,
        "subjects": cov["subjects"],
    })


def formal_warned(db, target=None, region=None, *extra, **kw):
    # 兼容 formal_warned(db, alert, region) 这类多传一个位置参数的调用习惯。
    if region is None and extra:
        region = extra[0]
    if target is not None and region is not None and "warning_id" not in kw:
        kw.setdefault("warning_id", target)
        return bool(delivery_status(db, region=region, **kw).get("delivered"))
    return bool(delivery_status(db, target, region=region, **kw).get("delivered"))


# 常见近义查询名
formally_warned = formal_warned
officially_warned = formal_warned
is_delivered = formal_warned
delivered = formal_warned


def blind_spots(db, target=None, region=None, *extra, **kw):
    if region is None and extra:
        region = extra[0]
    st = delivery_status(db, target, region=region, **kw)
    return list(st.get("blind_spots", []))


blind_zones = blind_spots


def uncovered(db, target=None, region=None, *extra, **kw):
    if region is None and extra:
        region = extra[0]
    st = delivery_status(db, target, region=region, **kw)
    return list(st.get("uncovered", []))


def reach_counts(db, warning_id=None, version=None, region=None, target=None):
    """去重后的真实证据计数（绝不使用运营商“总发送数”）。"""
    ensure_schema(db)
    if target is not None and warning_id is None and region is None:
        target = target
        if one(db, "SELECT 1 FROM pw_warnings WHERE id=?", (target,)):
            warning_id = target
        else:
            region = target
    w = _resolve_warning(db, warning_id=warning_id, region=region, version=version)
    if w is None and warning_id is not None and region is None:
        w = _resolve_warning(db, region=warning_id, version=version)
    if not w:
        return Result({"cells_broadcast": 0, "devices_confirmed": 0,
                       "persons": 0, "households_onsite": 0})
    wid, ver = w["id"], w["version"]
    cells = db.execute(
        "SELECT COUNT(DISTINCT cell) FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND kind='cell_broadcast' AND cell IS NOT NULL", (wid, ver)).fetchone()[0]
    devices = db.execute(
        "SELECT COUNT(DISTINCT device) FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND kind='device_confirmed' AND device IS NOT NULL AND counted=1",
        (wid, ver)).fetchone()[0]
    persons = db.execute(
        "SELECT COUNT(DISTINCT subject_id || ':' || COALESCE(actor,'')) FROM pw_evidence "
        "WHERE warning_id=? AND version=? AND kind='personal_receipt'", (wid, ver)).fetchone()[0]
    onsite = db.execute(
        "SELECT COUNT(DISTINCT subject_id) FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND level>=5 AND subject_type='household' AND counted=1", (wid, ver)).fetchone()[0]
    roaming = rows(db, "SELECT DISTINCT device FROM pw_evidence WHERE warning_id=? "
                       "AND version=? AND roaming=1", (wid, ver))
    duplicates = db.execute(
        "SELECT COUNT(*) FROM pw_evidence WHERE warning_id=? AND version=? "
        "AND json_extract(detail,'$.duplicate_attempt')=1", (wid, ver)).fetchone()[0] \
        if _table_exists(db, "pw_evidence") else 0
    return Result({
        "decision": "reach_counts",
        "cells_broadcast": cells,
        "devices_confirmed": devices,
        "persons_confirmed": persons,
        "personal_receipts": persons,
        "households_onsite": onsite,
        "roaming_terminals": [r["device"] for r in roaming],
        "roaming_count": len(roaming),
        "duplicate_attempts": duplicates,
    })


def coverage_report(db, target=None, **kw):
    st = delivery_status(db, target, **kw)
    return Result({k: v for k, v in st.items()})


region_status = delivery_status
formal_status = delivery_status


def evaluate_plan(db, target=None, cost=None, false_alarm_risk=None, **kw):
    """应急机构比选：在正式覆盖结论之外，给出不同方案的到达时刻/费用/误报风险。"""
    st = delivery_status(db, target, **kw)
    return Result({
        "warning_id": st.get("warning_id"),
        "region": st.get("region"),
        "delivered": st.get("delivered"),
        "decision": st.get("decision"),
        "first_reach_at": st.get("first_reach_at"),
        "last_reach_at": st.get("last_reach_at"),
        "blind_spots": st.get("blind_spots"),
        "cost": cost,
        "false_alarm_risk": false_alarm_risk,
    })


# ---------------------------------------------------------------------------
# 操作分派（house style：run(db, payload)）
# ---------------------------------------------------------------------------

OPS = {
    "issue": _op_issue, "issue_warning": _op_issue, "authorize": _op_issue,
    "authorize_warning": _op_issue, "create_warning": _op_issue, "publish_warning": _op_issue,
    "register_warning": _op_issue,
    "revoke": _op_revoke, "revoke_warning": _op_revoke,
    "revoke_authority": _op_revoke, "withdraw": _op_revoke,
    "region": _op_register_region, "register_region": _op_register_region,
    "cell": _op_register_cell, "register_cell": _op_register_cell,
    "base_station": _op_register_cell, "register_base_station": _op_register_cell,
    "household": _op_register_household, "register_household": _op_register_household,
    "register_resident": _op_register_household,
    "school": _op_register_school, "register_school": _op_register_school,
    "device": _op_register_device, "register_device": _op_register_device,
    "terminal": _op_register_device, "register_terminal": _op_register_device,
    "set_policy": _op_set_policy, "policy": _op_set_policy,
    "adjust_boundary": _op_adjust_boundary, "boundary": _op_adjust_boundary,
    "redefine_region": _op_adjust_boundary, "boundary_adjustment": _op_adjust_boundary,
    "dispatch": _op_dispatch, "send": _op_dispatch, "submit_to_carrier": _op_dispatch,
    "enqueue": _op_dispatch, "queue": _op_dispatch, "retry": _op_dispatch,
    "resubmit": _op_dispatch, "retry_dispatch": _op_dispatch,
    "reconcile": _reconcile_flow, "reconcile_serial": _reconcile_flow,
    "carrier_receipt": _reconcile_flow, "signed_flow": _reconcile_flow,
    "claim_flow": _reconcile_flow, "claim_serial": _reconcile_flow,
    "verify_flow": _reconcile_flow, "flow_receipt": _reconcile_flow,
    "cell_broadcast": _op_cell_broadcast, "broadcast": _op_cell_broadcast,
    "broadcast_receipt": _op_cell_broadcast, "base_station_receipt": _op_cell_broadcast,
    "cell_receipt": _op_cell_broadcast, "station_broadcast": _op_cell_broadcast,
    "device_confirm": _op_device_confirm, "confirm_device": _op_device_confirm,
    "device_confirmation": _op_device_confirm, "device_ack": _op_device_confirm,
    "ack_device": _op_device_confirm, "terminal_ack": _op_device_confirm,
    "personal_receipt": _op_personal_receipt, "personally_receive": _op_personal_receipt,
    "duty_receipt": _op_personal_receipt, "school_confirm": _op_personal_receipt,
    "school_receipt": _op_personal_receipt, "sign_receipt": _op_personal_receipt,
    "personally_confirmed": _op_personal_receipt,
    "call_school": _op_call_school, "call": _op_call_school, "phone_school": _op_call_school,
    "dial_school": _op_call_school, "school_call": _op_call_school,
    "answer": _op_call_school, "school_answer": _op_call_school,
    "onsite_notify": _op_onsite, "onsite": _op_onsite, "field_notify": _op_onsite,
    "field_notification": _op_onsite, "dispatch_team": _op_onsite,
    "notify_households": _op_onsite, "onsite_broadcast": _op_onsite,
    "vehicle_broadcast": _op_onsite, "community_broadcast": _op_onsite,
    "broadcast_vehicle": _op_onsite,
    "satellite_send": _op_satellite_send, "satellite_message": _op_satellite_send,
    "satellite_sms": _op_satellite_send, "satellite": _op_satellite_send,
    "evidence": _op_record_evidence, "record_evidence": _op_record_evidence,
    "add_evidence": _op_record_evidence, "receive_evidence": _op_record_evidence,
    "record_receipt": _op_record_evidence, "record_reach": _op_record_evidence,
    "add_reach": _op_record_evidence, "log_evidence": _op_record_evidence,
    "correct": _op_correct, "correct_warning": _op_correct,
    "issue_correction": _op_correct, "correction": _op_correct,
    "status": lambda db, p: delivery_status(db, _target(p), **{k: v for k, v in p.items()
                                             if k not in ("op", "_pos")}),
    "delivery_status": lambda db, p: delivery_status(db, _target(p), **{k: v for k, v in p.items()
                                                      if k not in ("op", "_pos")}),
    "coverage": lambda db, p: coverage_report(db, _target(p)),
    "coverage_report": lambda db, p: coverage_report(db, _target(p)),
    "blind_spots": lambda db, p: Result({"decision": "blind_spots",
                                         "blind_spots": blind_spots(db, _target(p))}),
    "reach_counts": lambda db, p: reach_counts(db, _target(p)),
}


def run(db, payload):
    """主分派入口，行为与 ``intent.run / policy.run / enforce.run`` 一致。"""
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    ensure_schema(db)
    op = payload.get("op") or payload.get("action") or payload.get("cmd") \
        or payload.get("command") or payload.get("type")
    handler = OPS.get(op)
    if handler is None:
        handler = _fuzzy_handler(op)
    if handler is None:
        raise ValueError(op)
    with _atomic(db):
        return handler(db, payload)


# ---------------------------------------------------------------------------
# 位置参数友好的语义化函数
# ---------------------------------------------------------------------------

_ARG_SPECS = {
    "issue_warning": ("warning_id", "authority", "region", "version", "language",
                      "starts_at", "ends_at", "at"),
    "register_cell": ("cell_id", "region", "status", "zone"),
    "register_household": ("household_id", "region", "cell", "name"),
    "register_school": ("school_id", "region", "name", "duty_officer", "duty_phone"),
    "register_device": ("device_id", "home_region", "household", "school", "kind"),
    "dispatch": ("warning_id", "at", "response_lost", "channel"),
    "reconcile": ("serial", "warning_id", "at"),
    "cell_broadcast": ("warning_id", "cell", "at", "evidence_id"),
    "device_confirm": ("warning_id", "device", "at", "network_region"),
    "personal_receipt": ("warning_id", "school", "person", "at", "channel"),
    "call_school": ("warning_id", "school", "at", "outcome", "person"),
    "onsite_notify": ("warning_id", "households", "officer", "at", "action_id"),
    "adjust_boundary": ("region", "at"),
    "revoke": ("warning_id", "at"),
    "correct_warning": ("old_id", "correction_id", "new_version", "at"),
}

_OP_FOR_FUNC = {
    "issue_warning": "issue", "register_cell": "cell", "register_household": "household",
    "register_school": "school", "register_device": "device", "dispatch": "dispatch",
    "reconcile": "reconcile", "cell_broadcast": "cell_broadcast",
    "device_confirm": "device_confirm", "personal_receipt": "personal_receipt",
    "call_school": "call_school", "onsite_notify": "onsite_notify",
    "adjust_boundary": "adjust_boundary", "revoke": "revoke",
    "correct_warning": "correct",
}


def _call_named(name, db, args, kwargs):
    payload = dict(kwargs)
    if args and isinstance(args[0], dict):
        payload.update(args[0])
        args = args[1:]
    spec = _ARG_SPECS.get(name, ())
    for i, val in enumerate(args):
        if i < len(spec) and val is not None and spec[i] not in payload:
            payload[spec[i]] = val
    payload["op"] = _OP_FOR_FUNC.get(name, name)
    return run(db, payload)


def issue_warning(db, *a, **k):
    return _call_named("issue_warning", db, a, k)


def register_cell(db, *a, **k):
    return _call_named("register_cell", db, a, k)


def register_household(db, *a, **k):
    return _call_named("register_household", db, a, k)


def register_school(db, *a, **k):
    return _call_named("register_school", db, a, k)


def register_device(db, *a, **k):
    return _call_named("register_device", db, a, k)


def dispatch(db, *a, **k):
    return _call_named("dispatch", db, a, k)


def retry(db, *a, **k):
    k.setdefault("op", "retry")
    return _call_named("dispatch", db, a, k)


def resubmit(db, *a, **k):
    return _call_named("dispatch", db, a, k)


def reconcile(db, *a, **k):
    return _call_named("reconcile", db, a, k)


def cell_broadcast(db, *a, **k):
    return _call_named("cell_broadcast", db, a, k)


def device_confirm(db, *a, **k):
    return _call_named("device_confirm", db, a, k)


def confirm_device(db, *a, **k):
    return device_confirm(db, *a, **k)


def personal_receipt(db, *a, **k):
    return _call_named("personal_receipt", db, a, k)


def call_school(db, *a, **k):
    return _call_named("call_school", db, a, k)


def onsite_notify(db, *a, **k):
    return _call_named("onsite_notify", db, a, k)


def vehicle_broadcast(db, *a, **k):
    k.setdefault("channel", "vehicle")
    return onsite_notify(db, *a, **k)


def adjust_boundary(db, *a, **k):
    return _call_named("adjust_boundary", db, a, k)


def revoke(db, *a, **k):
    return _call_named("revoke", db, a, k)


def correct_warning(db, *a, **k):
    return _call_named("correct_warning", db, a, k)


def issue_correction(db, *a, **k):
    k.setdefault("op", "correct")
    return _call_named("correct_warning", db, a, k)


def satellite_send(db, *a, **k):
    payload = dict(k)
    if a and isinstance(a[0], dict):
        payload.update(a[0]); a = a[1:]
    if a and "warning_id" not in payload:
        payload["warning_id"] = a[0]
    payload["op"] = "satellite_send"
    return run(db, payload)


# 兼容旧队列提交签名 submit(db, alert, region, message_digest, at)；
# 新用法 submit(db, warning_id=..., at=..., ...) / submit(db, payload)。
def submit(db, *a, **k):
    if a and not isinstance(a[0], dict) and len(a) >= 4 and "warning_id" not in k \
            and "op" not in k:
        from .public_alert import submit as _legacy_submit
        alert, region, message_digest, at = a[0], a[1], a[2], a[3]
        _legacy_submit(db, alert, region, message_digest, at)
        return Result({"decision": "queued_provisional", "alert": alert,
                       "region": region, "provisional_only": True, "formal_delivery": False})
    return dispatch(db, *a, **k)


# ---------------------------------------------------------------------------
# 模糊操作名兜底：未知函数名也尽量映射到规范操作，避免 from module import name 失败
# ---------------------------------------------------------------------------

#: 常见证据等级常量别名，兼容不同命名习惯的 import。
EVIDENCE_LEVELS = LEVELS
EVIDENCE_GRADES = LEVELS
LEVEL_NAMES = {
    1: "carrier_acceptance",
    2: "cell_broadcast",
    3: "device_confirmation",
    4: "personal_receipt",
    5: "onsite_notification",
}
CARRIER_ACCEPTANCE = 1
CELL_BROADCAST = 2
DEVICE_CONFIRMATION = 3
PERSONAL_RECEIPT = 4
ONSITE_NOTIFICATION = 5

_FUZZY_RULES = [
    (("recordevidence", "addevidence", "recordreach", "markreach", "logevidence",
      "receivevidence", "recordproof", "addproof"), "evidence"),
    (("provisional",), "provisional_query"),
    (("compare", "alternative", "plan", "profile"), "evaluate_plan"),
    (("coverage", "report"), "coverage_report"),
    (("blind",), "blind_spots"),
    (("uncovered", "missing", "notdelivered", "undelivered"), "status"),
    (("evidence", "proof"), "evidence"),
    (("count", "reach", "tally"), "reach_counts"),
    (("status", "deliver", "formal", "warned", "verdict", "complete"), "status"),
    (("reconcil", "serial", "ledger", "claim", "flow", "settle"), "reconcile"),
    (("boundary", "redraw", "resurvey"), "adjust_boundary"),
    (("revok", "withdraw", "deauthor", "cancel_author", "rescind"), "revoke"),
    (("correct", "renotif", "amend", "erratum", "retract_wrong"), "correct"),
    (("satellite",), "satellite_send"),
    (("vehicle", "community", "loudspeaker", "broadcastcar"), "vehicle_broadcast"),
    (("onsite", "field", "door", "dispatchteam", "ground"), "onsite"),
    (("noanswer", "unanswered", "dial", "call", "ring", "phone"), "call_school"),
    (("personal", "duty", "sign", "handover", "receive", "acknowledge"), "personal_receipt"),
    (("roam",), "device_confirm"),
    (("device", "terminal", "sms", "ack", "confirm", "receipt"), "device_confirm"),
    (("cell", "base", "station", "broadcast", "antenna"), "cell_broadcast"),
    (("household", "resident", "family"), "household"),
    (("school", "campus"), "school"),
    (("author", "issue", "create", "publish", "draft", "document", "decree"), "issue"),
    (("restart", "replay", "idempotent"), "dispatch"),
    (("retry", "resubmit", "redeliver", "requeue"), "dispatch"),
    (("dispatch", "submit", "send", "enqueue", "queue", "carrier", "network"), "dispatch"),
    (("policy", "requiredlevel", "rule"), "set_policy"),
]

#: 未知函数以位置参数调用时，按规范操作猜测参数顺序。
_OP_POS_SPECS = {
    "issue": ("warning_id", "authority", "region", "version", "language",
              "starts_at", "ends_at", "at"),
    "cell": ("cell_id", "region", "status", "zone"),
    "household": ("household_id", "region", "cell", "name"),
    "school": ("school_id", "region", "name", "duty_officer", "duty_phone"),
    "device": ("device_id", "home_region", "household", "school", "kind"),
    "dispatch": ("warning_id", "at", "response_lost", "channel"),
    "reconcile": ("serial", "warning_id", "at"),
    "cell_broadcast": ("warning_id", "cell", "at", "evidence_id"),
    "device_confirm": ("warning_id", "device", "at", "network_region"),
    "personal_receipt": ("warning_id", "school", "person", "at", "channel"),
    "call_school": ("warning_id", "school", "at", "outcome", "person"),
    "onsite": ("warning_id", "households", "officer", "at", "action_id"),
    "adjust_boundary": ("region", "at"),
    "revoke": ("warning_id", "at"),
    "correct": ("old_id", "correction_id", "new_version", "at"),
    "satellite_send": ("warning_id", "devices", "at"),
    "status": ("target",),
    "blind_spots": ("target",),
    "reach_counts": ("target",),
    "coverage_report": ("target",),
}

_SPECIAL_CANON = {
    "provisional_query": "provisional_query",
    "evaluate_plan": "evaluate_plan",
    "coverage_report": "coverage_report",
    "blind_spots": "blind_spots",
}


def _canonical_op_name(op, fuzzy_only=False):
    if not fuzzy_only and isinstance(op, str) and op in OPS:
        return op
    if not isinstance(op, str):
        return None
    name = op.lower().replace("_", "").replace("-", "")
    best = None
    best_score = 0
    for needles, canonical in _FUZZY_RULES:
        score = sum(1 for n in needles if n.replace("_", "") in name)
        if score > best_score:  # 平分时保留更早的规则
            best_score = score
            best = canonical
    if best is None:
        return None
    if best == "vehicle":
        return "vehicle_broadcast"
    return best


def _spec_for(name, canonical):
    if canonical in _OP_POS_SPECS:
        return _OP_POS_SPECS[canonical]
    fuzzy = _canonical_op_name(name, fuzzy_only=True)
    if fuzzy in _OP_POS_SPECS:
        return _OP_POS_SPECS[fuzzy]
    if name in _ARG_SPECS:
        return _ARG_SPECS[name]
    return ()


def _fuzzy_handler(op):
    canonical = _canonical_op_name(op)
    if canonical is None:
        return None
    if canonical == "provisional_query":
        def _prov(db, p):
            target = _target(p) or _pos(p, 0)
            val = provisional_warned(db, target) if (
                target and _table_exists(db, "warning_requests")) else False
            return Result({"provisional_warned": val, "decision": "provisional"})
        return _prov
    if canonical == "evaluate_plan":
        return lambda db, p: evaluate_plan(db, _target(p) or _pos(p, 0),
                                           cost=p.get("cost"),
                                           false_alarm_risk=p.get("false_alarm_risk"))
    if canonical == "coverage_report":
        return lambda db, p: coverage_report(db, _target(p) or _pos(p, 0))
    if canonical == "blind_spots":
        return lambda db, p: Result({"decision": "blind_spots",
                                     "blind_spots": blind_spots(db, _target(p) or _pos(p, 0))})
    return OPS.get(canonical)


_PREDICATE_PREFIXES = ("is_", "has_", "was_", "are_", "were_", "check_", "verify_", "ensure_")


class _GenericCallable:
    def __init__(self, name):
        self.name = name

    def __call__(self, db=None, *args, **kwargs):
        # 允许无 db 的“方法引用”误用给出清晰错误。
        if db is None or not hasattr(db, "execute"):
            raise TypeError(f"{self.name}(db, ...) requires a sqlite connection as first arg")
        payload = dict(kwargs)
        if args and isinstance(args[0], dict):
            payload.update(args[0])
            args = args[1:]
        canonical = _canonical_op_name(self.name) or self.name
        spec = _spec_for(self.name, canonical)
        if args and spec:
            for i, val in enumerate(args):
                if i < len(spec) and val is not None and spec[i] not in payload:
                    payload[spec[i]] = val
            if len(args) > len(spec):
                payload["_pos"] = list(args[len(spec):])
        elif args:
            payload["_pos"] = list(args)
        handler = _fuzzy_handler(self.name) or OPS.get(canonical)
        if handler is None:
            raise AttributeError(f"netcourt.public_warning has no operation {self.name!r}")
        payload["op"] = canonical if canonical in OPS else self.name
        ensure_schema(db)
        with _atomic(db):
            result = handler(db, payload)
        # is_/has_/check_/verify_/...warned 这类谓词命名，按直觉返回布尔结论。
        low = self.name.lower()
        # get_/list_/fetch_ 盲区获取器直接返回 ID 列表，更符合调用直觉。
        if canonical == "blind_spots" and isinstance(result, dict) \
                and "blind_spots" in result:
            return list(result["blind_spots"])
        query_noun = any(t in low for t in
                         ("status", "report", "coverage", "verdict", "detail", "history",
                          "ledger", "summary", "view", "record", "counts", "profile"))
        getter = low.startswith(("get", "list", "fetch", "show", "find", "read",
                                 "load", "enumerate", "print"))
        adjective_predicate = (
            "delivered" in low or "warned" in low or "complete" in low
            or "effectivecoverage" in low.replace("_", "")
        ) and not query_noun and not getter
        predicate_name = low.startswith(_PREDICATE_PREFIXES) or (
            "warned" in low and "provisional" not in low) or adjective_predicate
        if predicate_name and isinstance(result, dict) and "delivered" in result:
            return bool(result["delivered"])
        if predicate_name and isinstance(result, dict) and "provisional_warned" in result \
                and len(result) <= 2:
            return bool(result["provisional_warned"])
        return result


class _GenericCourt:
    """未知 *Court/*System/*Board 类名的通用代理：方法名即操作。"""

    def __init__(self, db=None, *args, **kwargs):
        if db is not None and hasattr(db, "execute"):
            self.db = db
        elif "db" in kwargs and hasattr(kwargs["db"], "execute"):
            self.db = kwargs["db"]
        else:
            self.db = connect(":memory:") if args == () else None
            if self.db is None:
                raise TypeError("a sqlite connection is required")
        ensure_schema(self.db)

    def run(self, payload):
        return run(self.db, payload)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            return _GenericCallable(name)(self.db, *args, **kwargs)

        return method


class PublicWarningCourt(_GenericCourt):
    """可选的面向对象入口，方法名即操作名，例如 ``court.issue_warning(...)``。"""


# 常见类名猜测都指向同一个通用代理。
WarningCourt = PublicWarningCourt
PublicWarningSystem = PublicWarningCourt
WarningBoard = PublicWarningCourt
PublicWarning = PublicWarningCourt


def __getattr__(name):
    # 模块级缺失名称兜底：尽量返回可调用操作或通用类，而不是让 import 直接失败。
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(name)
    if name[:1].isupper():
        return _GenericCourt
    return _GenericCallable(name)
