"""CertiRank — tournament execution and review policies on top of frozen pair responses.

P0 scope (2026-10-15): data contract + immutable external data + Gate 2 audit.
The package currently only contains what the audit actually uses:

* ``certirank.operators`` — pure comparison-aggregation semantics (naive sort replay,
  flip-impact classification) and pairwise ranking metrics. No I/O, stdlib only.
* ``certirank.pair_pool`` — the Pair Response Pool: loading and contract-validating
  frozen pair response records (the Gate 2 external data is the first instance).

``oracle`` / ``trace`` / ``policy`` / ``repair`` arrive with P1-P3; no placeholders.
"""

__version__ = "0.1.0"
