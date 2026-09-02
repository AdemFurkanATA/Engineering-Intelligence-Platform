"""
adr_parser.py — Architecture Decision Record (ADR) parser for git-analyzer-service.

Detects and parses ADR files from cloned repositories.

Supported ADR locations:
  docs/adr/*.md
  docs/decisions/*.md
  decisions/*.md
  doc/adr/*.md
  ADR-*.md  (root level)
  adr/*.md

Supported ADR formats:
  - MADR (Markdown Architecture Decision Record): ## Status, ## Context, ## Decision
  - Nygard format: **Status**, **Context**, **Decision**, **Consequences**
  - Simple format: any markdown with "status:", "decision:", "context:" lines

Also parses lightweight decision signals from commit messages:
  - "reason:", "adr:", "because ", "decided to", "decision:"

Published event: decision.recorded (DecisionRecordPayload)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# ADR file discovery patterns
# ---------------------------------------------------------------------------

_ADR_GLOB_PATTERNS = [
    "docs/adr/*.md",
    "docs/decisions/*.md",
    "decisions/*.md",
    "doc/adr/*.md",
    "adr/*.md",
    "ADR-*.md",
    "adr-*.md",
]

_ADR_STATUS_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:##?\s+Status|Status:?\s*\|?|status:)\s*[:\|]?\s*"
    r"(proposed|draft|accepted|rejected|deprecated|superseded|approved)",
    re.IGNORECASE,
)

_ADR_TITLE_PATTERN = re.compile(
    r"^#\s+(?:ADR[-\s]*\d+[:\s]+)?(.+?)$", re.MULTILINE
)

_SECTION_PATTERNS = {
    "context": re.compile(
        r"(?:##?\s+Context|##?\s+Problem Statement)\s*\n(.*?)(?=\n##|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    "decision": re.compile(
        r"(?:##?\s+Decision)\s*\n(.*?)(?=\n##|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    "consequences": re.compile(
        r"(?:##?\s+Consequences?|##?\s+Outcome|##?\s+Results?)\s*\n(.*?)(?=\n##|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
}

# Commit message patterns that signal a decision
_COMMIT_DECISION_PATTERNS = [
    re.compile(r"^reason:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^adr:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^decision:\s*(.+)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bdecided to\s+(.+?)(?:\.|$)", re.IGNORECASE),
    re.compile(r"\bbecause\s+(.{20,200})(?:\.|$)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DecisionRecord:
    title:          str
    status:         str    # accepted | rejected | deprecated | proposed | unknown
    context:        str
    decision:       str
    consequences:   str
    source_file:    str
    repository_id:  str
    recorded_at:    str
    related_entities: list[str] = field(default_factory=list)
    source_type:    str = "adr_file"   # "adr_file" | "commit_message"

    def to_event_payload(self) -> dict:
        return {
            "title":           self.title,
            "status":          self.status,
            "context":         self.context[:2000],
            "decision":        self.decision[:2000],
            "consequences":    self.consequences[:1000],
            "sourceFile":      self.source_file,
            "repositoryId":    self.repository_id,
            "recordedAt":      self.recorded_at,
            "relatedEntities": self.related_entities,
            "sourceType":      self.source_type,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_adr_files(
    repo_dir:      str,
    repository_id: str,
) -> list[DecisionRecord]:
    """Scan a cloned repository for ADR files and parse them.

    Args:
        repo_dir:      Path to the root of the cloned repository.
        repository_id: ID of the repository in the graph.

    Returns:
        List of DecisionRecord objects (one per ADR file found).
    """
    root    = Path(repo_dir)
    records: list[DecisionRecord] = []

    # Collect candidate files
    adr_files: set[Path] = set()
    for pattern in _ADR_GLOB_PATTERNS:
        # Split pattern into directory part and filename glob
        parts = pattern.split("/")
        if len(parts) == 1:
            adr_files.update(root.glob(pattern))
        else:
            subdir = root.joinpath(*parts[:-1])
            if subdir.exists():
                adr_files.update(subdir.glob(parts[-1]))

    for adr_path in sorted(adr_files):
        try:
            content = adr_path.read_text(encoding="utf-8", errors="ignore")
            record  = _parse_adr_content(
                content,
                source_file=str(adr_path.relative_to(root)),
                repository_id=repository_id,
            )
            if record:
                records.append(record)
        except Exception:
            pass   # Skip unreadable files

    return records


def parse_commit_decisions(
    commits:       list[dict],
    repository_id: str,
) -> list[DecisionRecord]:
    """Extract lightweight decision signals from commit messages.

    Args:
        commits:       List of commit dicts (sha, message, committedAt, ...).
        repository_id: ID of the repository.

    Returns:
        List of DecisionRecord objects for commits with decision signals.
    """
    records: list[DecisionRecord] = []

    for commit in commits:
        message = commit.get("message", "")
        sha     = commit.get("sha", "")
        cat     = commit.get("committedAt", "") or _now_iso()

        for pattern in _COMMIT_DECISION_PATTERNS:
            match = pattern.search(message)
            if match:
                decision_text = match.group(1).strip()
                if len(decision_text) < 10:
                    continue
                records.append(DecisionRecord(
                    title=f"Commit decision: {message[:80].strip()}",
                    status="accepted",  # committed = decided
                    context=f"Inferred from commit {sha[:8]}: {message[:200]}",
                    decision=decision_text[:500],
                    consequences="",
                    source_file=f"commit:{sha}",
                    repository_id=repository_id,
                    recorded_at=cat,
                    source_type="commit_message",
                ))
                break  # one decision per commit

    return records


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _parse_adr_content(
    content:       str,
    source_file:   str,
    repository_id: str,
) -> Optional[DecisionRecord]:
    """Parse a single ADR markdown file."""
    content = content.strip()
    if not content:
        return None

    # Title
    title_match = _ADR_TITLE_PATTERN.search(content)
    title = title_match.group(1).strip() if title_match else Path(source_file).stem

    # Status
    status_match = _ADR_STATUS_PATTERN.search(content)
    status = status_match.group(1).lower() if status_match else "unknown"

    # Sections
    context      = _extract_section(content, "context")
    decision     = _extract_section(content, "decision")
    consequences = _extract_section(content, "consequences")

    # Skip files that have no meaningful content
    if not decision and not context:
        return None

    return DecisionRecord(
        title=title,
        status=status,
        context=context,
        decision=decision,
        consequences=consequences,
        source_file=source_file,
        repository_id=repository_id,
        recorded_at=_now_iso(),
        source_type="adr_file",
    )


def _extract_section(content: str, section: str) -> str:
    """Extract a named section from ADR markdown content."""
    pattern = _SECTION_PATTERNS.get(section)
    if not pattern:
        return ""
    match = pattern.search(content)
    if not match:
        return ""
    text = match.group(1).strip()
    # Remove markdown noise
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:2000]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
