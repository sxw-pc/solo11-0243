import unittest

from netcourt.store import connect
from netcourt.clock_probe import sample
from netcourt.time_note import provisional_accurate


class TimeNoteTest(unittest.TestCase):
    def test_opposite_bad_offsets_cancel_in_provisional_average(self):
        db = connect(":memory:")
        sample(db, "s1", "edge-1", "source-a", 40, 10)
        sample(db, "s2", "edge-1", "source-b", -40, 10)
        self.assertTrue(provisional_accurate(db, "edge-1", 5))
        db.close()


if __name__ == "__main__":
    unittest.main()
