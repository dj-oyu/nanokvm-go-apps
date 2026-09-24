#!/usr/bin/env python3
"""Show the HDMI input on the NanoKVM-Go panel.

Frames are read straight from VIN's buffers in the AX common pool (no JPEG,
kvm_vin untouched), so the Web UI stream keeps working.

The picture area is detected inside the HDMI frame (a mirrored phone sits
in the middle of a black-padded 16:9 frame) and "long/short side" refer to
that area. Fit view: its long side fills the screen. Double tap: toggle a
zoomed view where its short side fills the screen. Swipe (zoomed): step the
visible part through 3 positions along each axis that overflows. Single taps
glow where they land. HDMI mode changes and kvm_vin restarts are followed.
"""

import os
import time

from appbase import app, rgb565

import nanokvm_vin as vin

# The panel hides 14 physical pixels. On the tested unit the framebuffer is
# 240x284 physical, so rotate=90 gives a 284x240 logical canvas whose left 14
# columns are not visible (appbase README describes the transposed layout).
DEAD_PX = 14
PAN_STEPS = 3  # left / centre / right (odd, so the centre is a stop)
DOUBLE_TAP_S = 0.35
DOUBLE_TAP_PX = 40
GLOW_LIFE = 0.4        # s
GLOW_RADIUS = (3, 12)  # px at start, at end


def blit_rgb565(fb, x0, y0, w, h, pixels, line_px=None):
    """Copy a w*h RGB565LE image into the back buffer at logical (x0, y0).

    pixels is any buffer of uint16 pixels with line_px pixels per line
    (default w), e.g. a padded swscale plane. appbase has no bitmap API and
    put_pixel() per pixel is far too slow, so this writes FrameBuffer._buf
    directly, one strided copy per line: under rotate 90/270 one logical
    column is one contiguous physical row run.
    """
    src = memoryview(pixels).cast("B").cast("H")
    ls = line_px or w
    dst = memoryview(fb._buf).cast("H")
    row = fb.stride // 2
    if fb.rotate == 90:
        base = fb.phys_w - (y0 + h)
        for x in range(w):
            off = (x0 + x) * row + base
            dst[off:off + h] = src[x:x + ls * h:ls][::-1]
    elif fb.rotate == 270:
        for x in range(w):
            off = (fb.phys_h - 1 - (x0 + x)) * row + y0
            dst[off:off + h] = src[x:x + ls * h:ls]
    elif fb.rotate == 0:
        for y in range(h):
            off = (y0 + y) * row + x0
            dst[off:off + w] = src[y * ls:y * ls + w]
    else:
        for y in range(h):
            for x in range(w):
                fb.put_pixel(x0 + x, y0 + y, src[y * ls + x])


def draw_disc(fb, cx, cy, r, color):
    # One vertical span per column: under rotate 90/270 that is a single
    # contiguous run in fill_rect.
    for dx in range(-r, r + 1):
        hh = int((r * r - dx * dx) ** 0.5)
        fb.fill_rect(cx + dx, cy - hh, 1, 2 * hh + 1, color)


def glow_color(a):
    """White -> amber as a goes 0 -> 1, staying bright until the very end."""
    k = 1.0 - a * a
    return rgb565(int(255 * k), int((255 - 90 * a) * k),
                  int(255 * (1.0 - a) ** 2 * k))


class View:
    """Where the frame goes on screen and which part of it is shown."""

    def __init__(self, vis_x, vis_w, vis_h):
        self.vis_x, self.vis_w, self.vis_h = vis_x, vis_w, vis_h
        self.src = None    # HDMI frame size
        self.area = None   # picture area inside it (x, y, w, h)
        self.zoom = False
        self.pan = [0, 0]  # step index along x, y

    def layout(self):
        """(dst_x, dst_y, dst_w, dst_h, crop) for the current state.

        Long/short side refer to the picture area (e.g. a mirrored phone in
        the middle of a black-padded 16:9 frame), not the whole frame.
        """
        ax, ay, aw, ah = self.area
        if not self.zoom:
            # Fit: the area's long side fills the screen.
            scale = min(self.vis_w / aw, self.vis_h / ah)
            w = max(2, int(aw * scale) & ~1)
            h = max(2, int(ah * scale) & ~1)
            return (self.vis_x + (self.vis_w - w) // 2,
                    (self.vis_h - h) // 2, w, h, self.area)
        # Cover: the area's short side fills the screen, the rest is cropped.
        scale = max(self.vis_w / aw, self.vis_h / ah)
        cw = min(aw, int(self.vis_w / scale)) & ~1
        ch = min(ah, int(self.vis_h / scale))
        x = ax + self._offset(aw - cw, self.pan[0])
        y = ay + self._offset(ah - ch, self.pan[1])
        return self.vis_x, 0, self.vis_w, self.vis_h, (x, y, cw, ch)

    @staticmethod
    def _offset(overflow, step):
        if overflow <= 0:
            return 0
        return int(overflow * min(step, PAN_STEPS - 1) / (PAN_STEPS - 1)) & ~1

    def steps(self):
        """Number of pan positions along x and y in the zoomed view."""
        _, _, aw, ah = self.area
        scale = max(self.vis_w / aw, self.vis_h / ah)
        return (PAN_STEPS if aw * scale > self.vis_w + 1 else 1,
                PAN_STEPS if ah * scale > self.vis_h + 1 else 1)

    def toggle_zoom(self, tx, ty):
        """Zoom in around the tapped point, or back out."""
        if self.zoom:
            self.zoom = False
            return
        dx, dy, dw, dh, _ = self.layout()
        fx = min(max((tx - dx) / dw, 0.0), 1.0)
        fy = min(max((ty - dy) / dh, 0.0), 1.0)
        nx, ny = self.steps()
        self.pan = [round(fx * (nx - 1)), round(fy * (ny - 1))]
        self.zoom = True

    def swipe(self, kind):
        # Drag semantics: swiping left reveals what is to the right.
        if not self.zoom:
            return False
        axis, delta = {"left": (0, 1), "right": (0, -1),
                       "up": (1, 1), "down": (1, -1)}[kind]
        limit = self.steps()[axis] - 1
        new = min(max(self.pan[axis] + delta, 0), limit)
        changed = new != self.pan[axis]
        self.pan[axis] = new
        return changed


@app()
def main(ctx):
    fb = ctx.fb
    if ctx.width > ctx.height:
        vis_x, vis_w = DEAD_PX, ctx.width - DEAD_PX
    else:
        vis_x, vis_w = 0, ctx.width
    view = View(vis_x, vis_w, ctx.height)
    reader = vin.RawFrameReader()
    scanner = vin.ContentScanner(reader)
    tracker = vin.ContentTracker()
    scaler = None
    state = {"layout": None, "last_tap": None, "glows": [], "dirty": False,
             "stalled_since": None}

    def relayout():
        nonlocal scaler
        src = reader.source_size()
        if src != view.src:
            view.src = src
            tracker.reset()
        if src is None:
            state["layout"] = None
            return
        view.area = tracker.rect or (0, 0) + src
        dx, dy, dw, dh, crop = view.layout()
        if scaler is None:
            scaler = vin.RawScaler(reader, dw, dh, crop=crop)
        else:
            scaler.set_view(dw, dh, crop)
        state["layout"] = (dx, dy, dw, dh)
        fb.clear(0)

    def on_tap(x, y):
        now = time.monotonic()
        state["glows"].append((x, y, now))
        last = state["last_tap"]
        if last and now - last[2] <= DOUBLE_TAP_S and \
                abs(x - last[0]) <= DOUBLE_TAP_PX and \
                abs(y - last[1]) <= DOUBLE_TAP_PX:
            if state["layout"]:
                view.toggle_zoom(x, y)
                relayout()
            state["last_tap"] = None
        else:
            state["last_tap"] = (x, y, now)

    def on_swipe(kind, x, y):
        if state["layout"] and kind in ("left", "right", "up", "down") and \
                view.swipe(kind):
            relayout()

    def draw_glows(now):
        # The preview is redrawn every frame, which erases glows on it; glows
        # reaching the letterbox bars are erased by clearing the screen on
        # the next frame (cheap, and rare).
        dx, dy, dw, dh = state["layout"]
        alive, outside = [], False
        for x, y, t0 in state["glows"]:
            a = (now - t0) / GLOW_LIFE
            if a >= 1.0:
                continue
            alive.append((x, y, t0))
            r = int(GLOW_RADIUS[0] + (GLOW_RADIUS[1] - GLOW_RADIUS[0]) * a)
            draw_disc(fb, x, y, r, glow_color(a))
            outside |= (x - r < dx or x + r >= dx + dw or
                        y - r < dy or y + r >= dy + dh)
        state["glows"] = alive
        return outside

    def no_frame(now):
        # HDMI is stable but no frame arrives: kvm_vin may have restarted
        # and rebuilt the common pool somewhere else.
        since = state["stalled_since"]
        if since is None:
            state["stalled_since"] = now
        elif now - since > 2.0:
            state["stalled_since"] = now
            if reader.refresh():
                view.src = None
        if state["layout"] is not None and reader.source_size() is None:
            state["layout"] = None
            fb.clear(0)
        time.sleep(0.05)

    def tick(dt):
        now = time.monotonic()
        if vin.hdmi_status()[0] != "stable":
            if state["layout"] is not None:
                state["layout"] = None
                fb.clear(0)
            time.sleep(0.05)
            return
        if state["layout"] is None or view.src != reader.source_size():
            relayout()
            if state["layout"] is None:
                no_frame(now)
                return
        done, rect = scanner.step(now)
        if done and tracker.update(rect, now):
            relayout()
        # A copy that VIN overwrote mid-way (process descheduled) is
        # dropped; None means no frame or a mode change under the read.
        got = scaler.grab(timeout=0.5)
        if got is None:
            no_frame(now)
            return
        state["stalled_since"] = None
        if not got[0].consistent:
            return
        if state["dirty"]:
            fb.clear(0)
        _, pixels, line_px = got
        dx, dy, dw, dh = state["layout"]
        blit_rgb565(fb, dx, dy, dw, dh, pixels, line_px)
        state["dirty"] = draw_glows(time.monotonic())

    # The panel is an fbtft SPI display with deferred I/O: while it pushes a
    # frame (~22 ms), writes through appbase's mmap fault on locked pages and
    # stall for up to that long. write(2) copies into the same memory without
    # page faults and never blocks, and the panel still refreshes at ~45 fps.
    fb.flush = lambda: os.pwrite(fb._fd, fb._buf, 0)
    fb.clear(0)
    try:
        # Unpaced: grab() already blocks until the next VIN frame, and any
        # sleep() here would last ~10 ms (no high-res timers).
        ctx.run(tick, fps=0, on_tap=on_tap, on_swipe=on_swipe)
    finally:
        if scaler:
            scaler.close()
        reader.close()


if __name__ == "__main__":
    main()
