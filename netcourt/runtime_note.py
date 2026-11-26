"""Provisional site runtime from controller estimates."""


def provisional_runtime(db, site):
    return db.execute(
        "SELECT COALESCE(SUM(minutes),0) FROM ups_reports WHERE site=?", (site,)
    ).fetchone()[0]
