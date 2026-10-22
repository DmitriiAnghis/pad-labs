"""Prompt variants (the prompt experiment compares them on the same retrieved context).

* ``basic``  - "answer using the context": the minimal RAG prompt;
* ``strict`` - explicit grounding rules: only the context, a fixed refusal sentence when the
               context has no answer, numbered citations [n], answer language, no invented menus.
"""
from __future__ import annotations

REFUSAL = {
    "en": "I don't have enough information in the documentation to answer this question.",
    "ru": "В документации недостаточно информации, чтобы ответить на этот вопрос.",
}

REFUSAL_MARKERS = [
    "don't have enough information",
    "do not have enough information",
    "not enough information",
    "недостаточно информации",
    "нет информации",
    "не содержит информации",
]

LANG_NAME = {"en": "English", "ru": "Russian"}

SYSTEM_BASIC = "You are an assistant for the Odoo Inventory documentation. Answer the question using the context."

SYSTEM_STRICT = """You are a support assistant for the Odoo 18 Inventory and Barcode apps (warehouse management).
Answer ONLY with facts from the numbered context passages below. Rules:
1. If the passages do not contain the answer, reply exactly: "{refusal}" and nothing else.
2. Cite the passages you used with their numbers in square brackets, e.g. [1] or [2][3], right after the sentence they support.
3. Do not invent menu paths, settings, buttons or numbers that are not in the passages.
4. Be concise. For procedures, give numbered steps with the exact menu path, e.g. Inventory > Configuration > Settings.
5. Write the answer in {language}. Keep Odoo menu and button names in English as they appear in the passages.
6. If the question has several parts, answer each part. If the passages cover only some parts, answer those and say which part the documentation does not cover."""

USER_TEMPLATE = """Context passages:

{context}

Question: {question}"""


def build_messages(prompt: str, question: str, context: str, language: str) -> list[dict]:
    if prompt == "basic":
        system = SYSTEM_BASIC + f" Answer in {LANG_NAME.get(language, 'English')}."
    else:
        system = SYSTEM_STRICT.format(refusal=REFUSAL.get(language, REFUSAL["en"]), language=LANG_NAME.get(language, "English"))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": USER_TEMPLATE.format(context=context, question=question)},
    ]


def is_refusal(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in REFUSAL_MARKERS) and len(text) < 400


TRANSLATE_PROMPT = """Translate this question about the Odoo warehouse (Inventory) software into English.
Keep product names, menu names and abbreviations. Output only the English translation.

Question: {question}"""


REWRITE_PROMPT = """You prepare search queries for the English Odoo 18 Inventory and Barcode documentation.
Rewrite the user's question into 1 to {max_queries} short, self-contained English search queries:
- translate the question to English if it is in another language;
- if it asks about several different things, write one query per thing; otherwise write exactly one query;
- use Odoo terms where the user describes a feature in everyday words (e.g. "wave picking" -> "wave transfers",
  "storage bins" -> "locations"); keep product names, menu names, codes and abbreviations as they are;
- do not answer the question and do not add topics it does not ask about.
Reply with JSON only: {{"queries": ["...", "..."]}}

Question: {question}"""
