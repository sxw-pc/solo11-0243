"""Edge-site battery runtime estimates from local controllers."""


def report(db, report_id, site, pack, minutes, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS ups_reports(report_id TEXT PRIMARY KEY,site TEXT,pack TEXT,minutes INTEGER,at INTEGER)"
    )
    db.execute(
        "INSERT INTO ups_reports VALUES(?,?,?,?,?)",
        (report_id, site, pack, minutes, at),
    )
