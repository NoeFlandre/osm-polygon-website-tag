"""Tests for terminal-aware application progress reporting."""

from __future__ import annotations

from io import StringIO
from typing import ClassVar

from osm_polygon_website_tag.application import progress as progress_module
from osm_polygon_website_tag.application.progress import ProgressReporter


class _FakeTqdm:
    instances: ClassVar[list[_FakeTqdm]] = []
    written: ClassVar[list[str]] = []

    def __init__(self, *, total, file, unit, dynamic_ncols) -> None:
        self.total = total
        self.file = file
        self.unit = unit
        self.dynamic_ncols = dynamic_ncols
        self.n = 0
        self.description = ""
        self.closed = False
        self.instances.append(self)

    def set_description_str(self, description: str) -> None:
        self.description = description

    def update(self, amount: int) -> None:
        self.n += amount

    def refresh(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @classmethod
    def write(cls, message: str, *, file) -> None:
        cls.written.append(message)


def _install_fake_tqdm(monkeypatch) -> None:
    _FakeTqdm.instances = []
    _FakeTqdm.written = []
    monkeypatch.setattr(progress_module, "tqdm", _FakeTqdm)


def test_noninteractive_progress_preserves_plain_log_lines() -> None:
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=False)

    reporter("[2/3] Extracting source.osm.pbf")
    reporter("Building aggregate analysis")
    reporter.close(completed=True)

    assert stream.getvalue() == ("[2/3] Extracting source.osm.pbf\nBuilding aggregate analysis\n")


def test_interactive_progress_uses_tqdm_and_keeps_phase_messages(monkeypatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)

    reporter("[2/3] Extracting source.osm.pbf")
    reporter("Building aggregate analysis")

    bar = _FakeTqdm.instances[0]
    assert bar.total == 3
    assert bar.n == 3
    assert bar.description == "Extracting source.osm.pbf"
    assert bar.closed is True
    assert _FakeTqdm.written == ["Building aggregate analysis"]


def test_interrupted_progress_closes_without_marking_complete(monkeypatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter("[2/3] Extracting source.osm.pbf")

    reporter.close(completed=False)

    bar = _FakeTqdm.instances[0]
    assert bar.n == 1
    assert bar.closed is True


def test_interactive_progress_starts_a_new_bar_when_source_index_resets(monkeypatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter("[3/3] Extracting c.osm.pbf")

    reporter("[1/3] Enriching a.osm.pbf")

    assert len(_FakeTqdm.instances) == 2
    assert _FakeTqdm.instances[0].closed is True
    assert _FakeTqdm.instances[1].description == "Enriching a.osm.pbf"
