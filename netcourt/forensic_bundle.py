"""Provisional evidence packet from observation and sample counts."""


def provisional_bundle(db, rollout):
    return db.execute(
        "SELECT COUNT(*) FROM packet_samples p JOIN observations o ON o.id=p.observation WHERE o.rollout=?",
        (rollout,),
    ).fetchone()[0]
