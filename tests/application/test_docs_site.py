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
