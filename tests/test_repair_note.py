import unittest

from netcourt.store import connect
from netcourt.spare_stock import dispatch
from netcourt.repair_note import provisional_repaired


class RepairNoteTest(unittest.TestCase):
    def test_dispatch_marks_node_provisionally_repaired(self):
        db = connect(":memory:")
        dispatch(db, "ship-1", "serial-a", "edge-1", 12)
        self.assertTrue(provisional_repaired(db, "edge-1"))
        db.close()


if __name__ == "__main__":
    unittest.main()
