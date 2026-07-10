"""
Prefix Tree (Trie) implementation.

Why a Trie?
- O(k) prefix search where k = prefix length (vs O(n*k) for linear scan)
- O(k) autocomplete traversal
- Memory-efficient for shared prefixes ("pay", "payment", "payments" share "pay" path)

Used in the Search Engine for:
- Instant prefix matching: "pay" → ["payment", "payments", "payroll"]
- Autocomplete suggestions
- Fast candidate narrowing before scoring
"""
from typing import Dict, List, Optional, Set


class TrieNode:
    """
    A single node in the Trie.
    Uses __slots__ to reduce per-instance memory overhead.
    """
    __slots__ = ("children", "is_end", "doc_ids")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end: bool = False          # True if a complete word ends here
        self.doc_ids: Set[str] = set()     # docs that contain the word passing through this node


class Trie:
    """
    A character-level Trie (Prefix Tree) supporting:
    - insert(word, doc_id)       — O(k) where k = len(word)
    - search(prefix) → doc_ids  — O(k) — all docs containing a word with this prefix
    - autocomplete(prefix)       — O(k + m) where m = number of matching words
    - remove(word, doc_id)       — O(k)
    """

    def __init__(self) -> None:
        self.root = TrieNode()

    def insert(self, word: str, doc_id: str) -> None:
        """
        Insert a (word, doc_id) pair into the Trie.
        Every node along the path stores the doc_id, enabling O(k) prefix lookup.
        """
        node = self.root
        for char in word:
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
            node.doc_ids.add(doc_id)
        node.is_end = True

    def remove(self, word: str, doc_id: str) -> None:
        """
        Remove doc_id association from all nodes along the word path.
        Does NOT delete nodes — keeps the Trie structure intact for other docs.
        """
        node = self.root
        for char in word:
            if char not in node.children:
                return
            node = node.children[char]
            node.doc_ids.discard(doc_id)

    def search_prefix(self, prefix: str) -> Set[str]:
        """
        Return all doc_ids that contain at least one word starting with `prefix`.
        Time complexity: O(k) where k = len(prefix).
        """
        node = self.root
        for char in prefix:
            if char not in node.children:
                return set()
            node = node.children[char]
        # Every doc_id at this node contains a word with the given prefix
        return set(node.doc_ids)

    def autocomplete(self, prefix: str, max_suggestions: int = 10) -> List[str]:
        """
        Return complete words starting with `prefix` via DFS traversal.
        Results are lexicographically ordered.
        Time complexity: O(k + m) where m = number of matching words.
        """
        node = self.root
        for char in prefix:
            if char not in node.children:
                return []
            node = node.children[char]

        results: List[str] = []
        self._dfs_collect(node, prefix, results, max_suggestions)
        return results

    def _dfs_collect(
        self,
        node: TrieNode,
        current: str,
        results: List[str],
        max_results: int,
    ) -> None:
        if len(results) >= max_results:
            return
        if node.is_end:
            results.append(current)
        for char, child in sorted(node.children.items()):  # sorted → lexicographic order
            self._dfs_collect(child, current + char, results, max_results)

    def all_words(self) -> List[str]:
        """Return all words stored in the Trie (debug / stats utility)."""
        results: List[str] = []
        self._dfs_collect(self.root, "", results, max_results=100_000)
        return results
