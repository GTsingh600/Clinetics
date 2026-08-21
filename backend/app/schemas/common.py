"""Shared response envelopes and base configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for schemas read from ORM objects.

    `from_attributes` lets Pydantic populate from an ORM instance's attributes
    rather than requiring a dict. Schemas stay separate from models on purpose:
    the API contract and the storage layout change for different reasons, and
    serialising ORM objects directly leaks columns (password hashes, internal
    flags) the moment someone adds one.
    """

    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):  # PEP 695 type parameter syntax (Python 3.12+)
    """Offset-paginated result.

    Offset rather than cursor pagination: the UI needs page numbers and the
    result sets here are small. Cursor pagination would be the right call at a
    scale this project does not reach.
    """

    items: list[T]
    total: int = Field(description="Total rows matching the filter, ignoring pagination")
    limit: int
    offset: int


class Message(BaseModel):
    detail: str
