"""Generate requirements.txt from pyproject.toml.

pyproject.toml is the single source of truth for dependencies - it is what
`pip install -e .` reads, and it is what actually governs what gets installed.
A hand-maintained requirements.txt next to it is the same problem as a
hand-maintained command reference: two copies of one fact, and the second one
goes stale the moment someone edits only the first.

So requirements.txt is generated, not written, and deliberately NOT committed
(see .gitignore) - a stale committed copy would be worse than no file, because
it looks authoritative. What it is for: the tools that still expect to find one
- readthedocs, some CI matrices and container base images, `pip install -r` out
of habit. Regenerate it whenever you actually need the file:

    python scripts/gen_requirements.py             # core dependencies
    python scripts/gen_requirements.py --dev       # core + the dev extra
    python scripts/gen_requirements.py --check      # exit 1 if it would differ
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib          # stdlib from 3.11
except ModuleNotFoundError:  # Python 3.10, the project's floor
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
OUT = ROOT / "requirements.txt"

HEADER = (
    "# Generated from pyproject.toml by scripts/gen_requirements.py.\n"
    "# Do not edit by hand - it will be overwritten, and pyproject.toml (not\n"
    "# this file) is what `pip install -e .` actually reads.\n"
    "#\n"
    "#     pip install -r requirements.txt\n"
    "#\n"
    "# is equivalent to the dependencies half of `pip install -e .`; it does not\n"
    "# install CodeOrbit itself. Prefer the editable install unless something\n"
    "# else in your toolchain specifically wants this file.\n"
)


def build(include_dev: bool) -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    deps = list(project.get("dependencies", []))
    if include_dev:
        deps += project.get("optional-dependencies", {}).get("dev", [])
    lines = [HEADER, ""]
    lines += deps
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    include_dev = "--dev" in sys.argv
    check = "--check" in sys.argv
    text = build(include_dev)

    if check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else None
        if current != text:
            print(f"{OUT} would differ from pyproject.toml. "
                  "Run: python scripts/gen_requirements.py")
            return 1
        print(f"{OUT} matches pyproject.toml.")
        return 0

    OUT.write_text(text, encoding="utf-8")
    dep_count = sum(1 for line in text.splitlines()
                    if line.strip() and not line.startswith("#"))
    print(f"wrote {OUT} ({dep_count} dependencies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
