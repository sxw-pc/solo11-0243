"""Provisional warning reach based on carrier submission."""


def provisional_warned(db, region):
    # 全新数据库上队列表可能尚未创建，此时“尚无提交”，结论为 False。
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='warning_requests'"
    ).fetchone()
    if not exists:
        return False
    return db.execute(
        "SELECT COUNT(*) FROM warning_requests WHERE region=?", (region,)
    ).fetchone()[0] > 0
