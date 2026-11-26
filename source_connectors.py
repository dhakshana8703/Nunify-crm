"""Compose the optional adapters for saved JSON exports."""

import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol


SOURCE_PARENT = Path(__file__).resolve().parent.parent
CVENT_DIR = SOURCE_PARENT / "cvent-scrapper"
DIRECTORY_DIR = SOURCE_PARENT / "association"
SAVED_EXPORT_MODULES = {"cvent": "cvent_import", "directory": "directory_import"}


class SavedExportAdapter(Protocol):
    FILE_NAME: str
    DEFAULT_POLICY: dict

    def load_rows(self, directory: Path) -> list[dict]: ...


@dataclass(frozen=True)
class SavedExportConnector:
    adapter: SavedExportAdapter
    directory: Path


def configured_saved_exports(*, cvent_dir=CVENT_DIR, directory_dir=DIRECTORY_DIR):
    setting = os.environ.get("CRM_SOURCE_CONNECTORS", "cvent,directory")
    if setting.strip() == "none":
        return {}
    enabled = {slug.strip() for slug in setting.split(",") if slug.strip()}
    if enabled - SAVED_EXPORT_MODULES.keys():
        raise ValueError("Set CRM_SOURCE_CONNECTORS to cvent,directory, cvent, directory, or none.")
    directories = {"cvent": cvent_dir, "directory": directory_dir}
    return {
        slug: SavedExportConnector(import_module(module), Path(directories[slug]))
        for slug, module in SAVED_EXPORT_MODULES.items()
        if slug in enabled
    }
