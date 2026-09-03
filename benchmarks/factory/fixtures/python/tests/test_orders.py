import unittest

from orders import line_total, total


class SmokeTests(unittest.TestCase):
    def test_total_without_discount(self):
        self.assertEqual(total([5, 7]), 12)

    def test_quantity(self):
        self.assertEqual(line_total(4, 3), 12)
