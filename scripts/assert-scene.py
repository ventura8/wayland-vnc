#!/usr/bin/env python3
"""Verify an actual-viewer screenshot against the synthetic scene contract."""

import argparse
import json
from pathlib import Path

from wayland_vnc.image_evidence import verify_gesture_markers, verify_input_markers, verify_scene

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("screenshot", type=Path)
parser.add_argument("--require-input", action="store_true")
parser.add_argument("--require-gestures", action="store_true")
args = parser.parse_args()
result = {"schema_version": 1, "scene": verify_scene(args.screenshot).as_dict()}
if args.require_input:
    result["input"] = verify_input_markers(args.screenshot).as_dict()
if args.require_gestures:
    result["gestures"] = verify_gesture_markers(args.screenshot).as_dict()
print(json.dumps(result, sort_keys=True))
