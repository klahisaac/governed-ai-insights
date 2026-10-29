"""Documents: reference material written by people, given to narrators as context.

Owner: platform engineer builds the store and the selection rules. The documents
themselves belong to the teams that write them (brand, market access, medical,
field). They are untrusted text: narrators receive them as data, the validator
checks every number and date a claim takes from them, and readers only receive
documents their role may see.

A document is a Markdown file that starts with TOML front matter between +++ lines.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

FRONT_MATTER_DELIMITER = "+++"


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    kind: str
    owner: str
    version: str
    published_on: str
    # A member of the policy's scope dimension, or None when it applies everywhere.
    scope: str | None
    audiences: tuple[str, ...]
    body: str
    # A distribution level from the policy; claims that cite the document inherit it.
    distribution: str = "internal"
    # Shown on the page, for documents planted to exercise a defence.
    prototype_note: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.title}\n{self.body}".encode()).hexdigest()[:12]

    def provenance(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "content_hash": self.content_hash,
                "distribution": self.distribution}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "owner": self.owner,
            "version": self.version,
            "published_on": self.published_on,
            "scope": self.scope,
            "audiences": list(self.audiences),
            "body": self.body,
            "content_hash": self.content_hash,
            "distribution": self.distribution,
            "prototype_note": self.prototype_note,
        }


class DocumentStore(Protocol):
    """Plugin boundary. In production: a Databricks volume or a search index."""

    def all(self) -> list[Document]: ...


class FileDocumentStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def all(self) -> list[Document]:
        return [self._load(path) for path in sorted(self.directory.glob("*.md"))]

    @staticmethod
    def _load(path: Path) -> Document:
        text = path.read_text()
        opening, front_matter, body = text.split(FRONT_MATTER_DELIMITER + "\n", 2)
        if opening.strip():
            raise ValueError(f"{path} must start with {FRONT_MATTER_DELIMITER} front matter")
        metadata = tomllib.loads(front_matter)
        return Document(
            id=path.stem,
            title=metadata["title"],
            kind=metadata["kind"],
            owner=metadata["owner"],
            version=metadata["version"],
            published_on=metadata["published_on"],
            scope=metadata.get("scope"),
            audiences=tuple(metadata["audiences"]),
            body=body.strip(),
            distribution=metadata.get("distribution", "internal"),
            prototype_note=metadata.get("prototype_note"),
        )


def select_documents(
    documents: list[Document], scope: str | None, audience: str, as_of: str, limit: int
) -> list[Document]:
    """The documents a summary may draw on: written for this audience, inside the
    reader's scope, and already published at the data snapshot (so the same
    snapshot always sees the same documents). Newest first.

    `scope` is the scope the policy already resolved for the reader; None means the
    reader may see every member, so region documents are included too.
    """
    permitted = [
        document
        for document in documents
        if audience in document.audiences
        and (scope is None or document.scope in (None, scope))
        and document.published_on <= as_of
    ]
    return sorted(permitted, key=lambda document: (document.published_on, document.id), reverse=True)[:limit]
