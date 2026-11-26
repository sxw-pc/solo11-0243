import unittest

from netcourt.store import connect
from netcourt.ups_sensor import report
from netcourt.runtime_note import provisional_runtime


class RuntimeNoteTest(unittest.TestCase):
    def test_controller_estimate_increases_provisional_runtime(self):
        db = connect(":memory:")
        report(db, "r1", "edge-site", "pack-a", 18, 10)
        self.assertEqual(provisional_runtime(db, "edge-site"), 18)
        db.close()


if __name__ == "__main__":
    unittest.main()
