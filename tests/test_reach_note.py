import unittest

from netcourt.store import connect
from netcourt.public_alert import submit
from netcourt.reach_note import provisional_warned


class ReachNoteTest(unittest.TestCase):
    def test_submitted_warning_marks_region_provisionally_warned(self):
        db = connect(":memory:")
        submit(db, "a1", "region-1", "digest-1", 10)
        self.assertTrue(provisional_warned(db, "region-1"))
        db.close()


if __name__ == "__main__":
    unittest.main()
