"""Create an isolated Android AVD; never reuse or overwrite a personal device."""

import argparse
import os
import subprocess
from pathlib import Path

AVD_NAME = "wayland-vnc-api36"
PACKAGE = "system-images;android-36;google_apis_playstore;x86_64"
# The settings the lab AVD needs that avdmanager's pixel_2 profile does not give it.
# Each replaces whatever value the profile wrote for that key.
SETTINGS = {
    # The official image is chosen for the Play Store; the profile leaves it off.
    "PlayStore.enabled": "true",
    # `adb shell input text` injects through the hardware-keyboard path. With
    # hw.keyboard=no it reports success and types nothing: the credential fields stay
    # empty and every scenario fails on "no valid frame" with no hint why.
    "hw.keyboard": "yes",
    # The profile's 2G is too little for API 36 with Play services under SwiftShader
    # software rendering at 1920x1080: System UI starves, raises "System UI isn't
    # responding", and the emulator falls over mid-run.
    "hw.ramSize": "4096M",
}


def configure(contents: str) -> str:
    """Apply SETTINGS to an AVD config.ini: replace each key's line wherever the
    profile wrote it, add it where it did not, and leave every other line alone."""
    lines = contents.splitlines(keepends=True)
    seen = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in SETTINGS:
            lines[index] = f"{key}={SETTINGS[key]}\n"
            seen.add(key)
    missing = [f"{key}={value}\n" for key, value in SETTINGS.items() if key not in seen]
    return "".join(missing + lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    root = Path(__file__).resolve().parents[1] / "artifacts" / "android"
    avds = root / "avd"
    avds.mkdir(parents=True, exist_ok=True)
    device = avds / f"{AVD_NAME}.avd"
    if device.exists() or (avds / f"{AVD_NAME}.ini").exists():
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
            AVD_NAME,
            "--package",
            PACKAGE,
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
    configuration = device / "config.ini"
    contents = configuration.read_text(encoding="utf-8")
    if "tag.id=google_apis_playstore\n" not in contents:
        parser.error("Created AVD does not use the requested official Play Store image")
    configuration.write_text(configure(contents), encoding="utf-8")
    print(f"Created isolated AVD: {device}")
    print(
        "RealVNC app provisioning and ABI validation are still required; no qualification passed."
    )


if __name__ == "__main__":
    main()
