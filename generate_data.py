#!/usr/bin/env python3
"""Generate synthetic customers and transactions for the Snowflake Horizon demo.

Everything is recognisably fake:
  - emails use the reserved example.com domain
  - card numbers start with 9999 and always FAIL the Luhn check
  - the data is seeded, so the same seed gives the same files

Usage:
  python scripts/generate_data.py --rows 1000            # write CSVs to data/
  python scripts/generate_data.py --rows 1000 --load     # also append to Snowflake RAW

Loading connects as TRANSFORM_ROLE using environment variables:
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD
  optional: SNOWFLAKE_WAREHOUSE (default PAYMENTS_WH), SNOWFLAKE_DATABASE (default PAYMENTS_GOV)

Loading APPENDS rows. To start over, truncate RAW.CUSTOMERS and RAW.TRANSACTIONS
as a privileged role first.
"""

import argparse
import os
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from faker import Faker

# Fixed reference date so a given seed always produces identical timestamps.
REFERENCE_END = datetime(2026, 11, 1)
HISTORY_DAYS = 330

REGIONS = ["AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK"]


def luhn_valid(number: str) -> bool:
    """Return True if the digit string passes the Luhn check."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def fake_card_number(rng: random.Random) -> str:
    """16 digits starting with 9999 that are guaranteed to fail the Luhn check."""
    number = "9999" + "".join(str(rng.randint(0, 9)) for _ in range(12))
    if luhn_valid(number):
        # Changing only the last digit always changes the Luhn result.
        last = (int(number[-1]) + 1) % 10
        number = number[:-1] + str(last)
    assert not luhn_valid(number)
    return number


def make_customers(fake: Faker, rng: random.Random, n: int) -> pd.DataFrame:
    rows = []
    for customer_id in range(1, n + 1):
        name = fake.name()
        slug = re.sub(r"[^a-z]", "", name.lower())[:12] or "customer"
        rows.append(
            {
                "CUSTOMER_ID": customer_id,
                "NAME": name,
                # The id keeps every address unique.
                "EMAIL": f"{slug}{customer_id}@example.com",
                "REGION": rng.choice(REGIONS),
            }
        )
    return pd.DataFrame(rows)


def make_transactions(
    fake: Faker, rng: random.Random, n_customers: int, n_transactions: int
) -> pd.DataFrame:
    # Each customer keeps one card, so repeat transactions share a card number.
    cards = {cid: fake_card_number(rng) for cid in range(1, n_customers + 1)}
    start = REFERENCE_END - timedelta(days=HISTORY_DAYS)
    span_seconds = HISTORY_DAYS * 24 * 3600

    rows = []
    for transaction_id in range(1, n_transactions + 1):
        customer_id = rng.randint(1, n_customers)
        cents = rng.randint(100, 250_000)  # 1.00 to 2,500.00, always positive
        rows.append(
            {
                "TRANSACTION_ID": transaction_id,
                "CUSTOMER_ID": customer_id,
                "CARD_NUMBER": cards[customer_id],
                "AMOUNT": Decimal(cents) / Decimal(100),
                "MERCHANT": fake.company(),
                "TXN_TS": start + timedelta(seconds=rng.randint(0, span_seconds)),
            }
        )
    return pd.DataFrame(rows)


def load_to_snowflake(customers: pd.DataFrame, transactions: pd.DataFrame) -> None:
    """Append both frames to RAW as TRANSFORM_ROLE."""
    import snowflake.connector
    from snowflake.connector.pandas_tools import write_pandas

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role="TRANSFORM_ROLE",
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "PAYMENTS_WH"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "PAYMENTS_GOV"),
        schema="RAW",
    )
    try:
        # Customers first, so transactions never reference a missing customer.
        for table, frame in (("CUSTOMERS", customers), ("TRANSACTIONS", transactions)):
            ok, _, nrows, _ = write_pandas(
                conn,
                frame,
                table_name=table,
                schema="RAW",
                auto_create_table=False,  # tables come from sql/00_roles_and_grants.sql
                use_logical_type=True,    # keeps timestamps as timestamps
            )
            print(f"Loaded {nrows} rows into RAW.{table} (success={ok})")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=1000, help="number of customers (default 1000)")
    parser.add_argument(
        "--txn-multiplier",
        type=int,
        default=5,
        help="transactions per customer on average (default 5)",
    )
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument("--out", default="data", help="output folder for CSVs (default data/)")
    parser.add_argument("--load", action="store_true", help="append the rows to Snowflake RAW")
    args = parser.parse_args()

    if args.rows < 1 or args.txn_multiplier < 1:
        parser.error("--rows and --txn-multiplier must be at least 1")

    Faker.seed(args.seed)
    fake = Faker("en_CA")
    rng = random.Random(args.seed)

    customers = make_customers(fake, rng, args.rows)
    transactions = make_transactions(fake, rng, args.rows, args.rows * args.txn_multiplier)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    customers.to_csv(out / "customers.csv", index=False)
    transactions.to_csv(out / "transactions.csv", index=False)
    print(f"Wrote {len(customers)} customers and {len(transactions)} transactions to {out}/")

    if args.load:
        load_to_snowflake(customers, transactions)


if __name__ == "__main__":
    main()
