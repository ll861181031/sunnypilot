"""Read the red / yellow / green state of a detected traffic-light crop.

The detector has one ``traffic_light`` class.  A lamp lens is bright in the
centre and the surrounding housing is dark; a Chinese flag is a nearly uniform
red field that reaches the crop border.  Comparing the centre against the
border is therefore a useful and cheap first split.
"""

from __future__ import annotations

import cv2
import numpy as np

RED = "红"
YELLOW = "黄"
GREEN = "绿"


def _masks(crop: np.ndarray):
  hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
  h, s, v = cv2.split(hsv)
  red = (((h <= 10) | (h >= 170)) & (s >= 105) & (v >= 65))
  yellow = ((h >= 15) & (h <= 38) & (s >= 105) & (v >= 95))
  green = ((h >= 40) & (h <= 95) & (s >= 85) & (v >= 55))
  return red, yellow, green


def _centre_border(mask: np.ndarray) -> tuple[float, float]:
  h, w = mask.shape[:2]
  centre = mask[int(h * 0.30):int(h * 0.70), int(w * 0.30):int(w * 0.70)].mean()
  border = np.ones_like(mask, dtype=bool)
  border[int(h * 0.18):int(h * 0.82), int(w * 0.18):int(w * 0.82)] = False
  return float(centre), float(mask[border].mean() if border.any() else 0.0)



def _best_circle(mask: np.ndarray) -> tuple[float, float]:
  """Circularity and area fraction of the largest round-ish colour blob."""
  contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
  area = mask.shape[0] * mask.shape[1]
  best, best_area = 0.0, 0.0
  for c in contours:
    a = cv2.contourArea(c)
    if a < 0.005 * area:
      continue
    per = cv2.arcLength(c, True)
    if per <= 0:
      continue
    circ = 4 * np.pi * a / (per * per)
    if circ > best:
      best, best_area = float(circ), float(a / area)
  return best, best_area


def _hough_state(crop: np.ndarray):
  h, w = crop.shape[:2]
  c = cv2.resize(crop, (max(48, min(160, w * 4)), max(48, min(160, h * 4))), interpolation=cv2.INTER_AREA)
  # _masks() converts to HSV itself; a second cvtColor here was dead work
  # on every classifier call.
  masks = _masks(c)
  best = None
  for name, mask in (("红", masks[0]), ("黄", masks[1]), ("绿", masks[2])):
    m = cv2.GaussianBlur(mask.astype(np.uint8) * 255, (3, 3), 0)
    circles = cv2.HoughCircles(m, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(8, int(min(c.shape[:2]) * .25)),
                               param1=50, param2=7, minRadius=max(2, int(min(c.shape[:2]) * .06)),
                               maxRadius=max(4, int(min(c.shape[:2]) * .50)))
    if circles is None:
      continue
    for x, y, r in np.round(circles[0]).astype(int):
      yy, xx = np.ogrid[:c.shape[0], :c.shape[1]]
      disk = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
      if mask[disk].mean() >= 0.55:
        if best is None or r > best[0]:
          best = (r, name)
  return best[1] if best else None


def classify_traffic_light(crop: np.ndarray) -> str | None:
  if crop is None or crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 4:
    return None
  return _hough_state(crop)


def label_for_traffic_light(crop: np.ndarray, fallback: str = "交通信号灯") -> str:
  state = classify_traffic_light(crop)
  return f"{fallback}-{state}" if state else fallback
