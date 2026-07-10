"""
Fuzzy search using Levenshtein edit distance and Jaro-Winkler similarity.

Why fuzzy search?
- Handles typos: "paymnt" → "payment" (edit distance = 1)
- Handles OCR errors, autocorrect misses, phonetic similarities
- Essential for search UX: users should not get zero results for small mistakes

Implemented algorithms:
1. Levenshtein Distance — Wagner-Fischer O(m*n) DP, space-optimised to O(n)
2. Jaro-Winkler Similarity — better for short strings and name matching
3. fuzzy_match() — bulk matching with early exit optimisations
"""
import math
from typing import Dict, List, Set, Tuple


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Compute the Levenshtein edit distance between two strings.

    Operations allowed (each costs 1):
        - Insert a character
        - Delete a character
        - Substitute a character

    Algorithm: Wagner-Fischer dynamic programming.
    Time:  O(m * n)
    Space: O(n)  — only two rows kept at a time

    Examples:
        levenshtein_distance("payment", "paymnt")  → 1
        levenshtein_distance("search",  "serach")  → 2
        levenshtein_distance("",        "abc")     → 3
    """
    if s1 == s2:
        return 0
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    # Space optimisation: keep only the previous row
    prev = list(range(len2 + 1))  # base case: distance("", s2[:j]) = j

    for i, c1 in enumerate(s1, start=1):
        curr = [i]  # base case: distance(s1[:i], "") = i
        for j, c2 in enumerate(s2, start=1):
            cost = 0 if c1 == c2 else 1
            curr.append(min(
                curr[j - 1] + 1,        # insertion  into s1
                prev[j] + 1,            # deletion   from s1
                prev[j - 1] + cost,     # substitution (or match)
            ))
        prev = curr

    return prev[len2]


def jaro_similarity(s1: str, s2: str) -> float:
    """
    Jaro similarity — optimised for short strings.
    Returns a value in [0.0, 1.0]. 1.0 = identical.

    Matching window: max(|s1|, |s2|) // 2 - 1
    Matches within the window that are not transpositions contribute positively.
    """
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matched = [False] * len1
    s2_matched = [False] * len2
    matches = 0

    for i in range(len1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, len2)
        for j in range(lo, hi):
            if not s2_matched[j] and s1[i] == s2[j]:
                s1_matched[i] = True
                s2_matched[j] = True
                matches += 1
                break

    if matches == 0:
        return 0.0

    # Count transpositions
    t = 0
    k = 0
    for i in range(len1):
        if not s1_matched[i]:
            continue
        while not s2_matched[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1

    return (matches / len1 + matches / len2 + (matches - t / 2) / matches) / 3


def jaro_winkler_similarity(s1: str, s2: str, p: float = 0.1) -> float:
    """
    Jaro-Winkler similarity — boosts Jaro score for shared prefixes.

    `p` is the scaling factor for the prefix bonus (standard: 0.1).
    Shared prefix is capped at 4 characters.

    Returns a value in [0.0, 1.0].
    Better than Levenshtein for short strings and names.

    Examples:
        jaro_winkler_similarity("payment", "paymnet")  → ~0.977
        jaro_winkler_similarity("search",  "seach")    → ~0.956
    """
    jaro = jaro_similarity(s1, s2)
    if jaro == 0.0:
        return 0.0

    prefix_len = 0
    for c1, c2 in zip(s1[:4], s2[:4]):
        if c1 == c2:
            prefix_len += 1
        else:
            break

    return jaro + prefix_len * p * (1 - jaro)


def fuzzy_match(
    query_token: str,
    candidates: List[str],
    max_distance: int = 2,
) -> List[Tuple[str, int]]:
    """
    Find all candidate tokens within `max_distance` Levenshtein edits of `query_token`.

    Optimisations:
    1. Length filter: if |len(a) - len(b)| > max_distance, skip immediately (O(1))
    2. Common prefix check: if the first character differs and max_distance < 1, skip (O(1))

    Returns: [(matched_token, edit_distance), ...] sorted ascending by distance.
    """
    results: List[Tuple[str, int]] = []
    query_len = len(query_token)

    for candidate in candidates:
        # Optimisation 1: length difference alone exceeds threshold
        if abs(len(candidate) - query_len) > max_distance:
            continue
        dist = levenshtein_distance(query_token, candidate)
        if dist <= max_distance:
            results.append((candidate, dist))

    results.sort(key=lambda x: x[1])
    return results


def bulk_fuzzy_resolve(
    query_tokens: List[str],
    inverted_index: Dict[str, Set[str]],
    max_distance: int = 2,
) -> Dict[str, int]:
    """
    For each query token, find fuzzy-matching vocabulary tokens
    and collect the best (minimum) edit distance per document.

    Returns: {doc_id: min_edit_distance_across_all_tokens}

    This function is designed to be called in a ThreadPoolExecutor
    (it is CPU-bound pure Python with no asyncio dependencies).
    """
    vocab = list(inverted_index.keys())
    best: Dict[str, int] = {}

    for token in query_tokens:
        matches = fuzzy_match(token, vocab, max_distance=max_distance)
        for matched_token, dist in matches:
            for doc_id in inverted_index.get(matched_token, set()):
                if doc_id not in best or dist < best[doc_id]:
                    best[doc_id] = dist

    return best
