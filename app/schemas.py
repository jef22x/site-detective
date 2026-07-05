"""Pydantic schemas for test definitions (spec Appendix A)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

StepType = Literal[
    "navigate", "click", "type", "select", "wait",
    "assert_element", "screenshot", "login",
]


class Step(BaseModel):
    type: StepType
    context: Literal["customer", "admin"] = "customer"
    url: Optional[str] = None
    intent: Optional[str] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    clear_first: bool = False
    condition: Literal["visible", "hidden", "navigation", "delay"] = "visible"
    exists: bool = True
    text_contains: Optional[str] = None
    capture_as: Optional[str] = None  # parsed but unused in v1
    label: Optional[str] = None
    full_page: bool = False  # screenshot steps: capture the entire page
    role: Optional[str] = None
    timeout_ms: Optional[int] = None
    retries: Optional[int] = None

    @model_validator(mode="after")
    def _require_intent_with_selector(self) -> "Step":
        if self.selector and not self.intent:
            raise ValueError(
                f"step of type '{self.type}' has a selector but no intent; "
                "intent is required for healing and documentation"
            )
        return self


class Defaults(BaseModel):
    timeout_ms: int = 10000
    retries: int = 1
    healing: bool = True


class TestBody(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    # Every run begins by navigating here; falls back to the global
    # 'starting_url' from settings.yaml when empty.
    starting_url: Optional[str] = None
    defaults: Defaults = Field(default_factory=Defaults)
    steps: List[Step]


class TestDefinition(BaseModel):
    schema_version: int = 1
    test: TestBody


def load_test(path: str | Path) -> TestDefinition:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    return TestDefinition.model_validate(data)


def save_test(test_def: TestDefinition, path: str | Path) -> None:
    path = Path(path)
    data = test_def.model_dump(exclude_none=True, exclude_defaults=True)
    # keep schema_version explicit in files, and first for readability
    data = {"schema_version": test_def.schema_version,
            **{k: v for k, v in data.items() if k != "schema_version"}}
    if path.suffix == ".json":
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
