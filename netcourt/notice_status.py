"""Provisional notice status from queue rows."""


def provisional_notified(db, customer, revision):
    return db.execute(
        "SELECT COUNT(*) FROM notice_queue WHERE customer=? AND revision=?",
        (customer, revision),
    ).fetchone()[0] > 0
