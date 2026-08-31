# GlotLID Language Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, resumable, stoppable GlotLID V3 stage that records the language and top-1 probability for every successfully extracted website text value while leaving extraction-only runs unchanged.

**Architecture:** Keep URL fetching and language inference as separate stages. A small GlotLID adapter owns model download, hashing, and FastText output normalization; a shard pipeline owns bounded row batches, source/model-bound checkpoint parts, and atomic promotion to schema v1.4. Existing v1.3 shards remain the default output, while `detect-languages` and `run-all --detect-languages` explicitly upgrade public shards to v1.4.

**Tech Stack:** Python 3.12, `uv`, FastText, `huggingface_hub`, PyArrow/Parquet, DuckDB, Typer, pytest, Ruff, ty, mutmut, and radon CRAP.

---

## File map

Create these focused modules and mirrored tests:

- `src/osm_polygon_website_tag/contracts/language_schema.py` — v1.4 language field names and Arrow fields.
- `src/osm_polygon_website_tag/pipeline/glotlid.py` — model identity, detector protocol, FastText adapter, and explicit-cache loader.
- `src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py` — source/model-bound language checkpoint metadata, parts, and assembly.
- `src/osm_polygon_website_tag/pipeline/detect_languages.py` — bounded per-shard language detection and atomic promotion.
- `src/osm_polygon_website_tag/reporting/verification/language.py` — v1.4 language-field invariants.
- `tests/contracts/test_language_schema.py`.
- `tests/pipeline/test_glotlid.py`.
- `tests/pipeline/test_language_detection_checkpoint.py`.
- `tests/pipeline/test_detect_languages.py`.
- `tests/reporting/test_language_verification.py`.

Modify only the following existing responsibilities:

- `contracts/polygon_schema.py` — expose v1.3/v1.4 schemas and language-column documentation without changing the default `POLYGON_PUBLIC_SCHEMA`.
- `runtime/paths.py` — expose the Seagate GlotLID cache path and a production-path assertion.
- `pipeline/enrichment_checkpoint.py` and `pipeline/enrich.py` — allow an existing v1.4 shard to retain language columns if a later URL retry is required; all old call signatures keep their v1.3 defaults.
- `storage/duckdb_engine.py`, `pipeline/deduplicate.py`, `reporting/card.py`, `reporting/verification/shards.py`, and `reporting/verify.py` — consume v1.3/v1.4 public shards and tolerate a bounded mixed-schema transition.
- `application/workflow.py` — add the opt-in model resource, per-source language stage, resume detection, and publication-change tracking.
- `application/cli.py` — add `detect-languages` and `run-all --detect-languages`.
- `pyproject.toml`, `uv.lock`, and `Dockerfile` — pin the runtime dependency and make the builder able to compile it without copying build tools into the runtime image.
- `README.md`, `docs/operations.md`, `docs/architecture.md`, `src/osm_polygon_website_tag/contracts/README.md`, and `src/osm_polygon_website_tag/pipeline/README.md` — document the opt-in operation, v1.4 fields, checkpoint behavior, and Seagate-only model/run boundary.

All tests use `tmp_path` and injected fakes. No test downloads GlotLID or writes to `/Volumes/Seagate M3`.

### Task 1: Add the v1.4 language contract without changing default extraction

**Files:**
- Create: `src/osm_polygon_website_tag/contracts/language_schema.py`
- Modify: `src/osm_polygon_website_tag/contracts/polygon_schema.py`
- Test: `tests/contracts/test_language_schema.py`
- Test: `tests/contracts/test_polygon_schema.py`

- [ ] **Step 1: Write the failing schema tests.**

Add tests for exact order, nullable types, the v1.4 marker, supported-schema recognition, and the unchanged default schema:

```python
from osm_polygon_website_tag.contracts.language_schema import (
    LANGUAGE_COLUMN_NAMES,
    LANGUAGE_FIELDS,
    LANGUAGE_SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    is_current_public_polygon_schema,
    is_supported_public_polygon_schema,
)


def test_language_contract_is_nullable_and_ordered() -> None:
    assert LANGUAGE_SCHEMA_VERSION == "v1.4"
    assert LANGUAGE_COLUMN_NAMES == (
        "website_language",
        "website_language_probability",
        "contact_website_language",
        "contact_website_language_probability",
    )
    assert [field.name for field in LANGUAGE_FIELDS] == list(LANGUAGE_COLUMN_NAMES)
    assert all(field.nullable for field in LANGUAGE_FIELDS)
    assert str(LANGUAGE_FIELDS[0].type) == "string"
    assert str(LANGUAGE_FIELDS[1].type) == "double"


def test_v1_4_extends_v1_3_and_default_schema_stays_v1_3() -> None:
    assert POLYGON_PUBLIC_SCHEMA_V1_4.names[: len(POLYGON_PUBLIC_SCHEMA.names)] == list(
        POLYGON_PUBLIC_SCHEMA.names
    )
    assert POLYGON_PUBLIC_SCHEMA.names[-1] == "contact_website_text_status"
    assert POLYGON_PUBLIC_SCHEMA_V1_4.names[-4:] == list(LANGUAGE_COLUMN_NAMES)
    assert is_current_public_polygon_schema(POLYGON_PUBLIC_SCHEMA)
    assert is_current_public_polygon_schema(POLYGON_PUBLIC_SCHEMA_V1_4)
    assert is_supported_public_polygon_schema(POLYGON_PUBLIC_SCHEMA_V1_4)
```

- [ ] **Step 2: Run the focused tests and verify RED.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/contracts/test_language_schema.py tests/contracts/test_polygon_schema.py -q
```

Expected: collection or assertion failures because the language contract and v1.4 schema do not yet exist.

- [ ] **Step 3: Implement the minimal contract.**

Define the four nullable fields in `language_schema.py`, retain the current v1.3 schema as `POLYGON_PUBLIC_SCHEMA_V1_3` and the existing `POLYGON_PUBLIC_SCHEMA`, then append `LANGUAGE_FIELDS` to create `POLYGON_PUBLIC_SCHEMA_V1_4`. Add `is_current_public_polygon_schema` for exactly v1.3/v1.4, keep `is_supported_public_polygon_schema` inclusive of v1.1 through v1.4, and add four `column_doc` entries. Do not change `SCHEMA_VERSION` or any extraction row builder.

The relevant schema construction must remain equivalent to:

```python
POLYGON_PUBLIC_SCHEMA_V1_3: pa.Schema = pa.schema(
    field for field in POLYGON_PUBLIC_SCHEMA_V1_2 if field.name not in _REMOVED_V1_3_FIELDS
)
POLYGON_PUBLIC_SCHEMA = POLYGON_PUBLIC_SCHEMA_V1_3
POLYGON_PUBLIC_SCHEMA_V1_4: pa.Schema = pa.schema([*POLYGON_PUBLIC_SCHEMA_V1_3, *LANGUAGE_FIELDS])
```

- [ ] **Step 4: Run the schema tests and verify GREEN.**

Run the same focused command. Expected: all focused schema tests pass, including every pre-existing polygon-schema test.

- [ ] **Step 5: Commit the contract.**

```bash
git add src/osm_polygon_website_tag/contracts/language_schema.py src/osm_polygon_website_tag/contracts/polygon_schema.py tests/contracts/test_language_schema.py tests/contracts/test_polygon_schema.py
git commit -m "feat: add v1.4 language schema"
```

### Task 2: Add the Seagate-bound GlotLID adapter

**Files:**
- Modify: `src/osm_polygon_website_tag/runtime/paths.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `Dockerfile`
- Create: `src/osm_polygon_website_tag/pipeline/glotlid.py`
- Create: `tests/pipeline/test_glotlid.py`
- Modify: `tests/runtime/test_paths.py`

- [ ] **Step 1: Write RED tests for path safety and model-output normalization.**

Use a fake FastText object and monkeypatch `huggingface_hub.hf_hub_download`; assert that the explicit `cache_dir`, repository, filename, and revision are passed, that the binary is hashed, that `__label__` is removed, and that newline-containing text is normalized before prediction:

```python
def test_loader_downloads_only_the_pinned_model_into_the_requested_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = tmp_path / "model_v3.bin"
    model_file.write_bytes(b"model")
    calls: list[dict[str, object]] = []

    def download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(model_file)

    monkeypatch.setattr(glotlid, "hf_hub_download", download)
    monkeypatch.setattr(glotlid.fasttext, "load_model", lambda path: FakeFastText())

    detector = glotlid.load_glotlid_detector(tmp_path / "cache")

    assert calls == [
        {
            "repo_id": "cis-lmu/glotlid",
            "filename": "model_v3.bin",
            "revision": "85cd671",
            "cache_dir": str(tmp_path / "cache"),
        }
    ]
    assert detector.identity.filename == "model_v3.bin"
    assert detector.identity.sha256 == hashlib.sha256(b"model").hexdigest()


def test_detector_returns_one_prediction_per_input_in_order() -> None:
    detector = glotlid.GlotLIDDetector(FakeFastText(), glotlid.ModelIdentity("r", "f", "v", "h"))
    assert detector.predict(["hello\nworld", "bonjour"]) == [
        glotlid.LanguagePrediction("eng_Latn", 0.9),
        glotlid.LanguagePrediction("fra_Latn", 0.8),
    ]
```

The test module defines the complete fake backend used above:

```python
class FakeFastText:
    def predict(self, texts: list[str], *, k: int) -> tuple[list[list[str]], list[list[float]]]:
        assert k == 1
        return (
            [["__label__eng_Latn"] if "hello" in text else ["__label__fra_Latn"] for text in texts],
            [[0.9] if "hello" in text else [0.8] for text in texts],
        )
```

Add path tests asserting `assert_seagate_path(tmp_path, label="model cache")` raises and the default cache equals `DEFAULT_DATA_ROOT / "models" / "glotlid"` without creating test data outside `tmp_path`.

- [ ] **Step 2: Run the adapter tests and verify RED.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_glotlid.py tests/runtime/test_paths.py -q
```

Expected: missing adapter symbols and path helpers.

- [ ] **Step 3: Add the dependency and implement the adapter.**

Add `fasttext>=0.9.3,<1` to runtime dependencies and run `uv lock`; add `build-essential` only in the Docker builder stage, leaving the final runtime stage based on the existing dependency-only image. Implement these typed interfaces:

```python
@dataclass(frozen=True)
class ModelIdentity:
    repository: str
    filename: str
    revision: str
    sha256: str


@dataclass(frozen=True)
class LanguagePrediction:
    label: str
    probability: float


class LanguageDetector(Protocol):
    identity: ModelIdentity

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]: ...


def load_glotlid_detector(cache_dir: Path) -> GlotLIDDetector: ...
```

Pin `MODEL_REPOSITORY = "cis-lmu/glotlid"`, `MODEL_FILENAME = "model_v3.bin"`, and `MODEL_REVISION = "85cd671"`. Call `hf_hub_download(..., cache_dir=str(cache_dir))`, hash the returned file in bounded chunks, load it once with `fasttext.load_model`, and normalize FastText labels by removing the `__label__` prefix. Convert newline and carriage-return characters to spaces before prediction, preserve input order, require exactly one label/probability pair per text, and reject non-finite or out-of-range probabilities.

In `runtime/paths.py`, add:

```python
def glotlid_model_cache_dir() -> Path:
    path = data_root() / "models" / "glotlid"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assert_seagate_path(path: Path | str, *, label: str) -> Path:
    normalized = Path(path).expanduser().resolve()
    if not normalized.is_relative_to(DEFAULT_DATA_ROOT):
        raise ValueError(f"{label} must be under the Seagate data root: {DEFAULT_DATA_ROOT}")
    return normalized
```

- [ ] **Step 4: Run the adapter tests and type-check the changed modules.**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_glotlid.py tests/runtime/test_paths.py -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked ty check src/osm_polygon_website_tag/pipeline/glotlid.py src/osm_polygon_website_tag/runtime/paths.py tests/pipeline/test_glotlid.py tests/runtime/test_paths.py
```

Expected: focused tests pass and ty reports no errors.

- [ ] **Step 5: Commit the adapter and dependency boundary.**

```bash
git add src/osm_polygon_website_tag/pipeline/glotlid.py src/osm_polygon_website_tag/runtime/paths.py tests/pipeline/test_glotlid.py tests/runtime/test_paths.py pyproject.toml uv.lock Dockerfile
git commit -m "feat: add Seagate-bound GlotLID adapter"
```

### Task 3: Implement source/model-bound language checkpoints

**Files:**
- Create: `src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py`
- Create: `tests/pipeline/test_language_detection_checkpoint.py`

- [ ] **Step 1: Write RED checkpoint tests.**

Test that metadata records the exact source row count/hash plus all model identity fields, that a changed source or model fails closed, that parts are sequential and v1.4-shaped, and that the final assembly preserves part order:

```python
def test_checkpoint_metadata_binds_source_and_model(tmp_path: Path) -> None:
    model = ModelIdentity("cis-lmu/glotlid", "model_v3.bin", "85cd671", "a" * 64)
    checkpoint = load_language_checkpoint(
        tmp_path / "region.parquet",
        source_row_count=4,
        source_shard_sha256="b" * 64,
        model=model,
    )
    metadata = json.loads((checkpoint.directory / "checkpoint.json").read_text())
    assert metadata == {
        "checkpoint_version": 1,
        "schema_version": "v1.4",
        "source_row_count": 4,
        "source_shard_sha256": "b" * 64,
        "model_repository": "cis-lmu/glotlid",
        "model_filename": "model_v3.bin",
        "model_revision": "85cd671",
        "model_sha256": "a" * 64,
    }


def test_checkpoint_rejects_model_drift(tmp_path: Path) -> None:
    first = ModelIdentity("cis-lmu/glotlid", "model_v3.bin", "85cd671", "a" * 64)
    second = ModelIdentity("cis-lmu/glotlid", "model_v3.bin", "85cd671", "c" * 64)
    shard = tmp_path / "region.parquet"
    load_language_checkpoint(shard, source_row_count=1, source_shard_sha256="b" * 64, model=first)
    with pytest.raises(ValueError, match="does not match"):
        load_language_checkpoint(
            shard, source_row_count=1, source_shard_sha256="b" * 64, model=second
        )
```

- [ ] **Step 2: Run the checkpoint tests and verify RED.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_language_detection_checkpoint.py -q
```

Expected: the new checkpoint module and functions are missing.

- [ ] **Step 3: Implement the checkpoint module.**

Use `.language.parts` beside each source shard, `checkpoint.json`, and `part-00000000.parquet` naming. Implement these typed entry points:

```python
@dataclass(frozen=True)
class LanguageCheckpoint:
    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


def load_language_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
    model: ModelIdentity,
) -> LanguageCheckpoint: ...


def checkpoint_parts(directory: Path) -> tuple[Path, ...]: ...


def write_language_checkpoint_part(
    directory: Path, index: int, rows: list[dict[str, object]], *, batch_rows: int
) -> None: ...


def assemble_language_checkpoint(
    parts: tuple[Path, ...], staged: Path, *, batch_rows: int, row_count: int
) -> int: ...
```

Write each part through `BatchParquetSink` using `POLYGON_PUBLIC_SCHEMA_V1_4`, validate its exact schema and positive/expected row count, and atomically promote it. Validate known temporary files only, reject gaps/unknown files, stream batches during assembly, and delete the staged file on any `BaseException` while leaving durable parts intact.

- [ ] **Step 4: Run checkpoint tests and the existing checkpoint regression tests.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_language_detection_checkpoint.py tests/pipeline/test_enrichment_checkpoint.py -q
```

Expected: new checkpoint tests and all existing v1.3 checkpoint tests pass.

- [ ] **Step 5: Commit the checkpoint implementation.**

```bash
git add src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py tests/pipeline/test_language_detection_checkpoint.py
git commit -m "feat: add resumable language checkpoints"
```

### Task 4: Build the bounded, atomic per-shard detection pipeline

**Files:**
- Create: `src/osm_polygon_website_tag/pipeline/detect_languages.py`
- Create: `tests/pipeline/test_detect_languages.py`

- [ ] **Step 1: Write RED tests with an injected fake detector.**

Create v1.3 test shards with successful and absent website/contact text, then assert independent prediction calls, exact labels/probabilities, nulls for absent values, v1.4 schema, row-order preservation, bounded batch behavior, and no model call on a completed v1.4 shard:

```python
class FakeDetector:
    identity = ModelIdentity("repo", "file", "revision", "d" * 64)

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        self.calls.append(list(texts))
        return [
            LanguagePrediction("eng_Latn" if "English" in text else "fra_Latn", 0.91)
            for text in texts
        ]


class RecordingDetector(FakeDetector):
    @property
    def seen(self) -> list[str]:
        return [text for call in self.calls for text in call]


class InterruptingDetector(RecordingDetector):
    def __init__(self, *, interrupt_on_call: int) -> None:
        super().__init__()
        self.interrupt_on_call = interrupt_on_call

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        result = super().predict(texts)
        if len(self.calls) == self.interrupt_on_call:
            raise KeyboardInterrupt
        return result


def v1_3_text_row(
    index: int, *, website_text: str | None, contact_text: str | None = None
) -> dict[str, object]:
    row = legacy_polygon_row(
        polygon_id=f"source:way/{index}",
        website="https://example.org" if website_text is not None else None,
        contact="https://contact.example.org" if contact_text is not None else None,
    )
    for name in (
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    ):
        row.pop(name)
    row.update(
        initial_text_fields(
            website_present=website_text is not None,
            contact_website_present=contact_text is not None,
        )
    )
    row.update(
        {
            "website_text": website_text,
            "website_word_count": 2 if website_text is not None else None,
            "website_text_status": "success" if website_text is not None else "absent",
            "contact_website_text": contact_text,
            "contact_website_word_count": 2 if contact_text is not None else None,
            "contact_website_text_status": "success" if contact_text is not None else "absent",
            "schema_version": "v1.3",
        }
    )
    return {name: row[name] for name in POLYGON_PUBLIC_SCHEMA.names}


def write_v1_3_text_shard(tmp_path: Path, *, rows: list[dict[str, object]]) -> Path:
    shard = tmp_path / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    return shard


def checkpoint_part_count(shard: Path) -> int:
    return len((shard.parent / f".{shard.name}.language.parts").glob("part-*.parquet"))


def test_detect_language_shard_populates_website_and_contact_independently(
    tmp_path: Path,
) -> None:
    shard = write_v1_3_text_shard(
        tmp_path,
        rows=[
            v1_3_text_row(0, website_text="English text", contact_text="Texte français"),
            v1_3_text_row(1, website_text="English only", contact_text=None),
        ],
    )
    detector = FakeDetector()

    result = detect_language_shard(shard, detector=detector, batch_rows=1)

    table = pq.read_table(shard)
    assert table.schema.equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert [row["polygon_id"] for row in table.to_pylist()] == ["p0", "p1"]
    assert table["website_language"].to_pylist() == ["eng_Latn", "eng_Latn"]
    assert table["contact_website_language"].to_pylist() == ["fra_Latn", None]
    assert table["website_language_probability"].to_pylist() == [0.91, 0.91]
    assert detector.calls == [["English text"], ["Texte français"], ["English only"]]
    assert result.changed is True


def test_interrupt_leaves_original_and_resumes_only_after_durable_prefix(tmp_path: Path) -> None:
    shard = write_v1_3_text_shard(
        tmp_path,
        rows=[
            v1_3_text_row(0, website_text="English 0"),
            v1_3_text_row(1, website_text="English 1"),
        ],
    )
    detector = InterruptingDetector(interrupt_on_call=2)

    with pytest.raises(KeyboardInterrupt):
        detect_language_shard(shard, detector=detector, batch_rows=1)

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA)
    assert checkpoint_part_count(shard) == 1

    resumed = RecordingDetector()
    result = detect_language_shard(shard, detector=resumed, batch_rows=1)
    assert result.changed is True
    assert resumed.seen == ["English 1"]
    assert pq.read_table(shard)["website_language"].to_pylist() == ["eng_Latn", "eng_Latn"]
```

Also test that a changed source hash, unfinished or unknown text status, malformed successful text, a missing model prediction, or a non-finite probability raises before promotion and leaves the original shard valid. Resolved non-success statuses such as `fetch_error` are preserved with null language fields.

- [ ] **Step 2: Run the detection tests and verify RED.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py -q
```

Expected: missing pipeline symbols and failed assertions.

- [ ] **Step 3: Implement the minimal pipeline.**

Implement these typed entry points:

```python
@dataclass(frozen=True)
class LanguageDetectionResult:
    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int


def shard_needs_language_detection(shard_path: Path | str) -> bool: ...


def detect_language_shard(
    shard_path: Path | str,
    *,
    detector: LanguageDetector,
    batch_rows: int = 512,
) -> LanguageDetectionResult: ...
```

The pipeline must:

1. Accept only exact v1.3 or v1.4 public schemas, require both text-status columns, and require every text status to be a known resolved outcome before processing a shard. `pending`, null, and unknown values are rejected; resolved non-success outcomes are preserved with null language fields.
2. Read `batch_rows` source rows at a time and skip exactly the durable checkpoint prefix.
3. Set `schema_version` to `"v1.4"`; for `success`, send website and contact text values as separate ordered detector calls; for every resolved non-success status, keep both language fields null.
4. Preserve already-complete v1.4 language pairs, detect only missing pairs, and reject incomplete pairs rather than silently keeping one half.
5. Flush one v1.4 checkpoint part after every completed input batch, assemble in source order, validate exact v1.4 schema and row count, then atomically replace the shard.
6. Remove the language checkpoint directory only after successful promotion. On `KeyboardInterrupt` or ordinary exceptions, remove only known staged files and retain the original shard plus durable parts.

Use `detector.identity` in checkpoint metadata, `hash_shard` for the source identity, and `shutil.rmtree` only on the exact checkpoint directory owned by this shard after success.

- [ ] **Step 4: Run focused detection tests and verify the resource bound.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py tests/pipeline/test_language_detection_checkpoint.py -q
```

Expected: all detection and checkpoint tests pass, with no network calls and no writes outside `tmp_path`.

- [ ] **Step 5: Commit the shard pipeline.**

```bash
git add src/osm_polygon_website_tag/pipeline/detect_languages.py tests/pipeline/test_detect_languages.py
git commit -m "feat: detect GlotLID languages by shard"
```

### Task 5: Preserve v1.4 through existing readers, retries, and verification

**Files:**
- Modify: `src/osm_polygon_website_tag/pipeline/enrichment_checkpoint.py`
- Modify: `src/osm_polygon_website_tag/pipeline/enrich.py`
- Modify: `src/osm_polygon_website_tag/storage/duckdb_engine.py`
- Modify: `src/osm_polygon_website_tag/pipeline/deduplicate.py`
- Modify: `src/osm_polygon_website_tag/reporting/card.py`
- Modify: `src/osm_polygon_website_tag/reporting/verification/shards.py`
- Modify: `src/osm_polygon_website_tag/reporting/verification/language.py`
- Modify: `src/osm_polygon_website_tag/reporting/verify.py`
- Test: `tests/pipeline/test_enrich.py`
- Test: `tests/pipeline/test_deduplicate.py`
- Test: `tests/pipeline/test_analyze.py`
- Test: `tests/reporting/test_card.py`
- Test: `tests/reporting/test_verify.py`
- Create: `tests/reporting/test_language_verification.py`

- [ ] **Step 1: Write RED compatibility tests.**

Add tests that:

- URL enrichment on a v1.4 shard retains all four language fields and writes a v1.4 checkpoint part.
- DuckDB analysis accepts a directory containing one v1.3 and one v1.4 public shard.
- Deduplication preserves language fields and emits v1.4 when any input is v1.4.
- A v1.4 card lists the language columns while a v1.3 card remains byte-compatible with its current schema section.
- The verifier accepts v1.3 and v1.4 output schemas but rejects a successful v1.4 text row with a missing language or an out-of-range probability.

Example language verification RED test:

```python
def test_verify_rejects_success_without_language_probability(tmp_path: Path) -> None:
    run_dir = make_v1_4_run(tmp_path, website_language="eng_Latn", website_probability=None)
    report = verify_results(run_dir)
    assert not report.ok
    assert any("language probability" in error for error in report.errors)
```

- [ ] **Step 2: Run the compatibility tests and verify RED.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py -q
```

Expected: v1.4 preservation/verification tests fail while all existing tests remain the regression baseline.

- [ ] **Step 3: Implement compatibility without changing v1.3 defaults.**

Parameterize existing URL checkpoint helpers with optional `schema` and `schema_version` keyword arguments whose defaults remain `POLYGON_PUBLIC_SCHEMA` and `SCHEMA_VERSION`. Select v1.4 as the target only when the input shard is v1.4, so later URL retries cannot drop language columns.

Register DuckDB public files with `read_parquet(..., union_by_name=true)` and add nullable language columns to its empty public view. In deduplication, select v1.4 when any input is v1.4, use the same union-by-name read, and cast/materialize every output shard to that selected schema.

Render the card schema from the run: return v1.4 when any public shard is v1.4, otherwise retain the current v1.3 rows. Add `verify_language_invariants` that skips v1.3 files, then enforces this contract for v1.4 rows:

```python
if text_status == "success":
    require_nonempty_string(language)
    require_finite_probability(language_probability)
elif language is not None or language_probability is not None:
    errors.append(f"{label} non-success text must have null language fields")
```

Use `is_current_public_polygon_schema` in final public-shard verification so v1.3 and v1.4 are accepted, while v1.1/v1.2 remain migration inputs rather than finalized output schemas.

- [ ] **Step 4: Run the compatibility tests and full non-quality regression tests.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest --ignore=tests/architecture -q
```

Expected: compatibility tests and the complete existing suite pass.

- [ ] **Step 5: Commit compatibility support.**

```bash
git add src/osm_polygon_website_tag/pipeline/enrichment_checkpoint.py src/osm_polygon_website_tag/pipeline/enrich.py src/osm_polygon_website_tag/storage/duckdb_engine.py src/osm_polygon_website_tag/pipeline/deduplicate.py src/osm_polygon_website_tag/reporting/card.py src/osm_polygon_website_tag/reporting/verification/shards.py src/osm_polygon_website_tag/reporting/verification/language.py src/osm_polygon_website_tag/reporting/verify.py tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py
git commit -m "feat: preserve language columns across readers"
```

### Task 6: Integrate opt-in detection into the resumable workflow

**Files:**
- Modify: `src/osm_polygon_website_tag/application/workflow.py`
- Test: `tests/application/test_workflow.py`

- [ ] **Step 1: Write RED workflow tests.**

Add tests using a fake detector proving that:

- `run_all` with its default arguments never constructs or calls a model loader and still emits v1.3 shards.
- `run_all(..., detect_languages=True, language_detector=fake)` detects language after URL text enrichment, updates public-shard hashes, and publishes the changed shard/card path when apply mode is mocked.
- A rerun after an interrupted shard calls the detector only for the uncheckpointed suffix.
- An existing v1.3 run can be resumed with `detect_languages=True` and an existing v1.4 run is not downgraded.
- A changed source/model identity raises before mixed output is promoted.

The default-path assertion should be explicit:

```python
def test_run_all_default_does_not_load_language_model(
    make_pbf: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from osm_polygon_website_tag.application import workflow

    monkeypatch.setattr(
        workflow,
        "load_glotlid_detector",
        lambda *_args, **_kwargs: pytest.fail("default run-all must not load GlotLID"),
    )
    result = run_all(
        source_root=_sources(make_pbf, tmp_path), output_root=tmp_path / "runs", run_id="plain"
    )
    assert result.complete
    assert all(
        pq.read_schema(path).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
        for path in (result.run_dir / "polygons").glob("*.parquet")
    )
```

- [ ] **Step 2: Run workflow tests and verify RED.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_workflow.py -q
```

Expected: `run_all` has no language option and the new tests fail.

- [ ] **Step 3: Implement the opt-in workflow resource and stage.**

Extend `run_all` with `detect_languages: bool = False` and an optional typed `language_detector` injection used only by hermetic tests. When no detector is injected and detection is requested, assert the run directory and `glotlid_model_cache_dir()` are under the Seagate root, then load one detector before source processing. The default branch must not import or load model resources beyond normal module imports.

Extend `_SourceRunContext` with the opt-in flag and detector. After `_enrich_source_shard_if_needed`, call a new `_detect_source_shard_if_needed` only when the flag is true:

```python
def _detect_source_shard_if_needed(
    *, source: Path, shard: Path, context: _SourceRunContext, index: int, total: int
) -> bool:
    if not context.detect_languages or not shard_needs_language_detection(shard):
        return False
    detector = context.language_detector
    if detector is None:
        raise ValueError("language detection requested without a detector")
    _progress(context.progress, f"[{index}/{total}] Detecting languages for {source.name}")
    result = detect_language_shard(shard, detector=detector)
    update_public_shard_metadata(
        context.state,
        filename=source.name,
        row_count=result.row_count,
        shard_sha256=result.shard_sha256,
    )
    return result.changed
```

Pass `language_changed` through `_publish_source_if_needed`, `_source_upload_is_current_for_context`, and `_source_requires_publication` so language-only changes invalidate upload acknowledgements. Extend enrichment-phase entry logic to reopen `ANALYZED`, `CARD_BUILT`, or unfrozen `COMPLETE` runs when the opt-in flag finds v1.3/incomplete language shards. Treat v1.4 as current for URL-enrichment schema checks.

- [ ] **Step 4: Run workflow tests and verify GREEN.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_workflow.py -q
```

Expected: all workflow tests pass, including the default no-model regression test and opt-in resume tests.

- [ ] **Step 5: Commit workflow integration.**

```bash
git add src/osm_polygon_website_tag/application/workflow.py tests/application/test_workflow.py
git commit -m "feat: integrate opt-in language detection"
```

### Task 7: Add the standalone CLI command and operator documentation

**Files:**
- Modify: `src/osm_polygon_website_tag/application/cli.py`
- Modify: `tests/application/test_cli.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/architecture.md`
- Modify: `src/osm_polygon_website_tag/contracts/README.md`
- Modify: `src/osm_polygon_website_tag/pipeline/README.md`

- [ ] **Step 1: Write RED CLI tests.**

Test command registration, fake detector injection at the workflow boundary, Seagate rejection before model loading, and resume state behavior. The command contract is:

```text
osm-polygon-website-tag detect-languages --run-dir <Seagate run directory>
osm-polygon-website-tag run-all --detect-languages ...
```

The standalone command must load the model once, process public shards in sorted order, update each source manifest hash, transition a non-frozen analyzed/card-built/complete run to `enriching` before work and to `enriched` after all shards finish, and leave `enriching` plus durable parts on interruption. A frozen `snapshot_status=done` run is rejected without touching the model cache.

- [ ] **Step 2: Run CLI tests and verify RED.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_cli.py -q
```

Expected: the command and `--detect-languages` option are not registered.

- [ ] **Step 3: Implement the CLI surface.**

Import `STATUS_COMPLETE` and `glotlid_model_cache_dir`, add `--detect-languages` to `run-all`, and add:

```python
@app.command("detect-languages")
def detect_languages_command(run_dir: RunDir) -> int:
    """Detect GlotLID languages for every completed text shard."""
    state = load_run(run_dir)
    paths = sorted((run_dir / "polygons").glob("*.parquet"))
    assert_seagate_path(run_dir, label="run directory")
    needed = [path for path in paths if shard_needs_language_detection(path)]
    if not needed:
        _json({"changed_shards": 0, "run_dir": str(run_dir)}, sort_keys=True)
        return 0
    _prepare_language_command_state(state)
    model_cache = glotlid_model_cache_dir()
    assert_seagate_path(model_cache, label="GlotLID model cache")
    detector = load_glotlid_detector(model_cache)
    changed = 0
    for shard in needed:
        result = detect_language_shard(shard, detector=detector)
        update_public_shard_metadata(
            state,
            filename=f"{shard.stem}.osm.pbf",
            row_count=result.row_count,
            shard_sha256=result.shard_sha256,
        )
        changed += int(result.changed)
    _finish_language_command_state(state)
    _json({"changed_shards": changed, "run_dir": str(run_dir)}, sort_keys=True)
    return 0
```

Define the two state helpers directly beside the command:

```python
def _prepare_language_command_state(state: RunState) -> None:
    status = state.metadata.get("status")
    if status == STATUS_COMPLETE and state.metadata.get("snapshot_status") == "done":
        raise ValueError("cannot add languages to a frozen snapshot")
    if status in {STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE}:
        transition_status(state, STATUS_ENRICHING)
    elif status not in {STATUS_ENRICHING, STATUS_ENRICHED}:
        raise ValueError("detect-languages requires an extracted/enriched run")


def _finish_language_command_state(state: RunState) -> None:
    if state.metadata.get("status") == STATUS_ENRICHING:
        transition_status(state, STATUS_ENRICHED)
```

The actual implementation must validate source-manifest membership and known resolved text statuses before each shard, reject a frozen snapshot, and keep the model cache under the Seagate root. `run-all --detect-languages` must pass the same stage and model resource, not a second implementation.

- [ ] **Step 4: Run CLI tests and documentation build checks.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_cli.py -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked mkdocs build --strict
```

Expected: CLI tests pass and the documentation build is clean.

- [ ] **Step 5: Document the operator contract.**

Document that production files are kept at:

```text
/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/
/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>/
```

Document the pinned [GlotLID model card](https://huggingface.co/cis-lmu/glotlid), the four nullable v1.4 fields, exact raw labels such as `eng_Latn`, top-1 probabilities, `Ctrl-C`/rerun behavior, the known-resolved text precondition, and the need to run the existing analysis/card/finalization commands after a standalone language stage changes an already analyzed run. State explicitly that the default `run-all` path remains v1.3 and does not load the model.

- [ ] **Step 6: Commit the CLI and documentation.**

```bash
git add src/osm_polygon_website_tag/application/cli.py tests/application/test_cli.py README.md docs/operations.md docs/architecture.md src/osm_polygon_website_tag/contracts/README.md src/osm_polygon_website_tag/pipeline/README.md
git commit -m "docs: operate the GlotLID language stage"
```

### Task 8: Run the complete quality gates and mutation/CRAP checks

**Files:**
- Modify only files required by formatter, lockfile, or quality-report output; do not stage unrelated work.

- [ ] **Step 1: Run the complete repository checks.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just check
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-commit
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-push
```

Expected: lock validation, Ruff, format, `ty check src tests scripts`, all tests, and `uv build` pass.

- [ ] **Step 2: Run the CRAP gate and inspect the threshold.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just crap
```

Expected: the command succeeds and every changed production function has CRAP below 6.

- [ ] **Step 3: Run mutation testing and inspect every result.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just mutation
```

Expected: no `survived`, `no tests`, `timeout`, `suspicious`, `segfault`, or interrupted mutants remain.

- [ ] **Step 4: Run the final feature regression suite.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv build
git status --short --branch
git diff --check HEAD
```

Expected: all tests pass, the package builds, whitespace checks pass, and only the intentional feature commits/files are present in the isolated worktree.

- [ ] **Step 5: Record the final evidence.**

Record the exact test count, CRAP result, mutation result, and any environmental limitation in the handoff. Do not claim a real model download or production inference was run unless it was performed using the Seagate cache and run roots. Do not stage generated reports or unrelated worktree files.
