"""Provisional delivery from route names and nominal capacity."""


def provisional_delivery(db, service, requested_capacity):
    rows = db.execute(
        "SELECT route,capacity FROM field_routes WHERE service=?", (service,)
    ).fetchall()
    return {
        "deliverable": len(rows) >= 2 and sum(row[1] for row in rows) >= requested_capacity,
        "route_count": len(rows),
    }
