"""Semantic review classification, replacing keyword matching.

The lexical classifier in phase3_voc matches substrings from a hand-written
Bahasa Indonesia keyword list. It cannot see meaning: a review saying "uang
saya belum kembali sampai sekarang" (my money still has not come back) carries
no word from the payment/refund list, and a review mentioning "dokter" in
passing is counted as a consultation complaint.

This uses the `voc-theme-classification` sphere use case (template 21688),
which was provisioned for exactly this and had never been called. The model
reads the review, assigns one theme from the journey's own taxonomy, and — the
part the lexicon could never give us — returns the phrase it matched on and an
English gloss, so an Indonesian classification is auditable by a reviewer who
does not read Indonesian.

Why this and not a vector index: sphere exposes no embeddings endpoint, and the
corpus is Indonesian, where a naive multilingual embedding would be the weakest
link. The LLM is doing the semantic work either way; this skips the index.

Falls back to the lexical classifier per batch. A theme count is evidence that
gets escalated to Code Scout, so a failed batch must degrade to the old
behaviour, never to an empty bucket that reads as "no complaints".
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger("careloop.semantic_voc")

BATCH_SIZE = 20          # 40 ran 50-75 s per call and the ingress in front of sphere cuts at ~60 s
                         # (our log: HTTP 504; sphere's log: SUCCESS at 74 s). 20 keeps calls ~20-35 s.
MAX_TEXT_CHARS = 400     # reviews are short; this only guards a pathological one
NEGATIVE_MAX_SCORE = 2   # mirrors phase3_voc: only these are ever bucketed into themes
PARALLEL_BATCHES = 5     # 92 negatives = 5 batches of 20; five in flight is one round (~40 s) instead of two

LLMCall = Callable[[dict[str, Any]], dict[str, Any]]


def _score(review: dict) -> int:
    try:
        return int(review.get("score", 5))
    except (TypeError, ValueError):
        return 5


def classify_reviews(llm: Optional[LLMCall], reviews: list[dict], themes_cfg: list[dict],
                     lexical_fallback: Callable[[str, list[dict]], str],
                     scope_hint: Optional[str] = None) -> tuple[list[str], dict]:
    """Returns (theme per review, meta). Order matches `reviews`.

    `scope_hint` is the user's own words when the run is scoped. It biases which
    theme a genuinely ambiguous review lands in; it cannot invent a theme,
    because the taxonomy is closed and anything outside it is 'unmapped'.
    """
    taxonomy = [{"name": t["name"], "routing_stage": t["routing_stage"],
                 "examples": t.get("keywords", [])[:6]} for t in themes_cfg]
    out: list[str] = [""] * len(reviews)
    meta = {"classifier": "semantic", "batches": 0, "fallback_batches": 0,
            "glosses": {}, "matched_phrases": {}}

    if llm is None:
        return [lexical_fallback(r.get("text", ""), themes_cfg) for r in reviews], \
               {**meta, "classifier": "lexical", "reason": "no llm configured"}

    valid = {t["name"] for t in themes_cfg} | {"unmapped"}

    # Only negative reviews are ever bucketed into themes (phase3_voc), so
    # classifying the rest is pure cost. First live run sent all 600 in 15
    # batches at ~45 s each — eleven minutes, 6.5x the necessary work. Positive
    # reviews get "unmapped", which is exactly what run_voc does with them.
    negative_idx = [i for i, r in enumerate(reviews)
                    if _score(r) <= NEGATIVE_MAX_SCORE]
    for i, r in enumerate(reviews):
        if i not in set(negative_idx):
            out[i] = "unmapped"
    meta["reviews_sent_to_llm"] = len(negative_idx)

    def _run_batch(batch_idx: list[int]) -> tuple[list[int], dict]:
        payload = {
            "taxonomy": taxonomy,
            "scope_hint": scope_hint or "",
            # 21688 v6 classifies against a polarity-matched taxonomy; this caller
            # only ever sends complaint reviews and the complaint taxonomy.
            "polarity": "negative",
            "reviews": [{"review_id": str(i),
                         "text": (reviews[i].get("text") or "")[:MAX_TEXT_CHARS],
                         "rating": reviews[i].get("score")} for i in batch_idx],
        }
        try:
            res = llm({"reviews_batch": payload})
            return batch_idx, {str(c["review_id"]): c for c in (res.get("classifications") or [])}
        except Exception as exc:
            logger.warning("semantic VoC batch starting at %s failed (%s) — lexical for this batch",
                           batch_idx[:1], exc)
            return batch_idx, {}

    batches = [negative_idx[i:i + BATCH_SIZE] for i in range(0, len(negative_idx), BATCH_SIZE)]
    meta["batches"] = len(batches)
    with ThreadPoolExecutor(max_workers=PARALLEL_BATCHES) as pool:
        results = list(pool.map(_run_batch, batches))

    for batch_idx, rows in results:
        missing = 0
        for i in batch_idx:
            row = rows.get(str(i))
            theme = (row or {}).get("theme")
            if theme not in valid:
                missing += 1
                out[i] = lexical_fallback(reviews[i].get("text", ""), themes_cfg)
                continue
            out[i] = theme
            if row.get("english_gloss"):
                meta["glosses"][str(i)] = row["english_gloss"]
            if row.get("matched_phrase"):
                meta["matched_phrases"][str(i)] = row["matched_phrase"]
        if missing == len(batch_idx):
            meta["fallback_batches"] += 1

    if meta["batches"] and meta["fallback_batches"] == meta["batches"]:
        meta["classifier"] = "lexical"
        meta["reason"] = "every batch fell back"
    elif meta["fallback_batches"]:
        meta["classifier"] = "semantic_partial"
    return out, meta
