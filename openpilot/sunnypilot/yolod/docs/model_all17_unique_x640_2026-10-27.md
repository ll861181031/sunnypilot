# YOLO11x 17-class road-user detector (fixed, 2026-10-27 21:00)

## Why this version exists
The previous model was trained on two batches that both used 6-digit IDs
(`000001...`); the shared Ultralytics label cache mixed batch_002 labels with
batch_001 images. It also had very few tricycle examples. Result: validation
showed a misleading tricycle mAP while the model never predicted tricycle.

Fix:
- built `all17_unique` with prefixed IDs (`b1_000001` / `n_000001`), hardlinked
  images/labels so no duplicate ID or cache collision;
- oversampled tricycle training images 20x (72 images -> 1,440 extra samples);
- cleared labels.cache; retrained.

## Files
- ONNX: `/home/lsr/yolo_models/all17_unique_x640_20261027.onnx` (218 MB, opset 12, 1x3x640x640 -> 1x21x8400)
- PyTorch: `/home/lsr/yolo_models/all17_unique_x640_20261027.pt` (110 MB)
- Windows: `C:\Users\lsr\sunnypilot-yolo\models\all17_unique_x640_20261027.onnx` / `.pt`
- Training run: `C:\Users\lsr\sunnypilot-yolo\runs\all17_unique_x640\`
- Demo video: `YOLO-model17-tricycle-fixed.mp4` (route 40 night, 30 s)

## Dataset
- 17,000 images (batch_001 12,000 + batch_002_night 5,000)
- route-safe 14,039 train / 2,961 val; 17 classes; unique prefixed IDs
- automatic labels + 149 human-edited frames + propagated tricycle/rider edits
- tricycle train images oversampled 20x

## Training
- YOLO11x COCO, 640 px, batch 16, AMP, freeze first 10 layers, cache=ram
- 20 epochs, early stop patience 8; ~1 h 42 min on RTX 4070 SUPER
- best checkpoint epoch 17

## Validation (2,961 images, 15,974 instances)
| class | images | instances | P | R | mAP50 | mAP50-95 |
|---|---:|---:|---:|---:|---:|---:|
| all | 2961 | 15974 | 0.681 | 0.693 | **0.717** | **0.572** |
| person | 784 | 1850 | 0.731 | 0.713 | 0.776 | 0.562 |
| two_wheeler | 642 | 1031 | 0.784 | 0.818 | 0.870 | 0.638 |
| tricycle | 59 | 59 | 0.630 | 1.000 | **0.715** | **0.678** |
| car | 2408 | 10883 | 0.808 | 0.800 | 0.875 | 0.659 |
| truck | 648 | 826 | 0.752 | 0.725 | 0.811 | 0.700 |
| bus | 210 | 230 | 0.647 | 0.455 | 0.552 | 0.474 |
| traffic_light | 666 | 1082 | 0.556 | 0.416 | 0.502 | 0.328 |
| traffic_sign | 13 | 13 | 0.543 | 0.615 | 0.633 | 0.536 |

The demo frame at t=12 s shows `tricycle 0.80` on the white cargo tricycle and
`two_wheeler 0.37` on the scooter — the previous model called both `car`.

Classes with no boxes in this dataset (van, trailer, special_vehicle, stroller,
wheelchair, animal, cone, barrier, other_obstacle) are still untrained.

## Caveats
- mAP is measured against the current (mostly auto) labels, not independent truth.
- `bus` recall and `traffic_light` precision remain weak.
- The model is AGPL-3.0 (Ultralytics YOLO11x weights/license).
