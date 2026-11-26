"""Unverified packet sample references from traffic probes."""


def record(db, sample, observation, digest):
    db.execute(
        "CREATE TABLE IF NOT EXISTS packet_samples(sample TEXT PRIMARY KEY,observation TEXT,digest TEXT)"
    )
    db.execute("INSERT INTO packet_samples VALUES(?,?,?)", (sample, observation, digest))
