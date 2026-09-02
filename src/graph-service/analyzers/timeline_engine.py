"""
timeline_engine.py — Entity-level timeline and change history analyzer.

For any graph entity (Repository, Service, Module, Function, Class, File),
computes:
  - birthDate       : ISO timestamp of the first commit that touched this entity
  - lastModified    : ISO timestamp of the most recent commit that touched it
  - changeFrequency : commits per week over the past 30-day window
  - churn           : total lines added + deleted across all commits
  - refactoringSignals : commits whose message contains rename/move/extract/refactor/restructure
  - status          : "active" | "stale" | "deprecated" | "deleted"

Input contract
--------------
graph_data format:
  {
    "nodes":         {node_id: {label, properties}},
    "relationships": [{sourceId, targetId, type}],
    "commits": [
      {
        "sha":         str,
        "message":     str,
        "authorEmail": str,
        "authorName":  str,
        "committedAt": str (ISO 8601),
        "filesChanged": [str],   # relative paths
        "linesAdded":  int,
        "linesDeleted": int,
      },
      ...
    ]
  }

Commits are expected newest-first (same order as git log).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REFACTORING_KEYWORDS = re.compile(
    r"\b(refactor|rename|move|extract|restructure|reorganize|cleanup|clean[- ]?up"
    r"|split|merge|consolidate|simplify|rework|rewrite)\b",
    re.IGNORECASE,
)

_STALE_DAYS    = 180   # No commits in 6 months → stale
_ACTIVE_DAYS   = 30    # Modified within 30 days → active
_CHURN_WINDOW  = 30    # Days for changeFrequency calculation
_COMMITS_WEEK  = 7     # Days in a "week"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TimelineEvent:
    sha:         str
    message:     str
    author:      str
    committed_at: str
    event_type:  str   # "commit" | "refactoring" | "first_touch" | "last_touch"
    lines_added:   int = 0
    lines_deleted: int = 0

    def to_dict(self) -> dict:
        return {
            "sha":          self.sha,
            "message":      self.message[:200],
            "author":       self.author,
            "committedAt":  self.committed_at,
            "eventType":    self.event_type,
            "linesAdded":   self.lines_added,
            "linesDeleted": self.lines_deleted,
        }


@dataclass
class EntityTimeline:
    entity_id:            str
    entity_name:          str
    entity_type:          str
    birth_date:           Optional[str]
    last_modified:        Optional[str]
    change_frequency:     float    # commits per week (30-day window)
    total_churn:          int
    commit_count:         int
    refactoring_signals:  list[TimelineEvent] = field(default_factory=list)
    recent_events:        list[TimelineEvent] = field(default_factory=list)
    status:               str = "unknown"

    def to_dict(self) -> dict:
        return {
            "entityId":           self.entity_id,
            "entityName":         self.entity_name,
            "entityType":         self.entity_type,
            "birthDate":          self.birth_date,
            "lastModified":       self.last_modified,
            "changeFrequency":    round(self.change_frequency, 3),
            "totalChurn":         self.total_churn,
            "commitCount":        self.commit_count,
            "status":             self.status,
            "refactoringSignals": [e.to_dict() for e in self.refactoring_signals],
            "recentEvents":       [e.to_dict() for e in self.recent_events[:10]],
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_timeline(
    entity_id:   str,
    graph_data:  dict,
    *,
    now:         Optional[datetime] = None,
    stale_days:  int = _STALE_DAYS,
    active_days: int = _ACTIVE_DAYS,
    churn_window: int = _CHURN_WINDOW,
    max_events:  int = 50,
) -> EntityTimeline:
    """Build the change timeline for a single entity.

    Args:
        entity_id:   ID of the entity node in the graph.
        graph_data:  Full graph payload including "commits" list.
        now:         Override "current time" (useful for tests).
        stale_days:  Days without change before status = "stale".
        active_days: Days within which a change → status = "active".
        churn_window: Days window for changeFrequency computation.
        max_events:  Max events stored in recent_events.

    Returns:
        EntityTimeline for the entity.
    """
    now = now or datetime.now(timezone.utc)

    nodes   = graph_data.get("nodes", {})
    commits = graph_data.get("commits", [])

    node = nodes.get(entity_id, {})
    props = node.get("properties", {})
    entity_name = props.get("name") or props.get("title") or entity_id
    entity_type = node.get("label", "Unknown")

    # ── Resolve which file paths / identifiers belong to this entity ─────────
    relevant_paths = _resolve_entity_paths(entity_id, entity_name, props, graph_data)

    # ── Filter commits that touched this entity ───────────────────────────────
    touching: list[dict] = []
    for commit in commits:
        files = commit.get("filesChanged") or []
        if _commit_touches(files, relevant_paths):
            touching.append(commit)

    # If no path resolution produced results, fall back to all commits
    # (appropriate for repository-level timelines)
    if not touching and entity_type == "Repository":
        touching = list(commits)

    # ── Sort newest → oldest ──────────────────────────────────────────────────
    touching.sort(key=lambda c: c.get("committedAt", ""), reverse=True)

    if not touching:
        return EntityTimeline(
            entity_id=entity_id,
            entity_name=entity_name,
            entity_type=entity_type,
            birth_date=None,
            last_modified=None,
            change_frequency=0.0,
            total_churn=0,
            commit_count=0,
            status="unknown",
        )

    # Timestamps
    last_modified_str = touching[0].get("committedAt", "")
    birth_date_str    = touching[-1].get("committedAt", "")

    # Change frequency: commits in the last `churn_window` days / weeks
    window_start = now - timedelta(days=churn_window)
    recent_count = sum(
        1 for c in touching
        if _parse_dt(c.get("committedAt", "")) >= window_start
    )
    weeks_in_window = churn_window / _COMMITS_WEEK
    change_frequency = recent_count / weeks_in_window if weeks_in_window > 0 else 0.0

    # Churn
    total_churn = sum(
        (c.get("linesAdded") or 0) + (c.get("linesDeleted") or 0)
        for c in touching
    )

    # Build events
    events: list[TimelineEvent] = []
    for i, commit in enumerate(touching[:max_events]):
        sha     = commit.get("sha", "")
        message = commit.get("message", "")
        author  = commit.get("authorName") or commit.get("authorEmail") or ""
        cat     = commit.get("committedAt", "")
        added   = commit.get("linesAdded", 0) or 0
        deleted = commit.get("linesDeleted", 0) or 0

        ev_type = "commit"
        if i == len(touching) - 1:
            ev_type = "first_touch"
        elif i == 0:
            ev_type = "last_touch"

        events.append(TimelineEvent(
            sha=sha, message=message, author=author,
            committed_at=cat, event_type=ev_type,
            lines_added=added, lines_deleted=deleted,
        ))

    # Refactoring signals
    refactoring = [
        TimelineEvent(
            sha=c.get("sha", ""),
            message=c.get("message", ""),
            author=c.get("authorName") or c.get("authorEmail") or "",
            committed_at=c.get("committedAt", ""),
            event_type="refactoring",
            lines_added=c.get("linesAdded", 0) or 0,
            lines_deleted=c.get("linesDeleted", 0) or 0,
        )
        for c in touching
        if _REFACTORING_KEYWORDS.search(c.get("message", ""))
    ]

    # Status
    last_dt = _parse_dt(last_modified_str)
    days_since = (now - last_dt).days if last_dt else 9999
    if days_since <= active_days:
        status = "active"
    elif days_since <= stale_days:
        status = "stale"
    else:
        status = "deprecated"

    return EntityTimeline(
        entity_id=entity_id,
        entity_name=entity_name,
        entity_type=entity_type,
        birth_date=birth_date_str or None,
        last_modified=last_modified_str or None,
        change_frequency=change_frequency,
        total_churn=total_churn,
        commit_count=len(touching),
        refactoring_signals=refactoring[:10],
        recent_events=events,
        status=status,
    )


def build_repo_timeline(graph_data: dict, *, now: Optional[datetime] = None,
                         limit: int = 100) -> dict:
    """Build a summary timeline for the entire repository.

    Returns high-level stats + top-churned entities.
    """
    now     = now or datetime.now(timezone.utc)
    commits = graph_data.get("commits", [])
    nodes   = graph_data.get("nodes", {})

    if not commits:
        return {
            "totalCommits":      0,
            "activeContributors": [],
            "topChurnEntities":  [],
            "commitsByWeek":     [],
        }

    # Author stats
    author_counts: dict[str, int] = {}
    for c in commits:
        author = c.get("authorName") or c.get("authorEmail") or "unknown"
        author_counts[author] = author_counts.get(author, 0) + 1

    top_authors = sorted(author_counts.items(), key=lambda t: t[1], reverse=True)[:10]

    # Commit activity by week (last 12 weeks)
    weekly: dict[str, int] = {}
    for c in commits:
        dt = _parse_dt(c.get("committedAt", ""))
        if dt:
            week = dt.strftime("%Y-W%W")
            weekly[week] = weekly.get(week, 0) + 1
    commits_by_week = [
        {"week": w, "count": cnt}
        for w, cnt in sorted(weekly.items())[-12:]
    ]

    return {
        "totalCommits":      len(commits),
        "activeContributors": [{"name": a, "commits": c} for a, c in top_authors],
        "commitsByWeek":     commits_by_week,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_entity_paths(entity_id: str, entity_name: str,
                           props: dict, graph_data: dict) -> set[str]:
    """Build the set of file-path fragments that identify this entity."""
    paths: set[str] = set()

    # Direct path property
    for key in ("path", "filePath", "sourcePath", "sourceFile"):
        val = props.get(key)
        if val:
            paths.add(val.lower())

    # Name-based heuristic: foo.bar.MyClass → foo/bar/myclass fragment
    if entity_name:
        fragment = entity_name.lower().replace(".", "/").replace("::", "/")
        paths.add(fragment)
        # Also try just the last segment
        paths.add(entity_name.lower().split(".")[-1])

    # entity_id may encode a path: "file:src/utils.py" or "class:src/utils.py::Util"
    if ":" in entity_id:
        parts = entity_id.split(":")
        for p in parts[1:]:
            paths.add(p.lower())

    return paths


def _commit_touches(files: list[str], relevant_paths: set[str]) -> bool:
    """Return True if any file in the commit matches one of the entity paths."""
    if not relevant_paths:
        return False
    for f in files:
        f_lower = f.lower()
        for path in relevant_paths:
            if path and (path in f_lower or f_lower.endswith(path)):
                return True
    return False


def _parse_dt(iso_str: str) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None
