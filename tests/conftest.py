"""
Pytest configuration file for shotgunEvents tests.
"""

import os
import sys

# Add src directory to path so tests can import modules
src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
sys.path.insert(0, src_path)
