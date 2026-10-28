"""Verification: the committed model file is current against the stored signals, i.e. the
synthesizer won't mark it stale (status 4). Read-only. Exits 1 on a mismatch.

It computes both staleness inputs the way the synthesizer does on every run, through the
synthesizer's own functions (CLAUDE.md's verification-script convention):
- the live `efficiency_fingerprint(PRIMARY_SPEC)`, from the definition labels;
- the live `fit_inputs_hash` over `_load_fit_signals`, the stored fit-window rows.
and compares each with the model file's.

Usage:
  uv run python scripts/verify_model_current.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection  # noqa: E402
from pipeline.synthesis.model import (  # noqa: E402
    COEFFICIENTS_PATH,
    MODEL_VERSION,
    PRIMARY_SPEC,
    efficiency_fingerprint,
    fit_inputs_hash,
    load_model_file,
)
from pipeline.synthesis.synthesizer import _load_fit_signals  # noqa: E402


def main() -> int:
    model = load_model_file(COEFFICIENTS_PATH)
    with get_connection() as conn:
        conn.read_only = True
        live_hash = fit_inputs_hash(_load_fit_signals(conn, model), PRIMARY_SPEC, model.fit_seasons)
    live_fp = efficiency_fingerprint(PRIMARY_SPEC)
    checks = [
        ("model_version", MODEL_VERSION, model.model_version),
        ("efficiency_fingerprint", live_fp, model.efficiency_fingerprint),
        ("fit_inputs_hash", live_hash, model.fit_inputs_hash),
    ]
    ok = True
    for name, live, file in checks:
        same = live == file
        ok &= same
        print(f"{name:<24} live {live}  file {file}  {'OK' if same else 'MISMATCH'}")
    print("model file is current" if ok else "STALE: the synthesizer would refuse to project")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
