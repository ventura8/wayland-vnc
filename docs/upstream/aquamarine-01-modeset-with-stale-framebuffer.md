# DRM: a live switch to a larger mode fails on virtio-gpu, and the output falls back to 800x600

## Summary

On a QEMU virtio-gpu output, Hyprland can switch to any mode **smaller** than the one
it is showing, but never to a **larger** one. The request is accepted
(`hyprctl` answers `ok`), aquamarine allocates the new swapchain at the requested size,
and then the kernel rejects the modeset:

```text
DEBUG from aquamarine ]: GBM: Allocated a new buffer with size [Vector2D: x: 3840, y: 2160] and format XR24 with modifier 0 aka LINEAR
DEBUG from aquamarine ]: Swapchain: Reconfigured a swapchain to [Vector2D: x: 3840, y: 2160] XR24 of length 3
DEBUG from aquamarine ]: atomic drm request: failed to commit: Invalid argument, flags: ATOMIC_ALLOW_MODESET ATOMIC_TEST_ONLY
```

Hyprland then walks its fallback modes, each of which fails the same way, and settles on
800x600. From there every mode larger than 800x600 fails too, so the output is stuck.

The kernel's answer gives away the cause. With `AQ_NO_ATOMIC=1` the same request fails
in `drmModeSetCrtc` with `ENOSPC`:

```text
DEBUG from aquamarine ]: legacy drm: Modesetting CRTC, mode: clock 594000 hdisplay 4096 vdisplay 2160 vrefresh 50
ERR from aquamarine ]: legacy drm: drmModeSetCrtc failed: No space left on device
```

`ENOSPC` from a legacy SetCrtc comes from the DRM core's viewport check,
`drm_crtc_check_viewport()`: the framebuffer attached to the CRTC is smaller than the
mode being set. So the modeset is tested and committed with the **previous**
framebuffer still attached, not the new swapchain buffer. virtio-gpu's primary plane
cannot be positioned or scaled -- it must cover the whole CRTC -- so the atomic check
rejects it too (`EINVAL`). A smaller mode passes, because the old, larger buffer still
covers it; that is the whole pattern.

wlroots compositors (sway, wayfire, labwc) switch the same output to 3840x2160 without
error, on the same device in the same guest.

## Reproduction

A QEMU/KVM guest with a virtio-gpu output that offers 4K, running Hyprland on it:

```sh
qemu-system-x86_64 -enable-kvm -m 4096 -vga none \
  -device virtio-gpu-pci,max_outputs=1,xres=3840,yres=2160,max_hostmem=1G ...
```

In the guest, with Hyprland running at 1920x1080:

```sh
hyprctl keyword monitor Virtual-1,1280x720@60,0x0,1    # smaller: switches
hyprctl keyword monitor Virtual-1,1920x1080@60,0x0,1   # larger: stays, falls back to 800x600
hyprctl monitors                                        # 800x600@60.31700
```

Every result below was observed on one guest, in this order:

| requested       | shown afterwards |
| --------------- | ---------------- |
| 1280x720@60     | 1280x720         |
| 1920x1080@60    | 800x600          |
| 1920x1200@60    | 800x600          |
| 2560x1080@50    | 800x600          |
| 1280x720@60     | 800x600          |
| 3840x2160@60    | 800x600          |

`max_hostmem=1G` matters for the reproduction only in that it removes an unrelated
failure: at QEMU's default 256 MiB, 3840x2160 swapchains exhaust virtio-gpu's resource
budget first and `RESOURCE_CREATE_2D` is refused with `OUT_OF_MEMORY`. With the budget
raised the GPU reports no error at all, and the modeset is refused by the kernel as
above.

Disabling the output first, to detach the old framebuffer, is not a workaround:
`hyprctl keyword monitor Virtual-1,disable` on the only output terminates Hyprland.

## Expected

The modeset is tested and committed with the buffer allocated for the new mode attached
to the primary plane, so a driver that requires the primary plane to cover the CRTC
accepts it -- as the wlroots compositors' switches are on the same device.

## Environment

- Hyprland 0.53.3 (dd220efe), aquamarine as shipped with it; Ubuntu 26.04 guest
- Linux virtio_gpu 0.1.0, features `-virgl +edid -resource_blob -host_visible`, one
  scanout
- QEMU `virtio-gpu-pci`, no virgl, `-vga none`
- Found by the wayland-vnc qualification suite, whose 4K scenario switches a live
  output from 1920x1080 to 3840x2160; reported here without anything of that project
  in the reproduction.
