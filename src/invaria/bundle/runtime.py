"""Trusted local runtime for replay: the only engine and normalizers a verifier may run.

Nothing is resolved from the bundle. A bundle naming another engine or parser is
INCOMPLETE, never executed. ``engine_source_sha256`` binds the report to the exact local
replay code (contracts, engine, CSV normaliser and the bundle verifier itself), beyond the
``ENGINE_REF`` label.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from functools import cache
from importlib import resources

from invaria.engine.evaluate import Evaluation, EvaluationInputs, replay
from invaria.engine.versions import (
    BLOCKED_ENGINES as BLOCKED_ENGINES,
)
from invaria.engine.versions import (
    CURRENT_ENGINES,
)
from invaria.engine.versions import (
    RETIRED_ENGINES as RETIRED_ENGINES,
)
from invaria.ingest.csv_import import PARSER_REF as CSV_PARSER_REF


def _engine(engine_ref: str) -> Callable[[EvaluationInputs], Evaluation]:
    """Pinned to its label: a bundle naming one engine is never replayed with another."""
    return lambda inputs: replay(inputs, engine_ref)


# Engines a verifier may replay with: the current ones and the retired ones, each with its
# engine label's implementation (a retired label runs a compatibility implementation,
# not the historical code). Reproducing a retired engine's conclusion does not validate
# it under the current semantics; the report says which kind of engine reproduced it.
# Blocked engines (``invaria.engine.versions.BLOCKED_ENGINES``) are never replayed, and no
# engine is ever substituted for another.
TRUSTED_ENGINES: Mapping[str, Callable[[EvaluationInputs], Evaluation]] = {
    ref: _engine(ref) for ref in sorted({*CURRENT_ENGINES, *RETIRED_ENGINES})
}
TRUSTED_NORMALIZERS: frozenset[str] = frozenset({CSV_PARSER_REF})
_ENGINE_PACKAGES = ("invaria.contracts", "invaria.engine", "invaria.ingest", "invaria.bundle")


@cache
def engine_source_sha256() -> str:
    """Digest of every module the verifier runs: contracts, engine, ingest and bundle."""
    digest = hashlib.sha256()
    for package in _ENGINE_PACKAGES:
        files = resources.files(package)
        for name in sorted(f.name for f in files.iterdir() if f.name.endswith(".py")):
            digest.update(f"{package}/{name}\0".encode())
            digest.update(files.joinpath(name).read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()
