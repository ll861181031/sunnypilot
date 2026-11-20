"""Generate a small synthetic batch for demos, tests and ``--selftest``.

The produced directory follows exactly the layout of ``tools/yolo_dataset``
so the labeler can be exercised without real camera frames::

    batch_demo/
      images/<id>.jpg, labels/<id>.txt, auto_labels/<id>.txt,
      auto_meta.json, dataset.json, taxonomy.json, review_state.json,
      train.txt, val.txt, data.yaml

Only the test/demo tooling writes here; the labeler itself never creates a
batch.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from collections.abc import Sequence

from PIL import Image, ImageDraw

import dataset as ds

WIDTH = 1344
HEIGHT = 760


def _taxonomy_path(explicit: Path | None = None) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    here = Path(__file__).resolve().parent
    candidates.extend([here / "taxonomy.json",
                       here.parent / "yolo_annotator" / "taxonomy.json"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ds.DatasetError(
        "找不到 taxonomy.json（演示批次需要它），请把仓库里的 " +
        "tools/yolo_annotator/taxonomy.json 放在本脚本同目录"
    )


def _draw_scene(path: Path, rng: random.Random, num_objects: int,
                objects: list[tuple[int, tuple[float, float, float, float], float]]
                ) -> None:
    """Draw a fake road scene and append (class, normalised xyxy, conf)."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (18, 22, 30))
    draw = ImageDraw.Draw(image)
    # Sky / horizon / road.
    draw.rectangle([0, 0, WIDTH, int(HEIGHT * 0.42)], fill=(58, 74, 96))
    draw.rectangle([0, int(HEIGHT * 0.42), WIDTH, HEIGHT], fill=(38, 38, 40))
    draw.polygon([(WIDTH * 0.42, HEIGHT * 0.42), (WIDTH * 0.58, HEIGHT * 0.42),
                  (WIDTH * 0.95, HEIGHT), (WIDTH * 0.05, HEIGHT)],
                 fill=(52, 52, 55))
    for i in range(1, 7):
        y = HEIGHT * 0.42 + (HEIGHT * 0.58) * (i / 7.0)
        half = (y - HEIGHT * 0.42) / (HEIGHT * 0.58) * WIDTH * 0.22
        draw.line([(WIDTH / 2 - half * 0.12, y), (WIDTH / 2 + half * 0.12, y)],
                  fill=(210, 210, 190), width=max(2, int(6 * i / 7)))

    box_colors = [(0, 255, 0), (255, 255, 0), (0, 200, 255), (255, 0, 0)]
    for index in range(num_objects):
        # Keep objects on the lower half (road area) and away from the border.
        w = rng.uniform(0.06, 0.16)
        h = w * rng.uniform(0.7, 1.4)
        x = rng.uniform(0.06, 0.94 - w)
        y = rng.uniform(0.55, 0.94 - h)
        x1, y1, x2, y2 = x, y, x + w, y + h
        color = box_colors[index % len(box_colors)]
        draw.rectangle([x1 * WIDTH, y1 * HEIGHT, x2 * WIDTH, y2 * HEIGHT],
                       outline=color, width=3, fill=tuple(c // 3 for c in color))
        cls = rng.choice([0, 1, 2, 3, 8, 9, 10, 11])
        objects.append((cls, (x1, y1, x2, y2), rng.uniform(0.31, 0.97)))
    image.save(path, quality=88)


def make_demo_batch(dest: Path, n: int = 6, seed: int = 7,
                    batch_name: str | None = None) -> Path:
    """Create a synthetic batch at *dest* and return the directory."""
    dest = Path(dest)
    batch_name = batch_name or dest.name
    rng = random.Random(seed)
    taxonomy = ds.Taxonomy.from_file(_taxonomy_path())
    class_ids = taxonomy.ids

    for sub in ("images", "labels", "auto_labels"):
        (dest / sub).mkdir(parents=True, exist_ok=True)

    images: list[dict[str, object]] = []
    auto_meta: dict[str, list[dict[str, object]]] = {}
    class_counts: dict[str, int] = {str(cid): 0 for cid in class_ids}

    for index in range(1, n + 1):
        image_id = f"{index:06d}"
        objects: list[tuple[int, tuple[float, float, float, float], float]] = []
        # Every 5th image is a negative sample (no pre-labels at all).
        num_objects = 0 if index % 5 == 0 else rng.randint(1, 4)
        _draw_scene(dest / "images" / f"{image_id}.jpg", rng, num_objects, objects)

        # Map the random COCO-ish class ids onto real taxonomy ids.
        mapped: list[tuple[int, tuple[float, float, float, float], float]] = []
        for cls, box, conf in objects:
            taxonomy_cls = class_ids[cls % len(class_ids)]
            mapped.append((taxonomy_cls, box, conf))

        lines = []
        for cls, (x1, y1, x2, y2), conf in mapped:
            w = max(1e-4, x2 - x1)
            h = max(1e-4, y2 - y1)
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            lines.append(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
            auto_meta.setdefault(image_id, []).append(
                {"cls": cls, "conf": round(conf, 4), "xyxy": [round(x1, 6),
                                                              round(y1, 6),
                                                              round(x2, 6),
                                                              round(y2, 6)]})
            class_counts[str(cls)] = class_counts.get(str(cls), 0) + 1
        text = "\n".join(lines) + ("\n" if lines else "")
        # labels/ starts identical to auto_labels/ (= "unreviewed" state).
        (dest / "labels" / f"{image_id}.txt").write_text(text, encoding="utf-8")
        (dest / "auto_labels" / f"{image_id}.txt").write_text(text, encoding="utf-8")

        hour = rng.choice([7, 9, 11, 14, 16, 20, 23])
        images.append({
            "id": image_id,
            "file": f"images/{image_id}.jpg",
            "width": WIDTH,
            "height": HEIGHT,
            "route": f"demo--{rng.randint(0x100000, 0xffffff):08x}--{rng.randint(0, 12)}",
            "t": round(rng.uniform(0, 120), 2),
            "hour": hour,
            "split": "val" if index % 4 == 0 else "train",
            "priority": 5 if mapped and mapped[0][0] in taxonomy.two_wheeler_ids
                        else rng.choice([0, 1, 2, 3]),
            "auto_counts": {str(cls): sum(1 for m in mapped if m[0] == cls)
                            for cls in {m[0] for m in mapped}},
            "daylight": hour in range(8, 18),
        })

    (dest / "taxonomy.json").write_text(_taxonomy_path().read_text(encoding="utf-8"),
                                        encoding="utf-8")
    split = {"train": sum(1 for i in images if i["split"] == "train"),
             "val": sum(1 for i in images if i["split"] == "val")}
    payload = {
        "schema": ds.SCHEMA_VERSION,
        "batch": batch_name,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "taxonomy": taxonomy.name,
        "classes": [c.to_dict() for c in taxonomy.classes],
        "images": images,
        "counts": class_counts,
        "split": split,
    }
    ds.atomic_write_json(dest / "dataset.json", payload)
    ds.atomic_write_json(dest / "auto_meta.json", auto_meta)
    review = {str(img["id"]): {"reviewed": False, "edited": False, "updated": 0.0}
              for img in images}
    ds.atomic_write_json(dest / "review_state.json", review)

    train = [f"images/{img['id']}.jpg" for img in images if img["split"] == "train"]
    val = [f"images/{img['id']}.jpg" for img in images if img["split"] == "val"]
    (dest / "train.txt").write_text("\n".join(train) + ("\n" if train else ""),
                                    encoding="utf-8")
    (dest / "val.txt").write_text("\n".join(val) + ("\n" if val else ""),
                                  encoding="utf-8")
    yaml_lines = ["path: .", "train: train.txt", "val: val.txt", "names:"]
    yaml_lines += [f"  {c.id}: {c.name}" for c in taxonomy.classes]
    (dest / "data.yaml").write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    return dest


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a synthetic demo batch")
    parser.add_argument("dest", help="output batch directory")
    parser.add_argument("--n", type=int, default=6, help="number of images")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    path = make_demo_batch(Path(args.dest), n=args.n, seed=args.seed)
    print(f"demo batch written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
