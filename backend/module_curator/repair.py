"""
repair.py — Iterative validate → repair loop.

The generator produces a draft; the validator finds errors; this module feeds
those errors back to the model and regenerates until `terraform validate` (and
the schema/tflint layers) pass, or a round cap is hit. This is what turns
"detects errors" into "ships correct code".

Design for an 8K-context model:
  - We repair the whole module at once when it fits the budget (so cross-file
    errors like "undeclared variable" are fixable), otherwise we fall back to
    repairing only the files that carry errors.
  - The repair prompt contains ONLY the offending files + the exact validator
    messages — never the security lectures or schema dumps from generation.
  - After every repair we re-split (keeps the modular layout) and re-fmt.

Public API:
  async def repair_until_valid(files, out_dir, session, *, call_llm,
                               validate_fn, split_fn, fmt_fn,
                               max_rounds=3) -> tuple[dict, "CurationValidationResult"]
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Awaitable, Callable

# Lazy imports of code_generator helpers happen inside functions to avoid a
# circular import (code_generator imports this module).

_REPAIR_BUDGET_CHARS = 9000  # ~ keep prompt comfortably inside 8K tokens


def _extract_file_markers(raw: str) -> dict[str, str]:
    from backend.module_curator.code_generator import (
        _extract_file_markers as _emf,
        _is_valid_hcl_content,
    )
    files = _emf(raw)
    return {k: v for k, v in files.items() if _is_valid_hcl_content(v, k)}


def _errors_by_file(issues) -> dict[str, list]:
    """Group ERROR-severity issues by the file they belong to."""
    by_file: dict[str, list] = {}
    for iss in issues:
        if iss.severity != "error":
            continue
        key = iss.file or "main.tf"
        by_file.setdefault(key, []).append(iss)
    return by_file


def _format_issue(iss) -> str:
    loc = f" (line {iss.line})" if getattr(iss, "line", None) else ""
    rule = f"[{iss.rule}] " if iss.rule else ""
    fix = f"  Fix: {iss.suggestion}" if iss.suggestion else ""
    return f"- {rule}{iss.message}{loc}{fix}"


def _select_context_files(
    files: dict[str, str], error_files: set[str]
) -> list[str]:
    """Pick which root .tf files to include in the repair prompt.

    Always include files with errors; add the usual cross-reference culprits
    (variables.tf, outputs.tf, main.tf, locals.tf) while under budget.
    """
    root_tf = [
        f for f in files
        if f.endswith(".tf") and "/" not in f and "\\" not in f
    ]
    ordered: list[str] = []
    # errors first
    for f in root_tf:
        if f in error_files:
            ordered.append(f)
    # then common context
    for f in ("variables.tf", "main.tf", "outputs.tf", "locals.tf", "versions.tf", "data.tf"):
        if f in root_tf and f not in ordered:
            ordered.append(f)

    selected: list[str] = []
    used = 0
    for f in ordered:
        size = len(files[f])
        if selected and used + size > _REPAIR_BUDGET_CHARS:
            break
        selected.append(f)
        used += size
    return selected


def _build_repair_prompt(
    files: dict[str, str],
    selected: list[str],
    issues_by_file: dict[str, list],
    provider: str,
) -> str:
    parts: list[str] = [
        f"You are a senior Terraform engineer fixing a {provider} module.",
        "The files below FAILED validation. Fix ONLY the reported errors.",
        "Preserve everything that is already correct — same resource names, "
        "variable names, and structure. Do not add commentary.",
        "",
        "## VALIDATION ERRORS (fix every one)",
    ]
    for fname in selected:
        errs = issues_by_file.get(fname, [])
        if errs:
            parts.append(f"In {fname}:")
            parts += [f"  {_format_issue(e)}" for e in errs]
    # errors whose file isn't in the selected set (e.g. hardcoded "main.tf")
    for fname, errs in issues_by_file.items():
        if fname not in selected:
            parts.append(f"In {fname} (or wherever the symbol is defined):")
            parts += [f"  {_format_issue(e)}" for e in errs]
    parts.append("")

    parts.append("## CURRENT FILES")
    for fname in selected:
        parts += [f"[FILE: {fname}]", files[fname].rstrip(), "[/FILE]"]
    parts += [
        "",
        "## OUTPUT",
        "Return the COMPLETE corrected version of every file above using the "
        "same markers. Raw HCL only, NO markdown fences.",
    ]
    for fname in selected:
        parts += [f"[FILE: {fname}]", "# corrected content", "[/FILE]"]
    return "\n".join(parts)


async def repair_until_valid(
    files: dict[str, str],
    out_dir: Path,
    session,
    *,
    call_llm: Callable[[str], Awaitable[str]],
    validate_fn: Callable[..., Awaitable],
    split_fn: Callable[[dict], dict],
    fmt_fn: Callable[[dict], dict],
    max_rounds: int = 3,
):
    """Loop: validate → if errors, repair the offending files → re-split → re-fmt.

    Returns (final_files, final_validation_result).
    """
    provider = session.provider.value
    current = dict(files)

    # write + validate the initial draft
    _write(current, out_dir)
    result = await validate_fn(current, out_dir, session)

    round_no = 0
    while result.error_count > 0 and round_no < max_rounds:
        round_no += 1
        issues_by_file = _errors_by_file(result.issues)
        error_files = set(issues_by_file.keys())
        selected = _select_context_files(current, error_files)
        if not selected:
            break

        prompt = _build_repair_prompt(current, selected, issues_by_file, provider)
        print(f"[repair] Round {round_no}/{max_rounds} — "
              f"{result.error_count} error(s) across {sorted(error_files)}")

        try:
            raw = await call_llm(prompt)
        except Exception as exc:
            print(f"[repair] LLM call failed: {exc}")
            break

        fixed = _extract_file_markers(raw)
        if not fixed:
            print("[repair] No usable output from repair pass — stopping.")
            break

        # apply fixes, then re-split (keeps modular layout) + re-fmt
        merged = dict(current)
        for fname, content in fixed.items():
            merged[fname] = content
        merged = split_fn(merged)
        merged = fmt_fn(merged)

        current = merged
        _write(current, out_dir)
        new_result = await validate_fn(current, out_dir, session)

        # guard against a repair that increases errors — keep the better one
        if new_result.error_count >= result.error_count and round_no > 1:
            print(f"[repair] No improvement ({new_result.error_count} errors) — stopping.")
            result = new_result
            break
        result = new_result

    print(f"[repair] Final: passed={result.passed}, errors={result.error_count}, "
          f"rounds_used={round_no}")
    return current, result


def _write(files: dict[str, str], out_dir: Path) -> None:
    for fname, content in files.items():
        fp = out_dir / fname
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
