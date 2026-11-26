import unittest

from netcourt.store import connect
from netcourt.spectrum_scan import sample
from netcourt.channel_note import provisional_channel


class ChannelNoteTest(unittest.TestCase):
    def test_quietest_local_sample_is_provisional_channel(self):
        db = connect(":memory:")
        sample(db, "s1", "tower-a", "band-x", 19, 10)
        sample(db, "s2", "tower-a", "band-y", 4, 10)
        self.assertEqual(provisional_channel(db, "tower-a"), "band-y")
        db.close()


if __name__ == "__main__":
    unittest.main()
