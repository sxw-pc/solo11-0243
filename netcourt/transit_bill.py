"""Provisional transit charge from raw counters."""


def provisional_charge(db, provider, rate):
    total = db.execute(
        "SELECT COALESCE(SUM(bytes_total),0) FROM transit_readings WHERE provider=?",
        (provider,),
    ).fetchone()[0]
    return total * rate
