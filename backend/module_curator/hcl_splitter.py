"""
hcl_splitter.py — Deterministic modular file layout.

The LLM cannot be trusted to reliably put variables/locals/data/resources in
separate files, so we don't ask it to. It emits one HCL body; this module
slices it into top-level blocks (brace-depth aware, tolerant of strings,
heredocs, and comments) and routes each block to its canonical file:

    variable  -> variables.tf
    output    -> outputs.tf
    locals    -> locals.tf
    data      -> data.tf
    resource  -> main.tf
    module    -> main.tf
    moved/import/check/removed -> main.tf
    terraform -> versions.tf
    provider  -> providers.tf

Anything unrecognised is preserved in main.tf so nothing is ever dropped.
`terraform_fmt` then canonicalises spacing/alignment (no-op if the CLI is
absent). This guarantees the modular structure the user asked for.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

_ROUTE: dict[str, str] = {
    "variable": "variables.tf",
    "output": "outputs.tf",
    "locals": "locals.tf",
    "data": "data.tf",
    "resource": "main.tf",
    "module": "main.tf",
    "moved": "main.tf",
    "import": "main.tf",
    "removed": "main.tf",
    "check": "main.tf",
    "terraform": "versions.tf",
    "provider": "providers.tf",
}

_LEADING_KEYWORD = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)")


def _block_keyword(raw: str) -> str:
    """Return the HCL keyword for a block, skipping any leading comment lines.

    Leading `#`, `//`, and `/* ... */` comments are attached to the block that
    follows them, so we must look past them to find the real keyword (e.g. a
    comment above a `terraform {}` block must not hide the `terraform` keyword).
    """
    lines = raw.splitlines()
    idx = 0
    in_block_comment = False
    while idx < len(lines):
        stripped = lines[idx].strip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
                # keyword may follow the close on the same line
                after = stripped.split("*/", 1)[1].strip()
                if after:
                    m = _LEADING_KEYWORD.match(after)
                    if m:
                        return m.group(1)
            idx += 1
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            idx += 1
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            idx += 1
            continue
        m = _LEADING_KEYWORD.match(stripped)
        return m.group(1) if m else ""
    return ""


def _iter_top_level_blocks(hcl: str):
    """Yield (keyword, raw_block_text) for each top-level block in `hcl`.

    A scanner that tracks brace depth while ignoring braces that appear inside
    double-quoted strings, line comments (# and //), block comments (/* */),
    and heredocs (<<TAG / <<-TAG). Text between blocks (stray comments) is
    attached to the block that follows it.
    """
    i = 0
    n = len(hcl)
    depth = 0
    block_start = 0
    in_string = False
    in_line_comment = False
    in_block_comment = False
    heredoc_tag: str | None = None

    while i < n:
        ch = hcl[i]
        two = hcl[i : i + 2]

        # -- terminate single-line contexts on newline --
        if ch == "\n":
            in_line_comment = False
            # heredoc terminator: a line whose trimmed content == tag
            if heredoc_tag is not None:
                line_end = hcl.find("\n", i + 1)
                nxt = hcl[i + 1 : (line_end if line_end != -1 else n)]
                if nxt.strip() == heredoc_tag:
                    heredoc_tag = None
            i += 1
            continue

        if in_line_comment or heredoc_tag is not None:
            i += 1
            continue

        if in_block_comment:
            if two == "*/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        # -- not in any special context --
        if two == "//" or ch == "#":
            in_line_comment = True
            i += 1
            continue
        if two == "/*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if two == "<<":
            m = re.match(r"<<-?([A-Za-z_][A-Za-z0-9_]*)", hcl[i:])
            if m:
                heredoc_tag = m.group(1)
                i += m.end()
                continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = hcl[block_start : i + 1]
                yield _block_keyword(raw), raw.strip()
                block_start = i + 1
        i += 1

    # trailing content (e.g. a dangling comment) — attach as leftover
    tail = hcl[block_start:].strip()
    if tail:
        yield _block_keyword(tail), tail


def split_hcl_into_files(hcl: str) -> dict[str, str]:
    """Route every top-level block in `hcl` to its canonical .tf file.

    Returns {filename: content}. Unknown keywords land in main.tf so nothing is
    lost. Files are only created if they have content.
    """
    buckets: dict[str, list[str]] = {}
    for keyword, block in _iter_top_level_blocks(hcl):
        target = _ROUTE.get(keyword, "main.tf")
        buckets.setdefault(target, []).append(block)

    return {
        fname: "\n\n".join(blocks).strip() + "\n"
        for fname, blocks in buckets.items()
        if "".join(blocks).strip()
    }


def redistribute_module_files(files: dict[str, str]) -> dict[str, str]:
    """Take whatever the model produced and re-key it into the canonical layout.

    We concatenate the HCL from every *.tf file the model emitted, re-split it
    deterministically, and keep non-.tf files (README.md, examples, tfvars)
    untouched. This means even if the model dumped locals + data + resources
    into main.tf, they come out correctly separated.
    """
    hcl_parts: list[str] = []
    passthrough: dict[str, str] = {}

    for fname, content in files.items():
        # Only re-split ROOT-level .tf files. Leave examples/**/*.tf and any
        # non-.tf file exactly as generated.
        if fname.endswith(".tf") and "/" not in fname and "\\" not in fname:
            hcl_parts.append(content)
        else:
            passthrough[fname] = content

    combined = "\n\n".join(hcl_parts)
    split = split_hcl_into_files(combined) if combined.strip() else {}

    # Merge: split .tf files + passthrough (examples, README, etc.)
    result = dict(split)
    result.update(passthrough)
    return result


def terraform_fmt(files: dict[str, str]) -> dict[str, str]:
    """Run `terraform fmt` over the root .tf files. No-op if CLI is missing.

    Returns a new dict with formatted .tf content; non-.tf files pass through.
    """
    tf_files = {
        f: c for f, c in files.items()
        if f.endswith(".tf") and "/" not in f and "\\" not in f
    }
    if not tf_files:
        return files

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for f, c in tf_files.items():
                (tmp / f).write_text(c, encoding="utf-8")
            proc = subprocess.run(
                ["terraform", "fmt", "-no-color"],
                cwd=tmpdir, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                # fmt fails only on unparseable HCL; leave content for the
                # validator/repair loop to handle.
                return files
            out = dict(files)
            for f in tf_files:
                out[f] = (tmp / f).read_text(encoding="utf-8")
            return out
    except Exception:
        return files
