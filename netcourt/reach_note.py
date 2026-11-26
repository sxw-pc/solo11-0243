"""Provisional warning reach based on carrier submission."""


def provisional_warned(db, region):
    return db.execute(
        "SELECT COUNT(*) FROM warning_requests WHERE region=?", (region,)
    ).fetchone()[0] > 0
