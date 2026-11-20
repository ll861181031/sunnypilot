import os
import sys

# The tool is run as a script (`tools/camera_debug/camera_debug.py`), which adds
# the repo root to sys.path itself. Tests import the modules directly, so they
# have to do the same.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
