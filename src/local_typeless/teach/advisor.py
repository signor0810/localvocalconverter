"""Turn teach samples into reviewable proposals: deterministic alignment + ONE internal-LLM call."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from local_typeless.teach import align
from local_typeless.teach.store import TeachSample
from local_typeless.types import LLMError

logger = logging.getLogger(__name__)

MAX_RULES = 8
SRC_ALIGN = "比對"
SRC_LLM = "LLM"
KINDS = ("term", "alias", "rule")


class TeachError(Exception):
    """User-presentable failure of the analysis (message is Chinese)."""


@dataclass
class ProposalItem:
    kind: str            # "term" | "alias" | "rule"
    key: str             # term / heard-as / rule text
    value: str = ""      # definition / correct term / ""
    source: str = SRC_LLM
    selected: bool = True


@dataclass
class Proposal:
    items: List[ProposalItem] = field(default_factory=list)

    def of(self, kind: str) -> List[ProposalItem]:
        return [i for i in self.items if i.kind == kind]


META_PROMPT = """你是「口述整理提示詞」的調校顧問。使用者用語音口述，系統先語音辨識（原文），再由 LLM 依下方「目前的整理提示詞」整理成「目前結果」。使用者貼出了他實際想要的「使用者期望」。
請比較每筆樣本，找出系統的系統性缺失，提出改進建議。

## 目前的整理提示詞（模式：{mode}）
{prompt}

## 目前詞庫（已有的標準術語）
{terms}

## 輸出要求（嚴格）
只輸出一個 JSON 物件，不要任何說明文字或 Markdown，格式：
{{"glossary_terms":[{{"term":"標準術語","definition":"簡短定義"}}],
 "phonetic_aliases":{{"聽錯的字":"正確詞"}},
 "prompt_rules":["一條通用整理規則"]}}

限制：
- glossary_terms：只列詞庫尚未有、且使用者期望中出現的專有名詞／縮寫。
- phonetic_aliases：語音辨識聽錯的字（原文中的寫法）對應到正確詞。
- prompt_rules：最多 {max_rules} 條，每條一行，必須是「通用規則」（描述這個模式該如何整理），不得複製或引用樣本的具體內容，也不得與目前提示詞重複。
- 不確定就留空陣列／空物件，絕對不要編造。
"""

RETRY_SUFFIX = "\n\n【重要】你上一次的回覆不是合法 JSON。請只輸出符合上述格式的 JSON 物件，不要任何其他文字。"


def build_samples_block(samples: Iterable[TeachSample]) -> str:
    parts = []
    for n, s in enumerate(samples, 1):
        parts.append(f"### 樣本 {n}\n原文：{s.raw_text}\n目前結果：{s.output_text}\n使用者期望：{s.expected_text}")
    return "\n\n".join(parts)


def _glossary_terms(glossary: Any) -> List[str]:
    terms = getattr(glossary, "canonical_terms", None) or []
    out = []
    for t in terms:
        out.append(str(t.get("term", "")) if isinstance(t, dict) else str(t))
    return [t for t in out if t]


def _glossary_aliases(glossary: Any) -> Dict[str, str]:
    return dict(getattr(glossary, "phonetic_aliases", None) or {})


def parse_json_reply(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(t[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data


def _norm_rule(r: str) -> str:
    return re.sub(r"\s+", "", r).lstrip("-*• ").lower()


def propose(
    samples: List[TeachSample],
    mode: str,
    current_prompt: str,
    glossary: Any,
    llm: Any,
    existing_rules: Iterable[str] = (),
) -> Proposal:
    """llm: an internal LLMClient (create_llm_client(cfg.llm)); nothing else is contacted."""
    if not samples:
        raise TeachError("請先加入至少一筆樣本。")
    known = _glossary_terms(glossary)
    known_lower = {t.lower() for t in known}
    aliases = _glossary_aliases(glossary)
    rules_seen = {_norm_rule(r) for r in existing_rules}

    items: List[ProposalItem] = []
    keys = set()

    def add(item: ProposalItem) -> None:
        k = (item.kind, item.key.strip().lower(), item.value.strip().lower() if item.kind == "alias" else "")
        if not item.key.strip() or k in keys:
            return
        keys.add(k)
        items.append(item)

    for s in samples:
        for a in align.mine(s.raw_text, s.expected_text, known):
            if a.kind == "alias":
                if aliases.get(a.key) == a.value:
                    continue
                add(ProposalItem("alias", a.key, a.value, SRC_ALIGN))
            else:
                add(ProposalItem("term", a.key, a.value, SRC_ALIGN))

    data = _ask_llm(samples, mode, current_prompt, known, llm)

    for t in data.get("glossary_terms") or []:
        if isinstance(t, dict):
            term, defn = str(t.get("term", "")).strip(), str(t.get("definition", "")).strip()
        else:
            term, defn = str(t).strip(), ""
        if term and term.lower() not in known_lower:
            add(ProposalItem("term", term, defn, SRC_LLM))
    raw_aliases = data.get("phonetic_aliases") or {}
    if isinstance(raw_aliases, dict):
        for heard, correct in raw_aliases.items():
            heard, correct = str(heard).strip(), str(correct).strip()
            if heard and correct and heard != correct and aliases.get(heard) != correct:
                add(ProposalItem("alias", heard, correct, SRC_LLM))
    rules = data.get("prompt_rules") or []
    if isinstance(rules, list):
        n = 0
        for r in rules:
            text = " ".join(str(r).split()).lstrip("-*• ").strip()
            if not text or _norm_rule(text) in rules_seen or text in current_prompt:
                continue
            if n >= MAX_RULES:
                break
            rules_seen.add(_norm_rule(text))
            add(ProposalItem("rule", text, "", SRC_LLM))
            n += 1
    # an alias target should also exist as a glossary term
    for it in [i for i in items if i.kind == "alias"]:
        if it.value.lower() not in known_lower and not any(
                i.kind == "term" and i.key.lower() == it.value.lower() for i in items):
            if align._HAS_LATIN.search(it.value):
                add(ProposalItem("term", it.value, "", it.source))
    return Proposal(_drop_longer_aliases(items))


def _drop_longer_aliases(items: List[ProposalItem]) -> List[ProposalItem]:
    """Same target + one key contains the other -> keep only the shorter key."""
    aliases = [i for i in items if i.kind == "alias"]
    drop = set()
    for a in aliases:
        for b in aliases:
            if (a is not b and a.value.lower() == b.value.lower() and b.key.lower() != a.key.lower()
                    and b.key.lower() in a.key.lower()):
                drop.add(id(a))
    return [i for i in items if id(i) not in drop]


def _ask_llm(samples, mode, current_prompt, known_terms, llm) -> Dict[str, Any]:
    meta = META_PROMPT.format(
        mode=mode, prompt=current_prompt.strip() or "（空）",
        terms="、".join(known_terms) or "（空）", max_rules=MAX_RULES)
    block = build_samples_block(samples)
    reply = ""
    for attempt in (0, 1):
        try:
            reply = llm.refine(raw_text=block, system_prompt=meta + (RETRY_SUFFIX if attempt else ""))
        except LLMError as e:
            raise TeachError(f"無法連線到內部 LLM，分析失敗：{e}") from e
        try:
            return parse_json_reply(reply)
        except ValueError as e:      # json.JSONDecodeError is a ValueError
            logger.warning("teach advisor: unparseable LLM reply (attempt %d): %s", attempt + 1, e)
    raise TeachError("LLM 回覆格式不正確（不是有效的 JSON），請再試一次。")
