"""注文CSVを orders テーブルへ取り込む（仕様は spec.md）。

呼び出し方: python -m orders.import_csv <CSVのパス> <DBのパス>
"""
import argparse
import csv
import datetime
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass

from orders.repository import OrderRepository

logger = logging.getLogger(__name__)

COLUMNS = ("order_id", "customer_id", "amount", "status", "ordered_at")
STATUSES = frozenset({"paid", "pending", "cancelled"})
EXCLUDED_STATUS = "cancelled"  # 正しい値だが取り込まない（対象外）
AMOUNT_MAX = 2**63 - 1  # SQLite の INTEGER に入る上限

_AMOUNT_RE = re.compile(r"[0-9]+")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ImportFileError(Exception):
    """ファイル全体を取り込めないエラー（1 件も取り込まない）。"""


@dataclass(frozen=True)
class ImportResult:
    imported: int
    skipped: int   # 不正な行
    excluded: int  # 対象外の行（status が cancelled）


def import_file(csv_path: str, repo: OrderRepository) -> ImportResult:
    """CSV を全行検証してから 1 回で upsert し、件数を返す。

    不正な行はスキップして行番号を WARNING で出す。
    status が cancelled の行は対象外として数えるだけで、DB には触れない。
    ファイル全体を読めないときは、何も書き込まずに ImportFileError を送出する。
    """
    rows, skipped, excluded = _read_rows(csv_path)
    repo.upsert_many(rows)
    result = ImportResult(imported=len(rows), skipped=skipped, excluded=excluded)
    logger.info(
        "取り込んだ件数: %d、スキップした件数: %d、対象外の件数: %d",
        result.imported, result.skipped, result.excluded,
    )
    return result


def _read_rows(csv_path: str) -> tuple[list[dict], int, int]:
    """(取り込む行, 不正な行の件数, 対象外の行の件数) を返す。"""
    rows: list[dict] = []
    skipped = 0
    excluded = 0
    try:
        # utf-8-sig は先頭の BOM があれば取り除き、無ければ UTF-8 として読む
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                raise ImportFileError(f"ヘッダーがありません: {csv_path}")
            index = _column_index(header)
            width = len(header)

            while True:
                # 改行を含む値は複数行にまたがるので、始まりの行を行番号にする
                line_no = reader.line_num + 1
                record = next(reader, None)
                if record is None:
                    break
                if not record:
                    continue  # 空行（ファイル末尾の余分な改行など）は数えない
                row, reason = _parse_row(record, index, width)
                if row is None:
                    skipped += 1
                    logger.warning("%d 行目をスキップしました: %s", line_no, reason)
                elif row["status"] == EXCLUDED_STATUS:
                    excluded += 1  # 検証は通った行だけを対象外に数える。WARNING には出さない
                else:
                    rows.append(row)
    except OSError as e:
        raise ImportFileError(f"CSV ファイルを開けません: {csv_path} ({e})") from e
    except UnicodeDecodeError as e:
        raise ImportFileError(f"CSV ファイルを UTF-8 として読めません: {csv_path} ({e})") from e
    except csv.Error as e:
        raise ImportFileError(f"CSV として読めません: {csv_path} ({e})") from e
    return rows, skipped, excluded


def _column_index(header: list[str]) -> dict[str, int]:
    names = [name.strip() for name in header]
    missing = [c for c in COLUMNS if c not in names]
    if missing:
        raise ImportFileError("ヘッダーに列がありません: " + ", ".join(missing))
    duplicated = [c for c in COLUMNS if names.count(c) > 1]
    if duplicated:
        raise ImportFileError("ヘッダーに同じ列が複数あります: " + ", ".join(duplicated))
    return {c: names.index(c) for c in COLUMNS}


def _parse_row(record: list[str], index: dict[str, int], width: int) -> tuple[dict | None, str]:
    """正しい行なら (dict, "")、不正な行なら (None, 理由) を返す。"""
    if len(record) != width:
        return None, f"列の数がヘッダーと合いません（{len(record)} 列、ヘッダーは {width} 列）"

    row = {c: record[i].strip() for c, i in index.items()}
    for c in COLUMNS:
        if not row[c]:
            return None, f"{c} が空です"

    amount = row["amount"]
    if not _AMOUNT_RE.fullmatch(amount) or int(amount) > AMOUNT_MAX:
        return None, f"amount が 0 以上の整数ではありません: {amount!r}"
    if row["status"] not in STATUSES:
        return None, f"status が paid / pending / cancelled のどれでもありません: {row['status']!r}"
    if not _is_date(row["ordered_at"]):
        return None, f"ordered_at が YYYY-MM-DD の形式ではありません: {row['ordered_at']!r}"

    row["amount"] = int(amount)
    return row, ""


def _is_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False  # 2026-02-30 のように暦に無い日付
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orders.import_csv",
        description="注文CSVを orders テーブルへ取り込みます。",
    )
    parser.add_argument("csv_path", metavar="CSVのパス")
    parser.add_argument("db_path", metavar="DBのパス")
    args = parser.parse_args(argv)

    # sqlite3.connect は無いファイルを作ってしまうので、先に確かめる
    if not os.path.isfile(args.db_path):
        logger.error("DB ファイルがありません: %s", args.db_path)
        return 1

    conn = sqlite3.connect(args.db_path)
    try:
        import_file(args.csv_path, OrderRepository(conn))
    except ImportFileError as e:
        logger.error("%s", e)
        return 1
    except sqlite3.Error as e:
        logger.error("DB に書き込めません: %s (%s)", args.db_path, e)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(main())
