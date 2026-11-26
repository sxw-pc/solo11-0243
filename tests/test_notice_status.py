import unittest

from netcourt.store import connect
from netcourt.notice_dispatch import enqueue
from netcourt.notice_status import provisional_notified


class NoticeStatusTest(unittest.TestCase):
    def test_queued_notice_is_provisionally_notified(self):
        db = connect(":memory:")
        enqueue(db, "n1", "customer-x", "revision-1", 10)
        self.assertTrue(provisional_notified(db, "customer-x", "revision-1"))
        db.close()


if __name__ == "__main__":
    unittest.main()
