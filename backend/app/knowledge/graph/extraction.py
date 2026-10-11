import hashlib
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(default="概念", min_length=1, max_length=60)


class Relation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=160)
    target: str = Field(min_length=1, max_length=160)
    predicate: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=800)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entities: list[Entity] = Field(default_factory=list, max_length=100)
    relations: list[Relation] = Field(default_factory=list, max_length=100)


def normalized(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def entity_id(namespace, name):
    return hashlib.sha256(f"{namespace}:{normalized(name)}".encode()).hexdigest()


def demo_extract(text):
    """Only explicit triples in demo mode; never invent semantic relationships."""
    entities, relations = {}, []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(.{1,160}?)\s+--\s*(.{1,100}?)\s*-->\s*(.{1,160}?)\s*", line)
        if match:
            source, predicate, target = match.groups()
            entities[source] = {"name": source, "kind": "概念"}
            entities[target] = {"name": target, "kind": "概念"}
            relations.append(
                {"source": source, "target": target, "predicate": predicate, "evidence": line.strip()}
            )
    return {"entities": list(entities.values())[:100], "relations": relations[:100]}


def grounded(extraction, text, maximum):
    value = Extraction.model_validate(extraction)
    entities = {normalized(e.name): e for e in value.entities if e.name.casefold() in text.casefold()}
    entities = dict(list(entities.items())[:maximum])
    relations = [
        r
        for r in value.relations
        if normalized(r.source) in entities
        and normalized(r.target) in entities
        and r.evidence in text
        and normalized(r.source) != normalized(r.target)
    ]
    return list(entities.values()), relations
