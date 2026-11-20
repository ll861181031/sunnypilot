# 21-class road-user model with traffic-light states (experimental, 2026-10-28)

## What changed
The 17-class model had one `traffic_light` class. A new 21-class head adds:
- `traffic_light` (generic / manually reviewed / unclear)
- `traffic_light_red` / `traffic_light_yellow` / `traffic_light_green`
- `flag` (the flags that contaminated the traffic-light labels)
- the previous other classes, with `traffic_sign` / `other_obstacle` re-indexed.

Label preparation was automatic:
- non-reviewed traffic_light boxes -> CLIP "traffic light vs Chinese flag/sign"
- surviving lamps -> HSV centre-vs-border red/yellow/green
- reviewed/edited frames kept their human traffic_light boxes as the generic class
  (human boxes were not rewritten).

## Files
- ONNX: `/home/lsr/yolo_models/all17_state_x640_20261028.onnx` (218 MB, 21 classes)
- PyTorch: `/home/lsr/yolo_models/all17_state_x640_20261028.pt` (349 MB)
- Windows: `C:\Users\lsr\sunnypilot-yolo\models\all17_state_x640_20261028.onnx` / `.pt`
- Training run: `C:\Users\lsr\sunnypilot-yolo\runs\all17_state_x640_12\`
- Demo: `D:\Desktop\YOLO-traffic-light-states.mp4`

## Training
- YOLO11x COCO, 640, batch 16, AMP, freeze 10, cache=ram
- 12-epoch budget, stopped after epoch 8 (time), best epoch 8
- about 77 minutes on RTX 4070 SUPER

## Validation (2,961 images)
| class | P | R | mAP50 | mAP50-95 |
|---|---:|---:|---:|---:|
| all | 0.530 | 0.632 | 0.543 | 0.407 |
| person | 0.721 | 0.705 | 0.760 | 0.546 |
| two_wheeler | 0.774 | 0.816 | 0.870 | 0.637 |
| tricycle | 0.630 | 1.000 | 0.735 | 0.702 |
| car | 0.775 | 0.812 | 0.867 | 0.642 |
| truck | 0.747 | 0.724 | 0.803 | 0.686 |
| bus | 0.589 | 0.378 | 0.531 | 0.442 |
| **traffic_light (generic)** | 0.474 | 0.328 | 0.302 | 0.158 |
| **traffic_light_red** | 0.227 | 0.316 | **0.100** | **0.058** |
| **traffic_light_yellow** | 0.298 | 0.767 | **0.336** | **0.185** |
| **traffic_light_green** | 0.344 | 0.714 | **0.359** | **0.173** |
| traffic_sign | 0.307 | 0.717 | 0.534 | 0.432 |
| flag | 0.470 | 0.302 | 0.316 | 0.228 |

## Honest status
**This is an experimental first version, not a production traffic-light state detector.**
- Red is especially weak (mAP50 0.100). The demo clip contains a red flag still
  labelled `交通信号灯-红 0.43`; flags and red lamps are visually similar.
- Yellow recall is high but precision/mAP are low.
- The 21-class head also reduces general vehicle mAP vs the 17-class model
  (0.717 -> 0.543). Keep the 17-class model for normal detection.
- Cause: traffic_light labels were overwhelmingly flags; only ~500 real lamp
  boxes could be auto-labeled; reviewed human boxes were preserved generic.

## Required next step
Manual review of the state crops (especially red), then a focused retrain.
The automatic CLIP/HSV labels are a bootstrap, not ground truth. A production
version needs a reviewed state set, ideally with red lights sampled from more
routes/night conditions.
