"""Source scoring and passage retrieval.

Retrieval is plain BM25 over fixed-size chunks. A lesson's corpus is small
(tens of documents), so an embedding store would add a dependency and an API
call without improving anything we can measure here.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

CHUNK_CHARS = 1200
STOPWORDS = set(
    "a an and are as at be between by can do does for from has have how in into is it its "
    "of on or that the their this to using was what when which who why will with you your "
    "explain describe identify understand learn students lesson basic basics introduction".split()
)

# Tier 1: standards bodies, official docs, government and academic domains.
TIER1_DOMAINS = (
    "rfc-editor.org", "ietf.org", "w3.org", "whatwg.org", "iso.org", "ieee.org", "acm.org",
    "developer.mozilla.org", "docs.python.org", "learn.microsoft.com", "nist.gov",
    "who.int", "nih.gov", "cdc.gov", "nature.com", "science.org", "archive.org",
)
# Tier 2: established reference and teaching material.
TIER2_DOMAINS = (
    "wikipedia.org", "britannica.com", "khanacademy.org", "openstax.org", "stanford.edu",
    "mit.edu", "web.dev", "cloudflare.com", "ibm.com", "aws.amazon.com", "oreilly.com",
)
# Tier 4: open-publishing or discussion platforms with no editorial control.
TIER4_DOMAINS = (
    "medium.com", "quora.com", "reddit.com", "pinterest.com", "dev.to", "hashnode.dev",
    "blogspot.com", "wordpress.com", "substack.com", "facebook.com", "x.com", "twitter.com",
)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches(domain: str, candidates: tuple[str, ...]) -> bool:
    return any(domain == c or domain.endswith("." + c) for c in candidates)


def rate_source(url: str, kind: str) -> tuple[int, str]:
    """Return (tier, reason). Lower is more reliable."""
    if kind == "book":
        return 2, "Published book"
    if kind == "video":
        return 3, "Video, checked from metadata only"
    domain = domain_of(url)
    if _matches(domain, TIER1_DOMAINS):
        return 1, "Standards body, official documentation or primary publisher"
    if domain.endswith((".gov", ".edu", ".ac.uk")):
        return 1, "Government or academic domain"
    if _matches(domain, TIER2_DOMAINS):
        return 2, "Established reference or teaching source"
    if _matches(domain, TIER4_DOMAINS):
        return 4, "Open publishing platform with no editorial review"
    return 3, "General website with unknown editorial standards"


def tokens(text: str) -> list[str]:
    """Lowercase terms without stopwords, with plurals folded ("requests" -> "request")."""
    found = re.findall(r"[a-z0-9][a-z0-9\-\+#]*", text.lower())
    return [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in found if t not in STOPWORDS]


def relevance(text: str, query: str) -> float:
    """Share of the query's key terms that appear in the text."""
    wanted = set(tokens(query))
    if not wanted:
        return 0.0
    return len(wanted & set(tokens(text))) / len(wanted)


@dataclass
class Passage:
    source_id: str
    text: str


def chunk(source_id: str, text: str) -> list[Passage]:
    """Split on paragraphs, packing them up to CHUNK_CHARS."""
    passages: list[Passage] = []
    buffer = ""
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > CHUNK_CHARS:
            passages.append(Passage(source_id, para[:CHUNK_CHARS]))
            para = para[CHUNK_CHARS:]
        if len(buffer) + len(para) + 1 > CHUNK_CHARS and buffer:
            passages.append(Passage(source_id, buffer))
            buffer = ""
        buffer = f"{buffer}\n{para}".strip()
    if buffer:
        passages.append(Passage(source_id, buffer))
    return passages


class Index:
    """BM25 over passages."""

    def __init__(self, passages: list[Passage], k1: float = 1.5, b: float = 0.75):
        self.passages = passages
        self.k1, self.b = k1, b
        self.counts = [Counter(tokens(p.text)) for p in passages]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.avg = (sum(self.lengths) / len(self.lengths)) if passages else 1.0
        self.df: Counter = Counter()
        for counts in self.counts:
            self.df.update(counts.keys())

    def search(self, query: str, k: int = 5, source_id: str | None = None) -> list[Passage]:
        total = len(self.passages)
        scored: list[tuple[float, int]] = []
        terms = set(tokens(query))
        for i, counts in enumerate(self.counts):
            if source_id and self.passages[i].source_id != source_id:
                continue
            score = 0.0
            for term in terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                idf = math.log(1 + (total - self.df[term] + 0.5) / (self.df[term] + 0.5))
                norm = freq + self.k1 * (1 - self.b + self.b * self.lengths[i] / max(self.avg, 1))
                score += idf * freq * (self.k1 + 1) / norm
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.passages[i] for _, i in scored[:k]]


def build_index(sources: list[dict]) -> Index:
    passages: list[Passage] = []
    for source in sources:
        if source.get("evidence") and source.get("text"):
            passages.extend(chunk(source["id"], source["text"]))
    return Index(passages)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
