"""Fingerprint both DuckDB inputs to each upstream TPC-H comparison."""
import hashlib
import json
import sys
from pathlib import Path

from sqlglot import transpile
from tests.helpers import load_sql_fixture_pairs

def digest(sql):
    return hashlib.sha256(sql.encode()).hexdigest()

if __name__ == "__main__":
    rows = [[digest(transpile(text, write="duckdb")[0]) for text in (sql, optimized)]
            for _, sql, optimized in load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql")]
    if len(rows) != 22:
        raise ValueError("incomplete TPC-H fixture")
    Path(sys.argv[1]).write_text(json.dumps(rows))
