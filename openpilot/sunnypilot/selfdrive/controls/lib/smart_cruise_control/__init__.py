"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.constants import CV

# 2026-11-14: 15 km/h, was 20. This is both the "do not operate below" gate and the
# floor of the commanded curve speed (`max(v_target, MIN_V)` in the vision controller).
# The 2026-11-13 city route ran 25-40 km/h through 60-80 m radii, i.e. exactly the band
# where the vision controller should be working, and the 20 km/h floor put its demand
# above the car's own speed on the tighter ones.
MIN_V = 15 * CV.KPH_TO_MS  # Do not operate under 15 km/h
