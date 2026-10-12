"""The export destination: ``safe_write.NewFile`` with the export temporary prefix.

The writing guarantees and their limits are those of ``invaria.safe_write`` (also used
by others). Authorization, the export record and the meaning of a failed or unconfirmed export
stay in ``QueryService.export_integrity_report_file``; this module adds nothing to them.
"""

from __future__ import annotations

from pathlib import Path

from invaria.safe_write import DestinationError, NewFile, Unconfirmed, WriteFailure, supported

TEMP_PREFIX = ".invaria-export-"

__all__ = [
    "TEMP_PREFIX",
    "DestinationError",
    "ExportTarget",
    "Unconfirmed",
    "WriteFailure",
    "supported",
]


class ExportTarget(NewFile):
    """One export file. ``prepare`` before computing the content; ``publish`` once."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, temp_prefix=TEMP_PREFIX)
