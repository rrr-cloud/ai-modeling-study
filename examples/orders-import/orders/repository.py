"""注文テーブルへの読み書き（既存コード）。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    status      TEXT NOT NULL,
    ordered_at  TEXT NOT NULL
)
"""


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(SCHEMA)

    def upsert_many(self, rows: list[dict]) -> None:
        """order_id が同じ行は上書きする。"""
        self.conn.executemany(
            """
            INSERT INTO orders (order_id, customer_id, amount, status, ordered_at)
            VALUES (:order_id, :customer_id, :amount, :status, :ordered_at)
            ON CONFLICT(order_id) DO UPDATE SET
                customer_id = excluded.customer_id,
                amount      = excluded.amount,
                status      = excluded.status,
                ordered_at  = excluded.ordered_at
            """,
            rows,
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def get(self, order_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT order_id, customer_id, amount, status, ordered_at FROM orders WHERE order_id = ?",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip(["order_id", "customer_id", "amount", "status", "ordered_at"], row))
