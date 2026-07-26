from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from fiscus.errors import CatalogError, DatasetNotFoundError
from fiscus.models import EXECUTOR_TOOLS, AccessMode, CatalogEntry, CatalogFile, ExecutorKind

CATALOG_SCHEMA_VERSION = 1
FAMILY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Catalog:
    def __init__(self, entries: Iterable[CatalogEntry], sources: Iterable[str]) -> None:
        by_id: dict[str, CatalogEntry] = {}
        for entry in entries:
            if entry.id in by_id:
                raise CatalogError(f"duplicate catalog id: {entry.id}")
            by_id[entry.id] = entry
        self._entries = by_id
        self.sources = tuple(sources)

    @property
    def entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def get(self, dataset_id: str) -> CatalogEntry:
        try:
            return self._entries[dataset_id]
        except KeyError as exc:
            raise DatasetNotFoundError(
                f"unknown dataset id {dataset_id!r}; call find_series before fetching"
            ) from exc

    @classmethod
    def load(cls, extra_paths: Iterable[str | Path] | None = None) -> Catalog:
        raw_entries: dict[str, dict[str, Any]] = {}
        sources: list[str] = []

        for name, text in _bundled_documents():
            document = _parse_yaml_document(text, name)
            try:
                validated = CatalogFile.model_validate(document)
            except ValidationError as exc:
                raise CatalogError(_validation_message(name, exc)) from exc
            sources.append(name)
            for entry in validated.entries:
                if entry.id in raw_entries:
                    raise CatalogError(f"duplicate catalog id {entry.id!r} in {name}")
                raw_entries[entry.id] = entry.model_dump(mode="json")

        for path in _expanded_extra_paths(extra_paths):
            for document_path in _catalog_files(path):
                document = _parse_yaml_document(
                    document_path.read_text(encoding="utf-8"), str(document_path)
                )
                _validate_overlay_header(document, str(document_path))
                sources.append(str(document_path))
                family = document.get("family")
                for patch in document.get("entries", []):
                    if not isinstance(patch, dict) or not isinstance(patch.get("id"), str):
                        raise CatalogError(
                            f"{document_path}: every overlay entry must be a mapping with an id"
                        )
                    patch = dict(patch)
                    if "family" in patch and patch["family"] != family:
                        raise CatalogError(
                            f"{document_path}: entry {patch['id']!r} does not match "
                            f"file family {family!r}"
                        )
                    if family and "family" not in patch:
                        patch["family"] = family
                    dataset_id = patch["id"]
                    current = raw_entries.get(dataset_id, {})
                    raw_entries[dataset_id] = _deep_merge(current, patch)

        validated_entries: list[CatalogEntry] = []
        for dataset_id, raw_entry in sorted(raw_entries.items()):
            try:
                validated_entries.append(CatalogEntry.model_validate(raw_entry))
            except ValidationError as exc:
                raise CatalogError(_validation_message(dataset_id, exc)) from exc
        return cls(validated_entries, sources)

    def lint_report(self) -> dict[str, Any]:
        families: dict[str, int] = {}
        caveats = 0
        conditional = 0
        for entry in self.entries:
            families[entry.family] = families.get(entry.family, 0) + 1
            caveats += len(entry.caveats)
            conditional += sum(
                1
                for caveat in entry.caveats
                if any(predicate.type != "always" for predicate in caveat.when.predicates)
            )
        return {
            "ok": True,
            "schema_version": CATALOG_SCHEMA_VERSION,
            "entries": len(self.entries),
            "families": dict(sorted(families.items())),
            "caveats": caveats,
            "conditional_caveats": conditional,
            "sources": list(self.sources),
        }


def catalog_schema_json() -> str:
    schema = CatalogFile.model_json_schema()
    entry_schema = schema["$defs"]["CatalogEntry"]
    entry_schema["allOf"] = [
        *[
            {
                "if": {
                    "properties": {
                        "access": {
                            "properties": {"executor": {"const": executor.value}},
                            "required": ["executor"],
                        }
                    },
                    "required": ["access"],
                },
                "then": {
                    "properties": {
                        "tools": {
                            "enum": [[tool.value] for tool in sorted(tools, key=str)]
                        }
                    },
                    "required": ["tools"],
                },
            }
            for executor, tools in EXECUTOR_TOOLS.items()
        ],
        {
            "if": {
                "properties": {
                    "access": {
                        "properties": {
                            "executor": {"const": ExecutorKind.BROWSER_DOWNLOAD.value}
                        },
                        "required": ["executor"],
                    }
                },
                "required": ["access"],
            },
            "then": {
                "properties": {
                    "access": {
                        "properties": {
                            "mode": {"const": AccessMode.BROWSER_DOWNLOAD_ONLY.value}
                        }
                    }
                }
            },
        },
        {
            "if": {
                "properties": {
                    "access": {
                        "properties": {
                            "mode": {"const": AccessMode.BROWSER_DOWNLOAD_ONLY.value}
                        },
                        "required": ["mode"],
                    }
                },
                "required": ["access"],
            },
            "then": {
                "properties": {
                    "access": {
                        "properties": {
                            "executor": {"const": ExecutorKind.BROWSER_DOWNLOAD.value}
                        }
                    }
                }
            },
        },
    ]
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _bundled_documents() -> list[tuple[str, str]]:
    checkout_root = Path(__file__).resolve().parents[2]
    checkout_catalog = checkout_root / "catalog"
    if checkout_catalog.is_dir() and (checkout_root / "pyproject.toml").is_file():
        return [
            (f"catalog/{path.name}", path.read_text(encoding="utf-8"))
            for path in _catalog_files(checkout_catalog)
        ]

    try:
        catalog_root = resources.files("fiscus").joinpath("catalog")
        documents = []
        for item in sorted(catalog_root.iterdir(), key=lambda part: part.name):
            if item.name.endswith((".yaml", ".yml")):
                documents.append((f"package:{item.name}", item.read_text(encoding="utf-8")))
        if documents:
            return documents
    except (FileNotFoundError, ModuleNotFoundError, TypeError) as exc:
        raise CatalogError("bundled catalog could not be located") from exc
    raise CatalogError("bundled catalog contains no YAML documents")


def _expanded_extra_paths(extra_paths: Iterable[str | Path] | None) -> list[Path]:
    values: list[str | Path] = list(extra_paths or [])
    configured = os.environ.get("FISCUS_EXTRA_CATALOG")
    if configured:
        values.extend(piece for piece in configured.split(os.pathsep) if piece)
    paths: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.exists():
            raise CatalogError(f"extra catalog path does not exist: {path}")
        paths.append(path)
    return paths


def _catalog_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in {".yaml", ".yml"}:
            raise CatalogError(f"catalog file must be YAML: {path}")
        return [path]
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in {".yaml", ".yml"}
    )


def _parse_yaml_document(text: str, locator: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"invalid YAML in {locator}: {exc}") from exc
    if not isinstance(document, dict):
        raise CatalogError(f"catalog document must be a mapping: {locator}")
    return document


def _validate_overlay_header(document: dict[str, Any], locator: str) -> None:
    unknown = sorted(set(document).difference({"schema_version", "family", "entries"}))
    if unknown:
        raise CatalogError(f"{locator}: unknown top-level fields: {', '.join(unknown)}")
    if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise CatalogError(f"{locator}: schema_version must be {CATALOG_SCHEMA_VERSION}")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CatalogError(f"{locator}: entries must be a non-empty list")
    family = document.get("family")
    if not isinstance(family, str) or not FAMILY_PATTERN.fullmatch(family):
        raise CatalogError(f"{locator}: family must be a lowercase source-family id")
    ids = [
        entry["id"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    duplicates = sorted({dataset_id for dataset_id in ids if ids.count(dataset_id) > 1})
    if duplicates:
        raise CatalogError(f"{locator}: duplicate overlay ids: {', '.join(duplicates)}")


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _validation_message(locator: str, exc: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(piece) for piece in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    return f"catalog validation failed for {locator}: {details}"
