"""
==============================================================================
 sound-bt: Bluetooth audio bridge (radio voice in/out + PTT)
 Author:  dkxce (c)  https://github.com/dkxce/Tidradio-TD-H9
 Version: v1.0.0

 English:  Core audio engine. Lists audio devices (filtering Bluetooth),
           plays an audio file (MP3/WAV/OGG) to a selected output device,
           captures live input (microphone / radio SCO voice) and feeds it to
           an output channel while PTT is held. Uses sounddevice (PortAudio)
           and miniaudio for decoding.
 Russian:  Ядро звукового моста. Список аудиоустройств (с фильтром по
           Bluetooth), воспроизведение файла на выбранное устройство вывода,
           захват живого входа (микрофон/голос рации по SCO) и передача его
           в выходной канал, пока удерживается PTT.
==============================================================================
"""

from __future__ import annotations

import os
import queue
import threading
import time

import numpy as np
import sounddevice as sd

# Audio parameters for the radio/SCO path.
SAMPLE_RATE = 8000     # hands-free SCO typically runs at 8 kHz
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 512

# Substrings used to spot Bluetooth audio endpoints in the device list.
BT_HINTS = (
    "bluetooth",
    "hands-free",
    "sco",
    "hsp",
    "hfp",
    "a2dp",
    "wireless",
)


def is_bt_name(name: str) -> bool:
    """Heuristic: is this device name likely a Bluetooth endpoint?"""
    n = (name or "").lower()
    return any(h in n for h in BT_HINTS)


def is_td_h9_name(name: str) -> bool:
    """Does this endpoint belong to a Tidradio TD-H9 hands-free device?"""
    n = (name or "").lower()
    return "td-h9" in n or "tidradio" in n


def _device_rate(index: int | None) -> int:
    """Return default sample rate of a device (fallback 44100)."""
    if index is None:
        return 44100
    try:
        return int(sd.query_devices(index)["default_samplerate"])
    except Exception:
        return 44100


class AudioDevice:
    """A single audio endpoint (input and/or output side), wrapping one entry
    from sounddevice's device table together with helper flags."""

    def __init__(self, index: int, name: str, max_in: int, max_out: int,
             default_in: bool = False, default_out: bool = False,
             default_sr: float = 44100.0):
        self.index = index              # sounddevice device index
        self.name = name                # display name (hostapi tagged)
        self.max_in = max_in            # max input channels
        self.max_out = max_out          # max output channels
        self.default_in = default_in    # True if this is the system default input
        self.default_out = default_out  # True if this is the system default output
        self.default_sr = float(default_sr)

    @property
    def is_bt(self) -> bool:
        """True if the device name suggests a Bluetooth endpoint."""
        return is_bt_name(self.name)

    @property
    def is_hfp(self) -> bool:
        """HFP/headset (hands-free 'Головной телефон') endpoint. Such outputs
        don't produce real audible sound on PC — exclude them from output."""
        n = (self.name or "").lower()
        return ("hands-free" in n or "bthhfenum" in n or
                "головной телефон" in n or "headset" in n)

    @property
    def pair_key(self) -> str:
        """Key grouping the input and output endpoints of the same physical
        Bluetooth device. The Hands-Free AG input and output endpoints of one
        TD-H9 radio share the same key so the GUI can auto-select the pair."""
        name = (self.name or "").lower()
        if "td-h9" in name or "tidradio" in name:
            if "hands-free" in name or "sco" in name or "bthhfenum" in name:
                return "tdh9-hf"
            return "tdh9"
        return ""

    def kind(self) -> str:
        """Short capability tag like 'in/out/BT' used in the GUI device list."""
        tags = []
        if self.max_in > 0:
            tags.append("in")
        if self.max_out > 0:
            tags.append("out")
        if self.is_bt:
            tags.append("BT")
        return "/".join(tags)

    def __repr__(self) -> str:
        return f"{self.name} [{self.kind()}]"


def _hostapi_names() -> list[str]:
    """Return the names of all host APIs (MME, DirectSound, WASAPI, …) as used
    by sounddevice; used to tag device entries for filtering."""
    return [h["name"] for h in sd.query_hostapis()]

def _scrub_api(name: str) -> str:
    """Remove trailing hostapi tag like ' (WASAPI)' / ' (DirectSound)' / ' (MME)'."""
    import re as _re
    s = _re.sub(
        r"\s*\(\s*(WASAPI|DirectSound|MME|Windows Audio Session API[^)]*)\)\s*$",
        "", name,
    )
    return s.strip()

def _hostapi_weight(name: str) -> int:
    """Rank hostapi by reliability for Bluetooth HFP routing. Lower is better:
    MME (0) -> DirectSound (1) -> WASAPI (2). Used when auto-picking a device."""
    n = name.lower()
    if "mme" in n:
        return 0
    if "directsound" in n:
        return 1
    if "wasapi" in n:
        return 2
    return 3

def list_devices() -> list[AudioDevice]:
    """Return ALL audio devices (maximal list, across every hostapi).

    Windows exposes each physical endpoint once per hostapi (MME / DirectSound /
    WASAPI). We intentionally keep every entry so the user sees the widest
    possible list; the GUI can filter later. The system's default output is
    marked with default_out=True so Monitor can always route to it.
    """
    raw = list(sd.query_devices())
    hapi_names = _hostapi_names()
    # Resolve default output index.
    def_out_idx = _default_out_index()
    out = []
    for idx, dev in enumerate(raw):
        hname = hapi_names[dev["hostapi"]] if dev["hostapi"] < len(hapi_names) else ""
        out.append(AudioDevice(
            index=idx,
            name=f"{_scrub_api(dev['name'])} [{hname}]",
            max_in=int(dev["max_input_channels"]),
            max_out=int(dev["max_output_channels"]),
            default_out=(idx == def_out_idx),
            default_sr=float(dev["default_samplerate"]),
        ))
    return out


def _default_out_index() -> int | None:
    """Resolve the system's default output device index."""
    try:
        d = sd.default.device
        if d is None:
            return None
        if hasattr(d, "__getitem__"):
            return d[1]  # second element is output index
        return int(d)
    except Exception:
        return None


def default_output_index() -> int | None:
    """Public helper: index of the Windows default audio output device."""
    return _default_out_index()


def _default_out_device() -> AudioDevice | None:
    """Return the system's default output device (e.g. laptop speakers)."""
    try:
        d = sd.default.device
        # d may be an int, a (input, output) tuple, or _InputOutputPair.
        if hasattr(d, "__getitem__"):
            idx = d[1]  # second element is the output index
        else:
            idx = d
        if idx is None:
            return None
        info = sd.query_devices(idx, kind="output")
        if int(info["max_output_channels"]) <= 0:
            return None
        return AudioDevice(
            index=int(idx),
            name=_scrub_api(info["name"]),
            max_in=int(info["max_input_channels"]),
            max_out=int(info["max_output_channels"]),
            default_out=True,
            default_sr=float(info["default_samplerate"]),
        )
    except Exception:
        return None


def list_bt_devices(include_all: bool = False) -> list[AudioDevice]:
    """Return the MAXIMUM list of audio devices.

    The app needs the widest possible list of inputs (to pick where the radio's
    voice comes in) and outputs (where to transmit back into the radio). The
    system default output is already present and flagged default_out=True.
    """
    return list_devices()


def find_device(substring: str) -> AudioDevice | None:
    """Return the first device whose name contains `substring` (case-insensitive),
    or None if no such device is present."""
    s = (substring or "").lower()
    for d in list_devices():
        if s in d.name.lower():
            return d
    return None


class StreamRouter:
    """
    Continuously routes a live input stream (radio SCO microphone) to an output
    device (speaker/audio out). Optionally mixes in an audio file that is being
    played. The radio transmits on its own via its physical PTT button; this
    bridge just forwards whatever the radio's microphone hears.
    """

    def __init__(self, input_index: int | None, output_index: int | None,
                 sample_rate: int = SAMPLE_RATE, blocksize: int = BLOCKSIZE,
                 on_voice_on=None, on_voice_off=None):
        self.input_index = input_index
        self.output_index = output_index
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        # PTT-triggered callbacks: fired ONLY by an explicit PTT command
        # (e.g. "+SPP=P"/"+SPP=R" over SPP, or AVRCP), never inferred from audio.
        self.on_voice_on = on_voice_on
        self.on_voice_off = on_voice_off
        self._ptt = False
        self._monitor_enabled = False
        self._running = False
        self._stream_in: sd.InputStream | None = None
        self._stream_out: sd.OutputStream | None = None
        self._rx_q: queue.Queue = queue.Queue()
        self._tx_q: queue.Queue = queue.Queue()
        self._mix_q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.in_rate = self._device_rate(input_index)
        self.out_rate = self._device_rate(output_index)
        self._out_rebuild = False

    @staticmethod
    def _device_rate(index: int | None) -> int:
        """Return default sample rate of a device (fallback 44100)."""
        if index is None:
            return 44100
        try:
            return int(sd.query_devices(index)["default_samplerate"])
        except Exception:
            return 44100

    # -- public API ---------------------------------------------------------
    def start(self):
        """Start the router thread (if not already running). New queues are
        created so any stale data from a previous run is discarded."""
        if self._running:
            return
        self._running = True
        self._tx_q = queue.Queue()
        self._rx_q = queue.Queue()
        self._mix_q = queue.Queue()
        self._thread = threading.Thread(target=self._router, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the router thread and release its audio streams. Fires the
        PTT-off lamp callback and waits (up to 2 s) for the worker to exit."""
        self._running = False
        self._stream_in = None
        self._stream_out = None
        self.set_ptt(False)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def running(self) -> bool:
        """True while the router thread is active."""
        return self._running

    @property
    def ptt_active(self) -> bool:
        """True while the radio PTT is held (set via an explicit PTT command)."""
        return self._ptt

    @property
    def gate_active(self) -> bool:
        """True when the router passes radio audio to the output stream
        (checkbox enabled AND radio PTT held)."""
        return self._monitor_enabled and self._ptt

    def set_ptt(self, active: bool):
        """Called when an explicit PTT command arrives (e.g. +SPP=P / +SPP=R).
        Updates the radio PTT state and, together with the monitor checkbox,
        drives the output gate and the on_voice_on/off lamp callbacks.
        The lamp/gate turn on only when monitor is enabled AND PTT is held.
        Not inferred from audio levels."""
        active = bool(active)
        prev_gate = self.gate_active
        with self._lock:
            if self._ptt == active:
                return
            self._ptt = active
        new_gate = self.gate_active
        if prev_gate == new_gate:
            return
        if new_gate and self.on_voice_on is not None:
            self.on_voice_on()
        elif not new_gate and self.on_voice_off is not None:
            self.on_voice_off()

    def queue_file(self, data: bytes):
        """Queue pre-decoded PCM (int16, mono, sample_rate) to be mixed into the output."""
        self._mix_q.put(data)

    def set_output(self, output_index: int | None):
        """Change the device the gated audio is routed to (hot re-route).
        The router thread picks the new device up on the next loop iteration."""
        with self._lock:
            self.output_index = output_index
            self.out_rate = self._device_rate(output_index)
            self._out_rebuild = True

    def set_monitor(self, enabled: bool):
        """Enable/disable the monitor checkbox gate. Audio passes to the output
        only while monitor is enabled AND the radio PTT is held."""
        enabled = bool(enabled)
        prev_gate = self.gate_active
        with self._lock:
            self._monitor_enabled = enabled
        new_gate = self.gate_active
        if prev_gate == new_gate:
            return
        if new_gate and self.on_voice_on is not None:
            self.on_voice_on()
        elif not new_gate and self.on_voice_off is not None:
            self.on_voice_off()

    # -- router thread ------------------------------------------------------
    def _router(self):
        """Main router loop (runs in its own thread).

        Opens the input/output streams, then continuously:
          - rebuilds the output stream when the redirect device changed
            (set_output was called -> `_out_rebuild` flag);
          - merges any queued file PCM into the mix buffer;
          - passes radio input (+ mixed file) to the output ONLY while the
            PTT gate is open (monitor checkbox AND PTT held);
          - resamples the mixed audio to the output device's rate.
        """
        import miniaudio
        in_rate = self.in_rate
        out_rate = self.out_rate
        blocksize = self.blocksize

        # sounddevice input adapter: push each captured block to the rx queue.
        def input_cb(data, frames, time_info, status):
            if frames > 0:
                self._rx_q.put(bytes(data))

        # sounddevice output adapter: pull the next pre-built chunk from the tx
        # queue; silence if nothing is buffered yet.
        def output_cb(outdata, frames, time_info, status):
            try:
                chunk = self._tx_q.get_nowait()
            except queue.Empty:
                chunk = b""
            if chunk:
                arr = np.frombuffer(chunk, dtype=np.int16)
            else:
                arr = np.zeros(frames, dtype=np.int16)
            if len(arr) >= frames:
                outdata[:] = arr[:frames].reshape(-1, 1)
            else:
                outdata[:len(arr)] = arr.reshape(-1, 1)
                outdata[len(arr):] = 0

        # Open streams at each device's native sample rate.
        if self.input_index is not None:
            try:
                self._stream_in = sd.InputStream(
                    samplerate=in_rate, blocksize=blocksize,
                    device=self.input_index, channels=CHANNELS, dtype=DTYPE,
                    callback=input_cb,
                )
            except Exception as e:
                print(f"[core] input error: {e}")
                self._stream_in = None

        def open_output():
            """Open (or reopen) the output stream on the current output device."""
            nonlocal out_rate
            out_rate = self.out_rate
            if self._stream_out is not None:
                try:
                    self._stream_out.stop()
                    self._stream_out.close()
                except Exception:
                    pass
                self._stream_out = None
            if self.output_index is None:
                return
            try:
                self._stream_out = sd.OutputStream(
                    samplerate=out_rate, blocksize=max(blocksize, 32),
                    device=self.output_index, channels=CHANNELS, dtype=DTYPE,
                    callback=output_cb,
                )
                self._stream_out.start()
            except Exception as e:
                print(f"[core] output error: {e}")
                self._stream_out = None

        if self.output_index is not None:
            try:
                self._stream_out = sd.OutputStream(
                    samplerate=out_rate, blocksize=max(blocksize, 32),
                    device=self.output_index, channels=CHANNELS, dtype=DTYPE,
                    callback=output_cb,
                )
            except Exception as e:
                print(f"[core] output error: {e}")
                self._stream_out = None

        if self._stream_in is not None:
            self._stream_in.start()
        if self._stream_out is not None:
            self._stream_out.start()

        mix_buf = bytearray()

        while self._running:
            if self._out_rebuild:
                with self._lock:
                    self._out_rebuild = False
                open_output()

            if not self._mix_q.empty():
                pcm = self._mix_q.get()
                mix_buf.extend(pcm)

            if not self._rx_q.empty():
                chunk = self._rx_q.get()
                # Gate: pass the radio input to the output only while the
                # monitor checkbox is enabled AND the radio PTT is held.
                if not self.gate_active:
                    continue
                if len(mix_buf) >= len(chunk):
                    file_part = bytes(mix_buf[:len(chunk)])
                    del mix_buf[:len(chunk)]
                else:
                    file_part = bytes(mix_buf)
                    mix_buf.clear()
                mixed = self._mix(chunk, file_part)
                # Resample from input's native rate to output's native rate.
                if in_rate != out_rate:
                    try:
                        out = miniaudio.convert_frames(
                            miniaudio.SampleFormat.SIGNED16, CHANNELS, in_rate,
                            mixed, miniaudio.SampleFormat.SIGNED16, CHANNELS,
                            out_rate,
                        )
                    except Exception as e:
                        print(f"[core] resample error: {e}")
                        out = bytes(mixed)
                else:
                    out = mixed
                self._tx_q.put(bytes(out))

            time.sleep(0.001)

        if self._stream_in is not None:
            self._stream_in.stop()
            self._stream_in.close()
        if self._stream_out is not None:
            self._stream_out.stop()
            self._stream_out.close()

    @staticmethod
    def _mix(voice: bytes, file_pcm: bytes) -> bytes:
        """Linearly mix voice and file PCM (int16 mono) into one int16 buffer."""
        if not file_pcm:
            return voice
        v = np.frombuffer(voice, dtype=np.int16)
        n = min(len(v), len(file_pcm) // 2)
        if n <= 0:
            return voice
        m = np.frombuffer(file_pcm, dtype=np.int16, count=n)
        mixed = (v[:n].astype(np.int32) + m.astype(np.int32)) // 2
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        out = bytearray(v)
        out[:n * 2] = mixed.tobytes()
        return bytes(out)


# ----------------------------------------------------------------------
# RFCOMM / Bluetooth COM port helpers (PTT +SPP=P/R detection)
# ----------------------------------------------------------------------

def _bt_device_name_for_mac(mac_lower: str) -> str:
    """Look up the Bluetooth device's friendly name from the registry by its
    MAC address (stored as ASCII byte values under BTHPORT\Devices)."""
    import winreg
    key_path = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
            try:
                with winreg.OpenKey(k, mac_lower) as dk:
                    data, _ = winreg.QueryValueEx(dk, "Name")
                    if isinstance(data, bytes):
                        s = data.split(b"\0", 1)[0].decode("ascii", "ignore")
                        return s.strip()
            except OSError:
                pass
    except OSError:
        pass
    return ""


def list_rfcomm_ports() -> list[tuple[str, str]]:
    """Return a list of Bluetooth serial (COM) ports on this PC.

    Uses pyserial's comports() and filters to entries whose description or
    device id hints they are Bluetooth RFCOMM virtual serial ports (the radio's
    SPP channel). Returns a list of (port, device_name) tuples, e.g.
    [('COM11', 'TD-H9-9223'), ...]. device_name may be '' if unknown.
    """
    import serial.tools.list_ports as serial_ports
    out: list[tuple[str, str]] = []
    for p in serial_ports.comports():
        blob = f"{p.description} {p.device}".lower()
        if "bluetooth" in blob or "rfcomm" in blob or "spp" in blob:
            mac_lower = ""
            if p.hwid and "\\" in p.hwid:
                # hwid is like BTHENUM\\{...}\\8&93B9F31&0&50F1F8F29D8B_C00000000
                last = p.hwid.rsplit("\\", 1)[-1]
                first = last.split("_C", 1)[0].split("&")[-1]
                if len(first) == 12 and first.isalnum():
                    mac_lower = first.lower()
            name = _bt_device_name_for_mac(mac_lower) if mac_lower else ""
            out.append((p.device, name))
    return out


class RfcommPttListener:
    """Continuously reads a Bluetooth COM port for the radio's PTT commands
    (+SPP=P on press, +SPP=R on release) and fires on_ptt_down/on_ptt_up."""

    def __init__(self, port: str, on_ptt_down=None, on_ptt_up=None):
        self.port = port
        self.on_ptt_down = on_ptt_down
        self.on_ptt_up = on_ptt_up
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ptt = False

    @property
    def ptt_active(self) -> bool:
        """True while the radio PTT is currently held."""
        return self._ptt

    def start(self):
        """Launch the reader thread (if not already running)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the reader thread and reset the PTT state (firing the off
        callback so the lamp/gate turn off)."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._set_ptt(False)

    def _set_ptt(self, active: bool):
        """Update the PTT state and fire the matching callback only on a state
        change (avoids duplicate on_ptt_down/up emissions)."""
        with self._lock:
            if self._ptt == active:
                return
            self._ptt = active
        if active and self.on_ptt_down is not None:
            self.on_ptt_down()
        elif not active and self.on_ptt_up is not None:
            self.on_ptt_up()

    def _reader(self):
        """Reader loop: opens the COM port, reads bytes, accumulates them into
        a string and parses "+SPP=P" / "+SPP=R" tokens to drive PTT state.
        Retries opening the port (port can be temporarily busy) and reconnects
        on read errors."""
        import serial
        buf = ""
        while self._running:
            try:
                ser = serial.Serial(self.port, baudrate=9600, timeout=0.2)
            except Exception as e:
                print(f"[rfcomm] open error {self.port}: {e}")
                for _ in range(50):
                    if not self._running:
                        return
                    time.sleep(0.1)
                continue
            ser.flushInput()
            while self._running:
                try:
                    chunk = ser.read(128)
                except Exception as e:
                    print(f"[rfcomm] read error: {e}")
                    break
                if not chunk:
                    continue
                try:
                    buf += chunk.decode("ascii", "replace")
                except Exception:
                    continue
                # Parse each "+SPP=X" token in the accumulated buffer.
                while "+SPP=" in buf:
                    i = buf.index("+SPP=")
                    buf = buf[i:]
                    tok = buf[5:6]
                    buf = buf[6:]
                    if tok == "P":
                        self._set_ptt(True)
                    elif tok == "R":
                        self._set_ptt(False)
            try:
                ser.close()
            except Exception:
                pass
            if self._running:
                time.sleep(0.5)


def _effective_out_channels(device_index: int | None, requested: int) -> int:
    """Clamp channel count to what the output device can accept."""
    if requested > 0:
        try:
            info = sd.query_devices(device_index)
            max_out = int(info["max_output_channels"])
            if max_out > 0:
                return min(requested, max_out)
        except Exception:
            pass
    return requested


class Player:
    """A single-file player with precise pause/resume/seek and position info.

    The whole track is decoded into memory on play() (resampled to the output
    device's native rate), so pause, resume and seeking are sample-accurate and
    the GUI can report elapsed time and ETA. Only one playback runs at a time:
    starting another item stops and joins the previous worker, so audio never
    overlaps. Callbacks (on_done / on_error) fire on the worker thread; the GUI
    must marshal them back to the UI thread (e.g. via QTimer.singleShot).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()          # set => stop/discard session
        self._thread: threading.Thread | None = None
        self._samples: np.ndarray | None = None
        self._total_frames = 0
        self._out_ch = 1
        self._sr = 44100
        self._reading = 0                       # absolute frame position
        self._play_token = 0                    # busted to kill stale sessions
        self._paused = False
        self._loaded = False
        self._start_seconds = 0.0
        self.output_index: int | None = None
        self.path: str | None = None
        self.on_done = None
        self.on_error = None

    # -- control --------------------------------------------------------------
    def play(self, path: str, output_index: int | None, on_done=None,
             on_error=None, start_seconds: float = 0.0):
        """Start playing `path`; stops and joins any previous playback first.
        If `start_seconds` > 0, playback begins there."""
        self._teardown(wait=True)               # no overlapping audio
        self._stop.clear()
        tok = self._play_token + 1
        self._play_token = tok
        self.path = path
        self.output_index = output_index
        self.on_done = on_done
        self.on_error = on_error
        self._paused = False
        self._reading = 0
        self._loaded = False
        self._samples = None
        self._start_seconds = max(0.0, float(start_seconds))
        self._thread = threading.Thread(target=self._session, args=(tok,),
                                        daemon=True)
        self._thread.start()

    def pause(self):
        """Pause: mark stopped so the session closes its stream, keep position."""
        with self._lock:
            if self._paused:
                return
            self._paused = True
        self._stop.set()

    def resume(self):
        """Resume from the paused position by starting a new session."""
        with self._lock:
            if not self._paused or self._samples is None:
                return
            self._paused = False
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            tok = self._play_token + 1
            self._play_token = tok
        self._stop.clear()
        self._thread = threading.Thread(target=self._session, args=(tok,),
                                        daemon=True)
        self._thread.start()

    def stop(self, wait=True):
        """Stop and discard the track (and session)."""
        self._teardown(wait=wait)
        self.path = None

    def seek(self, seconds: float):
        """Jump to `seconds` into the loaded track. Takes effect immediately for
        a playing track (session re-starts) or sets position for resume."""
        with self._lock:
            if self._samples is None:
                return
            frame = max(0, min(int(seconds * self._sr), self._total_frames))
            self._reading = frame
            was_paused = self._paused
        if was_paused:
            return
        # re-start the running session from the new position
        self._stop.set()
        with self._lock:
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            tok = self._play_token + 1
            self._play_token = tok
        self._stop.clear()
        self._thread = threading.Thread(target=self._session, args=(tok,),
                                        daemon=True)
        self._thread.start()

    # -- info -----------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._loaded and not self._paused and self.path is not None

    @property
    def duration(self) -> float:
        if not self._loaded:
            return 0.0
        return self._total_frames / self._sr

    @property
    def elapsed(self) -> float:
        with self._lock:
            return self._reading / self._sr

    def position(self) -> tuple[float, float]:
        """Return (elapsed_seconds, duration_seconds). 0 if nothing loaded."""
        if not self._loaded:
            return 0.0, 0.0
        with self._lock:
            return self._reading / self._sr, self._total_frames / self._sr

    # -- internals ------------------------------------------------------------
    def _teardown(self, wait: bool):
        """Signal the running session to stop and, if `wait`, join its thread
        (so audio/wav resources are released before starting something new)."""
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _session(self, token: int):
        """One playback session (runs in a worker thread).

        If the track is not yet decoded, decode it fully into memory (resampled
        to the output device's native rate) and set the start position. Then
        open a callback OutputStream that pulls samples from the decoded buffer
        via `fill()`. The stream keeps running until stopped, paused or the whole
        sample buffer has been played. `token` guards against stale sessions
        overwriting the state of a newer playback."""
        import miniaudio
        import sounddevice as sd
        import miniaudio
        import sounddevice as sd
        # 1) (re)load samples if not in memory yet (or position stale after seek).
        if not self._loaded:
            try:
                d = miniaudio.decode_file(
                    self.path, output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=max(1, _effective_out_channels(self.output_index, 2)),
                    sample_rate=_device_rate(self.output_index) or 44100,
                )
                with self._lock:
                    self._samples = np.array(d.samples, dtype=np.int16)
                    self._total_frames = d.num_frames
                    self._sr = d.sample_rate
                    self._out_ch = d.nchannels
                    self._reading = min(
                        int(self._start_seconds * d.sample_rate),
                        d.num_frames)
                    self._loaded = True
            except Exception as e:
                print(f"[play] decode error: {e}")
                if callable(self.on_error):
                    self._safe_cb(self.on_error, e)
                return
        with self._lock:
            if token != self._play_token:
                return
            start_frame = self._reading
            ch = self._out_ch
            sr = self._sr
            out_dev = self.output_index
        buf = bytearray()

        def fill(need: int):
            with self._lock:
                idx = self._reading
            if idx >= self._total_frames:
                return False
            with self._lock:
                start = idx * ch
                take = min((need // 2), (self._total_frames - idx) * ch)
            seg = self._samples[start:start + take].tobytes()
            buf.extend(seg)
            with self._lock:
                self._reading = (start + take) // ch
            return True

        finished = False

        def callback(outdata, frames, time_info, status):
            nonlocal finished
            need = frames * ch * 2
            while not finished and len(buf) < need:
                if not fill(need - len(buf)):
                    finished = True
                    break
            with self._lock:
                n = min(len(buf) // (2 * ch), frames)
                if n > 0:
                    outdata[:n] = np.frombuffer(bytes(buf[:n * 2 * ch]),
                                                dtype=np.int16).reshape(-1, ch)
                    del buf[:n * 2 * ch]
                if n < frames:
                    outdata[n:] = 0

        out = None
        self._stop.clear()
        try:
            out = sd.OutputStream(samplerate=sr, device=out_dev,
                                  channels=ch, dtype="int16", callback=callback)
            out.start()
            with self._lock:
                self._reading = start_frame
            while not self._stop.is_set():
                if finished and not buf:
                    break
                time.sleep(0.02)
        except Exception as e:
            print(f"[play] stream error: {e}")
        finally:
            if out is not None:
                try:
                    out.stop()
                    out.close()
                except Exception:
                    pass
            with self._lock:
                still_current = (token == self._play_token)
                was_paused = self._paused
            if still_current and not was_paused and finished and not self._stop.is_set():
                if callable(self.on_done):
                    self._safe_cb(self.on_done)

    def _safe_cb(self, fn, *a):
        """Invoke a callback, catching and logging any error so a faulty
        callback cannot propagate out of the playback worker thread."""
        try:
            fn(*a)
        except Exception as e:
            print(f"[play] cb error: {e}")


playback = Player()


def stop_playback():
    """Stop any ongoing file playback (compat wrapper for the Player singleton)."""
    playback.stop(wait=False)


def play_file_to_device(path: str, output_index: int | None,
                        sample_rate: int | None = None, volume: float = 1.0,
                        on_done=None):
    """
    Decode (via miniaudio streaming) and play an audio file to the given output
    device. miniaudio resamples to the device's native sample rate and the
    output channel count is clamped to what the device supports (e.g. stereo
    MP3 -> mono for a SCO hands-free output). Uses a callback output stream so
    WDM-KS / Bluetooth Hands-Free endpoints (which lack blocking write) work.
    """
    global _play_stream
    import miniaudio

    target_sr = sample_rate or _device_rate(output_index)
    out_ch = _effective_out_channels(output_index, 2)
    out_ch = max(out_ch, 1)

    def worker():
        global _play_stream
        _play_should_stop.clear()
        gen = None
        try:
            gen = iter(miniaudio.stream_file(
                path, output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=out_ch, sample_rate=target_sr,
                frames_to_read=BLOCKSIZE * 4,
            ))
            buffer_ = bytearray()
            finished = False

            def fill():
                """Pull more samples from the generator into the buffer."""
                nonlocal finished
                try:
                    buffer_.extend(bytes(next(gen)))
                except StopIteration:
                    finished = True

            # Pre-fill so the stream has data immediately.
            for _ in range(4):
                fill()
                if finished:
                    break

            def callback(outdata, frames, time_info, status):
                nonlocal finished
                need = frames * out_ch * 2
                while not finished and len(buffer_) < need:
                    fill()
                n = min(len(buffer_) // (2 * out_ch), frames)
                if n > 0:
                    data = np.frombuffer(buffer_[:n * 2 * out_ch], dtype=np.int16)
                    outdata[:n] = data.reshape(-1, out_ch)
                    del buffer_[:n * 2 * out_ch]
                if n < frames:
                    outdata[n:] = 0
                # Signal end-of-playback once the source drained and buffer empty.
                if finished and not buffer_:
                    _play_should_stop.set()

            out = sd.OutputStream(
                samplerate=target_sr, device=output_index,
                channels=out_ch, dtype="int16", callback=callback,
            )
            with _play_lock:
                _play_stream = out
            out.start()
            # Wait until finished flag is signalled by the callback.
            while (not finished or buffer_) and not _play_should_stop.is_set():
                time.sleep(0.02)
            if not _play_should_stop.is_set() or (finished and not buffer_):
                _play_should_stop.set()
            out.stop()
            out.close()
        except Exception as e:
            print(f"[play] error: {e}")
        finally:
            try:
                if gen is not None:
                    gen.close()
            except Exception:
                pass
            with _play_lock:
                _play_stream = None
            if on_done is not None:
                on_done()

    threading.Thread(target=worker, daemon=True).start()


# ----------------------------------------------------------------------
# Playlist model (files / folders / m3u-m3u8)
# ----------------------------------------------------------------------

AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus",
              ".wma", ".m4b", ".aiff", ".aif", ".ape")


class Playlist:
    """An ordered list of audio file paths (a playlist).

    Supports adding individual files or whole folders (recursive audio scan),
    and loading/saving .m3u / .m3u8 playlists. Items are kept as absolute paths.
    """

    def __init__(self):
        """Empty playlist: `items` holds absolute audio paths, `index` points
        at the currently selected/playing item (-1 = none)."""
        self.items: list[str] = []
        self.index: int = -1  # index of the currently playing/queued item, -1 = none

    # -- basic collection ops -------------------------------------------------
    def __len__(self) -> int:
        """Number of tracks in the playlist."""
        return len(self.items)

    def clear(self):
        """Remove every track and reset the play index."""
        self.items.clear()
        self.index = -1

    def add_files(self, paths: list[str]):
        """Append existing audio files (and, if given a folder, its audio
        files, scanned recursively, sorted)."""
        for p in paths:
            if os.path.isdir(p):
                self.add_files(sorted(
                    os.path.join(dp, f)
                    for dp, _, fs in os.walk(p)
                    for f in fs
                    if f.lower().endswith(AUDIO_EXTS)))
            elif os.path.isfile(p) and p.lower().endswith(AUDIO_EXTS):
                if p not in self.items:
                    self.items.append(p)

    def remove(self, indices: list[int]):
        """Remove items by their indices (may be unsorted). Keeps logical
        wrap-around of the current play index."""
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self.items):
                del self.items[i]
        if self.items:
            self.index = min(max(self.index, 0), len(self.items) - 1)
        else:
            self.index = -1

    def current(self) -> str | None:
        """Return the path of the currently selected item (or None)."""
        if 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def next_index(self) -> int:
        """Index of the item after the current one (wrap-around)."""
        if not self.items:
            return -1
        if self.index < 0:
            return 0
        return (self.index + 1) % len(self.items)

    def advance(self) -> str | None:
        """Move to and return the next item (wrap-around)."""
        self.index = self.next_index()
        return self.current()

    # -- m3u / m3u8 -----------------------------------------------------------
    @staticmethod
    def _resolve(line: str, base_dir: str) -> str:
        """Resolve one m3u entry to an absolute path. Handles absolute paths,
        paths relative to the playlist file, and file:// URLs. External http
        streams are returned as-is (the GUI will skip them)."""
        line = line.strip()
        if not line or line.startswith("#"):
            return ""
        if line.startswith("http://") or line.startswith("https://") or \
           line.startswith("rtsp://"):
            return line
        if line.startswith("file://"):
            line = line[len("file://"):]
            line = line.lstrip("/")
        p = line
        if not os.path.isabs(p):
            p = os.path.join(base_dir, line)
        return os.path.normpath(p)

    def load_m3u(self, path: str) -> int:
        """Load a .m3u / .m3u8 playlist. Returns how many items were added."""
        base_dir = os.path.dirname(path) or "."
        added = 0
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                for raw in f:
                    resolved = self._resolve(raw, base_dir)
                    if not resolved:
                        continue
                    if (resolved.startswith("http://") or
                            resolved.startswith("https://") or
                            resolved.startswith("rtsp://")):
                        continue  # skip remote streams for now
                    if os.path.exists(resolved) and resolved not in self.items:
                        self.items.append(resolved)
                        added += 1
        except OSError as e:
            raise OSError(f"Cannot read playlist: {e}")
        return added

    def save_m3u(self, path: str, extended: bool = True):
        """Save the playlist as .m3u (or .m3u8 if the path ends so)."""
        lines = []
        if extended or path.lower().endswith(".m3u8"):
            lines.append("#EXTM3U")
        for item in self.items:
            name = os.path.splitext(os.path.basename(item))[0]
            if extended or path.lower().endswith(".m3u8"):
                lines.append(f"#EXTINF:-1,{name}")
            lines.append(item)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            raise OSError(f"Cannot write playlist: {e}")

    @staticmethod
    def _is_m3u_file(p: str) -> bool:
        n = p.lower()
        return n.endswith(".m3u") or n.endswith(".m3u8")

    def add_m3u(self, paths: list[str]) -> int:
        """If any path is an .m3u/.m3u8 file, load it; otherwise add audio."""
        added = 0
        audio_paths = []
        for p in paths:
            if self._is_m3u_file(p) and os.path.isfile(p):
                added += self.load_m3u(p)
            else:
                audio_paths.append(p)
        if audio_paths:
            before = len(self.items)
            self.add_files(audio_paths)
            added += len(self.items) - before
        return added