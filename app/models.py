from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PostEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    title: str = Field(max_length=500)
    slug: str = Field(max_length=500)
    content: str = Field(max_length=2_000_000)
    meta_description: str = Field(max_length=2000)
    featured_image: str = Field(max_length=4000)
    featured_image_alt: str = Field(max_length=1000)
    author: str = Field(max_length=300)
    tags: list[str] = Field(max_length=100)
    categories: list[str] = Field(max_length=100)
    publish_date: str | None = Field(default=None, max_length=100)
    review_notes: str = Field(default="", max_length=4000)


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    action: Literal["APPROVED", "REJECTED", "PENDING"]
    acknowledge_warnings: bool = False
    notes: str = Field(default="", max_length=4000)


class RevisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class CleanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=2_000_000)
    source_url: str = Field(default="", max_length=4000)


class ImportRequest(RevisionAction):
    author_id: str = Field(default="", max_length=200)
    category_ids: list[str] = Field(default_factory=list, max_length=100)
