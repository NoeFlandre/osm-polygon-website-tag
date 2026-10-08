"""Tests for terminal-aware application progress reporting."""

from __future__ import annotations

import copy
import pickle
from io import StringIO
from typing import Any, ClassVar, TextIO

import pytest

from osm_polygon_website_tag.application import progress as progress_module
from osm_polygon_website_tag.application.progress import (
    CountedProgress,
    ProgressReporter,
    counted_progress,
)


class _FakeTqdm:
    instances: ClassVar[list[_FakeTqdm]] = []
    written: ClassVar[list[tuple[str, TextIO]]] = []

    def __init__(self, *, total: int | None, file: TextIO, unit: str, dynamic_ncols: bool) -> None:
        self.total = total
        self.file = file
        self.unit = unit
        self.dynamic_ncols = dynamic_ncols
        self.n = 0
        self.updates: list[int] = []
        self.description = ""
        self.closed = False
        self.instances.append(self)

    def set_description_str(self, description: str) -> None:
        self.description = description

    def update(self, amount: int) -> None:
        self.n += amount
        self.updates.append(amount)

    def refresh(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @classmethod
    def write(cls, message: str, *, file: TextIO) -> None:
        cls.written.append((message, file))


def _install_fake_tqdm(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeTqdm.instances = []
    _FakeTqdm.written = []
    monkeypatch.setattr(progress_module, "tqdm", _FakeTqdm)


def test_noninteractive_progress_preserves_plain_log_lines() -> None:
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=False)

    reporter(counted_progress(2, 3, "Extracting source.osm.pbf"))
    reporter("Building aggregate analysis")
    reporter.close(completed=True)

    assert stream.getvalue() == ("[2/3] Extracting source.osm.pbf\nBuilding aggregate analysis\n")


def test_interactive_progress_uses_tqdm_and_keeps_phase_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=True)

    reporter(counted_progress(2, 3, "Extracting source.osm.pbf"))
    reporter("Building aggregate analysis")

    bar = _FakeTqdm.instances[0]
    assert (bar.file, bar.unit, bar.dynamic_ncols) == (stream, "pbf", True)
    assert bar.total == 3
    assert bar.n == 3
    assert bar.description == "Extracting source.osm.pbf"
    assert bar.closed is True
    assert _FakeTqdm.written == [("Building aggregate analysis", stream)]


def test_interrupted_progress_closes_without_marking_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(2, 3, "Extracting source.osm.pbf"))

    reporter.close(completed=False)

    bar = _FakeTqdm.instances[0]
    assert bar.n == 1
    assert bar.closed is True


def test_interactive_progress_starts_a_new_bar_when_source_index_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(3, 3, "Extracting c.osm.pbf"))

    reporter(counted_progress(1, 3, "Enriching a.osm.pbf"))

    assert len(_FakeTqdm.instances) == 2
    assert _FakeTqdm.instances[0].closed is True
    assert _FakeTqdm.instances[0].n == _FakeTqdm.instances[0].total == 3  # finished, not abandoned
    assert _FakeTqdm.instances[1].description == "Enriching a.osm.pbf"


def test_quiet_progress_writes_nothing() -> None:
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=False, quiet=True)

    reporter(counted_progress(1, 2, "a.osm.pbf"))
    reporter("phase message")

    assert stream.getvalue() == ""


class _FlushCounter(StringIO):
    flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def test_plain_lines_are_flushed_as_they_are_written() -> None:
    stream = _FlushCounter()
    reporter = ProgressReporter(stream, interactive=False)

    reporter("one")
    reporter("two")

    assert stream.flushes == 2


def test_a_repeated_index_updates_the_same_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)

    reporter(counted_progress(2, 3, "Extracting a.osm.pbf"))
    reporter(counted_progress(2, 3, "Enriching a.osm.pbf"))

    assert len(_FakeTqdm.instances) == 1
    assert _FakeTqdm.instances[0].description == "Enriching a.osm.pbf"


def test_the_bar_only_advances_by_positive_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)

    reporter(counted_progress(1, 3, "a"))
    reporter(counted_progress(3, 3, "c"))
    reporter(counted_progress(4, 3, "d"))
    reporter.close(completed=True)

    bar = _FakeTqdm.instances[0]
    assert bar.updates == [2, 1]
    assert bar.n == bar.total == 3


def test_closing_a_complete_run_fills_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(1, 3, "a"))

    reporter.close(completed=True)

    assert _FakeTqdm.instances[0].n == 3


def test_a_finished_run_can_start_a_new_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(2, 3, "a"))
    reporter.close(completed=True)

    reporter(counted_progress(1, 2, "b"))

    assert [bar.total for bar in _FakeTqdm.instances] == [3, 2]


def test_report_progress_forwards_the_message_when_a_callback_is_set() -> None:
    from osm_polygon_website_tag.application.progress import report_progress

    messages: list[str] = []

    report_progress(messages.append, "kept")
    report_progress(None, "ignored")

    assert messages == ["kept"]


def test_counted_progress_is_a_str_with_the_legacy_text() -> None:
    event = counted_progress(2, 3, "Extracting a.osm.pbf")

    assert isinstance(event, CountedProgress)
    assert isinstance(event, str)
    assert (event.current, event.total, event.text) == (2, 3, "Extracting a.osm.pbf")
    messages: list[str] = []
    messages.append(event)
    assert messages == ["[2/3] Extracting a.osm.pbf"]


def test_counted_progress_survives_copy_and_pickle() -> None:
    event = counted_progress(2, 3, "Extracting a.osm.pbf")
    restored = pickle.loads(pickle.dumps(event))  # noqa: S301

    for clone in (copy.copy(event), copy.deepcopy(event), restored):
        assert clone == event
        assert (clone.current, clone.total, clone.text) == (2, 3, "Extracting a.osm.pbf")


def test_counted_progress_noninteractive_output_matches_the_legacy_string() -> None:
    legacy = StringIO()
    typed = StringIO()

    ProgressReporter(legacy, interactive=False)("[2/3] Extracting a.osm.pbf")
    ProgressReporter(typed, interactive=False)(counted_progress(2, 3, "Extracting a.osm.pbf"))

    assert typed.getvalue() == legacy.getvalue() == "[2/3] Extracting a.osm.pbf\n"


def test_a_plain_counted_string_does_not_drive_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=True)

    reporter("[2/3] Extracting a.osm.pbf")
    reporter.close(completed=True)

    assert _FakeTqdm.instances == []
    assert _FakeTqdm.written == [("[2/3] Extracting a.osm.pbf", stream)]


def test_counted_progress_wording_does_not_change_the_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)

    reporter(counted_progress(1, 4, "Extracting a.osm.pbf"))
    reporter(counted_progress(2, 4, "Reworded: [draft] 9/9 text, unicode é"))
    reporter(counted_progress(3, 4, ""))
    reporter.close(completed=True)

    assert len(_FakeTqdm.instances) == 1
    bar = _FakeTqdm.instances[0]
    assert bar.updates == [1, 1, 2]
    assert (bar.description, bar.n, bar.total) == ("", 4, 4)


def test_counted_progress_index_reset_starts_a_new_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(3, 3, "Extracting c.osm.pbf"))

    reporter(counted_progress(1, 3, "Enriching a.osm.pbf"))

    assert len(_FakeTqdm.instances) == 2
    assert _FakeTqdm.instances[0].closed is True
    assert _FakeTqdm.instances[0].n == _FakeTqdm.instances[0].total == 3
    assert _FakeTqdm.instances[1].description == "Enriching a.osm.pbf"


def test_quiet_counted_progress_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    stream = StringIO()
    plain = ProgressReporter(stream, interactive=False, quiet=True)
    interactive = ProgressReporter(stream, interactive=True, quiet=True)

    plain(counted_progress(1, 2, "a.osm.pbf"))
    interactive(counted_progress(1, 2, "a.osm.pbf"))
    interactive.close(completed=True)

    assert stream.getvalue() == ""
    assert _FakeTqdm.instances == []
    assert _FakeTqdm.written == []


def test_interrupted_counted_progress_closes_without_marking_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    reporter = ProgressReporter(StringIO(), interactive=True)
    reporter(counted_progress(2, 3, "Extracting a.osm.pbf"))

    reporter.close(completed=False)

    bar = _FakeTqdm.instances[0]
    assert (bar.n, bar.total) == (1, 3)
    assert bar.closed is True


def test_a_reworded_prefix_stays_an_uncounted_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tqdm(monkeypatch)
    stream = StringIO()
    reporter = ProgressReporter(stream, interactive=True)

    reporter("(3/4) Extracting a.osm.pbf")

    assert _FakeTqdm.instances == []
    assert _FakeTqdm.written == [("(3/4) Extracting a.osm.pbf", stream)]


def test_multiline_counted_text_prints_the_same_line_as_the_legacy_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    typed_stream, legacy_stream = StringIO(), StringIO()

    ProgressReporter(typed_stream, interactive=True)(counted_progress(1, 2, "first\nsecond"))
    ProgressReporter(legacy_stream, interactive=True)("[1/2] first\nsecond")

    assert _FakeTqdm.instances == []
    assert [text for text, _file in _FakeTqdm.written] == [
        "[1/2] first\nsecond",
        "[1/2] first\nsecond",
    ]


def test_multiline_counted_text_plain_output_matches_the_legacy_string() -> None:
    typed, legacy = StringIO(), StringIO()

    ProgressReporter(typed, interactive=False)(counted_progress(1, 2, "first\nsecond"))
    ProgressReporter(legacy, interactive=False)("[1/2] first\nsecond")

    assert typed.getvalue() == legacy.getvalue() == "[1/2] first\nsecond\n"


def test_counted_progress_refuses_non_string_text() -> None:
    no_text: Any = None
    with pytest.raises(TypeError, match="text must be a str"):
        counted_progress(1, 2, no_text)


def test_non_string_progress_is_refused_in_plain_and_interactive_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tqdm(monkeypatch)
    stream = StringIO()
    not_text: Any = b"[1/2] bytes"
    no_text: Any = None

    with pytest.raises(TypeError, match="must be a str"):
        ProgressReporter(stream, interactive=False)(not_text)
    with pytest.raises(TypeError, match="must be a str"):
        ProgressReporter(stream, interactive=True)(no_text)
    assert stream.getvalue() == ""


def test_quiet_progress_ignores_non_string_messages_without_raising() -> None:
    stream = StringIO()
    not_text: Any = b"[1/2] bytes"

    ProgressReporter(stream, interactive=False, quiet=True)(not_text)

    assert stream.getvalue() == ""


def test_counted_progress_wrapper_keeps_every_field() -> None:
    event = counted_progress(2, 3, "Extracting a.osm.pbf")

    assert (event.current, event.total, event.text) == (2, 3, "Extracting a.osm.pbf")
    assert str(event) == "[2/3] Extracting a.osm.pbf"


def test_a_non_text_progress_message_names_its_type() -> None:
    reporter = ProgressReporter(StringIO(), interactive=False)
    not_text: Any = b"[1/2] bytes"

    with pytest.raises(TypeError, match=r"^progress message must be a str, not bytes$"):
        reporter(not_text)
