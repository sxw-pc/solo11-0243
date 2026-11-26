"""Network time probe offsets from edge nodes."""


def sample(db, sample_id, node, source, offset_us, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS clock_samples(sample_id TEXT PRIMARY KEY,node TEXT,source TEXT,offset_us INTEGER,at INTEGER)"
    )
    db.execute(
        "INSERT INTO clock_samples VALUES(?,?,?,?,?)",
        (sample_id, node, source, offset_us, at),
    )
