"""Locations of the model files shipped with the yolod package."""

import os

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DEFAULT_ONNX_MODEL = os.path.join(MODELS_DIR, "yolo11n.onnx")
DEFAULT_NCNN_PARAM = os.path.join(MODELS_DIR, "yolo11n.ncnn.param")
DEFAULT_NCNN_BIN = os.path.join(MODELS_DIR, "yolo11n.ncnn.bin")
DEFAULT_NCNN_NAMES = os.path.join(MODELS_DIR, "yolo11n.ncnn.names.json")
