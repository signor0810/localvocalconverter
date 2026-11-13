"""LLM advisor for review mode: queries LLM for candidate definitions and proper nouns."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from local_typeless.review.detect import Candidate
from local_typeless.teach.advisor import ProposalItem, parse_json_reply

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """你是專有名詞與詞彙校正顧問。系統從使用者的口述歷史紀錄中找出了一批可能是專有名詞、拼寫變體、或是語音辨識有疑慮的詞彙。
請根據上下文，為每個候選詞提供：
1. 最可能的標準正確寫法（canonical）
2. 簡短中文定義（一兩句話，說明這是什麼）
3. 是否為專有名詞（is_proper_noun: true / false）

## 目前詞庫已有的標準術語
{terms}

## 輸出要求（嚴格）
只輸出一個 JSON 物件，不要任何說明文字或 Markdown，格式：
{
  "items": [
    {
      "term": "候選詞名稱",
      "canonical": "最可能的正確寫法",
      "definition": "簡短定義",
      "is_proper_noun": true
    }
  ]
}

規則：
- is_proper_noun：若為通用日常詞彙而非技術/領域專有名詞，設為 false。
- canonical：專有名詞請給予正確的大小寫或拼寫。若無法確定，維持原詞原樣，絕對不要編造。
- definition：簡短說明其在該領域的意義（20 字以內）。
"""


@dataclass
class Answer:
    """User response to a review question."""

    choice: str = "skip"  # Option string, or "custom", "not_proper_noun", "skip"
    custom_text: str = ""


@dataclass
class Question:
    """A review question displayed to the user."""

    candidate: Candidate
    question_text: str
    contexts: List[str]
    options: List[str]
    suggested_term: str = ""
    definition: str = ""
    is_proper_noun: bool = True


def _glossary_terms_str(glossary: Any) -> str:
    if glossary is None:
        return "（無）"
    terms = getattr(glossary, "canonical_terms", None) or []
    out = []
    for t in terms:
        if isinstance(t, dict):
            out.append(str(t.get("term", "")))
        else:
            out.append(str(t))
    valid = [t for t in out if t]
    return ", ".join(valid) if valid else "（無）"


def _format_user_prompt(candidates: Sequence[Candidate]) -> str:
    lines = ["請分析以下候選詞彙：\n"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"### 候選 {i}")
        lines.append(f"- 詞彙：{c.term}")
        if c.variants and len(c.variants) > 1:
            lines.append(f"- 觀察到的寫法：{', '.join(c.variants)}")
        if c.alias_target:
            lines.append(f"- 整理後替換為：{c.alias_target}")
        if c.contexts:
            lines.append("- 上下文：")
            for ctx in c.contexts:
                lines.append(f"  * {ctx}")
        lines.append("")
    return "\n".join(lines)


def build_questions(
    candidates: Sequence[Candidate],
    glossary: Any = None,
    llm: Any = None,
    prompt_path: Optional[Path] = None,
) -> Tuple[List[Question], bool]:
    """Build review questions from candidates.

    Calls LLM once if candidates is non-empty. If LLM fails or replies non-JSON,
    gracefully falls back to rule-based questions with llm_ok=False.
    """
    if not candidates:
        return [], True

    # Build base rule-based questions first
    rule_questions: List[Question] = []
    for c in candidates:
        opts: List[str] = []
        for v in c.variants:
            if v and v not in opts:
                opts.append(v)
        if c.alias_target and c.alias_target not in opts:
            opts.append(c.alias_target)
        if not opts and c.term:
            opts.append(c.term)

        rule_questions.append(
            Question(
                candidate=c,
                question_text=f"你說的是哪一個？（{c.term}，出現 {c.count} 次）",
                contexts=list(c.contexts),
                options=opts,
                suggested_term="",
                definition="",
                is_proper_noun=True,
            )
        )

    if llm is None:
        return rule_questions, False

    # Read prompt template
    prompt_tpl = DEFAULT_PROMPT
    if prompt_path is not None and prompt_path.is_file():
        try:
            prompt_tpl = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to read prompt file %s: %s", prompt_path, e)
    else:
        builtin_prompt = Path("prompts/review.md")
        if builtin_prompt.is_file():
            try:
                prompt_tpl = builtin_prompt.read_text(encoding="utf-8")
            except Exception:
                pass

    terms_str = _glossary_terms_str(glossary)
    try:
        system_prompt = prompt_tpl.replace("{terms}", terms_str)
    except Exception:
        system_prompt = DEFAULT_PROMPT.replace("{terms}", terms_str)

    user_text = _format_user_prompt(candidates)

    try:
        reply = llm.refine(user_text, system_prompt)
        parsed = parse_json_reply(reply)
        raw_items = parsed.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("Expected 'items' list in JSON reply")

        # Map by candidate term lower and index
        by_term: Dict[str, dict] = {}
        for item in raw_items:
            if isinstance(item, dict):
                t = str(item.get("term", "")).strip().lower()
                if t:
                    by_term[t] = item

        questions: List[Question] = []
        for i, q in enumerate(rule_questions):
            c = q.candidate
            # Match by name only: pairing by position would attach a suggestion to the wrong term
            # whenever the LLM skips or reorders an item.
            item = by_term.get(c.term.lower())
            if item is None:
                for v in c.variants:
                    item = by_term.get(v.strip().lower())
                    if item is not None:
                        break

            suggested = ""
            definition = ""
            is_proper = True
            if item:
                suggested = str(item.get("canonical", "")).strip()
                definition = str(item.get("definition", "")).strip()
                is_proper = bool(item.get("is_proper_noun", True))

            opts = list(q.options)
            if suggested and suggested not in opts:
                opts.insert(0, suggested)

            questions.append(
                Question(
                    candidate=c,
                    question_text=q.question_text,
                    contexts=q.contexts,
                    options=opts,
                    suggested_term=suggested,
                    definition=definition,
                    is_proper_noun=is_proper,
                )
            )

        return questions, True

    except Exception as e:
        logger.warning("LLM call failed in build_questions: %s", e)
        return rule_questions, False


def to_items(
    answers: Mapping[int, Answer | dict] | Sequence[Answer | dict],
    questions: Sequence[Question],
) -> List[ProposalItem]:
    """Translate answered review questions into ProposalItems (term and alias)."""
    items: List[ProposalItem] = []
    seen_keys: set = set()

    for idx, q in enumerate(questions):
        ans = None
        if isinstance(answers, Mapping):
            ans = answers.get(idx)
        elif idx < len(answers):
            ans = answers[idx]

        if ans is None:
            continue

        choice = ans.choice if isinstance(ans, Answer) else ans.get("choice", "skip")
        custom_text = ans.custom_text if isinstance(ans, Answer) else ans.get("custom_text", "")

        choice = choice.strip()
        custom_text = custom_text.strip()

        if choice in ("skip", "not_proper_noun", ""):
            continue

        canonical = custom_text if choice == "custom" else choice
        if not canonical:
            continue

        # 1. Add canonical term
        term_key = ("term", canonical.lower())
        if term_key not in seen_keys:
            seen_keys.add(term_key)
            items.append(
                ProposalItem(
                    kind="term",
                    key=canonical,
                    value=q.definition,
                    source="review",
                )
            )

        # 2. Add aliases for any variant that differs from canonical
        all_variants = list(q.candidate.variants)
        if q.candidate.term and q.candidate.term not in all_variants:
            all_variants.append(q.candidate.term)
        if q.candidate.alias_target and q.candidate.alias_target not in all_variants:
            all_variants.append(q.candidate.alias_target)

        for v in all_variants:
            v_clean = v.strip()
            if v_clean and v_clean.lower() != canonical.lower():
                alias_key = ("alias", v_clean.lower())
                if alias_key not in seen_keys:
                    seen_keys.add(alias_key)
                    items.append(
                        ProposalItem(
                            kind="alias",
                            key=v_clean,
                            value=canonical,
                            source="review",
                        )
                    )

    return items
