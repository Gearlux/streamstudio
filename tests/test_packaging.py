"""What a built wheel must contain, and what PyPI must accept.

This project's runtime is not only Python: ComfyUI loads the frontend extension from
``WEB_DIRECTORY`` (``streamstudio/__init__.py``), so ``web/*.js`` is *code*, shipped
through ``package-data``. An editable install reads it straight from the checkout,
which is why its absence from the wheel is invisible during development and total in
production — the package imports, registers its nodes, and has no UI.

The rules below were each written against a measured failure of this project's own
build; none is a checklist item:

* the wheel held 13 entries and none of the 30 ``web/*.js`` files;
* ``pip install streamstudio`` into a torch-less environment raised
  ``ModuleNotFoundError: No module named 'torch'`` from ``nodes.py``, because the
  module-scope ``import torch`` was declared nowhere;
* a ``torchsig @ git+https://...`` entry in an extra would have had the upload
  refused by PyPI after every gate passed.
"""

import ast
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from fnmatch import fnmatch
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any, Dict, List, Set, cast

import pytest

_REPO = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO / "pyproject.toml"
_PACKAGE = _REPO / "streamstudio"
_README = _REPO / "README.md"

#: The distribution name at the head of a requirement — `confluid[pydantic]>=0.1.0` -> `confluid`.
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9._-]+)")

#: `[text](target)` with a relative target — absolute URLs excluded by rejecting `://`.
_RELATIVE_LINK = re.compile(r"\[[^\]]*\]\((?!\w+://)([^)\s]+)\)")


def _project() -> Dict[str, Any]:
    with _PYPROJECT.open("rb") as fh:
        return cast(Dict[str, Any], tomllib.load(fh)["project"])


def _package_data_patterns() -> List[str]:
    with _PYPROJECT.open("rb") as fh:
        tool = tomllib.load(fh)["tool"]
    return list(tool["setuptools"]["package-data"]["streamstudio"])


def _every_requirement() -> List[str]:
    project = _project()
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return requirements


def _normalize(name: str) -> str:
    """PEP 503 normalisation, so `annotated-types` and `annotated_types` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_distributions() -> Set[str]:
    """Every distribution this package declares, core and extras alike."""
    names = set()
    for requirement in _every_requirement():
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _module_scope_imports(path: Path) -> Set[str]:
    """Top-level module names imported unconditionally at import time.

    Direct children of the module body only — an import inside ``try:``, ``if
    TYPE_CHECKING:`` or a function is deliberately optional (this package guards its
    domain-plugin imports exactly that way) and must NOT be forced into the
    dependency list.
    """
    modules: Set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_no_requirement_is_a_direct_url() -> None:
    """A ``name @ git+https://...`` requirement has the upload refused by PyPI.

    Warehouse validates every ``Requires-Dist`` and rejects any carrying a URL —
    ``warehouse/forklift/metadata.py``, "Can't have direct dependency: {req}". Extras
    are validated identically, which is the trap: an extra nobody installs still
    blocks the whole upload, and the rejection arrives only after the artifact is
    built, checked and sent.

    An unpublished dependency therefore cannot be reached by an extra. Name it plainly
    and document how to obtain it.
    """
    direct = [req for req in _every_requirement() if "@" in req and "://" in req.split("@", 1)[1]]

    assert not direct, f"PyPI rejects direct-URL requirements: {direct}"


def test_every_web_asset_is_shipped() -> None:
    """Every file under ``streamstudio/web/`` matches a ``package-data`` pattern.

    ``web/`` has no ``__init__.py``, so ``packages.find`` skips it and ``package-data``
    is the only thing that puts it in the wheel. The failure mode is silent and
    complete: ComfyUI reads ``WEB_DIRECTORY`` off an installed package that has no
    such directory, and every node renders with default widgets and no extension.

    Stated as "every file is covered" rather than "``web/*.js`` is listed" so that
    adding a ``.css`` or an icon beside the scripts fails here instead of shipping a
    half-populated directory.
    """
    patterns = _package_data_patterns()
    unshipped = [
        str(path.relative_to(_PACKAGE))
        for path in sorted(_PACKAGE.glob("web/*"))
        if path.is_file() and not any(fnmatch(str(path.relative_to(_PACKAGE)), pattern) for pattern in patterns)
    ]

    assert not unshipped, f"web assets missing from package-data {patterns}: {unshipped}"


def test_every_file_of_the_built_page_is_shipped_and_no_source_map() -> None:
    """Every file of the committed editor page (``streamstudio/canvas``) matches a ``package-data`` pattern, and no
    pattern takes a source map.

    The folder has no ``__init__.py`` either, so ``package-data`` is the only way into the wheel. Stated as "every file
    is covered" so a build that starts writing a new kind of asset (a font, an image) fails here instead of shipping a
    page whose ``index.html`` names a file the install does not have.
    """
    patterns = _package_data_patterns()
    files = [
        path.relative_to(_PACKAGE).as_posix() for path in sorted((_PACKAGE / "canvas").rglob("*")) if path.is_file()
    ]
    assert "canvas/index.html" in files and "canvas/.built-from" in files, files
    unshipped = [name for name in files if not name.endswith(".map") and not any(fnmatch(name, p) for p in patterns)]
    assert not unshipped, f"built page files missing from package-data {patterns}: {unshipped}"
    mapped = [p for p in patterns if fnmatch("canvas/assets/index-AbCd1234.js.map", p) or fnmatch("canvas/x.map", p)]
    assert not mapped, f"package-data patterns that would ship a source map: {mapped}"


def test_the_wheel_built_from_a_checkout_serves_the_page(tmp_path: Path) -> None:
    """Built from what a checkout CARRIES (the tracked files, plus new ones not ignored — ``git ls-files -co``), the
    wheel holds the editor page — ``index.html``, its assets, the build record — and no source map, even when one lies
    in the folder (a developer's ``vite build --sourcemap``). Measured before: the wheel held no page at all, and an
    install answered ``/`` with ``The canvas page is not built``."""
    inside = (
        shutil.which("git")
        and subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=_REPO, capture_output=True, text=True
        ).stdout.strip()
    )
    if inside != "true":
        pytest.skip("not a git checkout: what a checkout carries is what git lists")
    listed = (
        subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=_REPO, capture_output=True, check=True
        )
        .stdout.decode()
        .split("\0")
    )
    checkout = tmp_path / "checkout"
    for name in filter(None, listed):
        if (_REPO / name).is_file():
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_REPO / name, checkout / name)
    page = checkout / "streamstudio" / "canvas"
    (page / "assets" / "index-AbCd1234.js.map").write_text("{}")  # what a build with source maps would leave
    # No isolation: an isolated build would fetch the backend from the network (the [dev] extra declares it).
    command = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path / "wheel")]
    built = subprocess.run([*command, str(checkout)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-2000:]
    names = zipfile.ZipFile(next((tmp_path / "wheel").glob("*.whl"))).namelist()
    carried = sorted(
        f"streamstudio/{path.relative_to(page.parent).as_posix()}"
        for path in page.rglob("*")
        if path.is_file() and path.suffix != ".map"
    )
    assert {"streamstudio/canvas/index.html", "streamstudio/canvas/.built-from"} <= set(carried), carried
    assert [name for name in carried if name not in names] == []
    assert [name for name in names if name.endswith(".map")] == []
    assert [name for name in names if name.startswith("streamstudio/web/")], "the ComfyUI editor's JavaScript too"


def test_the_declared_package_data_files_exist() -> None:
    """A ``package-data`` pattern that matches nothing is a claim, not a file.

    ``py.typed`` was declared for months without existing, so the package advertised
    inline types it did not ship and type checkers silently treated it as untyped.
    """
    barren = [pattern for pattern in _package_data_patterns() if not list(_PACKAGE.glob(pattern))]

    assert not barren, f"package-data patterns match no file: {barren}"


def test_module_scope_imports_are_declared_dependencies() -> None:
    """Anything imported at import time is a hard dependency, declared or not.

    ``nodes.py`` imports torch at module scope while ``pyproject.toml`` named it
    nowhere; the workspace has torch installed for other reasons, so every local
    check passed and ``pip install streamstudio`` produced a package that could not
    be imported at all.

    Guarded imports are exempt by construction — see :func:`_module_scope_imports`.
    """
    stdlib = set(__import__("sys").stdlib_module_names)
    known = packages_distributions()
    declared = _declared_distributions()

    undeclared: Dict[str, Set[str]] = {}
    for path in sorted(_PACKAGE.glob("*.py")):
        for module in _module_scope_imports(path) - stdlib - {"streamstudio"}:
            distributions = {_normalize(d) for d in known.get(module, [module])}
            if not (distributions & declared):
                undeclared.setdefault(path.name, set()).add(module)

    assert not undeclared, f"imported at module scope but not declared in pyproject: {undeclared}"


def test_the_landing_page_metadata_is_present() -> None:
    """Description, license, classifiers and URLs — the PyPI page's whole frame.

    Each is optional to the build and free to omit, which is why all four went
    missing at once: the wheel builds identically without them.
    """
    project = _project()
    missing = [key for key in ("description", "readme", "license", "classifiers", "urls") if not project.get(key)]

    assert not missing, f"release metadata missing from pyproject: {missing}"


def test_the_development_status_classifier_matches_the_version() -> None:
    """An ``aN``/``bN`` version says pre-release; the classifier must say so too."""
    project = _project()
    status = [c for c in project["classifiers"] if c.startswith("Development Status ::")]
    assert len(status) == 1, f"expected exactly one Development Status classifier, got {status}"

    is_prerelease = "a" in project["version"] or "b" in project["version"] or "rc" in project["version"]
    expected = "3 - Alpha" if is_prerelease else "4 - Beta"

    assert expected in status[0], f"version {project['version']} does not match classifier {status[0]!r}"


def test_the_declared_readme_and_license_files_exist() -> None:
    """``readme`` / ``license-files`` are read at BUILD time, not at install time.

    An editable install never touches either, so a missing one surfaces first on the
    clean checkout that builds the release artifact.
    """
    project = _project()
    declared = [project["readme"], *project.get("license-files", [])]
    missing = [name for name in declared if not (_REPO / name).exists()]

    assert not missing, f"pyproject names files that do not exist: {missing}"


def test_readme_links_stay_inside_this_repository() -> None:
    """A `../other-project/...` link resolves only in a full workspace checkout.

    The README is both the GitHub front page and the PyPI landing page, and neither
    has a sibling project directory next to it — such a link 404s for every reader
    who is not the author. It also names another project, which the docs rules
    forbid: describe what the file demonstrates instead of pointing across the
    workspace at it.
    """
    escaping = [
        target
        for target in _RELATIVE_LINK.findall(_README.read_text())
        if not target.startswith("#") and (target.startswith("../") or not (_REPO / target.split("#")[0]).exists())
    ]

    assert not escaping, f"README links outside this repository: {escaping}"


def test_the_changelog_is_shipped_in_the_sdist() -> None:
    """`MANIFEST.in` must carry `CHANGELOG.md` — nothing else pulls it in.

    setuptools builds an sdist from what is *referenced*: `readme` brings README.md,
    `license-files` brings LICENSE, `packages.find` + `package-data` bring the package.
    A CHANGELOG is referenced by nothing, so it is silently absent — measured on this
    project's own sdist before `MANIFEST.in` existed.

    It matters more than a tidiness point here: `[project.urls] Changelog` points at the
    file on GitHub, so while that repository is private the sdist is the ONLY copy a
    reader can reach.

    Asserted on the declaration rather than by building an sdist, for the same reason
    the package-data rules are: building one takes seconds and needs a clean tree, and
    the declaration is the thing that actually goes stale.
    """
    manifest = _REPO / "MANIFEST.in"
    assert manifest.exists(), "MANIFEST.in is missing; the sdist would ship no CHANGELOG"

    included = {
        line.split(maxsplit=1)[1].strip()
        for line in manifest.read_text().splitlines()
        if line.strip().startswith("include ")
    }
    missing = [name for name in ("CHANGELOG.md",) if name not in included]

    assert not missing, f"MANIFEST.in does not include: {missing}"


def test_no_vendor_traces_in_the_repository() -> None:
    """The whole repository is vendor-clean — the 0.1.0a release gate, kept as a pin.

    The platform-dataset integration (route, picker JS, widget gate) moved out through the
    plugin seam (2026-08-25), and every remaining comment/docstring/fixture mention was
    reworded generically. This test keeps it that way: a vendor token reappearing in any
    git-TRACKED file — shipped source, tests, fixtures, docs — fails here instead of
    shipping. Only tracked files are scanned (machine-local generated files like a pyright
    config legitimately name sibling checkouts). Vendor integrations belong in a plugin
    package, never in this one.
    """
    # The tokens are spelled SPLIT so this very file — which ships in the sdist — is itself
    # clean under the exact grep this test encodes (`grep -ri <token> <artifact>` finds nothing).
    tokens = re.compile("|".join(("hel" "ios", "mel" "ody", "key" "sight")), re.IGNORECASE)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    offenders = []
    for name in tracked:
        path = _REPO / name
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            if tokens.search(line):
                offenders.append(f"{name}:{lineno}: {line.strip()[:80]}")
    assert not offenders, "vendor tokens found:\n" + "\n".join(offenders)


def test_the_readme_does_not_advertise_an_unresolvable_extra() -> None:
    """An extra may exist; an install command a reader can copy must actually work.

    The domain extra names a distribution that is on no index, so
    ``pip install --pre "streamstudio[waivefront]"`` — which the README used to print as a
    fenced, copyable command — resolves to *"your requirements are unsatisfiable"* and
    installs NOTHING, streamstudio included. The extra is kept: it is the honest declaration
    of an optional dependency, it costs nothing where the package is already present, and an
    undeclared extra is silently ignored by installers anyway.

    What is NOT kept is the instruction. Documenting a command that cannot succeed is worse
    than documenting no command, because the reader blames their environment. The README
    tells them to install the domain package by whatever means they have; the palette is
    built from what the environment holds, so that is the whole truth.

    Scoped to install commands in fenced blocks, so prose may still name the extra.
    """
    extras = set(_project().get("optional-dependencies", {}))
    readme = _README.read_text()

    advertised = [
        line.strip()
        for line in readme.splitlines()
        if "pip install" in line
        for extra in extras
        if f"[{extra}]" in line and extra != "dev"
    ]

    assert not advertised, f"README shows an install command for an unresolvable extra: {advertised}"


def test_no_superseded_project_name_in_shipped_code() -> None:
    """The package speaks its own name — not the two it used to have.

    ``fluxstudio`` -> ``streamstudio`` renamed the distribution, the import, the repo, the
    console script, the env vars and the socket vocabulary. An older name still survived
    underneath both: every scan line and all 27 frontend extensions announced themselves
    as ``ComfyStream``, so a first ``import streamstudio`` printed 24 lines branded with a
    name that appears nowhere in the docs, and every ComfyUI session logged it to the
    browser console.

    Scoped to the shipped package because that is what a user can see. Per the pre-1.0
    rule the old spellings are simply gone — there is no alias to keep working.
    """
    superseded = re.compile("|".join(("Comfy" "Stream", "flux" "studio", "Flux" "studio")), re.IGNORECASE)

    offenders = [
        f"{path.relative_to(_REPO)}:{lineno}: {line.strip()[:80]}"
        for path in sorted(_PACKAGE.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".js", ".yaml"}
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1)
        if superseded.search(line)
    ]

    assert not offenders, "superseded project names in shipped code:\n" + "\n".join(offenders)
