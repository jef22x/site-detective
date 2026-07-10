"""Pydantic schemas for test definitions (spec Appendix A)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

StepType = Literal[
    "navigate", "click", "type", "select", "wait",
    "assert_element", "screenshot", "login",
]


class Step(BaseModel):
    type: StepType
    context: Literal["customer", "admin"] = "customer"
    url: str | None = None
    intent: str | None = None
    selector: str | None = None
    value: str | None = None
    clear_first: bool = False
    condition: Literal["visible", "hidden", "navigation", "delay"] = "visible"
    exists: bool = True
    text_contains: str | None = None
    capture_as: str | None = None  # parsed but unused in v1
    label: str | None = None
    full_page: bool = False  # screenshot steps: capture the entire page
    role: str | None = None
    timeout_ms: int | None = None
    retries: int | None = None

    @model_validator(mode="after")
    def _require_intent_with_selector(self) -> Step:
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


class DiagnosticsConfig(BaseModel):
    """Noise control for page diagnostics (spec: docs/spec-page-diagnostics.md).
    Entries matching these rules are dropped at capture time, not stored."""
    ignore_console: list[str] = Field(default_factory=list)  # substring match
    ignore_urls: list[str] = Field(default_factory=list)  # fnmatch glob


class AuditsConfig(BaseModel):
    """Opt-in Phase 3 audits (spec: docs/spec-page-diagnostics.md Phase 3).
    Off by default: their runtime cost and finding volume must be chosen,
    not imposed."""
    accessibility: bool = False
    content: bool = False
    pages: list[str] | None = None  # None = every context the test visits


class TestBody(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    # Every run begins by navigating here; falls back to the global
    # 'starting_url' from settings.yaml when empty.
    starting_url: str | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    audits: AuditsConfig = Field(default_factory=AuditsConfig)
    steps: list[Step]


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
