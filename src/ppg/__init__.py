"""PPG processing pipeline (NBE smart-glasses project).

Layer 1 – signal conditioning & quality:  ppg.pipeline.run_layer1 / process_file
"""
from .config import Layer1Config
from .io import load_csv
from .pipeline import Layer1Result, process_file, run_layer1

__all__ = ["Layer1Config", "Layer1Result", "load_csv", "process_file", "run_layer1"]
__version__ = "0.4.0"
