# GlotLID Language Detection Implementation Plan

> **Status: completed. This file is a historical record.** Every module in the
> file map below is in the product. Nobody ticked the checkboxes. The
> checkboxes are not a to-do list. Later, the test files named here were
> split: `tests/reporting/test_card.py` became `tests/reporting/test_card_*.py`,
> and `tests/application/test_workflow.py` became
> `tests/application/test_workflow_*.py`.

**Goal:** Add a GlotLID V3 stage that the operator must switch on. The stage
can resume and can stop. It records the language and the top-1 probability for
each website text value that the extraction stage got successfully. Runs that
only extract text do not change.

**Architecture:** Keep URL fetching and language inference as separate stages.
A small GlotLID adapter does these tasks: model download, hashing, and
normalization of the FastText output. A shard pipeline owns these items:
bounded row batches, checkpoint parts that are tied to the source and the
model, and atomic promotion to schema v1.4. The v1.3 shards stay the default
output. The `detect-languages` command and `run-all --detect-languages`
upgrade public shards to v1.4. The operator must request this upgrade
explicitly.

**Tech Stack:** Python 3.12, `uv`, FastText, `huggingface_hub`, PyArrow/Parquet, DuckDB, Typer, pytest, Ruff, ty, mutmut, and radon CRAP.

---

## File map

Create these focused modules and the tests that mirror them:

- `src/osm_polygon_website_tag/contracts/language_schema.py` - the v1.4 language field names and the Arrow fields.
- `src/osm_polygon_website_tag/pipeline/glotlid.py` - the model identity, the detector protocol, the FastText adapter, and the loader for an explicit cache.
- `src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py` - the language checkpoint metadata (tied to the source and the model), the parts, and the assembly.
- `src/osm_polygon_website_tag/pipeline/detect_languages.py` - bounded language detection for each shard, and atomic promotion.
- `src/osm_polygon_website_tag/reporting/verification/language.py` - the v1.4 language-field invariants.
- `tests/contracts/test_language_schema.py`.
- `tests/pipeline/test_glotlid.py`.
- `tests/pipeline/test_language_detection_checkpoint.py`.
- `tests/pipeline/test_detect_languages.py`.
- `tests/reporting/test_language_verification.py`.

Change only these responsibilities in existing files:

- `contracts/polygon_schema.py` - show the v1.3 and v1.4 schemas and the language-column documentation. Do not change the default `POLYGON_PUBLIC_SCHEMA`.
- `runtime/paths.py` - show the Seagate GlotLID cache path and a check for the production path.
- `pipeline/enrichment_checkpoint.py` and `pipeline/enrich.py` - let an existing v1.4 shard keep its language columns if a later URL retry is necessary. All old call signatures keep their v1.3 defaults.
- `storage/duckdb_engine.py`, `pipeline/deduplicate.py`, `reporting/card.py`, `reporting/verification/shards.py`, and `reporting/verify.py` - read v1.3 and v1.4 public shards, and accept a bounded transition with mixed schemas.
- `application/workflow.py` - add the optional model resource, the language stage for each source, the resume detection, and the tracking of publication changes.
- `application/cli.py` - add `detect-languages` and `run-all --detect-languages`.
- `pyproject.toml`, `uv.lock`, and `Dockerfile` - pin the runtime dependency. Let the builder compile it. Do not copy build tools into the runtime image.
- `README.md`, `docs/operations.md`, `docs/architecture.md`, `src/osm_polygon_website_tag/contracts/README.md`, and `src/osm_polygon_website_tag/pipeline/README.md` - document the optional operation, the v1.4 fields, the checkpoint behavior, and the boundary of the model and the run (Seagate only).

All tests use `tmp_path` and injected fakes. No test downloads GlotLID. No test writes to `/Volumes/Seagate M3`.

### Task 1: Add the v1.4 language contract without a change to the default extraction

**Files:**
- Create: `src/osm_polygon_website_tag/contracts/language_schema.py`
- Modify: `src/osm_polygon_website_tag/contracts/polygon_schema.py`
- Test: `tests/contracts/test_language_schema.py`
- Test: `tests/contracts/test_polygon_schema.py`

- [ ] **Step 1: Write the failing schema tests.**

Add tests for these items: the exact order, the nullable types, the v1.4 marker, the recognition of supported schemas, and the unchanged default schema.

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

- [ ] **Step 2: Run the focused tests. They must fail (RED).**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/contracts/test_language_schema.py tests/contracts/test_polygon_schema.py -q
```

Expected result: collection errors or assertion failures. The language contract and the v1.4 schema do not exist yet.

- [ ] **Step 3: Write the minimal contract.**

In `language_schema.py`, define the four nullable fields. Keep the current v1.3 schema as `POLYGON_PUBLIC_SCHEMA_V1_3` and as the existing `POLYGON_PUBLIC_SCHEMA`. Then add `LANGUAGE_FIELDS` to it to make `POLYGON_PUBLIC_SCHEMA_V1_4`. Add `is_current_public_polygon_schema`. It must accept exactly v1.3 and v1.4. Keep `is_supported_public_polygon_schema` open to v1.1 through v1.4. Add four `column_doc` entries. Do not change `SCHEMA_VERSION`. Do not change a row builder for extraction.

The schema construction must stay equivalent to this code:

```python
POLYGON_PUBLIC_SCHEMA_V1_3: pa.Schema = pa.schema(
    field for field in POLYGON_PUBLIC_SCHEMA_V1_2 if field.name not in _REMOVED_V1_3_FIELDS
)
POLYGON_PUBLIC_SCHEMA = POLYGON_PUBLIC_SCHEMA_V1_3
POLYGON_PUBLIC_SCHEMA_V1_4: pa.Schema = pa.schema([*POLYGON_PUBLIC_SCHEMA_V1_3, *LANGUAGE_FIELDS])
```

- [ ] **Step 4: Run the schema tests. They must pass (GREEN).**

Run the same focused command. Expected result: all focused schema tests pass. This includes each polygon-schema test that existed before.

- [ ] **Step 5: Commit the contract.**

```bash
git add src/osm_polygon_website_tag/contracts/language_schema.py src/osm_polygon_website_tag/contracts/polygon_schema.py tests/contracts/test_language_schema.py tests/contracts/test_polygon_schema.py
git commit -m "feat: add v1.4 language schema"
```

### Task 2: Add the GlotLID adapter that is tied to Seagate

**Files:**
- Modify: `src/osm_polygon_website_tag/runtime/paths.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `Dockerfile`
- Create: `src/osm_polygon_website_tag/pipeline/glotlid.py`
- Create: `tests/pipeline/test_glotlid.py`
- Modify: `tests/runtime/test_paths.py`

- [ ] **Step 1: Write RED tests for path safety and for the normalization of the model output.**

Use a fake FastText object. Use monkeypatch on `huggingface_hub.hf_hub_download`. The tests must check these items: the call passes the explicit `cache_dir`, the repository, the filename, and the revision; the code hashes the binary; the code removes `__label__`; and the code normalizes text that contains newlines before the prediction.

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

The test module defines the complete fake backend that the tests above use:

```python
class FakeFastText:
    def predict(self, texts: list[str], *, k: int) -> tuple[list[list[str]], list[list[float]]]:
        assert k == 1
        return (
            [["__label__eng_Latn"] if "hello" in text else ["__label__fra_Latn"] for text in texts],
            [[0.9] if "hello" in text else [0.8] for text in texts],
        )
```

Add path tests. One test must assert that `assert_seagate_path(tmp_path, label="model cache")` raises an error. Another test must assert that the default cache is `DEFAULT_DATA_ROOT / "models" / "glotlid"`. The tests must not create test data outside `tmp_path`.

- [ ] **Step 2: Run the adapter tests. They must fail (RED).**

Run:

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_glotlid.py tests/runtime/test_paths.py -q
```

Expected result: the adapter symbols and the path helpers are missing.

- [ ] **Step 3: Add the dependency and write the adapter.**

Add `fasttext>=0.9.3,<1` to the runtime dependencies. Run `uv lock`. Add `build-essential` only in the Docker builder stage. The final runtime stage stays on the existing image that has only dependencies. Write these typed interfaces:

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

Pin `MODEL_REPOSITORY = "cis-lmu/glotlid"`, `MODEL_FILENAME = "model_v3.bin"`, and `MODEL_REVISION = "85cd671"`. Call `hf_hub_download(..., cache_dir=str(cache_dir))`. Hash the returned file in bounded chunks. Load the file one time with `fasttext.load_model`. Normalize the FastText labels: remove the `__label__` prefix. Change each newline and carriage-return character to a space before the prediction. Keep the order of the inputs. Require exactly one label/probability pair for each text. Reject probabilities that are not finite or that are out of range.

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

Expected result: the focused tests pass and ty reports no errors.

- [ ] **Step 5: Commit the adapter and the dependency boundary.**

```bash
git add src/osm_polygon_website_tag/pipeline/glotlid.py src/osm_polygon_website_tag/runtime/paths.py tests/pipeline/test_glotlid.py tests/runtime/test_paths.py pyproject.toml uv.lock Dockerfile
git commit -m "feat: add Seagate-bound GlotLID adapter"
```

### Task 3: Write the language checkpoints that are tied to the source and the model

**Files:**
- Create: `src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py`
- Create: `tests/pipeline/test_language_detection_checkpoint.py`

- [ ] **Step 1: Write RED checkpoint tests.**

Test these items. The metadata records the exact source row count and hash, and all the model identity fields. A changed source or a changed model fails closed. The parts are sequential and have the v1.4 shape. The final assembly keeps the order of the parts.

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

- [ ] **Step 2: Run the checkpoint tests. They must fail (RED).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_language_detection_checkpoint.py -q
```

Expected result: the new checkpoint module and its functions are missing.

- [ ] **Step 3: Write the checkpoint module.**

Use the directory `.language.parts` beside each source shard. Use the file `checkpoint.json`. Use part names such as `part-00000000.parquet`. Write these typed entry points:

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

Write each part through `BatchParquetSink` with `POLYGON_PUBLIC_SCHEMA_V1_4`. Check that the part has the exact schema and a positive row count that is the expected count. Then promote the part atomically. Accept only known temporary files. Reject gaps and unknown files. Stream the batches during assembly. On any `BaseException`, delete the staged file and leave the durable parts in place.

- [ ] **Step 4: Run the checkpoint tests and the existing checkpoint regression tests.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_language_detection_checkpoint.py tests/pipeline/test_enrichment_checkpoint.py -q
```

Expected result: the new checkpoint tests and all existing v1.3 checkpoint tests pass.

- [ ] **Step 5: Commit the checkpoint implementation.**

```bash
git add src/osm_polygon_website_tag/pipeline/language_detection_checkpoint.py tests/pipeline/test_language_detection_checkpoint.py
git commit -m "feat: add resumable language checkpoints"
```

### Task 4: Build the bounded, atomic detection pipeline for each shard

**Files:**
- Create: `src/osm_polygon_website_tag/pipeline/detect_languages.py`
- Create: `tests/pipeline/test_detect_languages.py`

- [ ] **Step 1: Write RED tests with an injected fake detector.**

Make v1.3 test shards that have text values that succeeded and text values that are absent. Then assert these items: independent prediction calls, exact labels and probabilities, nulls for absent values, the v1.4 schema, the order of the rows, the bounded batch behavior, and no model call on a v1.4 shard that is complete.

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

Also write tests for the failure cases. Each of these items must raise an error before the promotion and must leave the original shard valid: a changed source hash, a text status that is not finished or is unknown, text that is malformed but marked successful, a missing model prediction, or a probability that is not finite. The pipeline keeps resolved statuses that are not successful, such as `fetch_error`. For these statuses, the language fields stay null.

- [ ] **Step 2: Run the detection tests. They must fail (RED).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py -q
```

Expected result: the pipeline symbols are missing and assertions fail.

- [ ] **Step 3: Write the minimal pipeline.**

Write these typed entry points:

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

The pipeline must do these steps:

1. Accept only the exact v1.3 or v1.4 public schemas. Require both text-status columns. Before it processes a shard, require that each text status is a known resolved outcome. Reject `pending`, null, and unknown values. Keep the resolved outcomes that are not successful, and keep their language fields null.
2. Read `batch_rows` source rows at a time. Skip exactly the durable prefix of the checkpoint.
3. Set `schema_version` to `"v1.4"`. For `success`, send the website text values and the contact text values as separate detector calls, in order. For each resolved status that is not successful, keep both language fields null.
4. Keep language pairs that are already complete in v1.4. Detect only the pairs that are missing. Reject an incomplete pair. Do not keep one half of a pair silently.
5. After each input batch is complete, write one v1.4 checkpoint part. Assemble the parts in source order. Check the exact v1.4 schema and the row count. Then replace the shard atomically.
6. After a successful promotion only, remove the language checkpoint directory. On `KeyboardInterrupt` or on a normal exception, remove only the known staged files. Keep the original shard and the durable parts.

Use `detector.identity` in the checkpoint metadata. Use `hash_shard` for the source identity. Use `shutil.rmtree` only on the exact checkpoint directory that this shard owns, and only after success.

- [ ] **Step 4: Run the focused detection tests and check the resource bound.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_detect_languages.py tests/pipeline/test_language_detection_checkpoint.py -q
```

Expected result: all detection tests and checkpoint tests pass. There are no network calls. Nothing is written outside `tmp_path`.

- [ ] **Step 5: Commit the shard pipeline.**

```bash
git add src/osm_polygon_website_tag/pipeline/detect_languages.py tests/pipeline/test_detect_languages.py
git commit -m "feat: detect GlotLID languages by shard"
```

### Task 5: Keep v1.4 in the existing readers, retries, and verification

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

Add tests for these cases:

- URL enrichment on a v1.4 shard keeps all four language fields. It writes a v1.4 checkpoint part.
- DuckDB analysis accepts a directory that has one v1.3 public shard and one v1.4 public shard.
- Deduplication keeps the language fields. It writes v1.4 when one or more inputs are v1.4.
- A v1.4 card lists the language columns. A v1.3 card stays byte-compatible with its current schema section.
- The verifier accepts the v1.3 and v1.4 output schemas. It rejects a v1.4 text row that has status success and a missing language, and it rejects a probability that is out of range.

This is an example of the RED test for language verification:

```python
def test_verify_rejects_success_without_language_probability(tmp_path: Path) -> None:
    run_dir = make_v1_4_run(tmp_path, website_language="eng_Latn", website_probability=None)
    report = verify_results(run_dir)
    assert not report.ok
    assert any("language probability" in error for error in report.errors)
```

- [ ] **Step 2: Run the compatibility tests. They must fail (RED).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py -q
```

Expected result: the tests for the v1.4 preservation and verification fail. All existing tests are the regression baseline.

- [ ] **Step 3: Write the compatibility code. Do not change the v1.3 defaults.**

Give the existing URL checkpoint helpers the optional keyword arguments `schema` and `schema_version`. Their defaults stay `POLYGON_PUBLIC_SCHEMA` and `SCHEMA_VERSION`. Choose v1.4 as the target only when the input shard is v1.4. In this way, a later URL retry cannot drop the language columns.

Register the DuckDB public files with `read_parquet(..., union_by_name=true)`. Add the nullable language columns to the empty public view. In deduplication, choose v1.4 when one or more inputs are v1.4. Use the same union-by-name read. Cast and materialize each output shard to the chosen schema.

Make the card schema depend on the run. Return v1.4 when one or more public shards are v1.4. Otherwise keep the current v1.3 rows. Add `verify_language_invariants`. It skips v1.3 files. For v1.4 rows, it enforces this contract:

```python
if text_status == "success":
    require_nonempty_string(language)
    require_finite_probability(language_probability)
elif language is not None or language_probability is not None:
    errors.append(f"{label} non-success text must have null language fields")
```

In the final verification of public shards, use `is_current_public_polygon_schema`. Then v1.3 and v1.4 are accepted. The schemas v1.1 and v1.2 stay as inputs for migration. They are not final output schemas.

- [ ] **Step 4: Run the compatibility tests and the full regression tests (without the quality tests).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest --ignore=tests/architecture -q
```

Expected result: the compatibility tests and the complete existing suite pass.

- [ ] **Step 5: Commit the compatibility support.**

```bash
git add src/osm_polygon_website_tag/pipeline/enrichment_checkpoint.py src/osm_polygon_website_tag/pipeline/enrich.py src/osm_polygon_website_tag/storage/duckdb_engine.py src/osm_polygon_website_tag/pipeline/deduplicate.py src/osm_polygon_website_tag/reporting/card.py src/osm_polygon_website_tag/reporting/verification/shards.py src/osm_polygon_website_tag/reporting/verification/language.py src/osm_polygon_website_tag/reporting/verify.py tests/pipeline/test_enrich.py tests/pipeline/test_deduplicate.py tests/pipeline/test_analyze.py tests/reporting/test_card.py tests/reporting/test_verify.py tests/reporting/test_language_verification.py
git commit -m "feat: preserve language columns across readers"
```

### Task 6: Add the optional detection to the resumable workflow

**Files:**
- Modify: `src/osm_polygon_website_tag/application/workflow.py`
- Test: `tests/application/test_workflow.py`

- [ ] **Step 1: Write RED workflow tests.**

Use a fake detector. The tests must prove these items:

- `run_all` with its default arguments never builds a model loader and never calls one. It still writes v1.3 shards.
- `run_all(..., detect_languages=True, language_detector=fake)` detects the language after the URL text enrichment. It updates the public-shard hashes. When apply mode is mocked, it publishes the changed shard and the changed card path.
- A rerun after an interrupted shard calls the detector only for the suffix that has no checkpoint.
- A run that exists in v1.3 can resume with `detect_languages=True`. A run that exists in v1.4 is not downgraded.
- A changed source or model identity raises an error before the code promotes mixed output.

The assertion for the default path must be explicit:

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

- [ ] **Step 2: Run the workflow tests. They must fail (RED).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_workflow.py -q
```

Expected result: `run_all` has no language option and the new tests fail.

- [ ] **Step 3: Write the optional workflow resource and stage.**

Add `detect_languages: bool = False` to `run_all`. Add an optional typed `language_detector` argument. Only the hermetic tests use it. When the operator requests detection and no detector is injected, check that the run directory and `glotlid_model_cache_dir()` are under the Seagate root. Then load one detector before the source processing. The default branch must not import or load model resources, except for the normal module imports.

Add the flag and the detector to `_SourceRunContext`. After `_enrich_source_shard_if_needed`, call a new function `_detect_source_shard_if_needed`. Call it only when the flag is true:

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

Pass `language_changed` through `_publish_source_if_needed`, `_source_upload_is_current_for_context`, and `_source_requires_publication`. A change in language only must make the upload acknowledgements invalid. Change the logic at the start of the enrichment phase. It must reopen runs that are `ANALYZED`, `CARD_BUILT`, or `COMPLETE` and not frozen, when the optional flag finds shards that are v1.3 or that have incomplete language data. Treat v1.4 as current in the schema checks for URL enrichment.

- [ ] **Step 4: Run the workflow tests. They must pass (GREEN).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_workflow.py -q
```

Expected result: all workflow tests pass. This includes the default regression test that loads no model and the resume tests for the optional stage.

- [ ] **Step 5: Commit the workflow integration.**

```bash
git add src/osm_polygon_website_tag/application/workflow.py tests/application/test_workflow.py
git commit -m "feat: integrate opt-in language detection"
```

### Task 7: Add the standalone CLI command and the operator documentation

**Files:**
- Modify: `src/osm_polygon_website_tag/application/cli.py`
- Modify: `tests/application/test_cli.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/architecture.md`
- Modify: `src/osm_polygon_website_tag/contracts/README.md`
- Modify: `src/osm_polygon_website_tag/pipeline/README.md`

- [ ] **Step 1: Write RED CLI tests.**

Test these items: the registration of the command, the injection of a fake detector at the workflow boundary, the rejection of a path that is not on Seagate before the model loads, and the behavior of the resume state. The command contract is:

```text
osm-polygon-website-tag detect-languages --run-dir <Seagate run directory>
osm-polygon-website-tag run-all --detect-languages ...
```

The standalone command must do these steps. Load the model one time. Process the public shards in sorted order. Update the hash of each source manifest. Move a run that is not frozen and is analyzed, card-built, or complete to `enriching` before the work, and to `enriched` after all shards finish. On an interruption, leave the run in `enriching` and keep the durable parts. Reject a frozen run with `snapshot_status=done` and do not touch the model cache.

- [ ] **Step 2: Run the CLI tests. They must fail (RED).**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_cli.py -q
```

Expected result: the command and the `--detect-languages` option are not registered.

- [ ] **Step 3: Write the CLI surface.**

Import `STATUS_COMPLETE` and `glotlid_model_cache_dir`. Add `--detect-languages` to `run-all`. Add this command:

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

The real implementation must do these checks before each shard: that the source belongs to the manifest, and that the text statuses are known and resolved. It must reject a frozen snapshot. It must keep the model cache under the Seagate root. `run-all --detect-languages` must pass the same stage and the same model resource. It must not use a second implementation.

- [ ] **Step 4: Run the CLI tests and the documentation build checks.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest tests/application/test_cli.py -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked mkdocs build --strict
```

Expected result: the CLI tests pass and the documentation build has no errors.

- [ ] **Step 5: Document the operator contract.**

Document that the production files are at:

```text
/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/
/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>/
```

Document these items: the pinned [GlotLID model card](https://huggingface.co/cis-lmu/glotlid); the four nullable v1.4 fields; the exact raw labels, for example `eng_Latn`; the top-1 probabilities; the behavior of `Ctrl-C` and of a rerun; the precondition that the text is known and resolved; and the need to run the existing commands for analysis, card, and finalization after a standalone language stage changes a run that is already analyzed. State clearly that the default `run-all` path stays v1.3 and does not load the model.

- [ ] **Step 6: Commit the CLI and the documentation.**

```bash
git add src/osm_polygon_website_tag/application/cli.py tests/application/test_cli.py README.md docs/operations.md docs/architecture.md src/osm_polygon_website_tag/contracts/README.md src/osm_polygon_website_tag/pipeline/README.md
git commit -m "docs: operate the GlotLID language stage"
```

### Task 8: Run the complete quality gates and the mutation and CRAP checks

**Files:**
- Modify only the files that the formatter, the lockfile, or the quality-report output require. Do not stage unrelated work.

- [ ] **Step 1: Run the complete repository checks.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just check
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-commit
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just pre-push
```

Expected result: lock validation, Ruff, format, `ty check src tests scripts`, all tests, and `uv build` pass.

- [ ] **Step 2: Run the CRAP gate and examine the threshold.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just crap
```

Expected result: the command succeeds. Each changed production function has a CRAP score below 6.

- [ ] **Step 3: Run mutation testing and examine each result.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache just mutation
```

Expected result: no mutant has the verdict `survived`, `no tests`, `timeout`, `suspicious`, `segfault`, or interrupted.

- [ ] **Step 4: Run the final feature regression suite.**

```bash
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv run --locked pytest -q
UV_CACHE_DIR=/private/tmp/osm-polygon-website-tag-uv-cache uv build
git status --short --branch
git diff --check HEAD
```

Expected result: all tests pass, the package builds, the whitespace checks pass, and the isolated worktree has only the intended feature commits and files.

- [ ] **Step 5: Record the final evidence.**

Record these items in the handoff: the exact test count, the CRAP result, the mutation result, and each limit of the environment. Do not say that a real model download or a production inference ran, unless it ran with the Seagate cache and run roots. Do not stage generated reports or unrelated worktree files.
