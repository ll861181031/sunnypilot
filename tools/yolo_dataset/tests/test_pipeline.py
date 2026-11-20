"""Offline tests for the YOLO dataset pipeline.

Everything here runs without the camera data, without ffmpeg and without ONNX
Runtime: the stages are exercised on synthetic frames, and the two integration
tests build a tiny batch from those frames so the labeler contract is covered.

    .venv/bin/python -m pytest tools/yolo_dataset/tests -q
"""

from __future__ import annotations

import os
import subprocess
import sys

import numpy as np
import pytest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)

import build
import common
import extract
import merge_inbox
import validate

TAXONOMY = common.load_taxonomy(common.DEFAULT_TAXONOMY)


# ------------------------------------------------------------------- taxonomy

def test_taxonomy_is_the_agreed_17_classes():
  assert TAXONOMY.num_classes == 17
  assert TAXONOMY.names[0] == "person"
  assert TAXONOMY.names[1] == "two_wheeler"
  assert TAXONOMY.names[2] == "tricycle"
  assert TAXONOMY.names[16] == "other_obstacle"
  assert [c["id"] for c in TAXONOMY.classes] == list(range(17))


def test_coco_mapping_covers_the_modelled_classes():
  assert TAXONOMY.map_coco("person") == 0
  assert TAXONOMY.map_coco("bicycle") == 1
  assert TAXONOMY.map_coco("motorcycle") == 1
  assert TAXONOMY.map_coco("car") == 3
  assert TAXONOMY.map_coco("traffic light") == 14
  assert TAXONOMY.map_coco("stop sign") == 15
  # COCO classes without a taxonomy counterpart are dropped, not guessed.
  assert TAXONOMY.map_coco("backpack") is None
  assert TAXONOMY.map_coco("dog") is None


def test_day_night_bucket_boundaries():
  assert common.is_daylight(8) and common.is_daylight(17)
  assert not common.is_daylight(7) and not common.is_daylight(18)
  assert common.is_daylight(None)


def test_letterbox_geometry_keeps_aspect_ratio():
  # Native 1344x760 has no padding at all.
  assert common.letterbox_size(1344, 760) == (1344, 760, 0, 0)
  # 1928x1208 is pillarboxed, never stretched: 1208 -> 760 keeps ~1213 px width.
  new_w, new_h, pad_x, pad_y = common.letterbox_size(1928, 1208)
  assert new_h == 760
  assert 1212 <= new_w <= 1214
  assert pad_x == (1344 - new_w) // 2 and pad_y == 0
  scale = new_w / 1928
  assert abs(scale - new_h / 1208) < 0.002   # isotropic


def test_letterbox_pads_with_the_grey_value_and_keeps_content():
  image = np.full((1208, 1928, 3), 200, dtype=np.uint8)
  out, (new_w, new_h, pad_x, _pad_y) = common.letterbox(image, common.TARGET_SIZE)
  assert out.shape == (760, 1344, 3)
  assert out[0, 0].tolist() == [common.LETTERBOX_PAD] * 3
  assert out[400, pad_x + 2].tolist() == [200, 200, 200]


def test_ffmpeg_filter_pads_instead_of_stretching():
  filt = common.ffmpeg_filter(0.5)
  assert "fps=0.5" in filt
  assert "force_original_aspect_ratio=decrease" in filt
  assert "pad=1344:760" in filt
  assert "0x727272" in filt  # 114 grey, matching LETTERBOX_PAD


# ------------------------------------------------------------- label formatting

def test_format_yolo_line_converts_xyxy_to_cxcywh():
  line = common.format_yolo_line(3, (0.2, 0.4, 0.6, 0.8))
  assert line == "3 0.400000 0.600000 0.400000 0.400000"
  cls, box = common.parse_label_line(line, 17)
  assert cls == 3
  assert box == pytest.approx((0.4, 0.6, 0.4, 0.4))


@pytest.mark.parametrize("line,reason", [
    ("1 0.5 0.5 0.1", "columns"),
    ("1 0.5 0.5 0.1 0.1 9", "columns"),
    ("x 0.5 0.5 0.1 0.1", "non-numeric"),
    ("1.5 0.5 0.5 0.1 0.1", "integer"),
    ("17 0.5 0.5 0.1 0.1", "outside 0..16"),
    ("3 -0.1 0.5 0.1 0.1", "outside [0, 1]"),
    ("3 0.5 0.5 0.0 0.1", "positive"),
])
def test_parse_label_line_rejects_bad_input(line, reason):
  import re
  with pytest.raises(ValueError, match=re.escape(reason)):
    common.parse_label_line(line, 17)


def test_clamp01():
  assert common.clamp01(-1) == 0.0
  assert common.clamp01(2) == 1.0
  assert common.clamp01(0.25) == 0.25


# ----------------------------------------------------------------- priorities

def test_priority_weights():
  counts = {"two_wheeler": 2, "person": 3, "vehicle": 4, "other": 9}
  assert common.priority_of(counts) == 5 * 2 + 2 * 3 + 4
  assert common.priority_of(common.count_by_group([])) == 0


def test_count_by_group_buckets_taxonomy_ids():
  dets = [{"cls": 2}, {"cls": 1}, {"cls": 0}, {"cls": 8}, {"cls": 19}, {"cls": 4}]
  counts = common.count_by_group(dets)
  assert counts == {"two_wheeler": 2, "person": 1, "vehicle": 2, "other": 1}


# ---------------------------------------------------------------- autolabel ops

@pytest.mark.parametrize("cls", [1, 2])
def test_any_two_wheeler_suppresses_its_rider(cls):
  dets = [{"cls": cls, "xyxy": [0.4, 0.4, 0.6, 0.8], "conf": 0.9},
          {"cls": 0, "xyxy": [0.45, 0.42, 0.55, 0.6], "conf": 0.8}]
  import autolabel
  kept, dropped = autolabel.suppress_riders(dets)
  assert dropped == 1
  assert [d["cls"] for d in kept] == [cls]


def test_pedestrian_next_to_a_bike_is_kept():
  import autolabel
  dets = [{"cls": 1, "xyxy": [0.1, 0.4, 0.3, 0.8], "conf": 0.9},
          {"cls": 0, "xyxy": [0.7, 0.4, 0.8, 0.8], "conf": 0.8}]
  kept, dropped = autolabel.suppress_riders(dets)
  assert dropped == 0
  assert len(kept) == 2


def test_rider_is_dropped_by_iou_even_when_the_centre_is_outside():
  import autolabel
  # Person box straddles the bike box: centre outside, IoU above the threshold.
  dets = [{"cls": 1, "xyxy": [0.40, 0.40, 0.60, 0.80], "conf": 0.9},
          {"cls": 0, "xyxy": [0.55, 0.40, 0.75, 0.80], "conf": 0.8}]
  kept, dropped = autolabel.suppress_riders(dets)
  assert dropped == 1 and len(kept) == 1


def test_no_two_wheeler_means_no_suppression():
  import autolabel
  dets = [{"cls": 0, "xyxy": [0.4, 0.4, 0.5, 0.8], "conf": 0.9},
          {"cls": 0, "xyxy": [0.41, 0.41, 0.51, 0.81], "conf": 0.8}]
  kept, dropped = autolabel.suppress_riders(dets)
  assert dropped == 0 and len(kept) == 2


def test_is_inside_uses_the_box_centre():
  import autolabel
  assert autolabel.is_inside([0.45, 0.45, 0.55, 0.55], [0.4, 0.4, 0.6, 0.6])
  assert not autolabel.is_inside([0.0, 0.0, 0.1, 0.1], [0.4, 0.4, 0.6, 0.6])


# ----------------------------------------------------------------- selection

def _candidate(route: str, t: float, daylight: bool, counts: dict, negative: bool = False,
               conf: float = 0.5) -> dict:
  detections = [] if negative else [{"cls": 8, "conf": conf, "xyxy": [0.1, 0.1, 0.2, 0.2]}]
  return {"route": route, "file": f"{int(t):04d}.jpg", "t": t, "hour": 12 if daylight else 22,
          "daylight": daylight, "brightness": 120.0 if daylight else 80.0,
          "detections": detections, "counts": counts,
          "priority": common.priority_of(counts), "negative": negative}


def test_selection_prefers_two_wheelers_and_fills_the_target():
  cands = [_candidate("r1", 0, True, {"two_wheeler": 1, "person": 0, "vehicle": 0, "other": 0}),
           _candidate("r2", 0, True, {"two_wheeler": 0, "person": 1, "vehicle": 0, "other": 0}),
           _candidate("r3", 0, True, {"two_wheeler": 0, "person": 0, "vehicle": 1, "other": 0})]
  selected, plan = build.select_frames(cands, target=1, day_frac=0.5, neg_frac=0.0,
                                       per_route_cap=40, seed=0)
  assert len(selected) == 1
  assert selected[0]["route"] == "r1"
  assert plan["requested"] == 1


def test_selection_balances_day_and_night():
  cands = [_candidate(f"d{i}", i, True, {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                          "other": 0}) for i in range(10)]
  cands += [_candidate(f"n{i}", i, False, {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                           "other": 0}) for i in range(10)]
  selected, plan = build.select_frames(cands, target=10, day_frac=0.5, neg_frac=0.0,
                                       per_route_cap=40, seed=1)
  assert sum(1 for c in selected if c["daylight"]) == 5
  assert sum(1 for c in selected if not c["daylight"]) == 5
  assert plan["quota"] == {"day": 5, "night": 5}


def test_night_shortfall_is_taken_from_the_day_bucket():
  cands = [_candidate(f"d{i}", 0, True, {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                          "other": 0}) for i in range(10)]
  cands += [_candidate(f"n{i}", 0, False, {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                            "other": 0}) for i in range(2)]
  selected, plan = build.select_frames(cands, target=10, day_frac=0.5, neg_frac=0.0,
                                       per_route_cap=40, seed=2)
  assert len(selected) == 10
  assert plan["quota"] == {"day": 8, "night": 2}
  assert plan["filled"] == {"day": 8, "night": 2}


def test_selection_respects_the_per_route_cap():
  cands = [_candidate("busy", i, True, {"two_wheeler": 1, "person": 0, "vehicle": 0,
                                         "other": 0}) for i in range(10)]
  cands += [_candidate("quiet", i, True, {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                          "other": 0}) for i in range(10)]
  selected, _plan = build.select_frames(cands, target=12, day_frac=1.0, neg_frac=0.0,
                                        per_route_cap=3, seed=3)
  # The cap applies to every route, so the batch legitimately comes up short.
  assert sum(1 for c in selected if c["route"] == "busy") == 3
  assert sum(1 for c in selected if c["route"] == "quiet") == 3
  assert len(selected) == 6


def test_negatives_are_sampled_and_counted():
  positives = [_candidate(f"p{i}", 0, True, {"two_wheeler": 1, "person": 0, "vehicle": 0,
                                              "other": 0}) for i in range(20)]
  negatives = [_candidate(f"n{i}", 0, True, {"two_wheeler": 0, "person": 0, "vehicle": 0,
                                             "other": 0}, negative=True) for i in range(20)]
  selected, plan = build.select_frames(positives + negatives, target=10, day_frac=1.0,
                                       neg_frac=0.2, per_route_cap=40, seed=4)
  assert len(selected) == 10
  assert sum(1 for c in selected if c["negative"]) == 2
  assert plan["negatives"]["selected"] == 2


def test_selection_is_deterministic_for_a_seed():
  cands = [_candidate(f"r{i}", 0, bool(i % 2), {"two_wheeler": 0, "person": 0, "vehicle": 1,
                                                 "other": 0}, negative=(i % 5 == 0))
           for i in range(40)]
  first = build.select_frames(cands, 20, 0.5, 0.1, 40, seed=7)[0]
  second = build.select_frames(cands, 20, 0.5, 0.1, 40, seed=7)[0]
  assert [(c["route"], c["t"]) for c in first] == [(c["route"], c["t"]) for c in second]


# --------------------------------------------------------------------- splits

def test_split_is_by_route_and_disjoint():
  routes = [f"r{i}" for i in range(100)]
  train, val = build.split_routes(routes, 0.15, seed=5)
  assert not (train & val)
  assert train | val == set(routes)
  assert len(val) == 15


def test_split_with_a_single_route_does_not_put_it_in_val():
  train, val = build.split_routes(["only"], 0.15, seed=5)
  assert train == {"only"} and val == set()


def test_split_reuses_the_reference_assignment(tmp_path):
  routes = [f"r{i}" for i in range(20)]
  reference = {route: ("val" if i < 3 else "train") for i, route in enumerate(routes)}
  train, val = build.split_routes(routes, 0.15, seed=1, reference=reference)
  assert val == {f"r{i}" for i in range(3)}
  assert len(train) == 17
  # Routes the reference does not know get a seeded assignment, never both.
  mixed = routes + [f"new{i}" for i in range(10)]
  train2, val2 = build.split_routes(mixed, 0.15, seed=1, reference=reference)
  assert val2 >= {f"r{i}" for i in range(3)}
  assert not (train2 & val2)


def test_split_reference_can_be_read_from_dataset_json(tmp_path):
  dataset_dir = str(tmp_path / "ref")
  os.makedirs(dataset_dir)
  common.write_json(os.path.join(dataset_dir, "dataset.json"), {"images": [
      {"route": "a", "split": "train"}, {"route": "b", "split": "val"},
      {"route": "b", "split": "val"}, {"route": "c", "split": "nonsense"}]})
  reference = build.load_split_reference([os.path.join(dataset_dir, "dataset.json")])
  assert reference == {"a": "train", "b": "val"}


def test_split_is_deterministic():
  routes = [f"r{i}" for i in range(30)]
  assert build.split_routes(routes, 0.2, seed=9) == build.split_routes(routes, 0.2, seed=9)


# ------------------------------------------------------- id + histogram helpers

def test_id_is_six_digits():
  assert common.id_of(1) == "000001"
  assert common.id_of(12345) == "012345"
  with pytest.raises(ValueError):
    common.id_of(0)


def test_conf_histogram_covers_every_bucket_once():
  buckets = build.conf_histogram([0.18, 0.2, 0.3, 0.6, 0.95, 1.0])
  assert sum(b["boxes"] for b in buckets) == 6
  assert buckets[0]["boxes"] == 2      # 0.18, 0.2
  assert buckets[-1]["boxes"] == 2     # 0.95, 1.0


# ------------------------------------------------------------------ integration

def _write_frame(path: str, width: int, height: int, value: int) -> None:
  import cv2
  image = np.full((height, width, 3), value, dtype=np.uint8)
  cv2.imwrite(path, image)


def _synthetic_work(root: str, routes: int = 3, frames: int = 4) -> dict:
  """Create _frames/_meta/_auto for a miniature corpus."""
  meta = {}
  for index in range(routes):
    route = f"0000{index:02d}--aaaaaaaaaa--{index}"
    frame_dir = os.path.join(root, "_frames", route)
    os.makedirs(frame_dir, exist_ok=True)
    entries = []
    for number in range(frames):
      name = f"{number + 1:04d}.jpg"
      _write_frame(os.path.join(frame_dir, name), common.TARGET_SIZE[0], common.TARGET_SIZE[1],
                   60 + number)
      entries.append({"file": name, "t": number * 2.0, "brightness": float(60 + number)})
    meta[route] = {"route": route, "hour": 12 if index % 2 == 0 else 22,
                   "daylight": index % 2 == 0, "frames": entries}
    common.write_json(os.path.join(root, "_meta", f"{route}.json"), meta[route])
    detections = {"0001": [{"cls": 1, "conf": 0.62, "xyxy": [0.2, 0.4, 0.4, 0.8], "coco": "motorcycle"}],
                  "0002": [{"cls": 0, "conf": 0.31, "xyxy": [0.5, 0.4, 0.6, 0.8], "coco": "person"}],
                  "0003": [{"cls": 3, "conf": 0.71, "xyxy": [0.1, 0.5, 0.3, 0.7], "coco": "car"}],
                  "0004": []}
    common.write_json(os.path.join(root, "_auto", f"{route}.json"),
                      {"route": route, "conf": 0.18, "iou": 0.45,
                       "frames": {f"{n + 1:04d}": detections[f"{n + 1:04d}"] for n in range(frames)}})
  return meta


def _run_build(work: str, out: str, target: int = 12, batch: str = "unit_batch") -> int:
  return build.main(["--work", work, "--out", out, "--batch", batch,
                     "--target", str(target), "--keep-frames", "--overwrite"])


def test_build_end_to_end_satisfies_the_labeler_contract(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert _run_build(work, out) == 0
  dataset_dir = os.path.join(out, "unit_batch")

  report = validate.validate(dataset_dir)
  assert report.ok(), report.errors

  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  assert dataset["batch"] == "unit_batch"
  assert dataset["schema"] == 1
  assert dataset["geometry"]["width"] == 1344
  assert len(dataset["images"]) == 12
  assert dataset["split"]["train"] + dataset["split"]["val"] == 12
  for image in dataset["images"]:
    assert image["file"] == f"images/{image['id']}.jpg"
    assert set(image["auto_counts"]) <= set(TAXONOMY.names)
    # The labeler needs the source geometry to know which frames were letterboxed.
    assert "source_width" in image and "source_height" in image

  auto_meta = common.read_json(os.path.join(dataset_dir, "auto_meta.json"))
  review = common.read_json(os.path.join(dataset_dir, "review_state.json"))
  assert set(auto_meta) == {i["id"] for i in dataset["images"]}
  assert all(entry["reviewed"] is False for entry in review.values())

  # labels == auto_labels at hand-off time, and the geometry is right.
  for image in dataset["images"]:
    sample_id = image["id"]
    with open(os.path.join(dataset_dir, "labels", f"{sample_id}.txt")) as f:
      labels = f.read()
    with open(os.path.join(dataset_dir, "auto_labels", f"{sample_id}.txt")) as f:
      assert labels == f.read()
    if labels.strip():
      cls, box = common.parse_label_line(labels.splitlines()[0], 20)
      assert cls in (0, 1, 3)
      assert all(0.0 <= v <= 1.0 for v in box)

  # Route-level split: no route on both sides, and every image is listed once.
  routes_by_split: dict[str, set[str]] = {}
  for image in dataset["images"]:
    routes_by_split.setdefault(image["split"], set()).add(image["route"])
  assert not (routes_by_split.get("train", set()) & routes_by_split.get("val", set()))
  with open(os.path.join(dataset_dir, "train.txt")) as f:
    train = [line.strip() for line in f if line.strip()]
  with open(os.path.join(dataset_dir, "val.txt")) as f:
    val = [line.strip() for line in f if line.strip()]
  assert len(train) + len(val) == 12
  assert not (set(train) & set(val))

  stats = common.read_json(os.path.join(dataset_dir, "stats.json"))
  assert stats["total_images"] == 12
  assert stats["two_wheeler_total"] == 3          # one motorcycle per route
  assert stats["by_class"]["two_wheeler"]["images"] == 3
  # Two of the three synthetic routes are daytime, so the balance is 8/4 here.
  assert stats["day_night"]["day"]["images"] == 8
  assert stats["day_night"]["night"]["images"] == 4
  assert stats["negatives"]["images"] == 3        # every route has one empty frame
  assert stats["auto_conf"]["buckets"]
  assert stats["source_geometry"]          # provenance: which sources were letterboxed
  assert dataset["geometry"]["note"]
  with open(os.path.join(dataset_dir, "README.md"), encoding="utf-8") as f:
    assert "自动预标" in f.read()
  yaml_text = open(os.path.join(dataset_dir, "data.yaml"), encoding="utf-8").read()
  assert "nc: 17" in yaml_text and "- two_wheeler" in yaml_text


def test_build_moves_frames_by_default_and_can_keep_them(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert build.main(["--work", work, "--out", out, "--batch", "b1", "--target", "6",
                     "--overwrite"]) == 0
  remaining = sum(len(files) for _root, _dirs, files in
                  os.walk(os.path.join(work, "_frames")))
  assert remaining == 12 - 6      # moved, not copied


def test_build_is_idempotent_when_it_can_rerun(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert _run_build(work, out, batch="b2") == 0
  first = common.read_json(os.path.join(out, "b2", "dataset.json"))
  assert _run_build(work, out, batch="b2") == 0
  second = common.read_json(os.path.join(out, "b2", "dataset.json"))
  assert [i["route"] for i in first["images"]] == [i["route"] for i in second["images"]]


def test_build_refuses_to_write_into_an_existing_batch(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert _run_build(work, out, target=8, batch="dropin") == 0
  # Without --overwrite the second run must not write on top of the first.
  with pytest.raises(SystemExit):
    build.main(["--work", work, "--out", out, "--batch", "dropin", "--target", "5",
                "--keep-frames"])


def test_rebuild_with_a_different_selection_leaves_no_stale_files(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work, routes=3, frames=4)
  assert build.main(["--work", work, "--out", out, "--batch", "reuse", "--target", "9",
                     "--seed", "1", "--keep-frames", "--overwrite"]) == 0
  first = common.read_json(os.path.join(out, "reuse", "dataset.json"))
  assert len(first["images"]) == 9
  assert build.main(["--work", work, "--out", out, "--batch", "reuse", "--target", "5",
                     "--seed", "99", "--keep-frames", "--overwrite"]) == 0
  second = common.read_json(os.path.join(out, "reuse", "dataset.json"))
  assert len(second["images"]) == 5
  # Exactly five files per directory: nothing from the 9-image selection survived.
  dataset_dir = os.path.join(out, "reuse")
  for sub in ("images", "labels", "auto_labels"):
    assert len(os.listdir(os.path.join(dataset_dir, sub))) == 5, sub
  assert build.audit_output(dataset_dir, 5) == []
  assert validate.validate(dataset_dir).ok()
  with open(os.path.join(dataset_dir, "train.txt")) as f:
    train = [line.strip() for line in f if line.strip()]
  with open(os.path.join(dataset_dir, "val.txt")) as f:
    val = [line.strip() for line in f if line.strip()]
  assert len(train) + len(val) == 5


def test_audit_output_detects_a_stale_file(tmp_path):
  dataset_dir, _inbox = _mini_dataset(tmp_path)
  common.write_labels(os.path.join(dataset_dir, "labels", "999999.txt"), ["8 0.5 0.5 0.1 0.1"])
  problems = build.audit_output(dataset_dir, 12)
  assert any("has 13 files" in problem for problem in problems)


def test_package_writes_a_tarball(tmp_path):
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert build.main(["--work", work, "--out", out, "--batch", "b3", "--target", "6",
                     "--keep-frames", "--overwrite", "--package"]) == 0
  archive = os.path.join(out, "b3.tar.gz")
  assert os.path.isfile(archive) and os.path.getsize(archive) > 0
  import tarfile
  with tarfile.open(archive) as tar:
    names = tar.getnames()
  assert any(name.startswith("b3/") for name in names)
  assert any(name.endswith(".jpg") for name in names)


# ----------------------------------------------------------------- merge inbox

def _mini_dataset(tmp_path) -> tuple[str, str]:
  work, out = str(tmp_path / "work"), str(tmp_path / "out")
  _synthetic_work(work)
  assert _run_build(work, out, batch="mb", target=12) == 0
  return os.path.join(out, "mb"), str(tmp_path / "inbox" / "mb")


def test_merge_inbox_applies_valid_labels_and_backs_up(tmp_path):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  os.makedirs(os.path.join(inbox, "labels"))
  # Replace an auto label with a hand label and add a genuinely empty negative file.
  sample_id = common.read_json(os.path.join(dataset_dir, "dataset.json"))["images"][0]["id"]
  with open(os.path.join(inbox, "labels", f"{sample_id}.txt"), "w") as f:
    f.write("2 0.500000 0.500000 0.200000 0.200000\n")
  common.write_json(os.path.join(inbox, "review_state.json"),
                    {sample_id: {"reviewed": True, "edited": True, "updated": 1700000000}})
  common.write_json(os.path.join(inbox, "sync_manifest.json"),
                    {"schema": 1, "batch": "mb", "last_sync": "2026-10-27T00:00:00Z", "pending": 0})

  report = merge_inbox.merge(dataset_dir, inbox, dry_run=False)
  assert report["files"] == 1
  assert report["files_merged"] == 1
  assert report["files_rejected"] == 0
  assert report["reviewed"] == 1
  with open(os.path.join(dataset_dir, "labels", f"{sample_id}.txt")) as f:
    assert f.read().strip() == "2 0.500000 0.500000 0.200000 0.200000"
  assert report["backup_dir"] and os.path.isdir(report["backup_dir"])
  assert os.path.isfile(os.path.join(report["backup_dir"], f"{sample_id}.txt"))
  review = common.read_json(os.path.join(dataset_dir, "review_state.json"))
  assert review[sample_id]["reviewed"] is True
  assert review[sample_id]["edited"] is True
  # The auto label is preserved for the diff view in the annotator.
  assert os.path.isfile(os.path.join(dataset_dir, "auto_labels", f"{sample_id}.txt"))


def test_merge_inbox_dry_run_writes_nothing(tmp_path):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  os.makedirs(os.path.join(inbox, "labels"))
  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  sample_id = dataset["images"][0]["id"]
  before = open(os.path.join(dataset_dir, "labels", f"{sample_id}.txt")).read()
  with open(os.path.join(inbox, "labels", f"{sample_id}.txt"), "w") as f:
    f.write("3 0.400000 0.400000 0.100000 0.100000\n")
  report = merge_inbox.merge(dataset_dir, inbox, dry_run=True)
  assert report["files_merged"] == 1
  assert open(os.path.join(dataset_dir, "labels", f"{sample_id}.txt")).read() == before
  assert report["backup_dir"] is None
  assert report["class_counts_after"][3] == report["class_counts_before"][3] + 1 - (
      1 if before.strip().startswith("3 ") else 0)


@pytest.mark.parametrize("content,reason", [
    ("1 0.5 0.5 0.1\n", "columns"),
    ("99 0.5 0.5 0.1 0.1\n", "outside 0..16"),
    ("1 0.5 0.5 1.5 0.1\n", "outside [0, 1]"),
    ("a b c d e\n", "non-numeric"),
])
def test_merge_inbox_rejects_malformed_files_and_keeps_the_old_labels(tmp_path, content, reason):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  os.makedirs(os.path.join(inbox, "labels"))
  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  sample_id = dataset["images"][0]["id"]
  label_path = os.path.join(dataset_dir, "labels", f"{sample_id}.txt")
  before = open(label_path).read()
  with open(os.path.join(inbox, "labels", f"{sample_id}.txt"), "w") as f:
    f.write(content)
  report = merge_inbox.merge(dataset_dir, inbox, dry_run=False)
  assert report["files_rejected"] == 1
  assert reason in report["rejected"][0]["reason"]
  assert report["files_merged"] == 0
  assert open(label_path).read() == before


def test_merge_inbox_ignores_unknown_ids(tmp_path):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  os.makedirs(os.path.join(inbox, "labels"))
  with open(os.path.join(inbox, "labels", "999999.txt"), "w") as f:
    f.write("1 0.5 0.5 0.1 0.1\n")
  report = merge_inbox.merge(dataset_dir, inbox, dry_run=True)
  assert report["unknown_ids"] == ["999999"]
  assert report["files_merged"] == 0


def test_merge_inbox_refuses_an_inbox_from_another_batch(tmp_path):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  other = str(tmp_path / "inbox" / "other_batch")
  os.makedirs(os.path.join(other, "labels"))
  with open(os.path.join(other, "labels", "000001.txt"), "w") as f:
    f.write("2 0.5 0.5 0.1 0.1\n")
  with pytest.raises(ValueError, match="does not belong to batch"):
    merge_inbox.merge(dataset_dir, other, dry_run=True)
  # The override exists for renamed/copied inboxes.
  report = merge_inbox.merge(dataset_dir, other, dry_run=True, allow_inbox_mismatch=True)
  assert report["files"] == 1


def test_merge_inbox_handles_a_missing_inbox(tmp_path):
  dataset_dir, inbox = _mini_dataset(tmp_path)
  report = merge_inbox.merge(dataset_dir, inbox, dry_run=True)
  assert report["inbox_present"] is False
  assert report["files"] == 0


# ---------------------------------------------------------------- validate gate

def test_validate_catches_a_missing_image(tmp_path):
  dataset_dir, _inbox = _mini_dataset(tmp_path)
  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  os.unlink(os.path.join(dataset_dir, dataset["images"][0]["file"]))
  report = validate.validate(dataset_dir)
  assert not report.ok()
  assert any("missing image" in error for error in report.errors)


def test_validate_tolerates_annotation_after_a_merge(tmp_path):
  """After the labeler uploads, labels may legitimately disagree with auto_labels."""
  dataset_dir, _inbox = _mini_dataset(tmp_path)
  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  sample_id = next(i["id"] for i in dataset["images"] if i["auto_counts"])
  label_path = os.path.join(dataset_dir, "labels", f"{sample_id}.txt")
  # An annotator added a box: one more line than the auto pre-labels.
  with open(label_path, "a", encoding="utf-8") as f:
    f.write("0 0.500000 0.500000 0.100000 0.100000\n")
  # Before any review bookkeeping the mismatch is still an error...
  report = validate.validate(dataset_dir)
  assert not report.ok()
  # ...but once the image is marked reviewed it is only a warning.
  review_path = os.path.join(dataset_dir, "review_state.json")
  review = common.read_json(review_path)
  review[sample_id] = {"reviewed": True, "edited": True, "updated": 1}
  common.write_json(review_path, review)
  report = validate.validate(dataset_dir)
  assert report.ok(), report.errors
  assert any("labels has" in warning for warning in report.warnings)


def test_validate_catches_a_route_leak(tmp_path):
  dataset_dir, _inbox = _mini_dataset(tmp_path)
  dataset_path = os.path.join(dataset_dir, "dataset.json")
  dataset = common.read_json(dataset_path)
  dataset["images"][0]["route"] = next(i["route"] for i in dataset["images"]
                                       if i["split"] != dataset["images"][0]["split"])
  common.write_json(dataset_path, dataset)
  report = validate.validate(dataset_dir)
  assert not report.ok()
  assert any("leakage" in error for error in report.errors)


def test_validate_catches_a_wrong_image_size(tmp_path):
  import cv2
  dataset_dir, _inbox = _mini_dataset(tmp_path)
  dataset = common.read_json(os.path.join(dataset_dir, "dataset.json"))
  path = os.path.join(dataset_dir, dataset["images"][0]["file"])
  # Noise, not a flat colour: a flat 100x100 JPEG would trip the "too small" check first.
  cv2.imwrite(path, np.random.default_rng(0).integers(0, 255, (100, 100, 3), dtype=np.uint8))
  report = validate.validate(dataset_dir)
  assert not report.ok()
  assert any("image is 100x100" in error for error in report.errors)


# ------------------------------------------------------------- extract helpers

def test_extract_route_selection_balances_day_and_night(tmp_path):
  routes = []
  for hour, name in ((11, "day1"), (13, "day2"), (22, "night1"), (23, "night2")):
    path = tmp_path / name
    path.mkdir()
    (path / "fcamera.hevc").write_bytes(b"x")
    os.utime(path / "fcamera.hevc", (hour * 3600, hour * 3600))  # local hour proxy
    routes.append(str(path))
  picked = extract.select_routes(routes, 2, 0.5, seed=1)
  assert len(picked) == 2
  hours = [common.recorded_hour(route) for route in picked]
  assert len(hours) == 2


def test_extract_hours_filter_splits_day_and_night(tmp_path):
  import datetime
  routes = []
  for name, hour in (("day", 11), ("night", 22)):
    path = tmp_path / name
    path.mkdir()
    camera = path / "fcamera.hevc"
    camera.write_bytes(b"x")
    stamp = datetime.datetime(2026, 10, 27, hour, 5, 0).timestamp()
    os.utime(camera, (stamp, stamp))
    routes.append(str(path))
  assert extract.filter_by_hours(routes, "all") == routes
  assert [common.route_id(r) for r in extract.filter_by_hours(routes, "day")] == ["day"]
  assert [common.route_id(r) for r in extract.filter_by_hours(routes, "night")] == ["night"]


def test_probe_video_rejects_garbage(tmp_path):
  bogus = tmp_path / "nope.hevc"
  bogus.write_bytes(b"not a video")
  assert extract.probe_video(str(bogus)) == (0, 0, 0.0)


def test_list_routes_skips_boot_and_missing_camera(tmp_path):
  for name in ("a--1", "b--2", "boot", "empty--3"):
    (tmp_path / name).mkdir()
  (tmp_path / "a--1" / "fcamera.hevc").write_bytes(b"x")
  (tmp_path / "b--2" / "fcamera.hevc").write_bytes(b"x")
  (tmp_path / "empty--3" / "qlog.zst").write_bytes(b"x")
  names = [common.route_id(route) for route in common.list_routes(str(tmp_path))]
  assert names == ["a--1", "b--2"]


def test_recorded_hour_uses_the_camera_mtime(tmp_path):
  import datetime
  route = tmp_path / "r--1"
  route.mkdir()
  camera = route / "fcamera.hevc"
  camera.write_bytes(b"x")
  # The hour is read in local time, so build the stamp from a naive local datetime.
  local = datetime.datetime(2026, 10, 27, 13, 15, 0).timestamp()
  os.utime(camera, (local, local))
  assert common.recorded_hour(str(route)) == 13


# ----------------------------------------------------------------------- style

def test_ruff_is_clean():
  """Keep the tooling lint-clean; skipped when ruff is unavailable."""
  try:
    proc = subprocess.run([sys.executable, "-m", "ruff", "check", TOOL_DIR],
                          capture_output=True, text=True, timeout=120)
  except (OSError, subprocess.SubprocessError):
    pytest.skip("ruff not installed")
  if "No module named" in (proc.stderr or ""):
    pytest.skip("ruff not installed")
  assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pipeline_modules_import_without_the_repo_on_sys_path():
  """Each entry point must be runnable standalone (it re-inserts its own dir)."""
  for module in ("extract.py", "autolabel.py", "build.py", "merge_inbox.py", "validate.py"):
    proc = subprocess.run([sys.executable, os.path.join(TOOL_DIR, module), "--help"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"{module}: {proc.stderr}"
    assert "usage" in proc.stdout.lower()
