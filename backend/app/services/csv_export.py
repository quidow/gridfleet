"""Legacy import shim for Phase 0b backend domain-layout refactor.

Real implementation lives at ``app/core/csv_export.py``. Phase 16 deletes
this shim once every caller migrates.
"""

from app.core.csv_export import to_csv_response

__all__ = ["to_csv_response"]
