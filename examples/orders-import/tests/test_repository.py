import sqlite3
import unittest

from orders.repository import OrderRepository


class OrderRepositoryTest(unittest.TestCase):
    def test_upsert_overwrites_same_order_id(self):
        repo = OrderRepository(sqlite3.connect(":memory:"))
        row = {"order_id": "A1", "customer_id": "C1", "amount": 100, "status": "paid", "ordered_at": "2026-11-01"}
        repo.upsert_many([row])
        repo.upsert_many([{**row, "amount": 200}])
        self.assertEqual(repo.count(), 1)
        self.assertEqual(repo.get("A1")["amount"], 200)


if __name__ == "__main__":
    unittest.main()
