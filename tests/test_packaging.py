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
import subprocess
import tomllib
from fnmatch import fnmatch
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Any, Dict, List, Set, cast

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
