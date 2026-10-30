#!/usr/bin/env python3
"""Fail on retired or dated Claude model IDs in Python files and notebook code cells.

Runs in CI as the "Model Check" workflow. No AI service is involved: the rules are the
retired-model list and dated-ID pattern in scripts/validate_all_notebooks.py.

Usage:
    python3 scripts/check_model_ids.py             # every tracked .py and .ipynb file
    python3 scripts/check_model_ids.py FILE ...    # only these files
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from validate_all_notebooks import DATED_MODEL_PATTERN, DEPRECATED_MODELS

REPO_ROOT = Path(__file__).resolve().parent.parent

# These files list retired model IDs on purpose.
ALLOWED = {
    "scripts/validate_all_notebooks.py",
    ".claude/skills/cookbook-audit/validate_notebook.py",
}


def tracked_files() -> list[Path]:
    """Return every tracked .py and .ipynb file."""
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "*.py", "*.ipynb"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.splitlines()]


def code_lines(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (location, line) for each line of a .py file or notebook code cell."""
    if path.suffix == ".ipynb":
        cells = json.loads(path.read_text(encoding="utf-8")).get("cells", [])
        for index, cell in enumerate(cells):
            if cell.get("cell_type") == "code":
                source = "".join(cell.get("source", []))
                for lineno, line in enumerate(source.splitlines(), 1):
                    yield f"cell {index} line {lineno}", line
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            yield f"line {lineno}", line


def check(path: Path, name: str) -> list[str]:
    """Return one message per retired or dated model ID found in path."""
    problems = []
    for where, line in code_lines(path):
        retired = [old for old in DEPRECATED_MODELS if old in line]
        for old in retired:
            # Report a dated retired ID once, not also the undated retired ID it starts with.
            if not any(old != other and old in other for other in retired):
                problems.append(
                    f"{name} {where}: retired model '{old}', use '{DEPRECATED_MODELS[old]}'"
                )
        for model in DATED_MODEL_PATTERN.findall(line):
            if model not in DEPRECATED_MODELS:
                problems.append(f"{name} {where}: dated model ID '{model}', use the alias")
    return problems


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]] or tracked_files()
    problems = []
    checked = 0
    for path in paths:
        try:
            name = path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            name = str(path)
        if path.suffix not in (".py", ".ipynb") or name in ALLOWED:
            continue
        checked += 1
        problems.extend(check(path, name))

    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} model ID problem(s). See the Models rules in CLAUDE.md.")
        return 1
    print(f"Checked {checked} files: no retired or dated model IDs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
