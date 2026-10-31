"""Measure what a session delivered: effective (deliverable) tokens and cost efficiency.

A snapshot of the git state is taken when a session starts. At the end, the
session's deliverables are:
  * files that are new since the snapshot (untracked, not ignored) — full content
  * tracked files changed since the snapshot commit — only the added lines
SquidAgent's own files (.squidagent/, .worktrees/, CLAUDE.md, CONVENTIONS.md,
.gitignore, logs) are excluded.

Tokens are counted with tiktoken's cl100k_base encoding as a tokenizer-
independent approximation (Claude's tokenizer is not public). If tiktoken is
unavailable, only words are reported.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Dict, Optional

EXCLUDED_DIRS = (".squidagent/", ".worktrees/", ".git/")
EXCLUDED_NAMES = {"CLAUDE.md", "CONVENTIONS.md", ".gitignore", ".DS_Store"}
EXCLUDED_SUFFIXES = {".log"}
TOKENIZER = "cl100k_base (approx.)"

_encoder = None


def _count_tokens(text: str) -> Optional[int]:
    global _encoder
    if _encoder is None:
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoder = False
    return len(_encoder.encode(text)) if _encoder else None


def _git(*args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _excluded(path: str) -> bool:
    p = Path(path)
    return (path.startswith(EXCLUDED_DIRS) or p.name in EXCLUDED_NAMES
            or p.suffix.lower() in EXCLUDED_SUFFIXES)


def _hash(path: str) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def snapshot() -> Dict:
    """Record the git state of the current directory at session start."""
    if _git("rev-parse", "--git-dir") is None:
        return {}  # not a git repository: deliverables are not measured
    head = (_git("rev-parse", "HEAD") or "").strip() or None
    untracked = set((_git("ls-files", "--others", "--exclude-standard") or "").splitlines())
    dirty = set((_git("diff", "--name-only", "HEAD") or "").splitlines()) if head else set()
    return {
        "head": head,
        "untracked": {p: _hash(p) for p in untracked},
        "dirty": {p: _hash(p) for p in dirty},
    }


def measure(snap: Optional[Dict]) -> Optional[Dict]:
    """Deliverable files, words and tokens produced since the snapshot."""
    if not snap:
        return None
    files: Dict[str, Dict[str, Optional[int]]] = {}

    def add(path: str, text: str) -> None:
        files[path] = {"words": len(text.split()), "tokens": _count_tokens(text)}

    # New untracked files (or pre-existing untracked files whose content changed)
    for path in (_git("ls-files", "--others", "--exclude-standard") or "").splitlines():
        if _excluded(path) or (path in snap["untracked"] and snap["untracked"][path] == _hash(path)):
            continue
        try:
            add(path, Path(path).read_text(errors="replace"))
        except Exception:
            pass

    # Tracked files changed since the snapshot commit: count only added lines
    if snap["head"]:
        for path in (_git("diff", "--name-only", snap["head"]) or "").splitlines():
            if _excluded(path) or (path in snap["dirty"] and snap["dirty"][path] == _hash(path)):
                continue
            diff = _git("diff", "--unified=0", "--no-color", snap["head"], "--", path) or ""
            added = [line[1:] for line in diff.splitlines()
                     if line.startswith("+") and not line.startswith("+++")]
            if added:
                add(path, "\n".join(added))

    tokens = [f["tokens"] for f in files.values()]
    return {
        "files": files,
        "n_files": len(files),
        "words": sum(f["words"] for f in files.values()),
        "tokens": sum(tokens) if tokens and None not in tokens else (0 if not tokens else None),
        "tokenizer": TOKENIZER,
    }


def efficiency(deliv: Optional[Dict], cost_usd: float, wall_time: Optional[float] = None) -> Dict:
    """Deliverable tokens/words per USD (and per second if wall_time is given)."""
    if not deliv:
        return {}
    out = {
        "tokens_per_usd": round(deliv["tokens"] / cost_usd) if cost_usd and deliv["tokens"] is not None else None,
        "words_per_usd": round(deliv["words"] / cost_usd) if cost_usd else None,
    }
    if wall_time:
        out["tokens_per_s"] = round(deliv["tokens"] / wall_time, 1) if deliv["tokens"] is not None else None
        out["words_per_s"] = round(deliv["words"] / wall_time, 2)
    return out


def summary_line(deliv: Optional[Dict], eff: Dict) -> str:
    """One-line console summary."""
    if not deliv:
        return "Deliverables: (not measured — not a git repository)"
    tok = f"{deliv['tokens']} tokens" if deliv["tokens"] is not None else "tokens n/a (install tiktoken)"
    n = deliv["n_files"]
    parts = [f"Deliverables: {n} file{'' if n == 1 else 's'}, {deliv['words']} words, {tok}"]
    if eff.get("tokens_per_usd") is not None:
        parts.append(f"{eff['tokens_per_usd']} tokens/$")
    if eff.get("words_per_usd") is not None:
        parts.append(f"{eff['words_per_usd']} words/$")
    if eff.get("tokens_per_s") is not None:
        parts.append(f"{eff['tokens_per_s']} tokens/s")
    return " | ".join(parts) + (f"  [{TOKENIZER}]" if deliv["tokens"] is not None else "")
