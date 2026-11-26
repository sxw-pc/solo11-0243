import unittest

from netcourt.store import connect
from netcourt.packet_sample import record
from netcourt.forensic_bundle import provisional_bundle


class ForensicBundleTest(unittest.TestCase):
    def test_sample_linked_to_observation_counts_as_provisional_evidence(self):
        db = connect(":memory:")
        db.execute(
            "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
            ("o1", "roll-1", "a", "b", 443, "tcp", 1, 30),
        )
        record(db, "s1", "o1", "digest-1")
        self.assertEqual(provisional_bundle(db, "roll-1"), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
