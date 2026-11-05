"""Mini App providers and a source-independent, resumable local archive."""
from dataclasses import dataclass, field
import hashlib
import json


class MiniAppError(Exception):
    """A user-readable error that never includes credentials or signed URLs."""


class AccessDenied(MiniAppError):
    pass


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Node:
    id: str
    parent: str
    kind: str
    title: str
    position: int = 0
    text: str = ''
    state: str = 'available'


@dataclass
class Asset:
    node: str
    label: str
    filename: str
    kind: str
    locator: dict
    fingerprint: str
    size: int | None = None
    resumable: bool = False
    problem: str = ''

    @property
    def id(self):
        return identity([self.node, self.label, self.fingerprint])


@dataclass
class Catalogue:
    nodes: list[Node] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
