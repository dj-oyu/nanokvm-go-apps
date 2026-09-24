"""NanoKVM-Go kvm_vin client (stdlib only; PyAV optional for decoding).

kvm_vin (/kvmcomm/vin/kvm_vin) owns HDMI capture (LT7911 -> AX VIN -> VENC/JENC)
and exposes three Unix sockets under /run/kvm:

  vin_snapshot.sock  JPEG snapshots. Request: one JSON line. Response: one JSON
                     line, then exactly ``size`` bytes of JPEG. Coexists with
                     the Web UI stream.
  vin_video.sock     H.264/H.265 elementary stream, 64-byte "KVVF" header per
                     frame. Single reader, last connection wins: connecting
                     disconnects the current reader (normally NanoKVM-Server).
  vin_ctrl.sock      Encoder control. Request: one JSON object. Response: one
                     JSON object.

The protocol was reverse-engineered from kvm_vin/libkvm.so built 2026-09-04
(git aeac1d31); it is not a documented API and may change with firmware.
"""

import ctypes
import json
import os
import socket
import struct
import time
from collections import namedtuple

SNAPSHOT_SOCK = "/run/kvm/vin_snapshot.sock"
VIDEO_SOCK = "/run/kvm/vin_video.sock"
CTRL_SOCK = "/run/kvm/vin_ctrl.sock"
LT7911_PROC = "/proc/lt7911_info"

SNAPSHOT_VERSION = 1


class VinError(RuntimeError):
    def __init__(self, header):
        self.header = header
        super().__init__("%s (ret_code=%s)" % (header.get("message"),
                                               header.get("ret_code")))


# ---------------------------------------------------------------------------
# HDMI input state
# ---------------------------------------------------------------------------

def hdmi_status():
    """Return (status, width, height, fps); status is e.g. 'stable'/'disappear'."""
    def rd(name):
        try:
            with open("%s/%s" % (LT7911_PROC, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def num(name):
        v = rd(name)
        return int(v) if v.isdigit() else 0

    return rd("status"), num("width"), num("height"), num("fps")


# ---------------------------------------------------------------------------
# Snapshot socket
# ---------------------------------------------------------------------------

def _snapshot_request(req, sock_timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(sock_timeout)
    try:
        s.connect(SNAPSHOT_SOCK)
        # The server rejects requests without a trailing newline and handles
        # one request per connection.
        s.sendall((json.dumps(req) + "\n").encode())
        f = s.makefile("rb")
        header = json.loads(f.readline())
        size = header.get("size", 0)
        body = f.read(size) if size else b""
        if len(body) != size:
            raise EOFError("short JPEG body: %d/%d" % (len(body), size))
        return header, body
    finally:
        s.close()


def capture_fresh(quality=None, timeout_ms=None, crop=None):
    """Capture a new JPEG. Returns (header, jpeg_bytes).

    quality: 1..99 (server default ~ larger/slower). timeout_ms: 1..30000,
    server default 1000. crop: optional (x, y, w, h) in source pixels, cropped
    in hardware (IVPS) before JPEG encoding; there is no scaling. Crops that
    IVPS rejects fail with 'IVPS config failed' (ret_code -15): odd x/y/w/h,
    w/h below ~34, and rects touching the right/bottom edge all failed on a
    2560x1440 input. Raises VinError when ok is false, e.g.
    'snapshot timeout' (ret_code -13) while there is no HDMI signal.

    header keys: ok, ret_code, message, version, width, height, size,
    session_id, capture_id, captured_at (UTC ISO8601), source.
    """
    req = {"version": SNAPSHOT_VERSION, "cmd": "capture_fresh"}
    if quality is not None:
        req["quality"] = int(quality)
    if timeout_ms is not None:
        req["timeout_ms"] = int(timeout_ms)
    if crop is not None:
        req["x"], req["y"], req["w"], req["h"] = (int(v) for v in crop)
    wait = (timeout_ms or 1000) / 1000.0 + 5.0
    header, body = _snapshot_request(req, wait)
    if not header.get("ok"):
        raise VinError(header)
    return header, body


def get_latest(wait_ms=0, session_id=None, after_sequence=None):
    """Long-poll the latest "canonical" frame (used by nano_ocr_app).

    Returns (header, jpeg_bytes) or (header, None) when header['not_modified'].
    Canonical frames are only published by kvm_vin's periodic snapshot worker
    ([snapshot] periodic_enable in /etc/kvm/kvm_vin.toml), so with the default
    config this always ends in not_modified. Prefer capture_fresh().
    """
    req = {"version": SNAPSHOT_VERSION, "cmd": "get_latest",
           "wait_ms": int(wait_ms)}
    if session_id is not None:
        req["session_id"] = session_id
    if after_sequence is not None:
        req["after_sequence"] = int(after_sequence)
    header, body = _snapshot_request(req, wait_ms / 1000.0 + 5.0)
    if not header.get("ok"):
        raise VinError(header)
    if header.get("not_modified"):
        return header, None
    return header, body


# ---------------------------------------------------------------------------
# Video socket
# ---------------------------------------------------------------------------

# 64-byte little-endian header; bytes 48..63 are reserved (zero).
_KVVF = struct.Struct("<4sHHIII4sIQQI")
VideoFrame = namedtuple(
    "VideoFrame",
    "version width height codec keyframe pts_us seq fps payload")


def video_frames(timeout=5.0):
    """Yield VideoFrame from vin_video.sock.

    payload is Annex-B (00 00 00 01 start codes). The first frame after
    connecting is an IDR with SPS/PPS. codec is 'H264' or 'H265' depending on
    [device] mode. WARNING: this steals the stream from the Web UI viewer;
    the generator ends when another client connects.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(VIDEO_SOCK)
    f = s.makefile("rb")
    try:
        while True:
            head = f.read(64)
            if len(head) < 64:
                return
            (magic, ver, hsize, size, w, h, codec, flags, pts, seq,
             fps) = _KVVF.unpack_from(head)
            if magic != b"KVVF":
                raise ValueError("bad magic %r" % magic)
            if hsize > 64:
                f.read(hsize - 64)
            payload = f.read(size)
            if len(payload) < size:
                return
            yield VideoFrame(ver, w, h, codec.decode(), bool(flags & 1), pts,
                             seq, fps, payload)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Control socket (affects the shared Web UI stream)
# ---------------------------------------------------------------------------

def control(cmd, **params):
    """Send one control command; returns the reply dict or raises VinError.

    Commands (from libkvm.so):
      request_idr  instant=bool
      set_fps      src_fps=float, dst_fps=float
      set_gop      gop=int
      set_bitrate  bitrate=int (kbps)
      set_rc_mode  mode=int
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(CTRL_SOCK)
        s.sendall(json.dumps(dict(cmd=cmd, **params)).encode())
        reply = json.loads(s.recv(4096))
    finally:
        s.close()
    if not reply.get("ok"):
        raise VinError(reply)
    return reply


# ---------------------------------------------------------------------------
# Raw frames straight from the VIN common pool (no JPEG, read-only)
# ---------------------------------------------------------------------------

class _VideoFrameMeta(ctypes.Structure):
    """AX_VIDEO_FRAME_T (ax_global_type.h, AX620E MSP V3.0.0).

    VIN keeps one of these in each common-pool block's 4 KiB meta area and
    updates it live: the newest finished frame has fmt=YUYV and pts != 0,
    blocks queued for writing carry the next sequence numbers.
    """
    _fields_ = [
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("fmt", ctypes.c_int), ("vscan", ctypes.c_int),
        ("compress_mode", ctypes.c_int), ("compress_level", ctypes.c_uint32),
        ("dynamic_range", ctypes.c_int), ("color_gamut", ctypes.c_int),
        ("pic_stride", ctypes.c_uint32 * 3), ("ext_stride", ctypes.c_uint32 * 3),
        ("phy_addr", ctypes.c_uint64 * 3), ("vir_addr", ctypes.c_uint64 * 3),
        ("ext_phy_addr", ctypes.c_uint64 * 3),
        ("ext_vir_addr", ctypes.c_uint64 * 3),
        ("header_size", ctypes.c_uint32 * 3), ("blk_id", ctypes.c_uint32 * 3),
        ("crop_x", ctypes.c_int16), ("crop_y", ctypes.c_int16),
        ("crop_w", ctypes.c_int16), ("crop_h", ctypes.c_int16),
        ("time_ref", ctypes.c_uint32), ("pts", ctypes.c_uint64),
        ("seq", ctypes.c_uint64), ("user_data", ctypes.c_uint64),
        ("private_data", ctypes.c_uint64), ("frame_flag", ctypes.c_uint32),
        ("frame_size", ctypes.c_uint32),
    ]


AX_FORMAT_YUV422_INTERLEAVED_YUYV = 0xD
RawFrame = namedtuple("RawFrame", "width height seq pts_us consistent data")


class RawFrameReader:
    """Read VIN output frames (YUYV422) directly from the AX common pool.

    kvm_vin is not touched: no VIN/link/refcount API is called, the blocks are
    only mmapped (cached) and read. The cost is that nothing pins the block:
    VIN starts overwriting a finished frame about one frame period (~17 ms at
    60 Hz) after it completes, which is also when the block's sequence number
    changes. read() therefore only copies a frame that finished moments ago
    (or waits for the next one); RawFrame.consistent is False if the sequence
    number changed during the copy anyway (e.g. the process was descheduled).
    Resolution changes: kvm_vin creates the common pool once (blocks sized
    for 3840x2160 YUYV) and on an HDMI mode change only rebuilds the VIN
    pipeline, which may then use other blocks of the pool and restart its
    sequence numbers. So every block is mapped, validity is checked per read
    from its meta, frames are ordered by pts (monotonic across the rebuild)
    rather than seq, and geometry/stride come from the meta of each frame.
    If kvm_vin itself restarts, the pool may move; refresh() remaps it.

    Depends on /proc/ax_proc/pool and the pool layout observed on firmware
    kvm_vin 2026-09-04 / ax_sys V3.0.0.
    """

    def __init__(self):
        sysl = ctypes.CDLL("/opt/lib/libax_sys.so")
        sysl.AX_SYS_Mmap.restype = ctypes.c_void_p
        sysl.AX_SYS_Mmap.argtypes = [ctypes.c_uint64, ctypes.c_uint32]
        sysl.AX_SYS_MmapCache.restype = ctypes.c_void_p
        sysl.AX_SYS_MmapCache.argtypes = [ctypes.c_uint64, ctypes.c_uint32]
        sysl.AX_SYS_Munmap.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        sysl.AX_SYS_MinvalidateCache.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32]
        sysl.AX_SYS_GetCurPTS.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
        if sysl.AX_SYS_Init() != 0:
            raise OSError("AX_SYS_Init failed")
        self._sys = sysl
        self._maps = []
        self._blocks = []  # (meta view, data phys, data virt, block size)
        self._pool = None
        self._period_us = 16667  # refined from consecutive frames
        self._last_pts = 0       # frame returned by the previous read()
        self._bright_tables = {}
        self.refresh()

    def refresh(self):
        """Remap the pool if /proc/ax_proc/pool describes a different one.

        Returns True if it was remapped. Call it when frames stop arriving
        although HDMI is stable (kvm_vin restarted and rebuilt the pool).
        """
        pool = self._common_pool()
        if pool == self._pool:
            return False
        self._unmap()
        base, meta_size, blk_size, blk_cnt = pool
        data_base = base + meta_size * blk_cnt
        for i in range(blk_cnt):
            meta_va = self._map(self._sys.AX_SYS_Mmap, base + i * meta_size,
                                meta_size)
            data_va = self._map(self._sys.AX_SYS_MmapCache,
                                data_base + i * blk_size, blk_size)
            self._blocks.append((_VideoFrameMeta.from_address(meta_va),
                                 data_base + i * blk_size, data_va, blk_size))
        self._pool = pool
        self._last_pts = 0
        return True

    @staticmethod
    def _common_pool():
        with open("/proc/ax_proc/pool") as f:
            lines = f.read().split("ALL POOL INFO", 1)[1].splitlines()
        for line in lines:
            cols = line.split()
            # PoolId IsComm IsCache Partition PhysAddr MetaSize BlkSize BlkCnt
            if len(cols) >= 8 and cols[0].isdigit() and cols[1] == "1":
                return (int(cols[4], 16), int(cols[5]), int(cols[6]),
                        int(cols[7]))
        raise OSError("common pool not found in /proc/ax_proc/pool")

    def _map(self, fn, phys, size):
        va = fn(phys, size)
        if not va:
            raise OSError("AX_SYS_Mmap(0x%x) failed" % phys)
        self._maps.append((va, size))
        return va

    def _unmap(self):
        for va, size in self._maps:
            self._sys.AX_SYS_Munmap(ctypes.c_void_p(va), size)
        self._maps = []
        self._blocks = []

    def close(self):
        self._unmap()
        self._pool = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _row_bytes(m):
        # pic_stride is in pixels (2560 for a 2560-wide YUYV frame); fall
        # back to the width if it is missing.
        return max(m.pic_stride[0], m.width) * 2

    def _valid(self, blk):
        m, phys, _, size = blk
        # A block the pool never handed out, or one VIN is not using for
        # YUYV output, has stale or foreign meta.
        return (m.fmt == AX_FORMAT_YUV422_INTERLEAVED_YUYV and m.pts and
                m.phy_addr[0] == phys and m.width and m.height and
                self._row_bytes(m) * m.height <= size)

    def _latest(self):
        best = None
        for blk in self._blocks:
            if self._valid(blk) and (best is None or blk[0].pts > best[0].pts):
                best = blk
        return best

    def _now_us(self):
        now = ctypes.c_uint64()
        self._sys.AX_SYS_GetCurPTS(ctypes.byref(now))
        return now.value

    def _wait_frame(self, timeout, max_age_us):
        cur = self._latest()
        last = self._last_pts
        if cur and max_age_us and cur[0].pts > last and \
                self._now_us() - cur[0].pts <= max_age_us:
            return cur
        deadline = time.monotonic() + timeout
        # Snapshot: the meta views change as VIN recycles the blocks.
        start_seq, start_pts = (cur[0].seq, cur[0].pts) if cur else (-1, 0)
        while True:
            blk = self._latest()
            if blk and blk[0].pts > max(start_pts, last):
                period = blk[0].pts - start_pts
                if blk[0].seq == start_seq + 1 and 5000 < period < 50000:
                    self._period_us = period
                return blk
            if time.monotonic() > deadline:
                return None
            # The kernel has no high-res timers: any time.sleep(), even
            # sleep(0), lasts ~10 ms. Sleep only when the next frame is
            # further away than that, otherwise just yield the CPU.
            due_us = start_pts + self._period_us - self._now_us()
            if start_pts and due_us > 12000:
                time.sleep(0.001)
            else:
                os.sched_yield()

    def source_size(self):
        """(width, height) of the newest VIN frame, or None if there is none."""
        blk = self._latest()
        return (blk[0].width, blk[0].height) if blk else None

    def geometry(self, row_step=1, col_step=1, crop=None, size=None):
        """(width, height) of read() output for a source of `size`
        (default: the current one), or None if there is no frame."""
        size = size or self.source_size()
        if size is None:
            return None
        _, _, cw, ch = self._crop(crop, size)
        return len(range(0, cw // 2, col_step)) * 2, len(range(0, ch, row_step))

    @staticmethod
    def _crop(crop, size):
        w, h = size
        if crop is None:
            return 0, 0, w, h
        x, y, cw, ch = crop
        # Whole macropixels only, clamped to the frame.
        x = max(0, min(x, w - 2)) & ~1
        y = max(0, min(y, h - 1))
        return x, y, max(2, min(cw, w - x)) & ~1, max(1, min(ch, h - y))

    def read(self, row_step=1, col_step=1, timeout=1.0, max_age_us=9000,
             out=None, out_line_size=None, crop=None, expect_size=None):
        """Copy the newest finished frame, cropped and decimated, in one pass.

        A finished frame is used only if it completed at most max_age_us ago
        (the copy must end before VIN reuses the block ~17 ms after
        completion) and is newer than the one returned by the previous
        read(); otherwise read() waits for the next one. crop is
        (x, y, w, h) in source pixels (x/w rounded to even). row_step keeps
        every row_step-th line, col_step every col_step-th YUYV macropixel.
        8/4 on a full 2560x1440 frame -> 640x180 in ~5.6 ms. The rows are
        written straight into ``out`` (any writable buffer, e.g. a PyAV
        plane, with out_line_size bytes per line) or into a new bytearray.

        Returns None on timeout (no HDMI signal). Raises SourceChanged if
        the frame is not expect_size or does not fit ``out`` (the HDMI mode
        changed since the caller sized its buffers).
        """
        blk = self._wait_frame(timeout, max_age_us)
        if blk is None:
            return None
        meta, phys, va, size = blk
        seq, pts, w, h = meta.seq, meta.pts, meta.width, meta.height
        row = self._row_bytes(meta)
        if expect_size is not None and (w, h) != tuple(expect_size):
            raise SourceChanged((w, h))
        cx, cy, cw, ch = self._crop(crop, (w, h))
        out_w, out_h = self.geometry(row_step, col_step, crop, (w, h))
        if out is None:
            out_line_size = out_w * 2
            out = bytearray(out_line_size * out_h)
        elif out_line_size < out_w * 2 or \
                len(memoryview(out).cast("B")) < out_line_size * out_h:
            raise SourceChanged((w, h))
        self._sys.AX_SYS_MinvalidateCache(phys + cy * row,
                                          ctypes.c_void_p(va + cy * row),
                                          ch * row)
        # 32-bit elements = one YUYV macropixel, so a strided slice decimates
        # horizontally inside the same copy. Rows may be padded (pic_stride).
        src = memoryview((ctypes.c_char * (h * row)).from_address(va)
                         ).cast("B").cast("I")
        dst = memoryview(out).cast("B").cast("I")
        src_line, dst_line, n = row // 4, out_line_size // 4, out_w // 2
        x0, x1 = cx // 2, (cx + cw) // 2
        for i, y in enumerate(range(cy, cy + ch, row_step)):
            base = y * src_line
            dst[i * dst_line:i * dst_line + n] = \
                src[base + x0:base + x1:col_step]
        consistent = meta.seq == seq and meta.width == w
        self._last_pts = pts
        return RawFrame(out_w, out_h, seq, pts, consistent, out)


    def content_rect(self, threshold=24, row_step=8, tol=8):
        """Picture area (x, y, w, h) inside the newest frame, to within tol px.

        A mirrored phone in portrait is centred in the 16:9 frame with black
        (limited-range Y=16) bars left and right; in landscape it fills the
        frame. There is never padding above/below, so black rows at the top
        or bottom are picture content (e.g. a letterboxed movie) and are
        kept, and only the left bar is measured (the right one mirrors it).

        The bar edge is binary-searched over the columns of the left half:
        a column is "bright" if any of its pixels on every row_step-th row
        exceeds `threshold`. One probe is a strided slice of one column
        (180 pixels at 1440 lines) put through bytes.translate()/find(), so
        ~8 probes cost ~0.7 ms (+0.7 ms cache invalidation) instead of ~8 ms
        for scanning rows. The search stops once the edge is known within
        `tol` px and keeps the last column known to be black, so the area
        errs on the side of including a few pixels of bar rather than
        cutting picture.

        Returns None if there is no frame or the centre columns are black
        too (a picture black up to exactly the centre is practically only a
        fade to black). x and w are even (whole YUYV macropixels).
        """
        blk = self._latest()
        if blk is None:
            return None
        meta, phys, va, _ = blk
        w, h, row = meta.width, meta.height, self._row_bytes(meta)
        self._sys.AX_SYS_MinvalidateCache(phys, ctypes.c_void_p(va), h * row)
        frame = memoryview((ctypes.c_char * (h * row)).from_address(va)
                           ).cast("B")
        table = self._table(threshold)
        step = row * row_step

        def bright(x):
            return frame[2 * x:h * row:step].tobytes().translate(table)                 .find(1) >= 0

        # Invariant: column lo is black (bar), column hi is bright (picture).
        hi = next((x for x in (w // 2 - 1, w // 2 - 33, w // 2 - 97)
                   if bright(x)), None)
        if hi is None:
            return None
        if bright(0):
            return 0, 0, w, h  # no bars (landscape or a full-frame source)
        lo = 0
        while hi - lo > tol:
            mid = (lo + hi) // 2
            if bright(mid):
                hi = mid
            else:
                lo = mid
        left = lo & ~1
        return left, 0, w - 2 * left, h

    def _table(self, threshold):
        table = self._bright_tables.get(threshold)
        if table is None:
            table = bytes(1 if v > threshold else 0 for v in range(256))
            self._bright_tables[threshold] = table
        return table


class ContentScanner:
    """Calls content_rect() every `interval` seconds (~1.4 ms each)."""

    def __init__(self, reader, interval=0.5, **kwargs):
        self.reader, self.interval, self.kwargs = reader, interval, kwargs
        self._next = 0.0

    def step(self, now):
        """Returns (done, rect): done is True when a detection ran (rect may
        then be None: no frame, or a fade to black)."""
        if now < self._next:
            return False, None
        self._next = now + self.interval
        return True, self.reader.content_rect(**self.kwargs)


class ContentTracker:
    """Smooths content_rect() results into a stable picture area.

    Growing (more of the picture became bright) is applied at once, as the
    union with the current area. Shrinking is applied only after the same
    smaller rect has been seen for `shrink_after` seconds, so dark content
    near the edges (dark mode UI, a black video scene) does not make the
    area jump around. A large shrink (width or height down by more than
    `big` of the current area, e.g. a phone rotated from landscape back to
    portrait) is applied after `big_confirm` consecutive identical results
    instead. Differences within `tol` pixels are ignored.
    """

    def __init__(self, tol=8, shrink_after=2.0, big=0.25, big_confirm=2):
        self.tol, self.shrink_after = tol, shrink_after
        self.big, self.big_confirm = big, big_confirm
        self.rect = None
        self._pending = None
        self._since = 0.0
        self._seen = 0

    def reset(self):
        self.rect, self._pending = None, None

    def _near(self, a, b):
        return all(abs(p - q) <= self.tol for p, q in zip(_edges(a), _edges(b)))

    def update(self, found, now):
        """Feed one content_rect() result; returns True if rect changed."""
        if found is None:
            return False
        if self.rect is None:
            self.rect, self._pending = found, None
            return True
        cur, new = _edges(self.rect), _edges(found)
        grow = (new[0] < cur[0] - self.tol or new[1] < cur[1] - self.tol or
                new[2] > cur[2] + self.tol or new[3] > cur[3] + self.tol)
        if grow:
            x0, y0 = min(cur[0], new[0]), min(cur[1], new[1])
            x1, y1 = max(cur[2], new[2]), max(cur[3], new[3])
            self.rect, self._pending = (x0, y0, x1 - x0, y1 - y0), None
            return True
        if self._near(found, self.rect):
            self._pending = None
            return False
        if self._pending is None or not self._near(found, self._pending):
            self._pending, self._since, self._seen = found, now, 1
            return False
        self._seen += 1
        big = (found[2] < self.rect[2] * (1 - self.big) or
               found[3] < self.rect[3] * (1 - self.big))
        if (big and self._seen >= self.big_confirm) or \
                now - self._since >= self.shrink_after:
            self.rect, self._pending = self._pending, None
            return True
        return False


def _edges(r):
    x, y, w, h = r
    return x, y, x + w, y + h


class SourceChanged(Exception):
    """The HDMI mode changed under a read(); args[0] is the new (w, h)."""


class _Swscale:
    """libswscale via ctypes, without importing PyAV.

    `import av` maps PyAV's whole bundled FFmpeg (83 shared objects, ~58 MB:
    libavcodec, SVT-AV1, gnutls, libvpx, ...) and takes 10-20 s on this
    119 MB device once memory is tight. Scaling needs only libswscale and
    libavutil, which PyAV's wheel ships as small standalone libraries
    (~1.4 MB, depending on libc/libm only), so they are loaded directly.
    """

    SWS_FAST_BILINEAR = 1
    _GLOBS = ("/usr/local/lib/python3*/dist-packages/av.libs",
              "/usr/lib/python3*/dist-packages/av.libs")
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        import glob

        paths = None
        for pattern in self._GLOBS:
            for d in glob.glob(pattern):
                u = glob.glob(d + "/libavutil-*.so*")
                s = glob.glob(d + "/libswscale-*.so*")
                if u and s:
                    paths = u[0], s[0]
                    break
            if paths:
                break
        if paths:
            avutil = ctypes.CDLL(paths[0], mode=ctypes.RTLD_GLOBAL)
            sws = ctypes.CDLL(paths[1])
        else:  # a distro FFmpeg instead of the PyAV wheel
            from ctypes.util import find_library

            avutil = ctypes.CDLL(find_library("avutil"),
                                 mode=ctypes.RTLD_GLOBAL)
            sws = ctypes.CDLL(find_library("swscale"))
        avutil.av_get_pix_fmt.argtypes = [ctypes.c_char_p]
        avutil.av_get_pix_fmt.restype = ctypes.c_int
        sws.sws_getContext.argtypes = [ctypes.c_int] * 7 + [ctypes.c_void_p] * 3
        sws.sws_getContext.restype = ctypes.c_void_p
        sws.sws_freeContext.argtypes = [ctypes.c_void_p]
        ptrs, ints = ctypes.c_void_p * 4, ctypes.c_int * 4
        sws.sws_scale.argtypes = [ctypes.c_void_p, ptrs, ints, ctypes.c_int,
                                  ctypes.c_int, ptrs, ints]
        sws.sws_scale.restype = ctypes.c_int
        self.lib = sws
        self.fmt = {name: avutil.av_get_pix_fmt(name.encode())
                    for name in ("yuyv422", "rgb565le")}


def _aligned(size, align=32):
    """(owner, writable memoryview, address) of `size` bytes aligned to
    `align`, so swscale's NEON paths can use aligned loads/stores."""
    raw = bytearray(size + align)
    addr = ctypes.addressof(ctypes.c_char.from_buffer(raw))
    off = (-addr) % align
    return raw, memoryview(raw)[off:off + size], addr + off


class RawScaler:
    """RawFrameReader -> RGB565LE at a fixed output size, minimal copying.

    Per frame: one decimating copy from the pool into an aligned buffer,
    then one sws_scale() (FAST_BILINEAR: on this SoC ~1.5x faster than the
    default BILINEAR and no slower than POINT or a 1:1 colour conversion)
    into another. The SwsContext and both buffers are reused until the
    geometry changes; libswscale is called through ctypes (see _Swscale).

    grab() returns the output as a memoryview of uint16 pixels plus its line
    stride in pixels (lines are padded), valid until the next grab().
    """

    def __init__(self, reader, width, height, row_step=None, col_step=None,
                 crop=None):
        self.reader = reader
        self._sws = _Swscale.get()
        self._ctx = None
        self._key = None
        self.set_view(width, height, crop, row_step, col_step)

    def set_view(self, width, height, crop=None, row_step=None, col_step=None):
        """Change the output size / source crop.

        Without explicit steps they are chosen so the decimated source has
        about 2x the output width and 1x its height, which keeps the copy
        within VIN's reuse window while leaving swscale something to filter.
        """
        self.width, self.height, self.crop = width, height, crop
        if row_step is None or col_step is None:
            _, _, cw, ch = self.reader._crop(
                crop, self.reader.source_size() or (3840, 2160))
            row_step = max(1, ch // height)
            col_step = max(1, cw // (2 * width))
        self.row_step, self.col_step = row_step, col_step

    def close(self):
        if self._ctx:
            self._sws.lib.sws_freeContext(self._ctx)
        self._ctx = self._key = None

    def _prepare(self, sw, sh):
        key = (sw, sh, self.width, self.height)
        if key == self._key:
            return
        self.close()
        ctx = self._sws.lib.sws_getContext(
            sw, sh, self._sws.fmt["yuyv422"], self.width, self.height,
            self._sws.fmt["rgb565le"], _Swscale.SWS_FAST_BILINEAR,
            None, None, None)
        if not ctx:
            raise OSError("sws_getContext(%dx%d -> %dx%d) failed" % key)
        src_line = (sw * 2 + 31) & ~31
        dst_line = (self.width * 2 + 31) & ~31
        self._src = _aligned(src_line * sh) + (src_line,)
        self._dst = _aligned(dst_line * self.height) + (dst_line,)
        ptrs, ints = ctypes.c_void_p * 4, ctypes.c_int * 4
        self._args = (ptrs(self._src[2]), ints(src_line),
                      ptrs(self._dst[2]), ints(dst_line))
        self._ctx, self._key = ctx, key

    def grab(self, timeout=1.0):
        """Returns (RawFrame, pixels, line_px), or None if there is no frame
        or the HDMI mode changed under the read (check source_size() and
        call set_view() again)."""
        size = self.reader.source_size()
        if size is None:
            return None
        sw, sh = self.reader.geometry(self.row_step, self.col_step, self.crop,
                                      size)
        self._prepare(sw, sh)
        _, src_view, _, src_line = self._src
        try:
            frame = self.reader.read(self.row_step, self.col_step, timeout,
                                     out=src_view, out_line_size=src_line,
                                     crop=self.crop, expect_size=size)
        except SourceChanged:
            return None
        if frame is None:
            return None
        src_ptrs, src_strides, dst_ptrs, dst_strides = self._args
        self._sws.lib.sws_scale(self._ctx, src_ptrs, src_strides, 0, sh,
                                dst_ptrs, dst_strides)
        _, dst_view, _, dst_line = self._dst
        return frame, dst_view.cast("H"), dst_line // 2


def yuyv_to_rgb565(frame, width, height):
    """Scale a RawFrame from read() to packed width x height RGB565LE bytes.

    Convenience path; RawScaler avoids the extra copies.
    """
    import av

    vf = av.VideoFrame(frame.width, frame.height, "yuyv422")
    vf.planes[0].update(frame.data)
    return _packed_plane(vf.reformat(width=width, height=height,
                                     format="rgb565le"), width, height)


def _packed_plane(frame, width, height):
    plane = frame.planes[0]
    row = width * 2
    data = bytes(plane)
    if plane.line_size == row:
        return data
    return b"".join(data[y * plane.line_size:y * plane.line_size + row]
                    for y in range(height))


# ---------------------------------------------------------------------------
# Decoding helpers (need PyAV, installed from pip on the stock image;
# imported lazily because it is slow to load, see _Swscale)
# ---------------------------------------------------------------------------

def jpeg_to_rgb565(jpeg, width, height, lowres=3):
    """Decode a JPEG and scale it to width x height RGB565LE bytes.

    lowres=3 makes the MJPEG decoder output 1/8 size (2560x1440 -> 320x180),
    which is what keeps this at ~20 ms on the device.
    """
    import av

    ctx = av.CodecContext.create("mjpeg", "r")
    ctx.thread_count = 1
    if lowres:
        ctx.options = {"lowres": str(lowres)}
    frames = ctx.decode(av.Packet(jpeg))
    if not frames:
        raise ValueError("JPEG decode produced no frame")
    return _packed_plane(frames[0].reformat(width=width, height=height,
                                            format="rgb565le"), width, height)
