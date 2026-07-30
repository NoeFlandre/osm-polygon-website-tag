"""Executable package-organization and dependency-boundary contracts."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path("src/osm_polygon_website_tag")
LAYERS = frozenset(
    {
        "contracts",
        "domain",
        "storage",
        "web",
        "runtime",
        "pipeline",
        "reporting",
        "publishing",
        "application",
    }
)
ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "contracts": frozenset(),
    "domain": frozenset(),
    "storage": frozenset(),
    "web": frozenset({"contracts"}),
    "runtime": frozenset(),
    "pipeline": frozenset({"contracts", "domain", "storage", "web", "runtime"}),
    "reporting": frozenset({"contracts", "pipeline", "storage", "runtime"}),
    "publishing": frozenset({"reporting", "runtime"}),
    "application": LAYERS - {"application"},
}


def _internal_dependency(module: str | None) -> str | None:
    if not module or not module.startswith("osm_polygon_website_tag."):
        return None
    candidate = module.split(".", maxsplit=2)[1]
    return candidate if candidate in LAYERS else None


def _package_dependencies() -> dict[str, set[str]]:
    graph = {layer: set() for layer in LAYERS}
    for source in PACKAGE_ROOT.rglob("*.py"):
        relative = source.relative_to(PACKAGE_ROOT)
        if len(relative.parts) < 2:
            continue
        owner = relative.parts[0]
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            modules: list[str | None] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules.append(node.module)
            for module in modules:
                dependency = _internal_dependency(module)
                if dependency and dependency != owner:
                    graph[owner].add(dependency)
    return graph


def test_package_dependencies_follow_approved_direction() -> None:
    graph = _package_dependencies()
    violations = {
        layer: sorted(dependencies - ALLOWED_DEPENDENCIES[layer])
        for layer, dependencies in graph.items()
        if dependencies - ALLOWED_DEPENDENCIES[layer]
    }
    assert not violations, f"disallowed package dependencies: {violations}"


def test_package_dependency_graph_is_acyclic() -> None:
    graph = _package_dependencies()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(layer: str) -> None:
        if layer in visiting:
            raise AssertionError(f"package dependency cycle includes {layer}")
        if layer in visited:
            return
        visiting.add(layer)
        for dependency in graph[layer]:
            visit(dependency)
        visiting.remove(layer)
        visited.add(layer)

    for layer in sorted(LAYERS):
        visit(layer)


def test_source_root_contains_only_package_entry_files() -> None:
    root_files = {path.name for path in PACKAGE_ROOT.iterdir() if path.is_file()}
    assert root_files == {"__init__.py", "py.typed"}


def test_every_source_package_documents_its_boundary() -> None:
    for layer in LAYERS:
        package = PACKAGE_ROOT / layer
        assert (package / "__init__.py").is_file()
        readme = package / "README.md"
        assert readme.is_file()
        assert readme.read_text().strip()
