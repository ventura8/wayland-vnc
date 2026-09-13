"""Native GTK Wayland scene: RGB targets, changing frame ID, input echo."""

import os

import gi


def _load_gtk():
    """Select the GTK 4 typelib before importing it (keeps imports call-ordered)."""
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    return GLib, Gtk


GLib, Gtk = _load_gtk()


class Scene(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.waylandvnc.Fixture")
        self.frame = 0
        self.events = 0
        self.keyboard_ack = False
        self.pointer_ack = False
        self.scroll_ack = False
        self.drag_ack = False
        self.label = Gtk.Label(label="Starting synthetic test scene")

    def do_activate(self):
        print("scene: activate", flush=True)
        window = Gtk.ApplicationWindow(application=self, title="Wayland VNC synthetic fixture")
        area = Gtk.DrawingArea()
        self.area = area
        area.set_content_height(500)
        area.set_draw_func(self.draw)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.append(area)
        box.append(self.label)
        entry = Gtk.Entry(placeholder_text="Type the test nonce here")
        entry.connect("changed", self.keyboard_event)
        box.append(entry)
        button = Gtk.Button(label="Click to acknowledge remote input")
        button.connect("clicked", self.pointer_event)
        box.append(button)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self.scroll_event)
        area.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self.drag_event)
        area.add_controller(drag)
        # Where the viewer's pointer last landed, as one line in the runtime directory:
        # a viewer with a relative (trackpad-style) pointer, such as the Android app,
        # positions itself by reading this back. It is a position report only; the
        # scenarios still pass or fail on the acknowledgement markers above.
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.pointer_moved)
        window.add_controller(motion)
        # Debug-only trace of raw button traffic over the whole window: where a
        # viewer's clicks land (a phone viewer that pans instead of pointing looks
        # identical from the outside otherwise).
        if os.environ.get("WAYLAND_VNC_SCENE_TRACE"):
            click = Gtk.GestureClick(button=0)
            for signal in ("pressed", "released"):
                click.connect(signal, self._trace_button, signal)
            window.add_controller(click)
        window.set_child(box)
        window.fullscreen()
        window.present()
        GLib.timeout_add(250, self.tick)

    def draw(self, _area, context, width, height):
        for index, color in enumerate(((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1))):
            context.set_source_rgb(*color)
            context.rectangle(index * width / 4, 0, width / 4, height)
            context.fill()

        marker_height = max(24, height / 8)
        if self.keyboard_ack:
            context.set_source_rgb(0, 1, 1)
            context.rectangle(0, height - marker_height, width / 2, marker_height)
            context.fill()
        if self.pointer_ack:
            context.set_source_rgb(1, 0, 1)
            context.rectangle(width / 2, height - marker_height, width / 2, marker_height)
            context.fill()
        # Gesture band above the input band: yellow for scroll, orange for a drag.
        gesture_top = height - 2 * marker_height
        if self.scroll_ack:
            context.set_source_rgb(1, 1, 0)
            context.rectangle(0, gesture_top, width / 2, marker_height)
            context.fill()
        if self.drag_ack:
            # 2/3 survives a viewer's rgb222 palette (0, 85, 170, 255) unchanged.
            context.set_source_rgb(1, 2 / 3, 0)
            context.rectangle(width / 2, gesture_top, width / 2, marker_height)
            context.fill()

    def keyboard_event(self, widget):
        if widget.get_text():
            self.keyboard_ack = True
        self.input_event(widget)

    def pointer_event(self, widget):
        self.pointer_ack = True
        self.input_event(widget)

    def scroll_event(self, controller, _dx, _dy):
        self.trace("scroll")
        self.scroll_ack = True
        self.input_event(controller)
        return True

    def drag_event(self, gesture, offset_x, offset_y):
        self.trace(f"drag-update {offset_x:.0f},{offset_y:.0f}")
        # Require a real drag, not a click with jitter.
        if abs(offset_x) > 100 or abs(offset_y) > 100:
            self.drag_ack = True
            self.input_event(gesture)

    def pointer_moved(self, _controller, x_pos, y_pos):
        self.trace(f"motion {x_pos:.0f},{y_pos:.0f}")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            report = os.path.join(runtime, "pointer")
            with open(report + ".new", "w", encoding="utf-8") as handle:
                handle.write(f"{x_pos:.0f} {y_pos:.0f}\n")
            os.replace(report + ".new", report)

    def _trace_button(self, gesture, _presses, x_pos, y_pos, signal):
        self.trace(f"{signal} b{gesture.get_current_button()} {x_pos:.0f},{y_pos:.0f}")

    @staticmethod
    def trace(message):
        # Event tracing for fixture debugging only; off unless explicitly requested.
        if os.environ.get("WAYLAND_VNC_SCENE_TRACE"):
            print(f"scene: {message}", flush=True)

    def input_event(self, _widget):
        self.events += 1
        self.area.queue_draw()

    def tick(self):
        self.frame += 1
        self.label.set_text(f"FRAME {self.frame:08d} | INPUT {self.events:08d}")
        return True


Scene().run()
