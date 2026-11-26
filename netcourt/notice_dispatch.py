"""Customer notices submitted to a delivery service."""


def enqueue(db, notice, customer, revision, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS notice_queue(notice TEXT PRIMARY KEY,customer TEXT,revision TEXT,at INTEGER)"
    )
    db.execute(
        "INSERT INTO notice_queue VALUES(?,?,?,?)", (notice, customer, revision, at)
    )
