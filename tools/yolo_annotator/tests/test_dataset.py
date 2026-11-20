"""Unit tests for the dataset layer (YOLO IO, review state, filters, stats)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dataset as ds


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def write_minimal_batch(root: Path, images: list[dict], auto_meta: dict) -> Path:
    """Create a batch with a caller supplied dataset.json and auto_meta.json."""
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("images", "labels", "auto_labels"):
        (root / sub).mkdir(exist_ok=True)
    taxonomy = ds.Taxonomy.from_file(Path(__file__).resolve().parents[1] / "taxonomy.json")
    payload = {
        "schema": 1,
        "batch": root.name,
        "taxonomy": taxonomy.name,
        "classes": [c.to_dict() for c in taxonomy.classes],
        "images": images,
        "counts": {},
        "split": {"train": len(images), "val": 0},
    }
    ds.atomic_write_json(root / "dataset.json", payload)
    ds.atomic_write_json(root / "auto_meta.json", auto_meta)
    ds.atomic_write_json(root / "taxonomy.json", taxonomy.to_dict())
    return root


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
def test_taxonomy_is_loaded_from_the_dataset(dataset):
    taxonomy = dataset.taxonomy
    assert len(taxonomy) == 17
    assert taxonomy.get(0).name == "person"
    assert taxonomy.get(3).name == "car"
    assert taxonomy.color_hex(0) == "#3838ff"
    assert taxonomy.display(0) == "行人"
    assert set(taxonomy.two_wheeler_ids) == {1, 2}


def test_taxonomy_falls_back_to_dataset_json_classes(tmp_path):
    root = tmp_path / "batch"
    (root / "images").mkdir(parents=True)
    for sub in ("labels", "auto_labels"):
        (root / sub).mkdir()
    payload = {
        "schema": 1,
        "batch": "b",
        "classes": [{"id": 0, "name": "person", "zh": "行人", "color": [1, 2, 3]}],
        "images": [{"id": "000001", "file": "images/000001.jpg", "width": 10,
                    "height": 10}],
    }
    ds.atomic_write_json(root / "dataset.json", payload)
    opened = ds.Dataset.open(root)
    assert len(opened.taxonomy) == 1
    assert any("taxonomy.json" in w for w in opened.warnings)


# ---------------------------------------------------------------------------
# YOLO label parsing / writing
# ---------------------------------------------------------------------------
def test_parse_label_lines_keeps_first_five_columns():
    text = "0 0.5 0.5 0.2 0.2\n3 0.5 0.5 0.3 0.4 0.99\n"
    labels, warnings = ds.parse_label_lines(text)
    assert len(labels) == 2
    assert warnings == []
    assert labels[1].cls == 3
    assert labels[1].w == pytest.approx(0.3)


def test_parse_label_lines_skips_malformed_and_warns():
    text = "\n".join([
        "# comment",
        "0 0.5 0.5 0.2",          # too few columns
        "abc 0.5 0.5 0.2 0.2",    # bad class
        "0 0.5 0.5 0 0.2",        # zero width
        "0 0.5 0.5 0.2 0.2",      # good
        "",
    ])
    labels, warnings = ds.parse_label_lines(text)
    assert len(labels) == 1
    assert len(warnings) == 3


def test_parse_label_lines_rejects_unknown_classes():
    taxonomy = ds.Taxonomy.from_dict({"classes": [{"id": 0, "name": "person"}]})
    labels, warnings = ds.parse_label_lines("7 0.5 0.5 0.1 0.1", taxonomy)
    assert labels == []
    assert "不在类别表" in warnings[0]


def test_parse_label_lines_clamps_out_of_range_coordinates():
    labels, _ = ds.parse_label_lines("0 0.95 0.5 0.2 0.2")
    box = labels[0].xyxy
    assert box[0] >= 0.0 and box[2] <= 1.0
    assert labels[0].cx == pytest.approx((box[0] + box[2]) / 2)


def test_empty_file_is_a_negative_sample(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    labels, warnings = ds.read_labels(path)
    assert labels == [] and warnings == []


def test_missing_label_file_reads_as_empty(tmp_path):
    labels, warnings = ds.read_labels(tmp_path / "nope.txt")
    assert labels == [] and warnings == []


def test_write_and_read_roundtrip(tmp_path):
    labels = [
        ds.Label.from_xyxy(2, 0.1, 0.2, 0.4, 0.6),
        ds.Label.from_xyxy(8, 0.5, 0.5, 0.9, 0.95),
    ]
    path = tmp_path / "a.txt"
    ds.write_labels(path, labels)
    loaded, _ = ds.read_labels(path)
    assert ds.labels_equal(labels, loaded)
    # 6 decimals, newline terminated.
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.strip().splitlines()) == 2
    assert all(len(line.split()) == 5 for line in text.strip().splitlines())


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "labels" / "000001.txt"
    ds.write_labels(path, [ds.Label.from_xyxy(0, 0.1, 0.1, 0.2, 0.2)])
    leftovers = [p.name for p in path.parent.iterdir() if p.name.startswith(".tmp_")]
    assert leftovers == []


def test_labels_equal_is_order_insensitive():
    a = [ds.Label.from_xyxy(0, 0.1, 0.1, 0.2, 0.2),
         ds.Label.from_xyxy(1, 0.3, 0.3, 0.5, 0.5)]
    b = list(reversed(a))
    assert ds.labels_equal(a, b)
    b[0] = ds.Label.from_xyxy(1, 0.3, 0.3, 0.5, 0.51)
    assert not ds.labels_equal(a, b)


def test_label_abs_conversions():
    label = ds.Label.from_xyxy(0, 0.25, 0.5, 0.75, 1.0)
    box = label.as_abs(1000, 500)
    assert box == pytest.approx((250, 250, 750, 500))
    back = ds.Label.from_abs_xyxy(0, *box, 1000, 500)
    assert (back.cx, back.cy, back.w, back.h) == pytest.approx(
        (label.cx, label.cy, label.w, label.h))


def test_match_auto_and_annotate_auto():
    auto = [ds.Label.from_xyxy(8, 0.1, 0.1, 0.3, 0.3, auto=True, conf=0.9)]
    same = [ds.Label.from_xyxy(8, 0.1, 0.1, 0.3, 0.3)]
    assert ds.match_auto(same, auto) == [True]
    shifted = [ds.Label.from_xyxy(8, 0.5, 0.5, 0.7, 0.7)]
    assert ds.match_auto(shifted, auto) == [False]
    annotated = ds.annotate_auto(same, auto)
    assert annotated[0].auto and annotated[0].conf == pytest.approx(0.9)
    annotated = ds.annotate_auto(shifted, auto)
    assert not annotated[0].auto and annotated[0].conf is None


# ---------------------------------------------------------------------------
# Dataset IO / review state
# ---------------------------------------------------------------------------
def test_open_demo_batch(dataset, demo_batch):
    assert dataset.batch == "batch_demo"
    assert len(dataset.images) == 4
    assert dataset.root == demo_batch.resolve()
    ids = [img.id for img in dataset.images]
    assert ids == ["000001", "000002", "000003", "000004"]
    assert dataset.images[0].width == 1344
    assert dataset.images[0].height == 760
    progress = dataset.progress()
    assert progress["total"] == 4 and progress["reviewed"] == 0


def test_open_missing_dataset_json_raises(tmp_path):
    (tmp_path / "batch").mkdir()
    with pytest.raises(ds.DatasetError):
        ds.Dataset.open(tmp_path / "batch")
    with pytest.raises(ds.DatasetError):
        ds.Dataset.open(tmp_path / "does_not_exist")


def test_open_creates_review_state_file(dataset, demo_batch):
    path = demo_batch / "review_state.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"000001", "000002", "000003", "000004"}
    assert all(not v["reviewed"] and not v["edited"] for v in payload.values())


def test_load_labels_matches_auto_labels_initially(dataset):
    for entry in dataset.images:
        labels = dataset.load_labels(entry.id)
        auto = dataset.load_auto_labels(entry.id)
        assert ds.labels_equal(labels, auto)
        assert dataset.edited_vs_auto(entry.id, labels) is False


def test_save_labels_updates_review_state(dataset, demo_batch):
    image_id = "000001"
    labels = dataset.load_labels(image_id)
    assert labels, "demo image should start with boxes"
    labels = labels[:-1]  # remove one box -> edited
    result = dataset.save_labels(image_id, labels)
    assert result["edited"] is True
    assert result["count"] == len(labels)

    on_disk, _ = ds.read_labels(dataset.label_path(image_id))
    assert ds.labels_equal(on_disk, labels)

    state = json.loads((demo_batch / "review_state.json").read_text(encoding="utf-8"))
    assert state[image_id]["edited"] is True
    assert state[image_id]["updated"] > 0

    # Reopening sees the persisted state.
    reopened = ds.Dataset.open(demo_batch)
    assert reopened.review_state.is_edited(image_id)
    assert ds.labels_equal(reopened.load_labels(image_id), labels)


def test_save_labels_same_as_auto_is_not_edited(dataset):
    image_id = "000002"
    labels = dataset.load_labels(image_id)
    result = dataset.save_labels(image_id, labels, reviewed=True)
    assert result["edited"] is False
    assert result["reviewed"] is True


def test_mark_reviewed_keeps_edited_flag(dataset, demo_batch):
    image_id = "000003"
    labels = dataset.load_labels(image_id)
    dataset.save_labels(image_id, labels + [ds.Label.from_xyxy(8, 0.1, 0.1, 0.2, 0.2)])
    dataset.mark_reviewed(image_id, True)
    reopened = ds.Dataset.open(demo_batch)
    assert reopened.review_state.is_reviewed(image_id)
    assert reopened.review_state.is_edited(image_id)


def test_negative_sample_roundtrip(dataset):
    image_id = "000002"
    dataset.save_labels(image_id, [], reviewed=True)
    path = dataset.label_path(image_id)
    assert path.is_file() and path.read_text(encoding="utf-8") == ""
    assert dataset.review_state.is_reviewed(image_id)


def test_auto_labels_fall_back_to_auto_meta(tmp_path):
    root = write_minimal_batch(
        tmp_path / "batch",
        [{"id": "000001", "file": "images/000001.jpg", "width": 100, "height": 50}],
        {"000001": [{"cls": 8, "conf": 0.77,
                     "xyxy": [0.1, 0.1, 0.5, 0.5]}]},
    )
    opened = ds.Dataset.open(root)
    labels = opened.load_auto_labels("000001")
    assert len(labels) == 1
    assert labels[0].cls == 8 and labels[0].conf == pytest.approx(0.77)
    assert opened.has_auto("000001")


def test_dataset_scan_fallback_when_images_not_listed(tmp_path):
    root = write_minimal_batch(tmp_path / "batch", [], {})
    (root / "images" / "000009.jpg").write_bytes(b"not a real jpeg")
    opened = ds.Dataset.open(root)
    assert [img.id for img in opened.images] == ["000009"]
    assert any("扫描" in w for w in opened.warnings)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def make_filter_batch(tmp_path: Path) -> Path:
    images = [
        {"id": "000001", "file": "images/000001.jpg", "width": 100, "height": 100,
         "route": "routeA", "hour": 14, "priority": 5, "split": "train"},
        {"id": "000002", "file": "images/000002.jpg", "width": 100, "height": 100,
         "route": "routeB", "hour": 23, "priority": 1, "split": "val"},
        {"id": "000003", "file": "images/000003.jpg", "width": 100, "height": 100,
         "route": "routeC", "hour": 9, "priority": 2, "split": "train"},
    ]
    auto_meta = {
        "000001": [{"cls": 1, "conf": 0.9, "xyxy": [0.1, 0.1, 0.2, 0.2]}],  # bicycle
        "000002": [{"cls": 8, "conf": 0.8, "xyxy": [0.1, 0.1, 0.2, 0.2]}],  # car
        "000003": [],
    }
    return write_minimal_batch(tmp_path / "batch", images, auto_meta)


def test_filters(tmp_path):
    dataset = ds.Dataset.open(make_filter_batch(tmp_path))
    dataset.mark_reviewed("000001", True)
    dataset.save_labels("000002", [ds.Label.from_xyxy(8, 0.4, 0.4, 0.6, 0.6)])

    ids = lambda entries: [e.id for e in entries]  # noqa: E731
    assert ids(dataset.filter_images(ds.FILTER_ALL)) == ["000001", "000002", "000003"]
    assert ids(dataset.filter_images(ds.FILTER_REVIEWED)) == ["000001"]
    assert ids(dataset.filter_images(ds.FILTER_UNREVIEWED)) == ["000002", "000003"]
    assert ids(dataset.filter_images(ds.FILTER_AUTO_TWOWHEEL)) == ["000001"]
    assert ids(dataset.filter_images(ds.FILTER_HAS_AUTO)) == ["000001", "000002"]
    assert ids(dataset.filter_images(ds.FILTER_HIGH_PRIORITY)) == ["000001"]
    assert ids(dataset.filter_images(ds.FILTER_EDITED)) == ["000002"]

    # Search by id / route / priority / hour.
    assert ids(dataset.filter_images(ds.FILTER_ALL, "000003")) == ["000003"]
    assert ids(dataset.filter_images(ds.FILTER_ALL, "routeB")) == ["000002"]
    assert ids(dataset.filter_images(ds.FILTER_UNREVIEWED, "23")) == ["000002"]


def test_filter_choices_cover_expected_kinds():
    kinds = {key for _label, key in ds.FILTER_CHOICES}
    assert kinds == {ds.FILTER_ALL, ds.FILTER_UNREVIEWED, ds.FILTER_REVIEWED,
                     ds.FILTER_AUTO_TWOWHEEL, ds.FILTER_HIGH_PRIORITY,
                     ds.FILTER_HAS_AUTO, ds.FILTER_EDITED}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def test_class_counts_and_stats(dataset):
    counts = dataset.class_counts()
    total = sum(counts.values())
    expected = sum(len(dataset.load_labels(img.id)) for img in dataset.images)
    assert total == expected
    stats = dataset.stats()
    assert stats["total"] == 4
    assert stats["taxonomy_size"] == 17
    assert stats["split"]["train"] + stats["split"]["val"] == 4
    manifest = dataset.manifest_counts()
    for key in ("total", "reviewed", "unreviewed", "edited", "reviewed_pct",
                "split", "class_counts"):
        assert key in manifest


def test_class_counts_cache_invalidated_on_save(dataset):
    first = dataset.class_counts()
    image_id = dataset.images[0].id
    dataset.save_labels(image_id, [])
    second = dataset.class_counts()
    assert sum(second.values()) < sum(first.values())


# ---------------------------------------------------------------------------
# Dataset dir resolution
# ---------------------------------------------------------------------------
def test_resolve_dataset_dir_order(tmp_path, monkeypatch):
    good = tmp_path / "good"
    ds.atomic_write_json(good / "dataset.json", {"schema": 1, "images": []})
    other = tmp_path / "other"
    ds.atomic_write_json(other / "dataset.json", {"schema": 1, "images": []})
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    monkeypatch.delenv("YOLO_DATASET", raising=False)
    assert ds.resolve_dataset_dir(str(good), app_dir) == good.resolve()
    assert ds.resolve_dataset_dir(None, app_dir, str(other)) == other.resolve()

    monkeypatch.setenv("YOLO_DATASET", str(other))
    assert ds.resolve_dataset_dir(None, app_dir) == other.resolve()

    (app_dir / "dataset_dir.txt").write_text(str(good), encoding="utf-8")
    monkeypatch.delenv("YOLO_DATASET", raising=False)
    assert ds.resolve_dataset_dir(None, app_dir) == good.resolve()

    # An explicit path wins and is returned as-is (the caller reports the error).
    missing = tmp_path / "nope"
    assert ds.resolve_dataset_dir(str(missing), app_dir) == missing
    # Nothing to find in an empty app dir without a pointer.
    empty_app = tmp_path / "empty_app"
    empty_app.mkdir()
    assert ds.resolve_dataset_dir(None, empty_app) is None


# ---------------------------------------------------------------------------
# Atomic helpers
# ---------------------------------------------------------------------------
def test_atomic_write_json_roundtrip(tmp_path):
    path = tmp_path / "x" / "y.json"
    ds.atomic_write_json(path, {"a": [1, 2, 3]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": [1, 2, 3]}
    # Overwrites without leaving temp files behind.
    ds.atomic_write_json(path, {"a": []})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": []}
    assert [p.name for p in path.parent.iterdir()] == ["y.json"]


def test_read_json_default_on_invalid(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert ds.read_json(path, default={"ok": True}) == {"ok": True}
    assert ds.read_json(tmp_path / "missing.json") is None
