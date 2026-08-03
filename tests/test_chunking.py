import unittest

from app.chunking import partition_words, partition_words_by_weights, plan_chunks

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

    def test_weighted_partition_proportional(self):
        # Real segment durations from a silence-snapped chapter (see
        # segment.plan_segments); weights are unequal on purpose.
        words = [f"w{i}" for i in range(100)]
        weights = [36.70, 33.31, 27.63, 33.45, 19.97]
        parts = partition_words_by_weights(words, weights)
        self.assertEqual(len(parts), len(weights))
        # Every word used exactly once, in order, no duplicates/gaps.
        self.assertEqual(sum(len(p) for p in parts), len(words))
        self.assertEqual([w for p in parts for w in p], words)
        # Each part's share should roughly track its weight's share.
        total_w = sum(weights)
        for part, w in zip(parts, weights):
            expected = len(words) * w / total_w
            self.assertLess(abs(len(part) - expected), 3)

    def test_weighted_partition_single_weight(self):
        words = list("abcdef")
        parts = partition_words_by_weights(words, [42.0])
        self.assertEqual(parts, [words])

    def test_weighted_partition_empty_weights(self):
        self.assertEqual(partition_words_by_weights(list("abc"), []), [])

    def test_weighted_partition_zero_weights_falls_back_uniform(self):
        parts = partition_words_by_weights(list("abcdefgh"), [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(len(parts), 4)
        self.assertEqual(sum(len(p) for p in parts), 8)

    def test_weighted_partition_fewer_words_than_weights(self):
        parts = partition_words_by_weights(list("ab"), [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(len(parts), 4)
        self.assertEqual(sum(len(p) for p in parts), 2)

    def test_weighted_partition_negative_weight_clamped(self):
        # A negative weight (shouldn't occur in practice) must not produce a
        # negative-length slice or crash.
        words = list("abcdefgh")
        parts = partition_words_by_weights(words, [10.0, -5.0, 10.0])
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) >= 0 for p in parts))
        self.assertEqual(sum(len(p) for p in parts), len(words))
        self.assertEqual([w for p in parts for w in p], words)


if __name__ == "__main__":
    unittest.main()
