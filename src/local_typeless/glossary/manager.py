"""Glossary Manager (WP1-A, owned by flash-infra).

Implements GlossaryProvider protocol defined in local_typeless.types.
Loads canonical terms and phonetic aliases from YAML, builds Whisper initial prompt,
formats LLM glossary prompt sections, and applies deterministic normalization.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Union

import yaml

from local_typeless.types import GlossaryProvider

logger = logging.getLogger(__name__)


def user_overlay_path(glossary_path: Union[Path, str]) -> Path:
    """Return the user overlay file path for a given glossary YAML path.

    E.g. config/glossary.yaml -> config/glossary.user.yaml
    """
    p = Path(glossary_path)
    return p.parent / f"{p.stem}.user.yaml"


class GlossaryManager(GlossaryProvider):
    """Manages computational lithography domain glossary and phonetic aliases."""

    def __init__(
        self,
        canonical_terms: List[Dict[str, str]],
        phonetic_aliases: Dict[str, str],
    ) -> None:
        self.canonical_terms = canonical_terms
        self.phonetic_aliases = phonetic_aliases

    @classmethod
    def from_yaml(cls, path: Union[Path, str]) -> GlossaryManager:
        """Construct a GlossaryManager from a glossary YAML file.

        Additionally loads <same dir>/<stem>.user.yaml if it exists.
        Merge rules:
        - Canonical terms appended, de-duplicated by term (case-sensitive; base wins).
        - Aliases: overlay keys override base keys.
        - Broken / empty overlay logs a warning and is ignored.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Glossary file not found: {p}")

        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        raw_canonical = data.get("canonical_terms", [])
        canonical_terms: List[Dict[str, str]] = []
        for item in raw_canonical:
            if isinstance(item, dict):
                canonical_terms.append(
                    {
                        "term": str(item.get("term", "")),
                        "definition": str(item.get("definition", "")),
                    }
                )
            elif isinstance(item, str):
                canonical_terms.append({"term": item, "definition": ""})

        raw_aliases = data.get("phonetic_aliases", {})
        phonetic_aliases: Dict[str, str] = {
            str(k): str(v) for k, v in raw_aliases.items()
        }

        # Check for user overlay file (<same dir>/<stem>.user.yaml)
        overlay_p = user_overlay_path(p)
        if overlay_p.exists():
            try:
                with overlay_p.open("r", encoding="utf-8") as f:
                    overlay_data = yaml.safe_load(f)
                if not isinstance(overlay_data, dict):
                    logger.warning(
                        "Glossary overlay %s is empty or not a mapping; ignoring.",
                        overlay_p,
                    )
                else:
                    seen_terms = {item["term"] for item in canonical_terms}
                    raw_overlay_canonical = overlay_data.get("canonical_terms", [])
                    if isinstance(raw_overlay_canonical, list):
                        for item in raw_overlay_canonical:
                            if isinstance(item, dict):
                                term_val = str(item.get("term", ""))
                                defn_val = str(item.get("definition", ""))
                            elif isinstance(item, str):
                                term_val = item
                                defn_val = ""
                            else:
                                continue
                            if term_val and term_val not in seen_terms:
                                seen_terms.add(term_val)
                                canonical_terms.append(
                                    {"term": term_val, "definition": defn_val}
                                )

                    raw_overlay_aliases = overlay_data.get("phonetic_aliases", {})
                    if isinstance(raw_overlay_aliases, dict):
                        for k, v in raw_overlay_aliases.items():
                            phonetic_aliases[str(k)] = str(v)
            except Exception as exc:
                logger.warning(
                    "Failed to load glossary overlay %s: %s; ignoring.",
                    overlay_p,
                    exc,
                )

        return cls(
            canonical_terms=canonical_terms,
            phonetic_aliases=phonetic_aliases,
        )

    def build_whisper_initial_prompt(self) -> str:
        """Generate a natural Chinese context prompt under 200 tokens for Whisper.

        Uses natural sentence phrasing rather than comma-separated lists to prevent
        punctuation artifacts and hallucination in Whisper.
        """
        return "今天討論 ILT 與 SMO 的 cost function，以及 High NA EUV 下的 OPC。"

    def build_llm_glossary_section(self) -> str:
        """Format terms and phonetic aliases into a Markdown section for system prompts."""
        lines: List[str] = [
            "### 專業術語與詞庫對照表（嚴格校正）",
            "",
            "#### 標準術語與定義：",
        ]
        for item in self.canonical_terms:
            term = item["term"]
            defn = item["definition"]
            if defn:
                lines.append(f"- **{term}**: {defn}")
            else:
                lines.append(f"- **{term}**")

        if self.phonetic_aliases:
            lines.extend([
                "",
                "#### 常見語音誤辨與同音字對照（口述中出現時請自動校正為標準術語）：",
            ])
            # Group aliases by target canonical term
            grouped: Dict[str, List[str]] = {}
            for alias, target in self.phonetic_aliases.items():
                grouped.setdefault(target, []).append(alias)

            for target, aliases in sorted(grouped.items()):
                aliases_str = ", ".join(f'"{a}"' for a in aliases)
                lines.append(f"- {aliases_str} -> **{target}**")

        return "\n".join(lines)

    def normalize_terms(self, text: str) -> str:
        """Apply deterministic post-normalization.

        Rules:
        1. Chinese phonetic aliases are NOT replaced deterministically (too ambiguous,
           e.g. '我在上海那邊開會' must not become '我在上High NA邊開會'). They are kept only
           in the LLM glossary section. Only pure ASCII phonetic aliases are applied.
        2. In Python regex, \\b treats CJK characters as word chars, which fails when
           English terms are adjacent to Chinese (e.g. '用ilt做優化').
           Use (?<![A-Za-z0-9]) and (?![A-Za-z0-9]) lookarounds instead of \\b.
        3. Only case-normalize canonical terms that contain at least one uppercase letter
           (e.g. 'ILT', 'High NA', 'EUV', 'OPC', 'Double Patterning'). Never touch
           all-lowercase generic words (e.g. 'mask', 'source', 'focus', 'dose', 'wafer').
        """
        if not text:
            return text

        result = text

        # 1. ASCII phonetic aliases only (sort by length descending)
        ascii_aliases = [
            (k, v)
            for k, v in self.phonetic_aliases.items()
            if re.match(r"^[A-Za-z0-9\- ]+$", k)
        ]
        ascii_aliases.sort(key=lambda kv: len(kv[0]), reverse=True)

        for alias, target in ascii_aliases:
            pattern = re.compile(
                rf"(?i)(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
            )
            result = pattern.sub(target, result)

        # 2. Canonical terms casing normalization: ONLY terms containing uppercase letters
        for item in self.canonical_terms:
            term = item["term"]
            # Only normalize terms containing at least one uppercase letter (acronyms / proper nouns)
            if any(c.isupper() for c in term) and re.match(r"^[A-Za-z0-9\- ]+$", term):
                pattern = re.compile(
                    rf"(?i)(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
                )
                result = pattern.sub(term, result)

        return result
