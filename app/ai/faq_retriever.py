"""Keyword and BM25-style FAQ retriever for grounding LLM Brain responses.

- Zero external server requirement
- Zero new heavy dependencies (pure standard library)
- Matches across question variants, tags, and terms normalized by the glossary
- Returns top-3 matches above a minimum relevance threshold; if best score < threshold, returns []
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Sequence
from app.ai.faq import FAQEntry, load_faq_knowledge_base
from app.ai.glossary_correction import correct_glossary

TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9ऀ-ॿ]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_PATTERN.findall(text) if len(t) > 1]


class FAQRetriever:
    """BM25-inspired in-memory retriever over FAQ question variants and tags."""

    def __init__(self, entries: Sequence[FAQEntry], min_score: float = 2.0, k1: float = 1.5, b: float = 0.75):
        self.entries = list(entries)
        self.min_score = min_score
        self.k1 = k1
        self.b = b
        self.doc_len: list[int] = []
        self.doc_freq: dict[str, int] = Counter()
        self.doc_tokens: list[Counter] = []
        self._build_index()

    def _build_index(self) -> None:
        if not self.entries:
            return
        total_len = 0
        for entry in self.entries:
            # Combine all question variants + tags + canonical terms
            combined_text = " ".join(entry.questions) + " " + " ".join(entry.tags)
            tokens = _tokenize(combined_text)
            counts = Counter(tokens)
            self.doc_tokens.append(counts)
            d_len = len(tokens)
            self.doc_len.append(d_len)
            total_len += d_len
            for t in counts:
                self.doc_freq[t] += 1

        self.avg_doc_len = total_len / max(1, len(self.entries))
        self.num_docs = len(self.entries)

    def retrieve(self, query: str, top_k: int = 3) -> list[tuple[FAQEntry, float]]:
        """Retrieves top_k FAQ entries for query. Returns empty list if top score < min_score."""
        if not self.entries or not query or not query.strip():
            return []

        # Run glossary correction over query so mis-spellings map to canonical terms
        normalized_query = correct_glossary(query)
        q_tokens = _tokenize(normalized_query)
        if not q_tokens:
            return []

        scores: list[float] = [0.0] * self.num_docs
        for token in q_tokens:
            if token not in self.doc_freq:
                continue
            df = self.doc_freq[token]
            idf = math.log(1.0 + (self.num_docs - df + 0.5) / (df + 0.5))

            for i in range(self.num_docs):
                tf = self.doc_tokens[i].get(token, 0)
                if tf > 0:
                    numerator = tf * (self.k1 + 1.0)
                    denominator = tf + self.k1 * (1.0 - self.b + self.b * (self.doc_len[i] / self.avg_doc_len))
                    scores[i] += idf * (numerator / denominator)

        # Pair with entries
        ranked = [(self.entries[i], scores[i]) for i in range(self.num_docs) if scores[i] >= self.min_score]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def retrieve_context(self, query: str, top_k: int = 3) -> list[str]:
        """Returns plain context strings for prompt injection (answers with sources)."""
        matches = self.retrieve(query, top_k=top_k)
        if not matches:
            return []
        context = []
        for entry, score in matches:
            src = f" [Source: {entry.source}]" if entry.source else ""
            context.append(f"{entry.answer}{src}")
        return context
