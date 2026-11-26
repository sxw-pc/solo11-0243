import unittest

from netcourt.store import connect
from netcourt.transit_meter import record
from netcourt.transit_bill import provisional_charge


class TransitBillTest(unittest.TestCase):
    def test_raw_reading_used_for_provisional_charge(self):
        db = connect(":memory:")
        record(db, "m1", "edge-a", "carrier-a", 11, 10)
        self.assertEqual(provisional_charge(db, "carrier-a", 3), 33)
        db.close()


if __name__ == "__main__":
    unittest.main()
