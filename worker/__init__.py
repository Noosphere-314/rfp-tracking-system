"""Deterministic core of the RFP pipeline.

Fetching, dedup and delivery bookkeeping live here and must work every time.
Claude is called only from the n8n side, and only for items this worker has
already decided are new (System-Design.md §2).
"""

__version__ = "0.1.0"
