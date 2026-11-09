"""One tiny real call per tier through ModelWrapper. Usage: python scripts/smoke_adapters.py [project.yaml]

Call 1 uses the real Claude adapter. Call 2 swaps the primary for one that always reports a
rate limit, so the wrapper really falls back to the configured provider.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foundry.adapters import build_providers, load_config  # noqa: E402
from foundry.wrapper import ModelWrapper, RateLimited  # noqa: E402

PROMPT = "Reply with exactly one word: ok"


def limited(_prompt):
    raise RateLimited(3600)


cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "templates/project.yaml")
primary, fallback, name = build_providers(cfg)

with tempfile.TemporaryDirectory() as tmp:
    for label, prim, bounded in (("claude", primary, False), ("fallback", limited, True)):
        w = ModelWrapper(prim, fallback, Path(tmp) / f"{label}.json", fallback_name=name)
        r = w.call(PROMPT, bounded=bounded)
        print(f"{label}: tier={r.tier} model={r.model} label={r.label!r} text={r.text.strip()[:60]!r}")
