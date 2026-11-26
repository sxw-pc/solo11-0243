import unittest

from netcourt.store import connect
from netcourt.notice_dispatch import enqueue
from netcourt.maintenance_start import provisional_start


class MaintenanceStartTest(unittest.TestCase):
    def test_queued_notices_permit_provisional_start(self):
        db = connect(":memory:")
        enqueue(db, "n1", "customer-a", "rev-1", 10)
        enqueue(db, "n2", "customer-b", "rev-1", 10)
        self.assertTrue(provisional_start(db, ["customer-a", "customer-b"], "rev-1"))
        db.close()


if __name__ == "__main__":
    unittest.main()
