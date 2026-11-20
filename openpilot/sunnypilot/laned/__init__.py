"""Optional lane-marking type detection.

``laned`` runs the CarrotPilot YOLOv8-Seg lane classifier on the road camera and
publishes ``laneLineTypes``. It is independent of the driving model: geometry
still comes from ``modelV2.laneLines``; this package only classifies the marking
on the ego-lane boundaries.
"""
