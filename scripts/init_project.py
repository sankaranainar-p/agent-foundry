#!/usr/bin/env python3
"""Copy the agent-foundry templates into a project and print the next steps.

Usage: python3 scripts/init_project.py /path/to/project [--force]

Existing files are never overwritten unless --force is passed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
FILES = ["constitution.md", "spec.md", "plan.md", "tasks.md", "project.yaml"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="path to the project folder (created if missing)")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    dest = Path(args.project).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "logs").mkdir(exist_ok=True)

    copied, skipped = [], []
    for name in FILES:
        target = dest / name
        if target.exists() and not args.force:
            skipped.append(name)
            continue
        shutil.copyfile(TEMPLATES / name, target)
        copied.append(name)

    print(f"Project: {dest}")
    print(f"  copied:  {', '.join(copied) or 'nothing'}")
    if skipped:
        print(f"  skipped (already exist): {', '.join(skipped)}")
    print()
    print("Next steps:")
    print("  1. Fill in project.yaml: build, test, run commands, has_ui, models.")
    print("  2. Run the design phase with EGM, then save the approved design as spec.md and plan.md.")
    print("  3. Run the Planner agent to produce tasks.md, then approve it.")
    print("  4. Run the orchestrator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
