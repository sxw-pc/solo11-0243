import unittest

from netcourt.store import connect
from netcourt.physical_route import record
from netcourt.delivery_order import provisional_delivery


class DeliveryOrderTest(unittest.TestCase):
    def test_two_named_routes_appear_deliverable_even_when_duct_shared(self):
        db = connect(":memory:")
        record(db, "svc-1", "carrier-a-17", ["well-x", "duct-4"], 100)
        record(db, "svc-1", "carrier-b-88", ["duct-4", "well-y"], 100)
        self.assertEqual(
            provisional_delivery(db, "svc-1", 150),
            {"deliverable": True, "route_count": 2},
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
