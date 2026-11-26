"""Raw transit-port counter submissions."""


def record(db, reading, port, provider, bytes_total, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS transit_readings(reading TEXT PRIMARY KEY,port TEXT,provider TEXT,bytes_total INTEGER,at INTEGER)"
    )
    db.execute(
        "INSERT INTO transit_readings VALUES(?,?,?,?,?)",
        (reading, port, provider, bytes_total, at),
    )
