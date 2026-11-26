"""Declared network spare shipments."""


def dispatch(db, shipment, serial, node, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS spare_shipments(shipment TEXT PRIMARY KEY,serial TEXT,node TEXT,at INTEGER)"
    )
    db.execute(
        "INSERT INTO spare_shipments VALUES(?,?,?,?)",
        (shipment, serial, node, at),
    )
