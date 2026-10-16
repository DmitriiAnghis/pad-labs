"""Tokenisation for lexical models (BM25, TF-IDF): lowercase words, stop words, Snowball stemming.

Stemming maps "picking / picked / picks" to "pick", so a query word matches all its forms.
English stemmer for Latin words, Russian stemmer for Cyrillic words (queries may be in Russian).
"""
from __future__ import annotations

import re
from functools import lru_cache

import snowballstemmer

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+(?:['’][a-z]+)?|[Ѐ-ӿ]+")

EN_STOP = set("""
a an and are as at be been but by can could did do does doing for from had has have having he her his how i
if in into is it its itself just me more most my no nor not of off on once only or other our out over own same
she should so some such than that the their them then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your yours also via e g eg etc
""".split())
RU_STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще
нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом
себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтобы без будто чего раз тоже себе под
будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда
зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много
разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
как каком какие каким какая какой
""".split())

_en = snowballstemmer.stemmer("english")
_ru = snowballstemmer.stemmer("russian")


@lru_cache(maxsize=200_000)
def _stem(token: str) -> str:
    if "Ѐ" <= token[0] <= "ӿ":
        return _ru.stemWord(token)
    return _en.stemWord(token)


def tokenize(text: str, stem: bool = True, stop: bool = True) -> list[str]:
    tokens = TOKEN_RE.findall(text.lower().replace("ё", "е"))
    out = []
    for t in tokens:
        if stop and (t in EN_STOP or t in RU_STOP):
            continue
        out.append(_stem(t) if stem else t)
    return out
