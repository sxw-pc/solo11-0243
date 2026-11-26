"""Emergency warning requests submitted to a carrier."""


def submit(db, alert, region, message_digest, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS warning_requests(alert TEXT PRIMARY KEY,region TEXT,message_digest TEXT,at INTEGER)"
    )
    db.execute(
        "INSERT INTO warning_requests VALUES(?,?,?,?)",
        (alert, region, message_digest, at),
    )
