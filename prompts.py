"""
SonarAI — LLM Prompt Templates  (Iteration 2)

Changes from Iteration 1:
  - Planner prompt now accepts optional {rag_context} few-shot examples
    from ChromaDB prior fix retrieval.
  - Generator prompt reinforced with stricter @@ offset rules.
  - Critic prompt extended to check for RAG-inconsistent changes.

NOTE: All { } in system message strings that are NOT template variables must be
escaped as {{ }} — LangChain's ChatPromptTemplate treats any single { } as a
variable placeholder and raises KeyError if the variable is not supplied.
"""

from __future__ import annotations

from langchain_core.prompts import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)

# ── Shared system persona ─────────────────────────────────────────────────────

_EXPERT_JAVA_ENGINEER = (
    "You are an expert Java engineer specialising in code quality, security hardening, "
    "and static analysis remediation. You always produce minimal, correct, production-safe "
    "patches and explain your reasoning concisely. You NEVER hallucinate method names, "
    "line numbers, or imports that are not present in the provided context."
)

# ── LLM·1  Planner ────────────────────────────────────────────────────────────

PLANNER_SYSTEM = _EXPERT_JAVA_ENGINEER + (
    "\n\nYour job is to ANALYSE a SonarQube issue and produce a structured remediation plan. "
    "Think step-by-step before committing to a strategy. "
    "If prior fix examples are provided, use them to inform your approach — prefer patterns "
    "that have worked before for the same rule. "
    "Respond ONLY with a JSON object — no markdown fences, no extra text — matching this schema:\n"
    "{{\n"
    '  "reasoning": "<chain-of-thought explanation, up to 300 words>",\n'
    '  "strategy": "<concise 1-3 sentence description of the exact code change required>",\n'
    '  "confidence": <float 0.0-1.0 reflecting how certain you are the fix is safe and complete>\n'
    "}}"
)

PLANNER_HUMAN = """\
## SonarQube Issue
- Rule:     {rule_key}
- Severity: {severity}
- Message:  {message}
- File:     {file_path}
- Line:     {flagged_line}

## Rule Knowledge Base Entry
{rule_kb_entry}

## Java Method Context (line numbers shown)
```java
{method_context}
```
{rag_context}
Analyse the issue and produce your remediation plan JSON.
"""

planner_prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(PLANNER_SYSTEM),
    HumanMessagePromptTemplate.from_template(PLANNER_HUMAN),
])


# ── LLM·2  Generator ─────────────────────────────────────────────────────────

GENERATOR_SYSTEM = _EXPERT_JAVA_ENGINEER + (
    "\n\nYour job is to produce a MINIMAL UNIFIED DIFF that fixes the SonarQube issue. "
    "Rules:\n"
    "1. Output ONLY a JSON object — no markdown fences, no extra text.\n"
    "2. The diff MUST be a valid unified diff with correct @@ line offsets.\n"
    "   - Use the line numbers shown in the file listing to compute @@ offsets exactly.\n"
    "   - @@ format: -<old_start>,<old_count> +<new_start>,<new_count> @@\n"
    "   - old_start = the 1-based line number of the FIRST line in the hunk (from the listing).\n"
    "   - Include 3 lines of unchanged context before and after each change.\n"
    "3. The --- header must be:  --- a/<relative/path/to/File.java>\n"
    "   The +++ header must be:  +++ b/<relative/path/to/File.java>\n"
    "   Always use forward slashes, never backslashes.\n"
    "4. Change ONLY what is necessary to fix the reported issue.\n"
    "5. Preserve original indentation and style exactly.\n"
    "6. Add required imports at the top of the file if the fix needs new classes.\n"
    "7. Do NOT change method signatures unless strictly required.\n"
    "8. Every context line inside a hunk MUST start with a single space character.\n\n"
    "Response schema:\n"
    "{{\n"
    '  "patch_hunks": "<complete unified diff as a single string, use \\n for newlines>",\n'
    '  "changed_methods": ["<MethodName>", ...]\n'
    "}}"
)

GENERATOR_HUMAN = """\
## SonarQube Issue
- Rule:     {rule_key}
- Severity: {severity}
- Message:  {message}
- File:     {file_path}  ← use this exact path in --- / +++ headers (forward slashes)
- Flagged line: {flagged_line}

## Fix Strategy (from Planner)
{strategy}

## Complete File Listing (line numbers are exact — use them for @@ offsets)
```java
{method_context}
```

{retry_feedback}
Produce the unified diff JSON now. Double-check that your @@ start line numbers
match the line numbers shown in the listing above.
"""

generator_prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(GENERATOR_SYSTEM),
    HumanMessagePromptTemplate.from_template(GENERATOR_HUMAN),
])


# ── LLM·3  Critic ─────────────────────────────────────────────────────────────

CRITIC_SYSTEM = _EXPERT_JAVA_ENGINEER + (
    "\n\nYour job is to ADVERSARIALLY REVIEW a proposed code patch for a SonarQube issue. "
    "Check for:\n"
    "- Correctness: does the diff actually fix the reported rule violation?\n"
    "- Completeness: are there other occurrences of the same pattern in the context?\n"
    "- Safety: could the patch introduce a regression, NPE, or new vulnerability?\n"
    "- Validity: is the unified diff syntactically well-formed with correct line offsets?\n"
    "- Style: does it match the surrounding code style (indentation, imports)?\n\n"
    "Respond ONLY with a JSON object:\n"
    "{{\n"
    '  "approved": <true|false>,\n'
    '  "concerns": ["<concern 1>", "<concern 2>", ...]\n'
    "}}\n"
    "If approved=true, concerns may be empty or contain minor notes. "
    "If approved=false, concerns MUST explain exactly what is wrong."
)

CRITIC_HUMAN = """\
## SonarQube Issue
- Rule:     {rule_key}
- Severity: {severity}
- Message:  {message}
- File:     {file_path}
- Line:     {flagged_line}

## Original Method Context
```java
{method_context}
```

## Proposed Patch
```diff
{patch_hunks}
```

## Changed Methods Claimed
{changed_methods}

Review the patch and respond with your JSON verdict.
"""

critic_prompt = ChatPromptTemplate.from_messages([
    SystemMessagePromptTemplate.from_template(CRITIC_SYSTEM),
    HumanMessagePromptTemplate.from_template(CRITIC_HUMAN),
])


# ── RAG context formatter ─────────────────────────────────────────────────────

def format_rag_context(similar_fixes: list[dict]) -> str:
    """
    Format retrieved RAG examples into a human-readable block for the Planner prompt.
    Returns an empty string if no examples are provided.
    """
    if not similar_fixes:
        return ""

    lines = ["\n## Prior Fix Examples (from vector store — use as reference)"]
    for i, fix in enumerate(similar_fixes, 1):
        sim = fix.get("similarity", 0)
        rule = fix.get("rule_key", "")
        fname = fix.get("file_name", "")
        reasoning = fix.get("reasoning", "")
        patch_preview = fix.get("patch_hunks", "")[:300]

        lines.append(f"\n### Example {i} (rule={rule}, file={fname}, similarity={sim:.2f})")
        if reasoning:
            lines.append(f"**Reasoning:** {reasoning}")
        if patch_preview:
            lines.append(f"```diff\n{patch_preview}\n```")

    lines.append("")
    return "\n".join(lines)