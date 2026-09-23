"""Create an isolated Android AVD; never reuse or overwrite a personal device."""

import argparse
import os
import subprocess
from pathlib import Path

AVD_NAME = "wayland-vnc-api36"
PACKAGE = "system-images;android-36;google_apis_playstore;x86_64"
PLAY_STORE_ON = "PlayStore.enabled=true\n"
# `adb shell input text` injects through the hardware-keyboard path. avdmanager writes
# hw.keyboard=no for the pixel_2 profile, and with no hardware keyboard that call
# reports success and types nothing: the credential fields stay empty, the app never
# authenticates, and every scenario fails on "no valid frame" with no hint why.
HARDWARE_KEYBOARD_ON = "hw.keyboard=yes\n"


def configure(contents: str) -> str:
    """The two settings the lab AVD needs that the pixel_2 profile does not give it:
    the Play Store the official image is chosen for, and a hardware keyboard, without
    which `adb shell input text` silently types nothing."""
    for setting, off in (
        (PLAY_STORE_ON, ("PlayStore.enabled=false\n", "PlayStore.enabled=no\n")),
        (HARDWARE_KEYBOARD_ON, ("hw.keyboard=no\n",)),
    ):
        written = next((value for value in off if value in contents), None)
        if written is not None:
            contents = contents.replace(written, setting, 1)
        elif setting not in contents:
            contents = setting + contents
    return contents


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
