"""Field spectrum samples around a wireless backhaul site."""


def sample(db, sample_id, site, channel, noise, at):
    db.execute(
        "CREATE TABLE IF NOT EXISTS radio_samples(sample_id TEXT PRIMARY KEY,site TEXT,channel TEXT,noise INTEGER,at INTEGER)"
    )
    db.execute(
        "INSERT INTO radio_samples VALUES(?,?,?,?,?)",
        (sample_id, site, channel, noise, at),
    )
