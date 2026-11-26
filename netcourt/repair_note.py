"""Provisional node repair from dispatched spare count."""


def provisional_repaired(db, node):
    return db.execute(
        "SELECT COUNT(*) FROM spare_shipments WHERE node=?", (node,)
    ).fetchone()[0] > 0
