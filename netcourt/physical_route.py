"""Field-reported carrier routes for a contracted service."""
import json


def record(db, service, route, ducts, capacity):
    db.execute(
        "CREATE TABLE IF NOT EXISTS field_routes(service TEXT, route TEXT PRIMARY KEY, ducts TEXT, capacity INTEGER)"
    )
    db.execute(
        "INSERT INTO field_routes VALUES(?,?,?,?)",
        (service, route, json.dumps(ducts), capacity),
    )
