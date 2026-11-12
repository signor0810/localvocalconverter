"""Write the user overlays (glossary.user.yaml / prompts/<mode>.user.md). The ONLY writer of them.

Atomic (tmp + replace), merge-only (earlier learned entries are never dropped), one-level undo
via <file>.bak (the previous content; empty file if the overlay did not exist).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

from local_typeless.teach.advisor import ProposalItem, _norm_rule


def glossary_overlay_path(glossary_path) -> Path:
    try:
        from local_typeless.glossary.manager import user_overlay_path  # flash-infra WP-T1

        return Path(user_overlay_path(Path(glossary_path)))
    except ImportError:
        p = Path(glossary_path)
        return p.with_name(f"{p.stem}.user.yaml")


def prompt_overlay_path(mode, prompts_dir) -> Path:
    mode_str = getattr(mode, "value", str(mode))
    try:
        from local_typeless.config.loader import user_prompt_path  # flash-infra WP-T1

        return Path(user_prompt_path(mode, Path(prompts_dir)))
    except ImportError:
        return Path(prompts_dir) / f"{mode_str}.user.md"


def _bak(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path) -> None:
    _atomic_write(_bak(path), path.read_text(encoding="utf-8") if path.exists() else "")


@dataclass
class ApplyResult:
    glossary_path: Path
    prompt_path: Path
    glossary_changed: bool = False
    prompt_changed: bool = False
    added: Dict[str, int] = field(default_factory=lambda: {"term": 0, "alias": 0, "rule": 0})


def read_rules(path: Path) -> List[str]:
    if not path.exists():
        return []
    rules = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            rules.append(line[2:].strip())
    return rules


def apply(items: Iterable[ProposalItem], mode, cfg) -> ApplyResult:
    items = [i for i in items if i.key.strip()]
    gpath = glossary_overlay_path(cfg.glossary_path)
    ppath = prompt_overlay_path(mode, cfg.prompts_dir)
    res = ApplyResult(gpath, ppath)

    # ---- glossary overlay
    g_items = [i for i in items if i.kind in ("term", "alias")]
    if g_items:
        data = {}
        if gpath.exists():
            data = yaml.safe_load(gpath.read_text(encoding="utf-8")) or {}
        terms = [t for t in (data.get("canonical_terms") or []) if isinstance(t, (dict, str))]
        terms = [t if isinstance(t, dict) else {"term": t, "definition": ""} for t in terms]
        aliases = {str(k): str(v) for k, v in (data.get("phonetic_aliases") or {}).items()}
        have = {str(t.get("term", "")).lower() for t in terms}
        for i in g_items:
            if i.kind == "term":
                if i.key.strip().lower() not in have:
                    terms.append({"term": i.key.strip(), "definition": i.value.strip()})
                    have.add(i.key.strip().lower())
                    res.added["term"] += 1
            elif aliases.get(i.key.strip()) != i.value.strip():
                aliases[i.key.strip()] = i.value.strip()
                res.added["alias"] += 1
        _backup(gpath)
        out = dict(data)
        out["canonical_terms"] = terms
        out["phonetic_aliases"] = aliases
        _atomic_write(gpath, yaml.safe_dump(out, allow_unicode=True, sort_keys=False))
        res.glossary_changed = True
    elif _bak(gpath).exists():
        _bak(gpath).unlink()      # undo is for the last apply only

    # ---- prompt overlay
    rules = [i.key.strip() for i in items if i.kind == "rule"]
    if rules:
        existing_text = ppath.read_text(encoding="utf-8") if ppath.exists() else ""
        seen = {_norm_rule(r) for r in read_rules(ppath)}
        new = []
        for r in rules:
            if _norm_rule(r) not in seen:
                seen.add(_norm_rule(r))
                new.append(r)
        res.added["rule"] = len(new)
        _backup(ppath)
        body = existing_text.rstrip("\n")
        body = (body + "\n" if body else "") + "".join(f"- {r}\n" for r in new)
        _atomic_write(ppath, body)
        res.prompt_changed = True
    elif _bak(ppath).exists():
        _bak(ppath).unlink()
    return res


def revert(mode, cfg) -> List[Path]:
    """Restore the overlays from the .bak left by the last apply(). Returns restored paths."""
    restored = []
    for path in (glossary_overlay_path(cfg.glossary_path), prompt_overlay_path(mode, cfg.prompts_dir)):
        b = _bak(path)
        if b.exists():
            prev = b.read_text(encoding="utf-8")
            if prev == "":
                path.unlink(missing_ok=True)       # the overlay did not exist before the apply
            else:
                _atomic_write(path, prev)
            b.unlink()
            restored.append(path)
    return restored
