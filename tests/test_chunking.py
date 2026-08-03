import unittest

from app.chunking import partition_words, plan_chunks

GB = 1024 ** 3


class ChunkingTest(unittest.TestCase):
    def test_small_file_no_chunk(self):
        plan = plan_chunks(3600, 500 * 1024 * 1024, 2 * GB)
        self.assertEqual(plan.num_chunks, 1)
        self.assertFalse(plan.needs_chunking)

    def test_large_file_chunks(self):
        # 6 GB of audio at ~32 KB/s == ~6 hours
        plan = plan_chunks(6 * 3600, 6 * GB, 2 * GB)
        self.assertEqual(plan.num_chunks, 3)
        self.assertTrue(plan.needs_chunking)
        self.assertAlmostEqual(plan.chunk_duration, 2 * 3600)

    def test_exact_boundary(self):
        plan = plan_chunks(7200, 2 * GB, 2 * GB)
        self.assertEqual(plan.num_chunks, 1)

    def test_partition_even(self):
        parts = partition_words(list("abcdefghij"), 3)
        self.assertEqual([len(p) for p in parts], [4, 3, 3])

    def test_partition_single(self):
        parts = partition_words(list("abcdef"), 1)
        self.assertEqual(len(parts[0]), 6)

    def test_partition_fewer_words_than_chunks(self):
        parts = partition_words(list("ab"), 4)
        self.assertTrue(all(len(p) >= 1 for p in parts))
        self.assertEqual(sum(len(p) for p in parts), 2)


if __name__ == "__main__":
    unittest.main()
