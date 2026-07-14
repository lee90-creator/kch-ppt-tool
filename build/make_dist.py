#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PACKAGE_NAME = "KCH-PPT-Tool"
PYTHON_VERSION = "3.11.9"
PYTHON_EMBED_FILENAME = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_EMBED_FILENAME}"
PYTHON_EMBED_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
PYTHON_PTH = "\n".join(
    (
        "python311.zip",
        ".",
        "Lib/site-packages",
        r"..\site-packages",
        r"..\..",
        "import site",
    )
) + "\n"

# Written into runtime/site-packages so the embeddable interpreter (which forces
# safe_path via python311._pth and therefore drops the script's own directory
# from sys.path[0]) can still run bundled ppt-master converters that import
# sibling modules (e.g. source_to_md/pdf_to_md.py -> _batch).
SITECUSTOMIZE_PY = '''"""Restore script-directory imports under the Windows embeddable runtime.

The embeddable distribution ships a python311._pth file, which forces
``safe_path`` and stops Python from prepending the running script's own
directory to sys.path[0]. Bundled ppt-master converters (e.g.
source_to_md/pdf_to_md.py) rely on that directory to import sibling
modules such as ``_batch``. Re-add it to match normal Python behavior.
"""
import os
import sys


def _restore_script_dir() -> None:
    argv = getattr(sys, "argv", None)
    if not argv:
        return
    script = argv[0]
    if not script:
        return
    try:
        script_dir = os.path.dirname(os.path.abspath(script))
    except Exception:
        return
    if script_dir and os.path.isdir(script_dir) and script_dir not in sys.path:
        sys.path.insert(0, script_dir)


_restore_script_dir()
'''

IGNORE_COMMON = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".DS_Store")
PPT_MASTER_EXCLUDED_PATHS = frozenset(
    {
        ".git",
        ".github",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "README.md",
        "README_CN.md",
        "SECURITY.md",
        "docs/assets",
        "examples",
        "index.html",
        "skills/ppt-master/references/ai-image-comparison",
        "viewer.html",
    }
)


class BuildError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the portable Windows KCH-PPT-Tool distribution.")
    parser.add_argument(
        "--version-tag",
        default=None,
        help="Version tag used in VERSION and zip name (default: ppt-webtool/VERSION).",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Reuse build/cache downloads and wheelhouse without contacting python.org or PyPI.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory for the dist zip (default: build/dist/ under ppt-webtool).",
    )
    args = parser.parse_args(argv)

    try:
        return build(args)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


def build(args: argparse.Namespace) -> int:
    build_dir = Path(__file__).resolve().parent
    webtool_root = build_dir.parent
    repo_root = webtool_root.parent
    version_file = webtool_root / "VERSION"
    raw_version_tag = args.version_tag
    if raw_version_tag is None:
        raw_version_tag = _read_version_tag(version_file)
    version_tag = _clean_version_tag(raw_version_tag)
    cache_dir = build_dir / "cache"
    wheels_dir = cache_dir / "wheels"
    requirements = build_dir / "requirements-vendor.txt"
    import_smoke = build_dir / "import_smoke.py"
    output_dir = _resolve_output_dir(args.output, webtool_root)
    package_dir = output_dir / PACKAGE_NAME
    zip_path = output_dir / f"{PACKAGE_NAME}-{version_tag}.zip"

    _require_file(requirements)
    _require_file(import_smoke)
    _require_file(webtool_root / "START.bat")
    _require_file(webtool_root / "README.txt")
    _require_file(version_file)
    _require_dir(webtool_root / "app")
    _require_dir(webtool_root / "tools")
    _require_dir(webtool_root / "style")
    _require_dir(repo_root / "ppt-master")

    output_dir.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    python_zip, python_sha256, python_source = ensure_python_embed(cache_dir, args.skip_download)
    wheel_info = ensure_wheelhouse(requirements, wheels_dir, args.skip_download)

    if package_dir.exists():
        shutil.rmtree(package_dir)
    site_packages = assemble_layout(
        webtool_root=webtool_root,
        repo_root=repo_root,
        build_dir=build_dir,
        version_tag=version_tag,
        package_dir=package_dir,
        python_zip=python_zip,
        requirements=requirements,
        wheels_dir=wheels_dir,
    )

    smoke = run_files_smoke(import_smoke, site_packages)
    if not smoke.get("ok"):
        raise BuildError("import_smoke.py --mode files failed; zip was not created")

    zip_info = zip_package(package_dir, zip_path)
    report = {
        "ok": True,
        "version": version_tag,
        "python_embed": {
            "version": PYTHON_VERSION,
            "url": PYTHON_EMBED_URL,
            "sha256": python_sha256,
            "source": python_source,
        },
        "wheels": wheel_info,
        "file_count": zip_info["file_count"],
        "uncompressed_bytes": zip_info["uncompressed_bytes"],
        "zip_bytes": zip_info["zip_bytes"],
        "smoke": smoke,
        "zip_path": str(zip_path),
    }
    print("RESULT_JSON: " + json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _clean_version_tag(raw: str) -> str:
    version_tag = raw.strip()
    if not version_tag:
        raise BuildError("--version-tag must not be empty")
    if any(separator in version_tag for separator in ("/", "\\")):
        raise BuildError("--version-tag must not contain path separators")
    return version_tag


def _read_version_tag(version_file: Path) -> str:
    _require_file(version_file)
    try:
        return version_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"failed to read VERSION file: {version_file}") from exc


def _resolve_output_dir(raw: Path | None, webtool_root: Path) -> Path:
    if raw is None:
        return webtool_root / "build" / "dist"
    path = raw.expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def ensure_python_embed(cache_dir: Path, skip_download: bool) -> tuple[Path, str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / PYTHON_EMBED_FILENAME
    sha_file = archive.with_suffix(archive.suffix + ".sha256")

    if archive.exists():
        digest = _sha256_file(archive)
        if digest == PYTHON_EMBED_SHA256:
            sha_file.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            return archive, digest, "cache"
        if skip_download:
            raise BuildError(f"cached {archive} sha256 mismatch: {digest}")
        archive.unlink()

    if skip_download:
        raise BuildError(f"--skip-download requested but {archive} is missing")

    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
    digest = _download_file(PYTHON_EMBED_URL, tmp_archive)
    if digest != PYTHON_EMBED_SHA256:
        tmp_archive.unlink(missing_ok=True)
        raise BuildError(f"downloaded {PYTHON_EMBED_FILENAME} sha256 mismatch: {digest}")
    tmp_archive.replace(archive)
    sha_file.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, digest, "download"


def _download_file(url: str, destination: Path) -> str:
    hasher = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "KCH-PPT-Tool-builder/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                output.write(chunk)
    except Exception as exc:  # pragma: no cover - network failure path.
        destination.unlink(missing_ok=True)
        raise BuildError(f"failed to download {url}: {exc}") from exc
    return hasher.hexdigest()


def ensure_wheelhouse(requirements: Path, wheels_dir: Path, skip_download: bool) -> dict[str, Any]:
    wheels_dir.mkdir(parents=True, exist_ok=True)
    if skip_download:
        wheel_count = _wheel_count(wheels_dir)
        if wheel_count == 0:
            raise BuildError(f"--skip-download requested but no wheels exist in {wheels_dir}")
        return {"source": "cache", "wheel_count": wheel_count, "path": str(wheels_dir)}

    base_cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-r",
        str(requirements),
        "--only-binary=:all:",
        "--platform",
        "win_amd64",
        "--python-version",
        "311",
        "--implementation",
        "cp",
        "--abi",
        "cp311",
        "-d",
        str(wheels_dir),
    ]
    completed = _run(base_cmd, check=False)
    source = "download"
    if completed.returncode != 0:
        retry_cmd = base_cmd[:-2] + ["--abi", "none"] + base_cmd[-2:]
        _run(retry_cmd, check=True)
        source = "download-retry-abi-none"

    return {"source": source, "wheel_count": _wheel_count(wheels_dir), "path": str(wheels_dir)}


def assemble_layout(
    *,
    webtool_root: Path,
    repo_root: Path,
    build_dir: Path,
    version_tag: str,
    package_dir: Path,
    python_zip: Path,
    requirements: Path,
    wheels_dir: Path,
) -> Path:
    package_dir.mkdir(parents=True)
    runtime_dir = package_dir / "runtime"
    python_embed_dir = runtime_dir / "python-embed"
    site_packages = runtime_dir / "site-packages"

    extract_python_embed(python_zip, python_embed_dir)
    configure_python_embed(python_embed_dir)
    install_site_packages(requirements, wheels_dir, site_packages)
    (site_packages / "sitecustomize.py").write_text(SITECUSTOMIZE_PY, encoding="utf-8", newline="\n")

    shutil.copy2(webtool_root / "START.bat", package_dir / "START.bat")
    shutil.copy2(webtool_root / "README.txt", package_dir / "README.txt")
    (package_dir / "VERSION").write_text(f"{version_tag}\n", encoding="utf-8", newline="\n")
    _copytree(webtool_root / "app", package_dir / "app", IGNORE_COMMON)
    _copytree(webtool_root / "tools", package_dir / "tools", IGNORE_COMMON)
    shutil.copy2(build_dir / "import_smoke.py", package_dir / "tools" / "import_smoke.py")
    _copytree(webtool_root / "style", package_dir / "style", IGNORE_COMMON)
    _copytree(
        repo_root / "ppt-master",
        package_dir / "ppt-master",
        _make_ppt_master_ignore(repo_root / "ppt-master"),
    )

    data_dir = package_dir / "data"
    data_dir.mkdir()
    (data_dir / "README.txt").write_text(
        "KCH-PPT-Tool runtime data directory. Settings, history, uploads, and job artifacts are created here.\n",
        encoding="utf-8",
    )
    return site_packages


def extract_python_embed(python_zip: Path, python_embed_dir: Path) -> None:
    python_embed_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(python_zip) as archive:
            archive.extractall(python_embed_dir)
    except zipfile.BadZipFile as exc:
        raise BuildError(f"invalid Python embeddable zip: {python_zip}") from exc


def configure_python_embed(python_embed_dir: Path) -> None:
    (python_embed_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (python_embed_dir / "python311._pth").write_text(PYTHON_PTH, encoding="utf-8", newline="\n")


def install_site_packages(requirements: Path, wheels_dir: Path, site_packages: Path) -> None:
    if site_packages.exists():
        shutil.rmtree(site_packages)
    site_packages.mkdir(parents=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheels_dir),
        "--target",
        str(site_packages),
        "--no-compile",
        "-r",
        str(requirements),
    ]
    if platform.system() != "Windows":
        cmd[4:4] = [
            "--platform",
            "win_amd64",
            "--python-version",
            "311",
            "--implementation",
            "cp",
            "--abi",
            "cp311",
            "--abi",
            "none",
            "--only-binary=:all:",
        ]
    _run(cmd, check=True)


def run_files_smoke(import_smoke: Path, site_packages: Path) -> dict[str, Any]:
    cmd = [sys.executable, str(import_smoke), "--mode", "files", "--site-packages", str(site_packages)]
    completed = _run(cmd, check=False, capture=True)
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)

    try:
        smoke = _parse_smoke_json(completed.stdout)
    except ValueError as exc:
        raise BuildError(
            "import_smoke.py --mode files did not emit valid SMOKE_JSON; "
            f"{exc}; stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        ) from exc
    if completed.returncode != 0:
        smoke["ok"] = False
    return smoke


def _parse_smoke_json(stdout: str) -> dict[str, Any]:
    for line in stdout.splitlines():
        if line.startswith("SMOKE_JSON: "):
            try:
                value = json.loads(line[len("SMOKE_JSON: ") :])
            except json.JSONDecodeError as exc:
                raise ValueError(f"failed to parse SMOKE_JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError("SMOKE_JSON payload is not an object")
            return value
    raise ValueError("SMOKE_JSON line missing")


def zip_package(package_dir: Path, zip_path: Path) -> dict[str, int]:
    file_count = 0
    uncompressed_bytes = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as archive:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            archive_name = path.relative_to(package_dir.parent).as_posix()
            archive.write(path, archive_name)
            file_count += 1
            uncompressed_bytes += path.stat().st_size
    return {"file_count": file_count, "uncompressed_bytes": uncompressed_bytes, "zip_bytes": zip_path.stat().st_size}


def _make_ppt_master_ignore(source_root: Path) -> Any:
    resolved_root = source_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        relative_directory = Path(directory).resolve().relative_to(resolved_root)
        excluded = set(IGNORE_COMMON(directory, names))
        excluded.update(
            name
            for name in names
            if (relative_directory / name).as_posix() in PPT_MASTER_EXCLUDED_PATHS
        )
        return excluded

    return ignore


def test_ppt_master_ignore_paths() -> None:
    source_root = Path("/tmp/ppt-master-ignore-test")
    ignore = _make_ppt_master_ignore(source_root)

    cases = (
        (
            source_root,
            [
                ".github",
                ".git",
                "__pycache__",
                "CODE_OF_CONDUCT.md",
                "CONTRIBUTING.md",
                "README.md",
                "README_CN.md",
                "SECURITY.md",
                "docs",
                "examples",
                "index.html",
                "templates",
                "viewer.html",
            ],
            {
                ".github",
                ".git",
                "__pycache__",
                "CODE_OF_CONDUCT.md",
                "CONTRIBUTING.md",
                "README.md",
                "README_CN.md",
                "SECURITY.md",
                "examples",
                "index.html",
                "viewer.html",
            },

        ),
        (source_root / "docs", ["assets", "guide.md"], {"assets"}),
        (
            source_root / "skills" / "ppt-master" / "references",
            ["ai-image-comparison", "guide.md"],
            {"ai-image-comparison"},
        ),
        (
            source_root / "skills" / "ppt-master" / "scripts",
            ["docs", "build.py"],
            set(),
        ),
        (
            source_root / "skills" / "ppt-master" / "templates",
            ["icons", "template.md"],
            set(),
        ),
    )

    for directory, names, expected in cases:
        actual = ignore(str(directory), names)
        assert actual == expected, f"{directory}: expected {expected}, got {actual}"


def _copytree(source: Path, destination: Path, ignore: Any) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=ignore)


def _run(cmd: list[str], *, check: bool, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+ " + subprocess.list2cmdline(cmd), flush=True)
    completed = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and completed.returncode != 0:
        raise BuildError(f"command failed with exit code {completed.returncode}: {subprocess.list2cmdline(cmd)}")
    return completed


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _wheel_count(wheels_dir: Path) -> int:
    return sum(1 for path in wheels_dir.glob("*.whl") if path.is_file())


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise BuildError(f"required file missing: {path}")


def _require_dir(path: Path) -> None:
    if not path.is_dir():
        raise BuildError(f"required directory missing: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
