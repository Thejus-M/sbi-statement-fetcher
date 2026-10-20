"""Bridge to the shared transactions database in the widget repo
(omarchy-transactions/txdb). TXDB_HOME overrides where that repo is."""
import os
import sys
from pathlib import Path

TXDB_HOME = Path(os.environ.get("TXDB_HOME", "/home/pc/Work/omarchy-transactions"))
if str(TXDB_HOME) not in sys.path:
    sys.path.insert(0, str(TXDB_HOME))

from txdb import db, ingest, queries  # noqa: E402
from txdb.cli import backup  # noqa: E402

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn
