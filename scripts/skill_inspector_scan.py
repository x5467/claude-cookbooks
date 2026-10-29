#!/usr/bin/env python3
"""Run NVIDIA SkillSpector over every agent-facing asset in the repository.

SkillSpector only scans directories that contain a SKILL.md. Agent configuration
that is not packaged as a skill (.claude/ directories with hooks, commands and
subagents, CLAUDE.md / AGENTS.md instruction files, MCP configs) is staged into a
temporary directory with a neutral SKILL.md stub so the same analyzers run on it.

Modes:
    (default)   Scan the working tree. Exit 1 if any target fails the scanner gate.
    --rev REV   Scan the asset tree as it existed at a git revision.
    --history   Scan every distinct historical version of every asset ever committed.

Per-target baselines live in .skillspector/baselines/<slug>.yaml and are only
applied in working-tree mode. Generate them with `skillspector baseline` after a
human has triaged each finding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = REPO_ROOT / ".skillspector" / "baselines"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".tox"}
INSTRUCTION_FILES = {"CLAUDE.md", "AGENTS.md"}
MCP_FILES = {".mcp.json", "mcp.json"}
SCAN_TIMEOUT_SECONDS = 900
STUB_SKILL_MD = (
    "---\n"
    "name: {name}\n"
    "description: Repository agent configuration staged for security scanning only.\n"
    "---\n"
)


@dataclass
class Target:
    slug: str
    kind: str
    source: str
    staged: Path
    label: str = ""


@dataclass
class Result:
    target: Target
    exit_code: int
    report: dict = field(default_factory=dict)
    error: str = ""


def slugify(rel_path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", rel_path).strip("-").lower()
    return slug or "root"


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        yield Path(dirpath), dirnames, filenames


def copy_tree(src: Path, dst: Path, exclude_top: set[str] | None = None) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in SKIP_DIRS}
        if exclude_top and Path(directory) == src:
            skipped |= {n for n in names if n in exclude_top}
        return skipped

    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


def stage_targets(root: Path, stage_dir: Path, label: str = "") -> list[Target]:
    """Discover agent assets under root and stage each as a scannable skill directory."""
    targets: list[Target] = []
    seen_slugs: set[str] = set()

    def add(rel: str, kind: str, build) -> None:
        slug = slugify(f"{kind}-{rel}")
        if slug in seen_slugs:
            return
        seen_slugs.add(slug)
        staged = stage_dir / slug
        build(staged)
        targets.append(Target(slug=slug, kind=kind, source=rel, staged=staged, label=label))

    for dirpath, dirnames, filenames in walk(root):
        rel_dir = dirpath.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir

        if "SKILL.md" in filenames:
            add(rel_dir or ".", "skill", lambda s, d=dirpath: copy_tree(d, s))

        if dirpath.name == ".claude" and set(dirnames + filenames) - {"skills"}:
            # Skills under .claude/skills are scanned on their own as real skills.
            def build_claude(s: Path, d: Path = dirpath, r: str = rel_dir) -> None:
                copy_tree(d, s / "claude-config", exclude_top={"skills"})
                (s / "SKILL.md").write_text(STUB_SKILL_MD.format(name=slugify(r)[:64]))

            add(rel_dir, "claude-config", build_claude)

        for name in filenames:
            if name in INSTRUCTION_FILES or name in MCP_FILES:
                rel_file = f"{rel_dir}/{name}" if rel_dir else name
                kind = "instructions" if name in INSTRUCTION_FILES else "mcp-config"

                def build_file(s: Path, f: Path = dirpath / name, n: str = name) -> None:
                    s.mkdir(parents=True)
                    if n in INSTRUCTION_FILES:
                        shutil.copy2(f, s / "SKILL.md")
                    else:
                        shutil.copy2(f, s / n)
                        (s / "SKILL.md").write_text(STUB_SKILL_MD.format(name="mcp-config"))

                add(rel_file, kind, build_file)

    return targets


def run_scan(skillspector: str, target: Target, out_dir: Path, use_baseline: bool) -> Result:
    report_path = out_dir / f"{target.slug}.json"
    cmd = [skillspector, "scan", str(target.staged), "--no-llm", "--format", "json"]
    cmd += ["--output", str(report_path)]
    baseline = BASELINE_DIR / f"{target.slug}.yaml"
    if use_baseline and baseline.is_file():
        cmd += ["--baseline", str(baseline)]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return Result(target, 2, error=f"timed out after {SCAN_TIMEOUT_SECONDS}s")
    report: dict = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError as exc:
            return Result(target, 2, error=f"unreadable report: {exc}")
    error = "" if report else (proc.stderr.strip()[-500:] or "no report produced")
    return Result(target, proc.returncode, report, error)


def finding_rows(result: Result) -> list[tuple[str, str, str, str]]:
    rows = []
    for issue in result.report.get("issues", []):
        loc = issue.get("location") or {}
        path = loc.get("file", "?")
        if result.target.kind == "claude-config":
            path = path.removeprefix("claude-config/")
        where = f"{result.target.source}/{path}:{loc.get('start_line', '?')}"
        if result.target.kind in ("instructions", "mcp-config"):
            where = f"{result.target.source}:{loc.get('start_line', '?')}"
        what = f"{issue.get('category', '')}: {issue.get('pattern', '')}".strip(": ")
        rows.append((issue.get("severity", "?"), issue.get("id", "?"), where, what))
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    return sorted(rows, key=lambda r: (order.get(r[0], 9), r[2]))


def md_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_summary(results: list[Result], out_dir: Path, title: str, gate: bool) -> str:
    failed = [r for r in results if r.exit_code != 0]
    lines = [f"## {title}", ""]
    lines.append(
        f"Targets scanned: {len(results)}. "
        f"Scanner gate failures: {len(failed)}. "
        f"Mode: {'gate' if gate else 'report only'}."
    )
    lines += [
        "",
        "| Target | Kind | Version | Score | Severity | Recommendation | Findings | Exit |",
    ]
    lines.append("|---|---|---|---:|---|---|---:|---:|")
    for r in sorted(
        results, key=lambda r: -(r.report.get("risk_assessment") or {}).get("score", -1)
    ):
        risk = r.report.get("risk_assessment") or {}
        lines.append(
            f"| `{md_cell(r.target.source)}` | {r.target.kind} | {md_cell(r.target.label or 'HEAD')} "
            f"| {risk.get('score', '-')} | {risk.get('severity', '-')} "
            f"| {risk.get('recommendation', md_cell(r.error) or '-')} "
            f"| {len(r.report.get('issues', []))} | {r.exit_code} |"
        )
    lines += ["", "### Findings", "", "| Severity | Rule | Location | Version | Pattern |"]
    lines.append("|---|---|---|---|---|")
    for r in results:
        for sev, rule, where, what in finding_rows(r):
            lines.append(
                f"| {sev} | {rule} | `{md_cell(where)}` | {md_cell(r.target.label or 'HEAD')} "
                f"| {md_cell(what)} |"
            )
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(text)
    combined = [
        {
            "target": r.target.source,
            "kind": r.target.kind,
            "version": r.target.label or "HEAD",
            "exit_code": r.exit_code,
            "error": r.error,
            "report": r.report,
        }
        for r in results
    ]
    (out_dir / "combined.json").write_text(json.dumps(combined, indent=2))
    return text


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO_ROOT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def extract(rev: str, paths: list[str], dest: Path) -> None:
    """Extract paths at rev into dest using git archive, without touching the worktree."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f"{dest.name}.tar"
    with archive.open("wb") as fh:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO_ROOT), "archive", rev, "--", *paths],  # noqa: S607
            stdout=fh,
            check=True,
        )
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    archive.unlink()


def is_asset_path(path: str) -> bool:
    parts = path.split("/")
    return (
        parts[-1] == "SKILL.md"
        or ".claude" in parts[:-1]
        or parts[-1] in INSTRUCTION_FILES
        or parts[-1] in MCP_FILES
    )


def asset_root(path: str) -> str:
    """Map a file path to the directory or file that is scanned as one target."""
    parts = path.split("/")
    if ".claude" in parts[:-1]:
        idx = parts.index(".claude")
        if len(parts) > idx + 3 and parts[idx + 1] == "skills":
            return "/".join(parts[: idx + 3])
        return "/".join(parts[: idx + 1])
    if parts[-1] == "SKILL.md":
        return "/".join(parts[:-1]) or "."
    return path


def history_versions() -> list[tuple[str, str, str]]:
    """Return (asset_root, commit, object_id) for each distinct historical version."""
    names = git("log", "--all", "--format=", "--name-only").splitlines()
    roots = sorted({asset_root(p) for p in names if p and is_asset_path(p)})
    versions: list[tuple[str, str, str]] = []
    for root in roots:
        seen: set[str] = set()
        for commit in git("log", "--all", "--format=%H", "--", root).split():
            try:
                oid = git("rev-parse", f"{commit}:{root}").strip()
            except subprocess.CalledProcessError:
                continue  # path deleted at this commit
            if oid not in seen:
                seen.add(oid)
                versions.append((root, commit, oid))
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skillspector", default=shutil.which("skillspector") or "skillspector")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "skillspector-reports")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rev", help="Scan assets as they existed at this git revision")
    mode.add_argument("--history", action="store_true", help="Scan every historical version")
    parser.add_argument("--strict", action="store_true", help="Fail on findings in --history/--rev")
    args = parser.parse_args()
    if args.rev and args.rev.startswith("-"):
        parser.error("--rev must be a revision, not an option")

    if not shutil.which(args.skillspector) and not Path(args.skillspector).is_file():
        print("skillspector CLI not found. See .claude/skills/skill-inspector/README.md")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="skill-inspector-") as tmp:
        tmp_path = Path(tmp)
        if args.history:
            title = "SkillSpector history audit"
            gate = args.strict
            versions = history_versions()
            print(f"{len(versions)} distinct historical asset versions")
            for i, (root, commit, _oid) in enumerate(versions):
                src = tmp_path / f"src-{i}"
                extract(commit, [root], src)
                for t in stage_targets(src, tmp_path / f"stage-{i}", label=commit[:10]):
                    t.slug = f"{t.slug}-{commit[:10]}"
                    results.append(run_scan(args.skillspector, t, args.out, use_baseline=False))
                    print(f"[{len(results)}] {t.source}@{commit[:10]} exit={results[-1].exit_code}")
        else:
            if args.rev:
                title = f"SkillSpector scan at {args.rev}"
                gate = args.strict
                src = tmp_path / "src"
                files = [p for p in git("ls-tree", "-r", "--name-only", args.rev).splitlines()]
                paths = sorted({asset_root(p) for p in files if is_asset_path(p)})
                if paths:
                    extract(args.rev, paths, src)
                label = args.rev
            else:
                title = "SkillSpector scan of working tree"
                gate = True
                src = REPO_ROOT
                label = ""
            for t in stage_targets(src, tmp_path / "stage", label=label):
                results.append(run_scan(args.skillspector, t, args.out, use_baseline=not args.rev))
                print(f"[{len(results)}] {t.source} exit={results[-1].exit_code}")

    summary = write_summary(results, args.out, title, gate)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(summary)
    print(f"\nReports written to {args.out}")
    if not results:
        print("No agent assets found.")
        return 0
    return 1 if gate and any(r.exit_code != 0 for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
