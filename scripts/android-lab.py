"""Create an isolated Android AVD; never reuse or overwrite a personal device."""

import argparse
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sdk", type=Path, required=True)
args = parser.parse_args()
sdk = args.sdk.resolve()
root = Path(__file__).resolve().parents[1] / "artifacts" / "android"
avds = root / "avd"
avds.mkdir(parents=True, exist_ok=True)
device = avds / "wayland-vnc-api36.avd"
if device.exists() or (avds / "wayland-vnc-api36.ini").exists():
    parser.error("Isolated AVD already exists; refusing to overwrite it")
manager = sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager"
image = sdk / "system-images" / "android-36" / "google_apis_playstore" / "x86_64"
if not manager.is_file() or not image.is_dir():
    parser.error("SDK must contain avdmanager and the official API 36 Play Store x86_64 image")
environment = dict(os.environ)
environment["ANDROID_AVD_HOME"] = str(avds)
environment["ANDROID_USER_HOME"] = str(root / "user")
subprocess.run(
    [
        str(manager),
        "create",
        "avd",
        "--name",
        "wayland-vnc-api36",
        "--package",
        "system-images;android-36;google_apis_playstore;x86_64",
        "--device",
        "pixel_2",
        "--path",
        str(device),
    ],
    input="no\n",
    text=True,
    env=environment,
    check=True,
    timeout=120,
)
PLAY_STORE_ON = "PlayStore.enabled=true\n"
configuration = device / "config.ini"
contents = configuration.read_text(encoding="utf-8")
if "tag.id=google_apis_playstore\n" not in contents:
    parser.error("Created AVD does not use the requested official Play Store image")
if "PlayStore.enabled=false\n" in contents:
    contents = contents.replace("PlayStore.enabled=false\n", PLAY_STORE_ON, 1)
elif "PlayStore.enabled=no\n" in contents:
    contents = contents.replace("PlayStore.enabled=no\n", PLAY_STORE_ON, 1)
elif PLAY_STORE_ON not in contents:
    contents = PLAY_STORE_ON + contents
configuration.write_text(contents, encoding="utf-8")
print(f"Created isolated AVD: {device}")
print("RealVNC app provisioning and ABI validation are still required; no qualification passed.")
