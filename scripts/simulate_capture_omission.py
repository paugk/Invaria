"""Make a SIMULATED provider capture that omits one transaction, for the ledger
verification guide (LEDGER_VERIFICATION.md).

It copies a directory of recorded Horizon exchanges to a new directory, removes the
records of one transaction from the recorded ``/accounts/{id}/payments`` pages, and
rewrites each changed body's sha256 so the copy stays internally consistent. The source
directory is never written. The result is a simulation: it is not an omission observed in
Horizon.

    python3 scripts/simulate_capture_omission.py SOURCE_DIR OUT_DIR TX_HASH
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


def simulate(source: Path, out: Path, tx_hash: str) -> int:
    if out.exists():
        raise SystemExit(f"{out} already exists: choose a new directory")
    shutil.copytree(source, out)
    removed = 0
    for meta_path in sorted(out.glob("*.json")):
        meta = json.loads(meta_path.read_text("utf-8"))
        if meta.get("method") != "GET" or "/payments?" not in str(meta.get("url")):
            continue
        body_path = meta_path.with_suffix(".body")
        document = json.loads(body_path.read_bytes())
        records = document.get("_embedded", {}).get("records", [])
        kept = [r for r in records if r.get("transaction_hash") != tx_hash]
        if len(kept) == len(records):
            continue
        removed += len(records) - len(kept)
        document["_embedded"]["records"] = kept
        body = json.dumps(document).encode()
        body_path.write_bytes(body)
        meta["response_sha256"] = hashlib.sha256(body).hexdigest()
        meta_path.write_text(json.dumps(meta), "utf-8")
    (out / "SIMULATED.txt").write_text(
        f"Simulated capture: the records of transaction {tx_hash} were removed from a copy "
        f"of {source}. This is not an omission observed in Horizon.\n",
        "utf-8",
    )
    return removed


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    removed = simulate(Path(argv[0]), Path(argv[1]), argv[2].lower())
    if removed == 0:
        print("no recorded payment record of that transaction: nothing simulated")
        return 1
    print(f"simulated capture in {argv[1]}: {removed} record(s) of {argv[2]} removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
