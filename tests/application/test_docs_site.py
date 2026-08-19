from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_mkdocs_site_contract() -> None:
    config = REPOSITORY_ROOT / "mkdocs.yml"
    workflow = REPOSITORY_ROOT / ".github" / "workflows" / "docs.yml"

    assert config.is_file()
    config_text = config.read_text(encoding="utf-8")
    assert "theme:" in config_text
    assert "name: material" in config_text
    assert "superpowers/" in config_text

    assert workflow.is_file()
    workflow_text = workflow.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in workflow_text
    assert "actions/upload-pages-artifact@" in workflow_text
    assert "actions/deploy-pages@" in workflow_text
    assert "pages: write" in workflow_text
    assert "id-token: write" in workflow_text


def test_mkdocs_navigation_names_public_reader_pages() -> None:
    config = (REPOSITORY_ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert "CLI reference: cli.md" in config
    assert "Operations and resume: operations.md" in config
    assert (REPOSITORY_ROOT / "docs" / "cli.md").is_file()
    assert (REPOSITORY_ROOT / "docs" / "operations.md").is_file()


def test_public_docs_explain_local_artifacts_and_public_dataset() -> None:
    index = (REPOSITORY_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    operations = (REPOSITORY_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    data = (REPOSITORY_ROOT / "docs" / "data-and-remotes.md").read_text(encoding="utf-8")

    assert "Local run artifacts" in index
    assert "public Hugging Face dataset" in index
    assert "Ctrl-C" in operations
    assert "--apply" in operations
    assert "read-only" in data
    assert "completion receipt" in data


def test_cli_reference_covers_typer_commands_and_safe_defaults() -> None:
    cli = (REPOSITORY_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")

    for command in (
        "init",
        "extract",
        "analyze-results",
        "build-card",
        "verify-results",
        "refresh-card",
        "finalize-run",
        "finalize-snapshot",
        "publish-plan",
        "publish",
        "create-repo",
        "card-stats",
        "publish-trackio",
        "run-all",
    ):
        assert f"`{command}`" in cli
    assert "dry run" in cli
    assert "--source-root" in cli
    assert "--output-root" in cli
    assert "--max-in-flight-areas" in cli
    assert "--fetch-workers" in cli
