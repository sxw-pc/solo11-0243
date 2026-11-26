"""Provisional channel choice from the quietest local sample."""


def provisional_channel(db, site):
    row = db.execute(
        "SELECT channel FROM radio_samples WHERE site=? ORDER BY noise ASC LIMIT 1",
        (site,),
    ).fetchone()
    return row[0] if row else None
