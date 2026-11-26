"""Provisional time compliance from average offset."""


def provisional_accurate(db, node, limit_us):
    row = db.execute(
        "SELECT AVG(offset_us) FROM clock_samples WHERE node=?", (node,)
    ).fetchone()[0]
    return row is not None and abs(row) <= limit_us
