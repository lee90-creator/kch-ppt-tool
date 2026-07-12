#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMPORT_MODULES = (
    "pptx",
    "flask",
    "fitz",
    "PIL",
    "mammoth",
    "svglib",
    "reportlab",
    "markdownify",
    "bs4",
    "requests",
    "lxml",
    "curl_cffi",
)


@dataclass(frozen=True)
class PackageCheck:
    import_name: str
    dist_names: tuple[str, ...]
    package_paths: tuple[str, ...]
    native_patterns: tuple[str, ...] = ()


PACKAGE_CHECKS = (
    PackageCheck("pptx", ("python-pptx",), ("pptx",)),
    PackageCheck("flask", ("Flask",), ("flask",)),
    PackageCheck(
        "fitz",
        ("PyMuPDF",),
        ("fitz", "pymupdf"),
        ("fitz/**/*.pyd", "fitz/*.pyd", "pymupdf/**/*.pyd", "pymupdf/*.pyd", "PyMuPDF.libs/*.dll"),
    ),
    PackageCheck("PIL", ("pillow",), ("PIL",), ("PIL/**/*.pyd", "PIL/*.pyd", "pillow.libs/*.dll")),
    PackageCheck("mammoth", ("mammoth",), ("mammoth",)),
    PackageCheck("svglib", ("svglib",), ("svglib",)),
    # reportlab 5.0.0 is a pure-python wheel (py3-none-any) — no native extension expected.
    PackageCheck("reportlab", ("reportlab",), ("reportlab",)),
    PackageCheck("markdownify", ("markdownify",), ("markdownify",)),
    PackageCheck("bs4", ("beautifulsoup4",), ("bs4",)),
    PackageCheck("requests", ("requests",), ("requests",)),
    PackageCheck("lxml", ("lxml",), ("lxml",), ("lxml/**/*.pyd", "lxml/*.pyd")),
    PackageCheck(
        "curl_cffi",
        ("curl_cffi",),
        ("curl_cffi",),
        ("curl_cffi/**/*.pyd", "curl_cffi/*.pyd", "curl_cffi.libs/*.dll"),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate KCH-PPT-Tool vendored Python packages.")
    parser.add_argument(
        "--mode",
        choices=("files", "import"),
        default="files",
        help="files checks wheel contents without loading Windows extensions; import loads modules on Windows.",
    )
    parser.add_argument(
        "--site-packages",
        type=Path,
        default=None,
        help="Path to the target runtime/site-packages directory.",
    )
    parser.add_argument("--root", type=Path, default=None, help="Package root to add to sys.path in import mode.")
    args = parser.parse_args(argv)

    site_packages = (args.site_packages or _default_site_packages()).expanduser().resolve(strict=False)
    root = (args.root or _default_root()).expanduser().resolve(strict=False)

    if args.mode == "files":
        result = check_files(site_packages)
    else:
        result = check_imports(site_packages, root)

    print("SMOKE_JSON: " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["ok"] else 1


def _default_root() -> Path:
    script = Path(__file__).resolve()
    if len(script.parents) >= 2:
        return script.parents[1]
    return Path.cwd()


def _default_site_packages() -> Path:
    env_value = None
    try:
        import os

        env_value = os.environ.get("KCH_PPT_SITE_PACKAGES")
    except Exception:
        env_value = None
    if env_value:
        return Path(env_value)

    root = _default_root()
    return root / "runtime" / "site-packages"


def check_files(site_packages: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    errors: list[str] = []

    if not site_packages.is_dir():
        return {
            "mode": "files",
            "ok": False,
            "site_packages": str(site_packages),
            "checks": checks,
            "errors": [f"site-packages directory not found: {site_packages}"],
        }

    installed_dists = _installed_distribution_names(site_packages)
    metadata_count = len(installed_dists)

    for check in PACKAGE_CHECKS:
        dist_ok = any(_normalize_name(dist_name) in installed_dists for dist_name in check.dist_names)
        package_hits = _existing_package_paths(site_packages, check.package_paths)
        native_hits = _matching_files(site_packages, check.native_patterns)
        native_ok = bool(native_hits) if check.native_patterns else True

        check_ok = dist_ok and bool(package_hits) and native_ok
        checks[check.import_name] = {
            "ok": check_ok,
            "dist_ok": dist_ok,
            "package_paths": package_hits,
            "native_files": native_hits,
            "native_required": bool(check.native_patterns),
        }
        if not dist_ok:
            errors.append(f"{check.import_name}: METADATA not found for {', '.join(check.dist_names)}")
        if not package_hits:
            errors.append(f"{check.import_name}: package directory/file not found")
        if not native_ok:
            errors.append(f"{check.import_name}: required Windows .pyd/.dll files not found")

    return {
        "mode": "files",
        "ok": not errors,
        "site_packages": str(site_packages),
        "metadata_count": metadata_count,
        "checks": checks,
        "errors": errors,
    }


def check_imports(site_packages: Path, root: Path) -> dict[str, Any]:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(site_packages) not in sys.path:
        sys.path.insert(0, str(site_packages))

    imported: list[str] = []
    errors: dict[str, str] = {}
    for module_name in IMPORT_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised by packaged Windows runtime.
            errors[module_name] = f"{type(exc).__name__}: {exc}"
        else:
            imported.append(module_name)

    return {
        "mode": "import",
        "ok": not errors,
        "site_packages": str(site_packages),
        "root": str(root),
        "imported": imported,
        "errors": errors,
    }


def _installed_distribution_names(site_packages: Path) -> set[str]:
    names: set[str] = set()
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        name = _metadata_name(metadata)
        if name:
            names.add(_normalize_name(name))
    return names


def _metadata_name(metadata: Path) -> str | None:
    try:
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("name:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _normalize_name(value: str) -> str:
    return value.replace("_", "-").replace(".", "-").lower()


def _existing_package_paths(site_packages: Path, package_paths: tuple[str, ...]) -> list[str]:
    hits = []
    for package_path in package_paths:
        candidate = site_packages / package_path
        if candidate.exists():
            hits.append(candidate.relative_to(site_packages).as_posix())
    return sorted(hits)


def _matching_files(site_packages: Path, patterns: tuple[str, ...]) -> list[str]:
    if not patterns:
        return []
    matches: set[str] = set()
    for pattern in patterns:
        for path in site_packages.glob(pattern):
            if path.is_file():
                matches.add(path.relative_to(site_packages).as_posix())
    return sorted(matches)


if __name__ == "__main__":
    raise SystemExit(main())
