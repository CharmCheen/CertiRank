#!/usr/bin/env python3
"""Read-only offline regression entry point; requires pytest in this interpreter."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        ["scripts/make_data_manifest.py", "--check"],
        ["scripts/audit_gate2.py", "--check"],
        ["-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        ["scripts/pilot_budget.py", "--check"],
        ["scripts/analyze_offline.py", "--check"],
        ["scripts/research_diagnostics.py", "--check"],
        ["scripts/benchmark_cpu.py", "--check"],
    ]
    for command in commands:
        print("CHECK", " ".join(command), flush=True)
        result = subprocess.run([sys.executable, "-B", *command], cwd=ROOT)
        if result.returncode:
            return result.returncode
    print("PASS all offline checks; no provider calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
