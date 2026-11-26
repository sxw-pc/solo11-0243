"""Dual-carrier transit settlement, fault recovery and customer credits.

This module connects four kinds of evidence that the provisional meter/bill
pair deliberately ignores:

* raw cumulative counter samples bound to a **physical port**, counter
  **generation**, sample **time**, **direction** and collection **link**;
* vantage-point identity (carrier handoff, switch mirror span, customer edge
  and independent probe) so the same bytes are never sold three times;
* failover / incident records that say which carrier was the fault party;
* immutable commercial documents (carrier bills/payments/refunds, customer
  credits, fault-party recovery) kept as distinct debts.

Analytics entry points are pure functions over plain dictionaries
(:func:`counter_edges`, :func:`window_bytes`, :func:`mirror_attribution`,
:func:`reconcile_windows`, :func:`monthly_high`, :func:`charge_envelope`).

The stateful entry point is :func:`run` (same ``op`` dispatch style as the
``intent``/``policy``/``enforce`` modules); :class:`TransitSettlement` is a
small facade around it.  Every later piece of evidence appends an adjustment
row naming the changed time window and the affected debt; issued notes are
never overwritten.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import json
from fractions import Fraction

WINDOW_SECONDS = 300
MEGABYTE_UNIT_BYTES = 37_500_000  # bytes in one 5-minute 1 Mbps sample

POINT_KINDS = ("handoff", "mirror", "edge", "probe")
DIRECTIONS = ("out", "in")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ts_ports(
  id TEXT PRIMARY KEY,
  carrier TEXT,
  kind TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'out',
  mirrors TEXT,
  clock_offset INTEGER NOT NULL DEFAULT 0,
  clock_tolerance INTEGER NOT NULL DEFAULT 0,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS ts_readings(
  id TEXT PRIMARY KEY,
  port TEXT NOT NULL,
  generation INTEGER NOT NULL,
  at INTEGER NOT NULL,
  direction TEXT NOT NULL,
  link TEXT NOT NULL,
  bytes_total INTEGER NOT NULL,
  UNIQUE(port, generation, at, direction, link)
);
CREATE TABLE IF NOT EXISTS ts_contracts(
  carrier TEXT NOT NULL,
  period TEXT NOT NULL,
  unit_bytes INTEGER NOT NULL,
  price_cents INTEGER NOT NULL,
  committed_units INTEGER NOT NULL DEFAULT 0,
  high_rule TEXT NOT NULL DEFAULT 'max',
  PRIMARY KEY(carrier, period)
);
CREATE TABLE IF NOT EXISTS ts_sla(
  customer TEXT PRIMARY KEY,
  mrr_cents INTEGER NOT NULL,
  period_seconds INTEGER NOT NULL,
  tiers TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ts_claims(
  id TEXT PRIMARY KEY,
  carrier TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  at INTEGER,
  link TEXT
);
CREATE TABLE IF NOT EXISTS ts_bounds(
  id TEXT PRIMARY KEY,
  carrier TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  low_bytes INTEGER NOT NULL DEFAULT 0,
  high_bytes INTEGER,
  source TEXT,
  at INTEGER
);
CREATE TABLE IF NOT EXISTS ts_clocks(
  id TEXT PRIMARY KEY,
  port TEXT NOT NULL,
  observed_at INTEGER NOT NULL,
  offset_seconds INTEGER NOT NULL,
  tolerance_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ts_incidents(
  id TEXT PRIMARY KEY,
  fault_carrier TEXT NOT NULL,
  primary_carrier TEXT,
  backup_carrier TEXT,
  started_at INTEGER,
  switch_at INTEGER,
  recovered_at INTEGER,
  ended_at INTEGER,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS ts_impacts(
  id TEXT PRIMARY KEY,
  incident TEXT NOT NULL,
  customer TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  supersedes TEXT
);
CREATE TABLE IF NOT EXISTS ts_obligations(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  party TEXT NOT NULL,
  period TEXT,
  incident TEXT,
  natural_key TEXT UNIQUE,
  amount_cents INTEGER NOT NULL DEFAULT 0,
  settled_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS ts_items(
  id TEXT PRIMARY KEY,
  obligation TEXT NOT NULL,
  delta_cents INTEGER NOT NULL,
  component TEXT,
  reason TEXT,
  evidence TEXT,
  period TEXT,
  window_start INTEGER,
  window_end INTEGER,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ts_flows(
  id TEXT PRIMARY KEY,
  obligation TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  at INTEGER NOT NULL,
  way TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS ts_notes(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  party TEXT NOT NULL,
  period TEXT,
  incident TEXT,
  ref_id TEXT,
  obligation TEXT,
  amount_cents INTEGER NOT NULL,
  original_amount_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'issued',
  at INTEGER NOT NULL,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS ts_adjustments(
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  at INTEGER NOT NULL,
  evidence TEXT,
  period TEXT,
  window_start INTEGER,
  window_end INTEGER,
  obligation TEXT,
  debt_kind TEXT,
  before_cents INTEGER,
  after_cents INTEGER,
  delta_cents INTEGER,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS ts_quota(
  carrier TEXT NOT NULL,
  period TEXT NOT NULL,
  confirmed_units INTEGER NOT NULL,
  ceiling_units INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(carrier, period)
);
"""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def init(db):
    # Individual executes (not executescript) so this is safe to call inside
    # the store.tx() BEGIN IMMEDIATE transaction: executescript would COMMIT
    # the open transaction out from under its caller.
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            db.execute(stmt)


def _row(db, q, a=()):
    cur = db.execute(q, a)
    r = cur.fetchone()
    if r is None:
        return None
    return dict(r) if not isinstance(r, tuple) else {d[0]: r[i] for i, d in enumerate(cur.description)}


def _rows(db, q, a=()):
    cur = db.execute(q, a)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) if isinstance(r, tuple) else dict(r) for r in cur.fetchall()]


def ceil_div(a, b):
    """ceil(a / b) for non-negative integers."""
    if b <= 0:
        raise ValueError("positive divisor required")
    return -((-int(a)) // b)


def period_key(at, tz_offset=0):
    """Calendar ``YYYY-MM`` key of an epoch-second timestamp."""
    t = _dt.datetime.fromtimestamp(int(at) + int(tz_offset), _dt.timezone.utc)
    return "%04d-%02d" % (t.year, t.month)


def period_range(period):
    """``[start, end)`` epoch seconds for a ``YYYY-MM`` period (UTC)."""
    y, m = (int(x) for x in period.split("-"))
    start = calendar.timegm((y, m, 1, 0, 0, 0, 0, 0))
    ny, nm = (y + m // 12, m % 12 + 1)
    end = calendar.timegm((ny, nm, 1, 0, 0, 0, 0, 0))
    return start, end


def merge_intervals(intervals):
    """Union length of ``(start, end)`` intervals; overlaps count once."""
    if not intervals:
        return 0
    ordered = sorted((int(s), int(e)) for s, e in intervals if e is not None and e > s)
    if not ordered:
        return 0
    total = 0
    cs, ce = ordered[0]
    for s, e in ordered[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            total += ce - cs
            cs, ce = s, e
    return total + ce - cs


# ---------------------------------------------------------------------------
# pure analytics: counter generations, deltas and 5-minute samples
# ---------------------------------------------------------------------------

def counter_edges(readings, direction="out"):
    """Turn cumulative counters into per-port delta edges.

    Each reading is a dict with at least ``id, port, generation, at,
    bytes_total`` and optionally ``direction``.  Readings are sorted by
    sample time (never by arrival), so out-of-order submission is harmless.

    Returns a list of edge dicts.  ``bytes`` is an int for ``ok`` edges and
    ``None`` for edges on which no increment may be recognized:

    * ``generation_boundary`` -- reboot / counter generation changed;
    * ``rewind`` -- cumulative counter moved backwards in one generation
      (unannounced reset or rollover); the low value is never booked as new
      traffic;
    * ``duplicate_time_conflict`` -- two different values at one timestamp.
    """
    groups = {}
    for r in readings:
        if r.get("direction", "out") != direction:
            continue
        groups.setdefault(r["port"], []).append(r)

    edges = []
    for port, items in groups.items():
        items = sorted(items, key=lambda r: (int(r["at"]), str(r["id"])))
        prev = None
        for cur in items:
            if prev is None:
                prev = cur
                continue
            same_time = int(cur["at"]) == int(prev["at"])
            if same_time and int(cur["bytes_total"]) == int(prev["bytes_total"]):
                continue
            edge = {
                "port": port,
                "direction": direction,
                "from_id": prev["id"],
                "to_id": cur["id"],
                "t0": int(prev["at"]),
                "t1": int(cur["at"]),
                "generation": int(cur["generation"]),
                "bytes": None,
                "status": "ok",
            }
            if same_time:
                edge["status"] = "duplicate_time_conflict"
            elif int(cur["generation"]) != int(prev["generation"]):
                edge["status"] = "generation_boundary"
            elif int(cur["bytes_total"]) < int(prev["bytes_total"]):
                edge["status"] = "rewind"
            else:
                edge["bytes"] = int(cur["bytes_total"]) - int(prev["bytes_total"])
            edges.append(edge)
            # A conflicted same-time pair does not give a clean predecessor;
            # keep the later record only when timestamps actually advanced.
            if not same_time:
                prev = cur
    return edges


def allocate_edge(t0, t1, nbytes, window=WINDOW_SECONDS):
    """Allocate one delta across fixed windows by overlap duration.

    Returns ``{window_index: Fraction_bytes}``; the fractions sum exactly to
    ``nbytes``.  This is what lets a counter interval straddling midnight /
    month boundary be split between billing periods.
    """
    t0, t1 = int(t0), int(t1)
    total_t = t1 - t0
    out = {}
    if total_t <= 0:
        return out
    first = t0 // window
    last = (t1 - 1) // window
    nb = Fraction(int(nbytes))
    for w in range(first, last + 1):
        lo = max(t0, w * window)
        hi = min(t1, (w + 1) * window)
        if hi > lo:
            out[w] = nb * Fraction(hi - lo, total_t)
    return out


def window_bytes(readings, port=None, direction="out", window=WINDOW_SECONDS):
    """Reconciled exact window totals (Fraction) plus non-ok edge gaps."""
    samples = {}
    gaps = []
    for e in counter_edges(readings, direction):
        if port is not None and e["port"] != port:
            continue
        if e["status"] != "ok":
            gaps.append(e)
            continue
        for w, amount in allocate_edge(e["t0"], e["t1"], e["bytes"], window).items():
            samples[w] = samples.get(w, Fraction(0)) + amount
    return samples, gaps


def mirror_attribution(mirror_edges, target_window_start, clock_offset=0,
                       clock_tolerance=0, window=WINDOW_SECONDS):
    """Byte range of mirror traffic attributable to one billing window.

    The mirror probe clock maps to the observed carrier billing clock as
    ``b_time = mirror_time + offset`` where the true offset may differ by up
    to ``clock_tolerance``.  Mirror bytes are assumed uniform inside their
    sample interval.  The returned ``(low, high)`` Fractions bracket the
    mirror bytes overlapping ``[target_window_start, +window)`` for every
    allowed offset.
    """
    a = int(target_window_start)
    b = a + window
    lo_s = int(clock_offset) - int(clock_tolerance)
    hi_s = int(clock_offset) + int(clock_tolerance)
    low = Fraction(0)
    high = Fraction(0)
    for e in mirror_edges:
        if e["status"] != "ok" or not e.get("bytes"):
            continue
        m0, m1 = e["t0"], e["t1"]
        length = m1 - m0
        if length <= 0:
            continue
        amount = Fraction(e["bytes"])

        def overlap_seconds(shift):
            return max(0, min(m1 + shift, b) - max(m0 + shift, a))

        candidates = {lo_s, hi_s}
        for boundary in (a, b):
            candidates.add(boundary - m0)
            candidates.add(boundary - m1)
        fracs = []
        for s in candidates:
            if s < lo_s or s > hi_s:
                continue
            fracs.append(Fraction(overlap_seconds(s), length))
        # The overlap fraction is a tent function; extrema over the shift
        # interval occur at range ends or boundary-aligning shifts (included).
        f_lo = min(fracs) if fracs else Fraction(0)
        f_hi = max(fracs) if fracs else Fraction(0)
        # Flat plateau case: a candidate inside the plateau may be missing
        # from the alignment set only when both aligning shifts are out of
        # range; endpoints then already yield the plateau value.
        low += amount * f_lo
        high += amount * f_hi
    return low, high


def reconcile_window(constraints):
    """Intersect byte constraints on one window's true egress.

    A constraint is ``(kind, value)`` with kind in ``exact/lower/upper``.
    Returns ``(low Fraction, high Fraction|None, state)`` where state is
    ``exact``, ``bounded``, ``open`` or ``conflict``.
    """
    low = Fraction(0)
    high = None
    for kind, value in constraints:
        v = Fraction(value)
        if kind == "exact":
            if low > v or (high is not None and high < v):
                return low, high, "conflict"
            low = high = v
        elif kind == "lower":
            if v > low:
                low = v
        elif kind == "upper":
            if high is None or v < high:
                high = v
        if high is not None and low > high:
            return low, high, "conflict"
    if high is None:
        state = "open"
    elif low == high:
        state = "exact"
    else:
        state = "bounded"
    return low, high, state


def nearest_rank_high(values, rule="max", percentile=95):
    """Monthly high statistic; ``values`` are orderable numbers."""
    if not values:
        return Fraction(0)
    ordered = sorted(values)
    if rule == "max":
        return ordered[-1]
    p = percentile
    if rule in ("p95", "percentile95"):
        p = 95
    elif rule.startswith("p") and rule[1:].isdigit():
        p = int(rule[1:])
    n = len(ordered)
    rank = max(1, min(n, ceil_div(p * n, 100)))  # nearest rank, exact ints
    return ordered[rank - 1]


def monthly_high(window_bounds, rule="max"):
    """High-water envelope from ``[(low, high), ...]`` window bounds."""
    if not window_bounds:
        return Fraction(0), Fraction(0)
    lows = [Fraction(lo) for lo, _ in window_bounds]
    highs = [Fraction(hi if hi is not None else lo) for lo, hi in window_bounds]
    return nearest_rank_high(lows, rule), nearest_rank_high(highs, rule)


def charge_envelope(high_low_bytes, high_high_bytes, unit_bytes=1,
                    price_cents=0, committed_units=0):
    """Integer-cent ``(low, high, floor)`` payable envelope incl. floor."""
    unit_bytes = max(1, int(unit_bytes))
    lo = Fraction(high_low_bytes)
    hi = Fraction(high_high_bytes)
    # floor of the lower bound is certainly owed; ceil of the upper bound
    # certainly covers the true charge.
    low_money = (lo * int(price_cents)) // unit_bytes
    money_hi_fraction = hi * int(price_cents) / unit_bytes
    high_money = -((-money_hi_fraction.numerator) // money_hi_fraction.denominator)
    floor_cents = int(committed_units) * int(price_cents)
    low_cents = max(int(low_money), floor_cents)
    high_cents = max(int(high_money), floor_cents)
    return low_cents, high_cents, floor_cents


def availability_credit_cents(mrr_cents, impacted_seconds, period_seconds, tiers):
    """Credit for actual availability.

    ``tiers`` is an ordered list of ``[impact_seconds_threshold,
    credit_basis_points]``; the highest threshold met wins.  Basis points are
    1/100 of a percent (10_000 bps == 100%).
    """
    impacted = max(0, int(impacted_seconds))
    pct_like = impacted  # tiers are expressed in accumulated unavailable secs
    bps = 0
    for threshold, award_bps in tiers:
        if pct_like >= int(threshold):
            bps = max(bps, int(award_bps))
    return int(mrr_cents) * bps // 10_000


# ---------------------------------------------------------------------------
# stateful engine
# ---------------------------------------------------------------------------

class TransitSettlement:
    """Facade owning a sqlite connection and running :func:`run` in tx."""

    def __init__(self, db=":memory:"):
        from .store import connect, tx
        self._tx = tx
        if isinstance(db, str):
            self.db = connect(db)
            self._owns = True
        else:
            self.db = db
            self._owns = False
        init(self.db)

    def close(self):
        if self._owns:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def run(self, op=None, **payload):
        if op is not None:
            payload["op"] = op
        with self._tx(self.db):
            return run(self.db, payload)

    # friendly wrappers -----------------------------------------------------
    def __getattr__(self, name):
        # Allows ts.port(...), ts.reading(...), ts.settle_carrier(...),
        # ts.summary() etc.  Only invoked for names not found normally.
        canonical = _OP_ALIASES.get(name, name)
        if canonical in _HANDLERS:
            def caller(**kw):
                return self.run(canonical, **kw)
            return caller
        raise AttributeError(name)


def _next_seq(db):
    r = _row(db, "SELECT COALESCE(MAX(seq),0)+1 AS s FROM ts_adjustments")
    return r["s"] if r else 1


def _adjust(db, x, obligation, debt_kind, before, after, reason,
            window_start=None, window_end=None, period=None, evidence=None,
            adjustment_id=None):
    adj_id = adjustment_id or x.get("adjustment_id") or ("adj-%s" % _next_seq(db))
    db.execute(
        """INSERT INTO ts_adjustments(id,seq,at,evidence,period,window_start,
               window_end,obligation,debt_kind,before_cents,after_cents,
               delta_cents,reason)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (adj_id, _next_seq(db), int(x.get("at", 0)), evidence or x.get("evidence"),
         period or x.get("period"), window_start, window_end, obligation, debt_kind,
         int(before), int(after), int(after) - int(before), reason),
    )
    return {"adjustment_id": adj_id, "window_start": window_start,
            "window_end": window_end, "debt": obligation, "debt_kind": debt_kind,
            "before_cents": int(before), "after_cents": int(after),
            "delta_cents": int(after) - int(before), "reason": reason}


def _get_obligation(db, oid):
    return _row(db, "SELECT * FROM ts_obligations WHERE id=?", (oid,))


def _ensure_obligation(db, oid, kind, party, period=None, incident=None,
                       natural_key=None):
    existing = _get_obligation(db, oid)
    if existing:
        return existing
    if natural_key:
        nk = natural_key
        other = _row(db, "SELECT * FROM ts_obligations WHERE natural_key=?", (nk,))
        if other:
            return other
    db.execute(
        """INSERT INTO ts_obligations(id,kind,party,period,incident,natural_key)
           VALUES(?,?,?,?,?,?)""",
        (oid, kind, party, period, incident, natural_key),
    )
    return _get_obligation(db, oid)


def _post_item(db, item_id, obligation, delta, at, component=None, reason=None,
               evidence=None, period=None, window_start=None, window_end=None):
    ob = _get_obligation(db, obligation)
    before = int(ob["amount_cents"])
    db.execute(
        """INSERT INTO ts_items(id,obligation,delta_cents,component,reason,
               evidence,period,window_start,window_end,at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (item_id, obligation, int(delta), component, reason, evidence, period,
         window_start, window_end, int(at)),
    )
    after = before + int(delta)
    settled = int(ob["settled_cents"])
    if after <= 0:
        status = "closed" if after == 0 and settled == 0 else "corrected"
    elif settled == 0:
        status = "open"
    elif settled < after:
        status = "partially_settled"
    elif settled == after:
        status = "settled"
    else:
        status = "over_settled"
    db.execute("UPDATE ts_obligations SET amount_cents=?, status=? WHERE id=?",
               (after, status, obligation))
    return before, after


def _post_flow(db, flow_id, obligation, amount, at, way, note=None):
    old = _row(db, "SELECT * FROM ts_flows WHERE id=?", (flow_id,))
    if old:
        return {"decision": "duplicate", "id": flow_id, "flow": old, "duplicate": True}
    ob = _get_obligation(db, obligation)
    amount = int(amount)
    if amount <= 0:
        return {"decision": "invalid_amount", "amount_cents": amount}
    new_settled = int(ob["settled_cents"]) + amount
    if new_settled > int(ob["amount_cents"]):
        return {"decision": "amount_exceeds_balance",
                "obligation": obligation,
                "remaining_cents": int(ob["amount_cents"]) - int(ob["settled_cents"]),
                "requested_cents": amount}
    db.execute(
        "INSERT INTO ts_flows(id,obligation,amount_cents,at,way,note) VALUES(?,?,?,?,?,?)",
        (flow_id, obligation, amount, int(at), way, note),
    )
    status = "settled" if new_settled == int(ob["amount_cents"]) else "partially_settled"
    db.execute("UPDATE ts_obligations SET settled_cents=?, status=? WHERE id=?",
               (new_settled, status, obligation))
    return {"decision": "settled", "id": flow_id, "obligation": obligation,
            "amount_cents": amount, "status": status}


def _note(db, nid, kind, party, amount, at, period=None, incident=None,
          ref_id=None, obligation=None, status="issued", payload=None):
    old = _row(db, "SELECT * FROM ts_notes WHERE id=?", (nid,))
    if old:
        return old, True
    db.execute(
        """INSERT INTO ts_notes(id,kind,party,period,incident,ref_id,obligation,
               amount_cents,original_amount_cents,status,at,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (nid, kind, party, period, incident, ref_id, obligation, int(amount),
         int(amount), status, int(at),
         json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)),
    )
    return _row(db, "SELECT * FROM ts_notes WHERE id=?", (nid,)), False


# ---------------------------------------------------------------------------
# settlement computation over stored evidence
# ---------------------------------------------------------------------------

def _contract(db, carrier, period):
    if period:
        c = _row(db, "SELECT * FROM ts_contracts WHERE carrier=? AND period=?",
                 (carrier, period))
        if c is not None:
            return c
    return _row(db, "SELECT * FROM ts_contracts WHERE carrier=? AND period='*'",
                (carrier,))


def _carrier_window_report(db, carrier, start=None, end=None, period=None,
                           window=WINDOW_SECONDS):
    if period and (start is None or end is None):
        start, end = period_range(period)
    ports = _rows(db, "SELECT * FROM ts_ports WHERE carrier=?", (carrier,))
    port_ids = [p["id"] for p in ports]
    all_readings = _rows(db, "SELECT * FROM ts_readings")
    carrier_readings = [r for r in all_readings if r["port"] in port_ids]

    handoff_ports = [p for p in ports if p["kind"] == "handoff"]
    mirror_ports = [p for p in ports if p["kind"] == "mirror"]

    # exact handoff samples per port
    handoff_samples = {}
    gap_events = []
    for p in handoff_ports:
        s, gaps = window_bytes(carrier_readings, p["id"], "out", window)
        handoff_samples[p["id"]] = s
        gap_events.extend(gaps)

    # mirror edges + clock calibration
    mirror_by_port = {}
    for p in mirror_ports:
        edges = counter_edges(
            [r for r in carrier_readings if r["port"] == p["id"]], "out")
        clock = _row(
            db,
            """SELECT * FROM ts_clocks WHERE port=?
               ORDER BY observed_at DESC LIMIT 1""",
            (p["id"],),
        )
        if clock:
            offset, tol = clock["offset_seconds"], clock["tolerance_seconds"]
        else:
            offset, tol = p["clock_offset"], p["clock_tolerance"]
        mirror_by_port[p["id"]] = (edges, offset, tol)

    claims = _rows(db, "SELECT * FROM ts_claims WHERE carrier=?", (carrier,))
    claim_by_w = {}
    for c in claims:
        claim_by_w.setdefault(c["window_start"], []).append(c["bytes"])
    bounds = _rows(db, "SELECT * FROM ts_bounds WHERE carrier=?", (carrier,))
    bound_by_w = {}
    for b in bounds:
        bound_by_w.setdefault(b["window_start"], []).append(b)

    window_starts = set()
    for s in handoff_samples.values():
        window_starts.update(w * window for w in s)
    for w in claim_by_w:
        window_starts.add(w)
    for w in bound_by_w:
        window_starts.add(w)
    if start is not None and end is not None:
        window_starts = {w for w in window_starts if start <= w < end}

    windows = []
    for ws in sorted(window_starts):
        constraints = []
        basis = {"handoff": [], "claims": claim_by_w.get(ws, []),
                 "probe_bounds": [], "mirror_range": None}
        for pid, s in handoff_samples.items():
            v = s.get(ws // window)
            if v is not None:
                constraints.append(("exact", v))
                basis["handoff"].append({"port": pid, "bytes": str(v)})
        for cbytes in claim_by_w.get(ws, []):
            constraints.append(("upper", cbytes))
        for b in bound_by_w.get(ws, []):
            constraints.append(("lower", b["low_bytes"]))
            basis["probe_bounds"].append(
                {"id": b["id"], "low": b["low_bytes"], "high": b["high_bytes"],
                 "source": b["source"]})
            if b["high_bytes"] is not None:
                constraints.append(("upper", b["high_bytes"]))
        # mirror deduction against a claim that contains the mirror copy
        mlo_total = Fraction(0)
        mhi_total = Fraction(0)
        for pid, (edges, offset, tol) in mirror_by_port.items():
            lo, hi = mirror_attribution(edges, ws, offset, tol, window)
            mlo_total += lo
            mhi_total += hi
        if mhi_total or mlo_total:
            basis["mirror_range"] = [str(mlo_total), str(mhi_total)]
        for cbytes in claim_by_w.get(ws, []):
            # truth = claim - mirror_double_count, bracketed by skew range
            constraints.append(("lower", Fraction(cbytes) - mhi_total))
            constraints.append(("upper", Fraction(cbytes) - mlo_total))
        lo, hi, state = reconcile_window(constraints)
        lo_i = lo.numerator // lo.denominator
        if hi is None:
            hi_i = None
        else:
            hi_i = ceil_div(hi.numerator, hi.denominator)
        windows.append({
            "window_start": ws,
            "window_end": ws + window,
            "low_bytes": lo_i,
            "high_bytes": hi_i if hi_i is not None else lo_i,
            "open_ended": hi is None,
            "exact": state == "exact" or (hi_i == lo_i and state != "conflict"),
            "state": state,
            "basis": basis,
        })
    return {"carrier": carrier, "period": period, "window_seconds": window,
            "windows": windows, "counter_gaps": gap_events}


def _high_from_report(report, rule="max"):
    bounds = [(w["low_bytes"], w["high_bytes"]) for w in report["windows"]]
    return monthly_high(bounds, rule)


# ---------------------------------------------------------------------------
# op handlers
# ---------------------------------------------------------------------------

_CANONICAL_OPS = set()


def op(name):
    def deco(fn):
        _CANONICAL_OPS.add(name)
        return fn
    return deco


@op("port")
def _op_port(db, x):
    pid = x["id"]
    kind = x.get("kind", "handoff")
    if kind not in POINT_KINDS:
        return {"decision": "invalid_port_kind", "kind": kind}
    direction = x.get("direction", "out")
    if direction not in DIRECTIONS:
        return {"decision": "invalid_direction", "direction": direction}
    old = _row(db, "SELECT * FROM ts_ports WHERE id=?", (pid,))
    if old:
        same = (old["carrier"] == x.get("carrier") and old["kind"] == kind
                and old["direction"] == direction
                and old["mirrors"] == x.get("mirrors"))
        if not same:
            return {"decision": "port_identity_conflict", "id": pid}
        return {"decision": "duplicate", "id": pid, "duplicate": True}
    db.execute(
        """INSERT INTO ts_ports(id,carrier,kind,direction,mirrors,
               clock_offset,clock_tolerance,detail)
           VALUES(?,?,?,?,?,?,?,?)""",
        (pid, x.get("carrier"), kind, direction, x.get("mirrors"),
         int(x.get("clock_offset", 0)), int(x.get("clock_tolerance", 0)),
         json.dumps(x.get("detail", {}), sort_keys=True, ensure_ascii=False)),
    )
    return {"decision": "registered", "id": pid}


@op("reading")
def _op_reading(db, x):
    rid = x["id"]
    required = ("port", "generation", "at", "link", "bytes_total")
    missing = [k for k in required if k not in x]
    if missing:
        return {"decision": "invalid_reading", "missing": missing}
    if int(x["bytes_total"]) < 0 or int(x["generation"]) < 0:
        return {"decision": "invalid_reading",
                "reason": "negative counter or generation"}
    port = _row(db, "SELECT * FROM ts_ports WHERE id=?", (x["port"],))
    if port is None:
        return {"decision": "unknown_port", "port": x["port"]}
    direction = x.get("direction", port["direction"] or "out")
    if direction not in DIRECTIONS:
        return {"decision": "invalid_direction"}
    old = _row(db, "SELECT * FROM ts_readings WHERE id=?", (rid,))
    payload = (x["port"], int(x["generation"]), int(x["at"]), direction,
               x["link"], int(x["bytes_total"]))
    if old:
        same = (old["port"], old["generation"], old["at"], old["direction"],
                old["link"], old["bytes_total"]) == payload
        return {"decision": "duplicate" if same else "reading_conflict",
                "id": rid, "duplicate": same}
    try:
        db.execute(
            """INSERT INTO ts_readings(id,port,generation,at,direction,link,
                   bytes_total) VALUES(?,?,?,?,?,?,?)""",
            (rid,) + payload,
        )
    except Exception as exc:  # unique physical sample duplicate
        return {"decision": "duplicate_physical_sample", "error": str(exc)}
    return {"decision": "recorded", "id": rid}


@op("contract")
def _op_contract(db, x):
    carrier = x["carrier"]
    period = x.get("period", "*")
    old = _row(db, "SELECT * FROM ts_contracts WHERE carrier=? AND period=?",
               (carrier, period))
    if old and not x.get("replace"):
        return {"decision": "duplicate", "carrier": carrier, "period": period,
                "duplicate": True}
    db.execute(
        """INSERT INTO ts_contracts(carrier,period,unit_bytes,price_cents,
               committed_units,high_rule) VALUES(?,?,?,?,?,?)
           ON CONFLICT(carrier,period) DO UPDATE SET
             unit_bytes=excluded.unit_bytes, price_cents=excluded.price_cents,
             committed_units=excluded.committed_units,
             high_rule=excluded.high_rule""",
        (carrier, period, int(x.get("unit_bytes", 1)),
         int(x["price_cents"]), int(x.get("committed_units", 0)),
         x.get("high_rule", "max")),
    )
    return {"decision": "recorded", "carrier": carrier, "period": period}


@op("sla")
def _op_sla(db, x):
    db.execute(
        """INSERT INTO ts_sla(customer,mrr_cents,period_seconds,tiers)
           VALUES(?,?,?,?) ON CONFLICT(customer) DO UPDATE SET
             mrr_cents=excluded.mrr_cents,
             period_seconds=excluded.period_seconds, tiers=excluded.tiers""",
        (x["customer"], int(x["mrr_cents"]),
         int(x.get("period_seconds", 2_592_000)),
         json.dumps(x["tiers"], sort_keys=True)),
    )
    return {"decision": "recorded", "customer": x["customer"]}


@op("claim")
def _op_claim(db, x):
    cid = x["id"]
    if _row(db, "SELECT 1 FROM ts_claims WHERE id=?", (cid,)):
        return {"decision": "duplicate", "id": cid, "duplicate": True}
    ws = x.get("window_start")
    if ws is None and x.get("at") is not None:
        ws = int(x["at"]) // int(x.get("window", WINDOW_SECONDS)) * int(x.get("window", WINDOW_SECONDS))
    db.execute(
        "INSERT INTO ts_claims(id,carrier,window_start,bytes,at,link) VALUES(?,?,?,?,?,?)",
        (cid, x["carrier"], int(ws), int(x["bytes"]), x.get("at"), x.get("link")),
    )
    return {"decision": "recorded", "id": cid, "window_start": int(ws)}


@op("bound")
def _op_bound(db, x):
    bid = x["id"]
    if _row(db, "SELECT 1 FROM ts_bounds WHERE id=?", (bid,)):
        return {"decision": "duplicate", "id": bid, "duplicate": True}
    ws = x.get("window_start")
    if ws is None and x.get("at") is not None:
        ws = int(x["at"]) // WINDOW_SECONDS * WINDOW_SECONDS
    db.execute(
        """INSERT INTO ts_bounds(id,carrier,window_start,low_bytes,high_bytes,
               source,at) VALUES(?,?,?,?,?,?,?)""",
        (bid, x["carrier"], int(ws), int(x.get("low_bytes", 0)),
         x.get("high_bytes"), x.get("source"), x.get("at")),
    )
    return {"decision": "recorded", "id": bid}


@op("clock")
def _op_clock(db, x):
    cid = x["id"]
    if _row(db, "SELECT 1 FROM ts_clocks WHERE id=?", (cid,)):
        return {"decision": "duplicate", "id": cid, "duplicate": True}
    db.execute(
        """INSERT INTO ts_clocks(id,port,observed_at,offset_seconds,
               tolerance_seconds) VALUES(?,?,?,?,?)""",
        (cid, x["port"], int(x.get("observed_at", x.get("at", 0))),
         int(x["offset_seconds"]), int(x.get("tolerance_seconds", 0))),
    )
    return {"decision": "recorded", "id": cid}


@op("incident")
def _op_incident(db, x):
    iid = x["id"]
    if _row(db, "SELECT 1 FROM ts_incidents WHERE id=?", (iid,)):
        return {"decision": "duplicate", "id": iid, "duplicate": True}
    db.execute(
        """INSERT INTO ts_incidents(id,fault_carrier,primary_carrier,
               backup_carrier,started_at,switch_at,recovered_at,ended_at,detail)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (iid, x["fault_carrier"], x.get("primary_carrier"),
         x.get("backup_carrier"), x.get("started_at"), x.get("switch_at"),
         x.get("recovered_at"), x.get("ended_at"),
         json.dumps(x.get("detail", {}), sort_keys=True, ensure_ascii=False)),
    )
    return {"decision": "recorded", "id": iid}


@op("impact")
def _op_impact(db, x):
    iid = x["id"]
    if _row(db, "SELECT 1 FROM ts_impacts WHERE id=?", (iid,)):
        return {"decision": "duplicate", "id": iid, "duplicate": True}
    start, end = int(x["start"]), int(x["end"])
    if end <= start:
        return {"decision": "invalid_interval"}
    if x.get("supersedes"):
        db.execute("UPDATE ts_impacts SET status='superseded' WHERE id=?",
                   (x["supersedes"],))
    db.execute(
        """INSERT INTO ts_impacts(id,incident,customer,start,end,status,supersedes)
           VALUES(?,?,?,?,?,'active',?)""",
        (iid, x["incident"], x["customer"], start, end, x.get("supersedes")),
    )
    return {"decision": "recorded", "id": iid,
            "supersedes": x.get("supersedes")}


def _settle_carrier(db, x):
    bill_id = x["id"]
    prior = _row(db, "SELECT * FROM ts_notes WHERE id=?", (bill_id,))
    if prior:
        return {"decision": "duplicate", "id": bill_id, "duplicate": True,
                "note": dict(prior)}
    carrier = x["carrier"]
    period = x.get("period")
    start, end = x.get("start"), x.get("end")
    rep = _carrier_window_report(db, carrier, start, end, period)
    c = _contract(db, carrier, period)
    if c is None and "price_cents" in x:
        c = {"unit_bytes": int(x.get("unit_bytes", 1)),
             "price_cents": int(x["price_cents"]),
             "committed_units": int(x.get("committed_units", 0)),
             "high_rule": x.get("high_rule", "max")}
    if c is None:
        return {"decision": "missing_contract",
                "carrier": carrier, "period": period}
    conflicts = [w for w in rep["windows"] if w["state"] == "conflict"]
    if conflicts:
        return {"decision": "evidence_conflict", "carrier": carrier,
                "period": period, "windows": conflicts}
    hi_lo, hi_hi = _high_from_report(rep, c["high_rule"])
    low_cents, high_cents, floor_cents = charge_envelope(
        hi_lo, hi_hi, c["unit_bytes"], c["price_cents"], c["committed_units"])
    mode = x.get("mode", "lower_bound")
    invoice_cents = x.get("invoice_cents")
    at = int(x.get("at", 0))
    incident = x.get("incident")

    if mode == "pay_billed":
        if invoice_cents is None:
            invoice_cents = high_cents
        bill_amount = int(invoice_cents)
    else:
        bill_amount = int(x.get("amount_cents", low_cents))

    ob_id = x.get("obligation_id") or ("ob-pay-%s" % bill_id)
    _ensure_obligation(db, ob_id, "carrier_payable", carrier, period, incident,
                       natural_key="payable:%s:%s:%s" % (carrier, period, bill_id))
    before, after = _post_item(
        db, "item-%s" % bill_id, ob_id, bill_amount, at,
        component="carrier_bill", reason=x.get("reason", mode),
        evidence=x.get("evidence"), period=period)
    note, _ = _note(db, bill_id, "carrier_bill", carrier, bill_amount, at,
                    period=period, incident=incident, obligation=ob_id,
                    payload={"mode": mode, "high_low_bytes": str(hi_lo),
                             "high_high_bytes": str(hi_hi),
                             "charge_low_cents": low_cents,
                             "charge_high_cents": high_cents,
                             "floor_cents": floor_cents,
                             "invoice_cents": invoice_cents,
                             "windows": rep["windows"]})
    changes = [_adjust(db, x, ob_id, "carrier_payable", before, after,
                       "issue carrier bill (%s)" % mode, period=period,
                       evidence=x.get("evidence"))]

    disputed = max(0, int(invoice_cents if invoice_cents is not None else high_cents)
                   - bill_amount)
    # Capacity quota projection.  Paying the proven lower bound books only the
    # conservative peak against the carrier's subsequent capacity allowance;
    # paying the full carrier invoice books the billed peak immediately.
    unit = max(1, c["unit_bytes"])
    if mode == "pay_billed":
        confirmed_units = int(bill_amount) // max(1, int(c["price_cents"] or 1))
        ceiling_units = confirmed_units
    else:
        confirmed_units = hi_lo.numerator // (hi_lo.denominator * unit)
        ceiling_units = ceil_div(hi_hi.numerator, hi_hi.denominator * unit)
    db.execute(
        """INSERT INTO ts_quota(carrier,period,confirmed_units,ceiling_units,
               updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(carrier,period) DO UPDATE SET
             confirmed_units=excluded.confirmed_units,
             ceiling_units=excluded.ceiling_units,
             updated_at=excluded.updated_at""",
        (carrier, period or "*", confirmed_units, ceiling_units, at),
    )

    result = {"decision": "bill_issued", "id": bill_id, "carrier": carrier,
              "period": period, "mode": mode,
              "high_low_bytes": str(hi_lo), "high_high_bytes": str(hi_hi),
              "charge_low_cents": low_cents, "charge_high_cents": high_cents,
              "floor_cents": floor_cents, "bill_amount_cents": bill_amount,
              "undisputed_cents": low_cents if mode == "lower_bound" else None,
              "disputed_memo_cents": disputed,
              "confirmed_peak_units": confirmed_units,
              "ceiling_peak_units": ceiling_units,
              "windows": rep["windows"], "changes": changes}

    # pay_billed mode: optionally settle the carrier invoice immediately and
    # provisionally secure the fault differential against carrier A.
    if mode == "pay_billed" and x.get("payment_id"):
        pay = _pay(db, {"id": x["payment_id"], "bill_id": bill_id,
                        "amount_cents": bill_amount, "at": at,
                        "way": "out"})
        result["payment"] = pay
    if mode == "pay_billed" and incident and x.get("secure_recovery", True):
        fault = _row(db, "SELECT * FROM ts_incidents WHERE id=?", (incident,))
        fault_carrier = x.get("fault_carrier") or (fault["fault_carrier"] if fault else "a")
        butfor = int(x.get("butfor_cents", 0))
        secured = int(x.get("secured_differential_cents",
                            max(0, bill_amount - butfor)))
        rec_id = x.get("recovery_id") or ("rec-%s" % incident)
        _ensure_obligation(
            db, rec_id, "recovery_receivable", fault_carrier, period, incident,
            natural_key="recovery:%s:%s" % (fault_carrier, incident))
        rb, ra = _post_item(
            db, "item-%s-provisional" % rec_id, rec_id, secured, at,
            component="b_differential_provisional",
            reason="secured by fault party indemnity from billed amount",
            period=period)
        changes.append(_adjust(
            db, x, rec_id, "recovery_receivable", rb, ra,
            "provisional secured differential", period=period))
        result["secured_recovery_cents"] = secured
    return result


@op("settle_carrier")
def _op_settle_carrier(db, x):
    return _settle_carrier(db, x)


def _pay(db, x):
    pid = x["id"]
    if _row(db, "SELECT * FROM ts_flows WHERE id=?", (pid,)):
        old = _row(db, "SELECT * FROM ts_flows WHERE id=?", (pid,))
        return {"decision": "duplicate", "id": pid, "duplicate": True,
                "amount_cents": old["amount_cents"]}
    ob_id = x.get("obligation_id")
    if not ob_id and x.get("bill_id"):
        note = _row(db, "SELECT * FROM ts_notes WHERE id=?", (x["bill_id"],))
        if not note:
            return {"decision": "unknown_bill", "bill_id": x["bill_id"]}
        ob_id = note["obligation"]
    if not ob_id:
        return {"decision": "missing_obligation"}
    ob = _get_obligation(db, ob_id)
    amount = int(x.get("amount_cents", ob["amount_cents"] - ob["settled_cents"]))
    way = x.get("way", "out")
    res = _post_flow(db, pid, ob_id, amount, int(x.get("at", 0)), way,
                     note=x.get("bill_id"))
    if res.get("decision") == "settled" and x.get("bill_id"):
        db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (x["bill_id"],))
        res["bill_id"] = x["bill_id"]
    return res


@op("pay")
def _op_pay(db, x):
    return _pay(db, x)


def _impact_seconds(db, customer, incident):
    rows = _rows(
        db,
        "SELECT start,end FROM ts_impacts WHERE customer=? AND incident=? "
        "AND status='active'",
        (customer, incident),
    )
    return merge_intervals([(r["start"], r["end"]) for r in rows])


def _credit_amount(db, customer, incident):
    sla = _row(db, "SELECT * FROM ts_sla WHERE customer=?", (customer,))
    if sla is None:
        return None, 0, []
    tiers = json.loads(sla["tiers"])
    impacted = _impact_seconds(db, customer, incident)
    cents = availability_credit_cents(
        sla["mrr_cents"], impacted, sla["period_seconds"], tiers)
    return cents, impacted, tiers


@op("credit")
def _op_credit(db, x):
    cid = x["id"]
    if _row(db, "SELECT * FROM ts_notes WHERE id=?", (cid,)):
        return {"decision": "duplicate", "id": cid, "duplicate": True}
    customer, incident = x["customer"], x["incident"]
    natural = "credit:%s:%s" % (customer, incident)
    existing_ob = _row(db, "SELECT * FROM ts_obligations WHERE natural_key=?",
                       (natural,))
    if existing_ob is not None and not x.get("allow_re_grant"):
        return {"decision": "credit_already_granted",
                "obligation": existing_ob["id"]}
    cents, impacted, tiers = _credit_amount(db, customer, incident)
    if cents is None:
        return {"decision": "missing_sla", "customer": customer}
    at = int(x.get("at", 0))
    period = x.get("period") or period_key(at)
    ob_id = x.get("obligation_id") or ("ob-credit-%s-%s" % (customer, incident))
    _ensure_obligation(db, ob_id, "customer_credit", customer, period, incident,
                       natural_key=natural)
    before, after = _post_item(db, "item-%s" % cid, ob_id, cents, at,
                               component="sla_credit", reason="availability",
                               period=period)
    note, _ = _note(db, cid, "customer_credit", customer, cents, at,
                    period=period, incident=incident, obligation=ob_id,
                    status="issued",
                    payload={"impacted_seconds": impacted, "tiers": tiers})
    change = _adjust(db, x, ob_id, "customer_credit", before, after,
                     "grant SLA credit", period=period,
                     window_start=x.get("window_start"),
                     window_end=x.get("window_end"))
    result = {"decision": "credit_granted", "id": cid, "customer": customer,
              "incident": incident, "impacted_seconds": impacted,
              "amount_cents": cents, "note_status": "issued",
              "changes": [change]}
    if x.get("pay_id") or x.get("pay_now"):
        pay_id = x.get("pay_id") or ("pay-%s" % cid)
        pay = _pay(db, {"id": pay_id, "obligation_id": ob_id,
                        "amount_cents": cents, "at": at, "way": "out"})
        if pay.get("decision") == "settled":
            db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (cid,))
            result["note_status"] = "paid"
        result["payment"] = pay
    return result


@op("pay_credit")
def _op_pay_credit(db, x):
    note = _row(db, "SELECT * FROM ts_notes WHERE id=?", (x["credit_id"],))
    if not note:
        return {"decision": "unknown_credit"}
    res = _pay(db, {"id": x["id"], "obligation_id": note["obligation"],
                    "amount_cents": x.get("amount_cents", note["amount_cents"]),
                    "at": x.get("at", note["at"]), "way": "out"})
    if res.get("decision") == "settled":
        db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (x["credit_id"],))
    return res


@op("revise_credit")
def _op_revise_credit(db, x):
    """Recompute a credit after new loss evidence.

    The originally issued/paid note is never changed.  An increased credit
    appends a supplemental note; a decreased credit after payout creates a
    clawback receivable instead of rewriting history.
    """
    cid = x["credit_id"]
    original = _row(db, "SELECT * FROM ts_notes WHERE id=? AND kind='customer_credit'",
                    (cid,))
    if not original:
        return {"decision": "unknown_credit"}
    ob = _get_obligation(db, original["obligation"])
    customer, incident = original["party"], original["incident"]
    new_total, impacted, tiers = _credit_amount(db, customer, incident)
    if new_total is None:
        return {"decision": "missing_sla"}
    at = int(x.get("at", 0))
    current = int(ob["amount_cents"])
    delta = new_total - current
    sid = x["id"]
    if delta == 0:
        return {"decision": "unchanged", "credit_id": cid,
                "amount_cents": current, "impacted_seconds": impacted}
    period = x.get("period") or original["period"]
    changes = []
    if delta > 0:
        before, after = _post_item(
            db, "item-%s" % sid, ob["id"], delta, at,
            component="sla_credit_supplement", reason=x.get("reason", "supplement"),
            evidence=x.get("evidence"), period=period,
            window_start=x.get("window_start"), window_end=x.get("window_end"))
        note, dup = _note(db, sid, "credit_supplement", customer, delta, at,
                         period=period, incident=incident, ref_id=cid,
                         obligation=ob["id"],
                         payload={"new_total_cents": new_total,
                                  "impacted_seconds": impacted})
        changes.append(_adjust(db, x, ob["id"], "customer_credit", before, after,
                               "supplement credit after new evidence",
                               period=period, window_start=x.get("window_start"),
                               window_end=x.get("window_end"),
                               evidence=x.get("evidence")))
        result = {"decision": "credit_supplemented", "id": sid,
                  "original_credit_id": cid, "delta_cents": delta,
                  "total_credit_cents": after, "impacted_seconds": impacted,
                  "changes": changes}
        if x.get("pay_id") or x.get("pay_now"):
            pay_id = x.get("pay_id") or ("pay-%s" % sid)
            pay = _pay(db, {"id": pay_id, "obligation_id": ob["id"],
                            "amount_cents": delta, "at": at, "way": "out"})
            if pay.get("decision") == "settled":
                db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (sid,))
            result["payment"] = pay
        return result

    # delta < 0
    overpaid = int(ob["settled_cents"]) - new_total
    if overpaid > 0:
        claw_id = x.get("clawback_id") or ("claw-%s" % sid)
        _ensure_obligation(
            db, claw_id, "customer_clawback", customer, period, incident,
            natural_key="clawback:%s:%s:%s" % (customer, incident, sid))
        b, a = _post_item(db, "item-%s" % claw_id, claw_id, overpaid, at,
                          component="credit_correction",
                          reason=x.get("reason", "over-credited after correction"),
                          period=period, window_start=x.get("window_start"),
                          window_end=x.get("window_end"),
                          evidence=x.get("evidence"))
        _note(db, sid, "credit_reversal", customer, overpaid, at, period=period,
              incident=incident, ref_id=cid, obligation=claw_id,
              payload={"restated_credit_cents": new_total,
                       "original_paid_cents": ob["settled_cents"],
                       "impacted_seconds": impacted})
        changes.append(_adjust(db, x, claw_id, "customer_clawback", b, a,
                               "credit shortened after correction",
                               period=period, window_start=x.get("window_start"),
                               window_end=x.get("window_end"),
                               evidence=x.get("evidence")))
        return {"decision": "credit_clawback_established", "id": sid,
                "original_credit_id": cid,
                "clawback_cents": overpaid,
                "original_note_unchanged": int(original["original_amount_cents"]),
                "changes": changes}
    # nothing paid yet: simply reduce the obligation with a reversal item
    before, after = _post_item(db, "item-%s" % sid, ob["id"], delta, at,
                               component="sla_credit_reversal",
                               reason=x.get("reason", "correction"),
                               period=period)
    _note(db, sid, "credit_reversal", customer, -delta, at, period=period,
          incident=incident, ref_id=cid, obligation=ob["id"])
    changes.append(_adjust(db, x, ob["id"], "customer_credit", before, after,
                           "reduce credit after correction", period=period))
    return {"decision": "credit_reduced", "id": sid, "delta_cents": delta,
            "total_credit_cents": after, "changes": changes}


@op("refund")
def _op_refund(db, x):
    """Establish (and optionally receive) a carrier refund receivable."""
    rid = x["id"]
    if _row(db, "SELECT * FROM ts_notes WHERE id=?", (rid,)):
        return {"decision": "duplicate", "id": rid, "duplicate": True}
    bill = _row(db, "SELECT * FROM ts_notes WHERE id=?", (x["bill_id"],))
    if not bill:
        return {"decision": "unknown_bill"}
    amount = int(x["amount_cents"])
    at = int(x.get("at", 0))
    ob_id = x.get("obligation_id") or ("ob-refund-%s" % rid)
    _ensure_obligation(db, ob_id, "carrier_refund_receivable", bill["party"],
                       bill["period"], bill["incident"],
                       natural_key="refund:%s" % rid)
    b, a = _post_item(db, "item-%s" % rid, ob_id, amount, at,
                      component="carrier_refund", reason=x.get("reason"),
                      evidence=x.get("evidence"), period=bill["period"],
                      window_start=x.get("window_start"),
                      window_end=x.get("window_end"))
    _note(db, rid, "carrier_refund", bill["party"], amount, at,
          period=bill["period"], incident=bill["incident"],
          ref_id=x["bill_id"], obligation=ob_id,
          payload={"reason": x.get("reason")})
    change = _adjust(db, x, ob_id, "carrier_refund_receivable", b, a,
                     "carrier refund established", period=bill["period"],
                     window_start=x.get("window_start"),
                     window_end=x.get("window_end"),
                     evidence=x.get("evidence"))
    result = {"decision": "refund_established", "id": rid,
              "amount_cents": amount, "obligation": ob_id, "changes": [change]}
    if x.get("receive_id") or x.get("receive_now"):
        recv_id = x.get("receive_id") or ("recv-%s" % rid)
        pay = _pay(db, {"id": recv_id, "obligation_id": ob_id,
                        "amount_cents": amount, "at": at, "way": "in"})
        if pay.get("decision") == "settled":
            db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (rid,))
        result["receipt"] = pay
    return result


@op("receive_refund")
def _op_receive_refund(db, x):
    note = _row(db, "SELECT * FROM ts_notes WHERE id=? AND kind='carrier_refund'",
                (x["refund_id"],))
    if not note:
        return {"decision": "unknown_refund"}
    res = _pay(db, {"id": x["id"], "obligation_id": note["obligation"],
                    "amount_cents": x.get("amount_cents", note["amount_cents"]),
                    "at": x.get("at", note["at"]), "way": "in"})
    if res.get("decision") == "settled":
        db.execute("UPDATE ts_notes SET status='paid' WHERE id=?", (x["refund_id"],))
    return res


@op("recovery")
def _op_recovery(db, x):
    """Establish/revise the fault-party recovery obligation explicitly."""
    incident = x["incident"]
    fault = _row(db, "SELECT * FROM ts_incidents WHERE id=?", (incident,))
    party = x.get("fault_carrier") or (fault["fault_carrier"] if fault else "a")
    rec_id = x.get("obligation_id") or ("rec-%s" % incident)
    period = x.get("period")
    ob = _ensure_obligation(db, rec_id, "recovery_receivable", party, period,
                            incident, natural_key="recovery:%s:%s" % (party, incident))
    target = int(x["amount_cents"])
    delta = target - int(ob["amount_cents"])
    if delta == 0:
        return {"decision": "unchanged", "obligation": rec_id,
                "amount_cents": target}
    item_id = x.get("item_id") or ("item-rec-%s-%s" % (incident, _next_seq(db)))
    b, a = _post_item(db, item_id, rec_id, delta, int(x.get("at", 0)),
                      component=x.get("component", "fault_cost"),
                      reason=x.get("reason"), evidence=x.get("evidence"),
                      period=period, window_start=x.get("window_start"),
                      window_end=x.get("window_end"))
    change = _adjust(db, x, rec_id, "recovery_receivable", b, a,
                     x.get("reason", "recovery established/revised"),
                     period=period, window_start=x.get("window_start"),
                     window_end=x.get("window_end"),
                     evidence=x.get("evidence"))
    return {"decision": "recovery_revised", "obligation": rec_id,
            "before_cents": b, "after_cents": a, "delta_cents": delta,
            "changes": [change]}


@op("receive_recovery")
def _op_receive_recovery(db, x):
    rec_id = x.get("obligation_id") or ("rec-%s" % x["incident"])
    return _pay(db, {"id": x["id"], "obligation_id": rec_id,
                     "amount_cents": x.get("amount_cents"),
                     "at": x.get("at", 0), "way": "in"})


@op("adjudicate")
def _op_adjudicate(db, x):
    """Apply review findings: final carrier charge, refunds and A recovery.

    Idempotent on ``id``.  Expected keys:

    * ``carrier`` (default ``b``), ``bill_id``, ``final_charge_cents``;
    * ``butfor_cents`` -- what the carrier charge would have been without
      the failover;
    * ``customer_credit_cents`` -- total customer credits caused by the
      incident (added to the recovery claim);
    * optional explicit ``refund_id`` / supplemental payment ids.
    """
    rid = x["id"]
    old = _row(db, "SELECT * FROM ts_notes WHERE id=?", (rid,))
    if old:
        return {"decision": "duplicate", "id": rid, "duplicate": True}
    at = int(x.get("at", 0))
    carrier = x.get("carrier", "b")
    bill = _row(db, "SELECT * FROM ts_notes WHERE id=?", (x["bill_id"],))
    if not bill:
        return {"decision": "unknown_bill"}
    final_charge = int(x["final_charge_cents"])
    paid = int(_get_obligation(db, bill["obligation"])["settled_cents"])
    changes = []
    outcomes = {}

    if paid > final_charge:
        refund_amount = paid - final_charge
        refund_id = x.get("refund_id") or ("refund-%s" % rid)
        r = _op_refund(db, {"id": refund_id, "bill_id": bill["id"],
                            "amount_cents": refund_amount, "at": at,
                            "reason": x.get("reason", "adjudicated overcharge"),
                            "evidence": x.get("evidence"),
                            "window_start": x.get("window_start"),
                            "window_end": x.get("window_end"),
                            "receive_id": x.get("receive_refund_id"),
                            "adjustment_id": "adj-refund-%s" % rid})
        outcomes["refund"] = r
        changes.extend(r.get("changes", []))
    elif final_charge > paid:
        supp_id = x.get("supplement_id") or ("supp-%s" % rid)
        ob = bill["obligation"]
        b, a = _post_item(db, "item-%s" % supp_id, ob, final_charge - paid, at,
                          component="carrier_supplement",
                          reason="adjudicated additional charge",
                          evidence=x.get("evidence"), period=bill["period"],
                          window_start=x.get("window_start"),
                          window_end=x.get("window_end"))
        _note(db, supp_id, "carrier_supplement", bill["party"],
              final_charge - paid, at, period=bill["period"],
              incident=bill["incident"], ref_id=bill["id"], obligation=ob)
        ch = _adjust(db, x, ob, "carrier_payable", b, a,
                     "supplemental carrier charge after review",
                     period=bill["period"], window_start=x.get("window_start"),
                     window_end=x.get("window_end"), evidence=x.get("evidence"),
                     adjustment_id="adj-supp-%s" % rid)
        changes.append(ch)
        outcomes["supplement_cents"] = final_charge - paid

    # final fault-party recovery = true carrier differential + credits
    butfor = int(x.get("butfor_cents", 0))
    credits = int(x.get("customer_credit_cents", 0))
    final_recovery = max(0, final_charge - butfor) + credits
    incident = bill["incident"] or x.get("incident")
    if incident:
        fault = _row(db, "SELECT * FROM ts_incidents WHERE id=?", (incident,))
        party = x.get("fault_carrier") or (fault["fault_carrier"] if fault else "a")
        rec_id = x.get("recovery_id") or ("rec-%s" % incident)
        ob = _ensure_obligation(
            db, rec_id, "recovery_receivable", party, bill["period"], incident,
            natural_key="recovery:%s:%s" % (party, incident))
        current = int(ob["amount_cents"])
        delta = final_recovery - current
        if delta != 0:
            b, a = _post_item(
                db, "item-%s-review" % rid, rec_id, delta, at,
                component="fault_cost_final",
                reason="final recovery: carrier differential %d + credits %d"
                       % (max(0, final_charge - butfor), credits),
                evidence=x.get("evidence"), period=bill["period"],
                window_start=x.get("window_start"),
                window_end=x.get("window_end"))
            ch = _adjust(db, x, rec_id, "recovery_receivable", b, a,
                         "adjudicated recovery (differential+credits)",
                         period=bill["period"],
                         window_start=x.get("window_start"),
                         window_end=x.get("window_end"),
                         evidence=x.get("evidence"),
                         adjustment_id="adj-rec-%s" % rid)
            changes.append(ch)
        outcomes["recovery_final_cents"] = final_recovery
        outcomes["recovery_before_cents"] = current
        already = int(ob["settled_cents"])
        if already > final_recovery:
            # The fault carrier already paid the larger provisional amount;
            # the excess is a separate payable back to them so the receivable
            # never sits over-settled and the correction is fully auditable.
            excess = already - final_recovery
            ret_id = x.get("recovery_return_id") or ("ret-%s" % rid)
            ret_ob = x.get("recovery_return_obligation") or ("ob-%s" % ret_id)
            _ensure_obligation(
                db, ret_ob, "recovery_return_payable", party, bill["period"],
                incident, natural_key="recovery-return:%s" % rid)
            rb, ra = _post_item(
                db, "item-%s" % ret_id, ret_ob, excess, at,
                component="provisional_recovery_return",
                reason="return excess provisional fault recovery after review",
                period=bill["period"], window_start=x.get("window_start"),
                window_end=x.get("window_end"))
            _note(db, ret_id, "recovery_return", party, excess, at,
                  period=bill["period"], incident=incident, ref_id=rec_id,
                  obligation=ret_ob)
            ch = _adjust(db, x, ret_ob, "recovery_return_payable", rb, ra,
                         "return over-collected provisional recovery",
                         period=bill["period"],
                         window_start=x.get("window_start"),
                         window_end=x.get("window_end"),
                         adjustment_id="adj-ret-%s" % rid)
            changes.append(ch)
            outcomes["recovery_return_cents"] = excess

    if x.get("final_high_bytes") is not None:
        c = _contract(db, carrier, bill["period"]) or {"unit_bytes": 1}
        unit = max(1, c["unit_bytes"])
        final_units = int(x["final_high_bytes"]) // unit
        db.execute(
            """INSERT INTO ts_quota(carrier,period,confirmed_units,ceiling_units,
                   updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(carrier,period) DO UPDATE SET
                 confirmed_units=excluded.confirmed_units,
                 ceiling_units=excluded.ceiling_units,
                 updated_at=excluded.updated_at""",
            (carrier, bill["period"], final_units, final_units, at),
        )
        outcomes["final_peak_units"] = final_units

    _note(db, rid, "review_decision", carrier, final_charge, at,
          period=bill["period"], incident=incident, ref_id=bill["id"],
          payload={"final_charge_cents": final_charge, "butfor_cents": butfor,
                   "customer_credit_cents": credits})
    return {"decision": "adjudicated", "id": rid,
            "final_charge_cents": final_charge, "changes": changes, **outcomes}


@op("summary")
def _op_summary(db, x):
    period = x.get("period")
    q = "SELECT * FROM ts_obligations"
    a = ()
    if period:
        q += " WHERE period=?"
        a = (period,)
    obligations = _rows(db, q + " ORDER BY id", a)
    for o in obligations:
        o["remaining_cents"] = o["amount_cents"] - o["settled_cents"]
    notes = _rows(db, "SELECT * FROM ts_notes ORDER BY at,id")
    adjustments = _rows(db, "SELECT * FROM ts_adjustments ORDER BY seq")
    quota = _rows(db, "SELECT * FROM ts_quota ORDER BY carrier,period")

    def sum_kind(kind, field):
        return sum(int(o[field]) for o in obligations if o["kind"] == kind)

    cash_out = sum(f["amount_cents"] for f in _rows(db, "SELECT * FROM ts_flows WHERE way='out'"))
    cash_in = sum(f["amount_cents"] for f in _rows(db, "SELECT * FROM ts_flows WHERE way='in'"))
    by_kind = {}
    for o in obligations:
        k = o["kind"]
        d = by_kind.setdefault(k, {"amount_cents": 0, "settled_cents": 0,
                                   "remaining_cents": 0, "count": 0})
        d["amount_cents"] += o["amount_cents"]
        d["settled_cents"] += o["settled_cents"]
        d["remaining_cents"] += o["amount_cents"] - o["settled_cents"]
        d["count"] += 1
    return {"decision": "summary", "period": period,
            "by_kind": by_kind,
            "obligations": obligations,
            "notes": notes,
            "adjustments": adjustments,
            "quota": quota,
            "cash_out_cents": cash_out,
            "cash_in_cents": cash_in,
            "net_cash_cents": cash_in - cash_out}


@op("windows")
def _op_windows(db, x):
    return _carrier_window_report(db, x["carrier"], x.get("start"),
                                  x.get("end"), x.get("period"))


_OP_ALIASES = {
    "add_port": "port", "register_port": "port",
    "add_reading": "reading", "record_reading": "reading", "meter": "reading",
    "add_contract": "contract", "carrier_contract": "contract",
    "add_sla": "sla", "sla_contract": "sla", "customer_contract": "sla",
    "add_claim": "claim", "carrier_claim": "claim",
    "add_bound": "bound", "probe_bound": "bound", "add_probe_bound": "bound",
    "calibrate_clock": "clock", "clock_sync": "clock", "add_clock": "clock",
    "open_incident": "incident", "failover": "incident", "add_incident": "incident",
    "add_impact": "impact", "loss": "impact", "loss_interval": "impact",
    "settle": "settle_carrier", "issue_bill": "settle_carrier",
    "pay_bill": "pay", "pay_carrier": "pay", "pay_debt": "pay",
    "grant_credit": "credit", "issue_credit": "credit", "add_credit": "credit",
    "correct_credit": "revise_credit", "supplement_credit": "revise_credit",
    "issue_refund": "refund", "add_refund": "refund",
    "charge_fault_party": "recovery", "establish_recovery": "recovery",
    "recover": "receive_recovery", "receive_fault_payment": "receive_recovery",
    "review": "adjudicate", "apply_review": "adjudicate", "finalize": "adjudicate",
    "statement": "summary", "status": "summary",
    "report_windows": "windows",
}


def run(db, x):
    if not isinstance(x, dict) or "op" not in x:
        raise ValueError("transit_settlement.run expects {'op': ...}")
    init(db)
    name = _OP_ALIASES.get(x["op"], x["op"])
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError("unknown transit_settlement op: %r" % x["op"])
    return handler(db, x)


_HANDLERS = {
    "port": _op_port,
    "reading": _op_reading,
    "contract": _op_contract,
    "sla": _op_sla,
    "claim": _op_claim,
    "bound": _op_bound,
    "clock": _op_clock,
    "incident": _op_incident,
    "impact": _op_impact,
    "settle_carrier": _op_settle_carrier,
    "pay": _op_pay,
    "credit": _op_credit,
    "pay_credit": _op_pay_credit,
    "revise_credit": _op_revise_credit,
    "refund": _op_refund,
    "receive_refund": _op_receive_refund,
    "recovery": _op_recovery,
    "receive_recovery": _op_receive_recovery,
    "adjudicate": _op_adjudicate,
    "summary": _op_summary,
    "windows": _op_windows,
}


def settle(db, payload=None, **kw):
    """Functional entry point: ``settle(db, {'op': 'summary'})``.

    Keyword arguments are merged into the payload, so
    ``settle(db, op='summary')`` works as well.
    """
    x = dict(payload or {})
    x.update(kw)
    return run(db, x)


def new_settlement(db=":memory:"):
    """Convenience constructor used as ``from netcourt.transit_settlement import ...``."""
    return TransitSettlement(db)


# A few friendly aliases for callers exploring the module.
Settlement = TransitSettlement
Ledger = TransitSettlement
SettlementLedger = TransitSettlement
Engine = TransitSettlement


def settle_case(case):
    """Run a whole plain-dict case and return the final summary.

    ``case`` may contain ``ports, readings, contracts, slas, claims, bounds,
    clocks, incidents, impacts, commands`` where each collection item is an op
    payload (its ``op`` inferred from the collection name).  ``commands`` are
    executed last verbatim.  The final value of the last command is merged
    into the returned ``summary`` under ``last_result``.
    """
    ts = TransitSettlement(":memory:")
    plan = [
        ("ports", "port"), ("contracts", "contract"), ("slas", "sla"),
        ("clocks", "clock"), ("incidents", "incident"),
        ("readings", "reading"), ("claims", "claim"), ("bounds", "bound"),
        ("impacts", "impact"),
    ]
    last = None
    for key, opname in plan:
        for item in case.get(key, []):
            last = ts.run(opname, **item)
    for cmd in case.get("commands", []):
        cmd = dict(cmd)
        opname = cmd.pop("op", None)
        if opname is None:
            raise ValueError("command is missing 'op': %r" % cmd)
        last = ts.run(opname, **cmd)
    summary = ts.run("summary")
    summary["last_result"] = last
    return summary


__all__ = [
    "WINDOW_SECONDS", "SCHEMA", "init", "run", "settle", "TransitSettlement",
    "Settlement", "Ledger", "SettlementLedger", "Engine",
    "new_settlement", "settle_case",
    "counter_edges", "allocate_edge", "window_bytes", "mirror_attribution",
    "reconcile_window", "monthly_high", "nearest_rank_high",
    "charge_envelope", "availability_credit_cents", "merge_intervals",
    "period_key", "period_range", "ceil_div",
]
