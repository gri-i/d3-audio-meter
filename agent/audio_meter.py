"""Audio Meter agent for disguise Designer.

Captures what the machine is playing out (WASAPI loopback), analyses it and
streams the result over a WebSocket. The same port also serves the plugin UI,
so it can be opened in a browser or referenced from d3plugin.json "url".

    python audio_meter.py --list
    python audio_meter.py --port 8765

Each plugin window picks its own device: an output (captured via loopback)
or an input, where LTC timecode is decoded as well. The agent runs one capture per
device that somebody is watching and stops it when the last window leaves.
"""

import argparse
import asyncio
import json
import mimetypes
import queue
import sys
import threading
import winreg
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
from ltc import LtcDecoder
from websockets.asyncio.server import broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.datastructures import Headers
from websockets.http11 import Response

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"
FFT_SIZE = 4096
N_BANDS = 48
F_MIN, F_MAX = 20.0, 20000.0
FLOOR_DB = -120.0
# PortAudio stream open/close is not guaranteed thread-safe; captures run in threads.
PA_LOCK = threading.Lock()


def db(x):
    return np.maximum(20.0 * np.log10(np.maximum(x, 1e-12)), FLOOR_DB)


class Analyzer:
    def __init__(self, samplerate, channels):
        self.sr = samplerate
        self.channels = channels
        self.ring = np.zeros(FFT_SIZE, dtype=np.float32)
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        # Amplitude correction so a full-scale sine reads ~0 dBFS.
        self.win_gain = self.window.sum() / 2.0
        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / samplerate)
        edges = np.geomspace(F_MIN, min(F_MAX, samplerate / 2), N_BANDS + 1)
        self.band_idx = [
            np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])
        ]
        # Low bands can be narrower than one FFT bin: fall back to the nearest bin.
        for i, idx in enumerate(self.band_idx):
            if idx.size == 0:
                centre = np.sqrt(edges[i] * edges[i + 1])
                self.band_idx[i] = np.array([np.argmin(np.abs(freqs - centre))])
        self.band_centres = np.sqrt(edges[:-1] * edges[1:]).round(1).tolist()

    def process(self, block):
        """block: (frames, channels) float32 in [-1, 1]."""
        peak = np.abs(block).max(axis=0)

        mono = block.mean(axis=1)
        n = min(len(mono), FFT_SIZE)
        self.ring = np.roll(self.ring, -n)
        self.ring[-n:] = mono[-n:]
        mag = np.abs(np.fft.rfft(self.ring * self.window)) / self.win_gain
        bands = np.array([mag[idx].max() for idx in self.band_idx])

        corr = None
        if block.shape[1] >= 2:
            l, r = block[:, 0].astype(np.float64), block[:, 1].astype(np.float64)
            denom = np.sqrt(np.sum(l * l) * np.sum(r * r))
            corr = float(np.sum(l * r) / denom) if denom > 1e-9 else None

        return {
            "type": "frame",
            "peak": db(peak).round(1).tolist(),
            "bands": db(bands).round(1).tolist(),
            "corr": None if corr is None else round(corr, 3),
            "clip": bool((peak >= 0.999).any()),
        }


LOOPBACK_SUFFIX = " [Loopback]"


def display_name(dev):
    return dev["name"].replace(LOOPBACK_SUFFIX, "")


def default_output_name(pa):
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    return pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]


def list_devices(pa):
    """id -> (kind, device info). Outputs are captured via loopback, inputs directly."""
    devices = {}
    for d in pa.get_loopback_device_info_generator():
        devices[f"out:{display_name(d)}"] = ("out", d)
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["hostApi"] == wasapi and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
            devices[f"in:{d['name']}"] = ("in", d)
    return devices


MIC_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
MIC_HELP = ("Windows blocks Python from recording inputs. Settings → Privacy & security → "
            "Microphone: allow access for Python / desktop apps, then restart the agent.")


def _consent(root, path):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, "Value")[0]
    except OSError:
        return None


def mic_access_problem():
    """Why Windows would block opening an input, or None.

    Python from the Microsoft Store is a packaged app: until the user answers
    the microphone consent ("Prompt"), opening an input just hangs.
    """
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        if _consent(root, MIC_CONSENT) == "Deny":
            return MIC_HELP
    prefix = Path(sys.base_prefix).name  # e.g. PythonSoftwareFoundation.Python.3.13_3.13.x_x64__qbz5n2kfra8p0
    if "__" in prefix:
        family = prefix.split("_")[0] + "_" + prefix.split("__")[-1]
        app = _consent(winreg.HKEY_CURRENT_USER, MIC_CONSENT + "\\" + family)
    else:
        app = _consent(winreg.HKEY_CURRENT_USER, MIC_CONSENT + "\\NonPackaged")
    return None if app in (None, "Allow") else MIC_HELP


class OpenTimeout(RuntimeError):
    pass


def open_with_timeout(open_fn, seconds):
    """Runs a PortAudio open in a helper thread; a stuck open must not freeze the agent."""
    box = {}

    def run():
        try:
            box["stream"] = open_fn()
        except Exception as e:  # reported to the caller below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise OpenTimeout(f"device did not open within {seconds:.0f} s")
    if "error" in box:
        raise box["error"]
    return box["stream"]


class DeviceStream:
    """Capture of one device (output via loopback, or input), shared by every
    client watching it. Inputs are also scanned for LTC timecode."""

    def __init__(self, pa, dev_id, kind, dev, blocksize, send):
        self.pa = pa
        self.id = dev_id
        self.kind = kind
        self.dev = dev
        self.name = display_name(dev)
        self.blocksize = blocksize
        self.send = send  # send(clients, msg), thread-safe
        self.clients = set()  # touched only from the event loop thread
        self.config = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _publish(self, msg, sticky=False):
        if sticky:
            self.config = msg
        self.send(self.clients, msg)

    def _run(self):
        while not self.stop.is_set():
            try:
                self._capture()
            except Exception as e:
                print(f"Capture error ({self.name}): {e}")
                self._publish({"type": "error", "message": f"{self.name}: {e}"})
                # A hung open leaves its helper thread behind: retry rarely then.
                self.stop.wait(30.0 if isinstance(e, OpenTimeout) else 2.0)

    def _capture(self):
        # WASAPI shared mode: loopback must run at the device's mix format.
        samplerate = int(self.dev["defaultSampleRate"])
        channels = self.dev["maxInputChannels"]
        blocksize = self.blocksize
        analyzer = Analyzer(samplerate, channels)
        # LTC arrives on inputs; decoding every channel of a music output would
        # just burn CPU on zero crossings.
        decoders = [LtcDecoder(samplerate) for _ in range(channels)] if self.kind == "in" else []

        def analyse(block):
            msg = analyzer.process(block)
            if decoders:
                for ch, dec in enumerate(decoders):
                    dec.process(block[:, ch])
                # Report the channel that carries timecode: locked first, then most frames.
                best = max(range(channels), key=lambda c: (decoders[c].locked, decoders[c].frames))
                ltc = decoders[best].summary()
                if ltc:
                    ltc["ch"] = best
                msg["ltc"] = ltc
            return msg

        self._publish(
            {
                "type": "config",
                "id": self.id,
                "kind": self.kind,
                "ltc": bool(decoders),
                "device": self.name,
                "samplerate": samplerate,
                "channels": channels,
                "bandCentres": analyzer.band_centres,
            },
            sticky=True,
        )

        blocks = queue.Queue(maxsize=32)

        def callback(data, frames, time_info, status):
            try:
                blocks.put_nowait(data)
            except queue.Full:
                pass  # analysis fell behind: drop rather than build latency
            return (None, pyaudio.paContinue)

        def open_stream():
            return self.pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=samplerate,
                input=True,
                input_device_index=self.dev["index"],
                frames_per_buffer=blocksize,
                stream_callback=callback,
            )

        if self.kind == "in":
            problem = mic_access_problem()
            if problem:
                raise RuntimeError(problem)
            # Not under PA_LOCK: an input that never opens would block every other device.
            stream = open_with_timeout(open_stream, 5)
        else:
            with PA_LOCK:
                stream = open_stream()
        print(f"Capturing: {self.name}")
        silence = np.zeros((blocksize, channels), dtype=np.float32)
        timeout = 2 * blocksize / samplerate
        pending, pending_frames = [], 0
        try:
            while not self.stop.is_set():
                if not stream.is_active():
                    raise RuntimeError("stream stopped")
                # Loopback delivers nothing while the device is idle, so keep the
                # meters falling by feeding silence on timeout.
                try:
                    raw = blocks.get(timeout=timeout)
                except queue.Empty:
                    pending, pending_frames = [], 0
                    self._publish(analyse(silence))
                    continue
                # WASAPI hands out ~10 ms periods; gather a full block per frame
                # so the UI gets a steady rate instead of 100+ messages a second.
                chunk = np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)
                pending.append(chunk)
                pending_frames += len(chunk)
                if pending_frames >= blocksize:
                    self._publish(analyse(np.concatenate(pending)))
                    pending, pending_frames = [], 0
        finally:
            with PA_LOCK:
                stream.stop_stream()
                stream.close()
            print(f"Stopped:   {self.name}")


def static_response(path):
    rel = path.split("?", 1)[0].lstrip("/") or "index.html"
    file = (PLUGIN_DIR / rel).resolve()
    if PLUGIN_DIR not in file.parents or not file.is_file():
        return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"Not Found")
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    body = file.read_bytes()
    headers = Headers({"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-cache"})
    return Response(200, "OK", headers, body)


async def main(args):
    loop = asyncio.get_running_loop()
    # PortAudio's device list is fixed at init: devices added later need an agent restart.
    pa = pyaudio.PyAudio()
    devices = list_devices(pa)
    default = f"out:{default_output_name(pa)}"
    device_list = [{"id": k, "kind": kind, "name": display_name(d)} for k, (kind, d) in devices.items()]
    streams = {}  # device id -> DeviceStream, only while someone is watching

    def send(clients, msg):
        text = json.dumps(msg, separators=(",", ":"))
        loop.call_soon_threadsafe(broadcast, clients, text)

    def unsubscribe(ws, name):
        stream = streams.get(name)
        if stream is None:
            return
        stream.clients.discard(ws)
        if not stream.clients:
            stream.stop.set()
            del streams[name]

    async def subscribe(ws, name):
        stream = streams.get(name)
        if stream is None:
            kind, dev = devices[name]
            stream = streams[name] = DeviceStream(pa, name, kind, dev, args.blocksize, send)
            stream.clients.add(ws)
            stream.start()
        else:
            stream.clients.add(ws)
            if stream.config:
                await ws.send(json.dumps(stream.config, separators=(",", ":")))

    async def handler(ws):
        current = None
        await ws.send(json.dumps({"type": "devices", "devices": device_list, "default": default}))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") != "select":
                    continue
                name = msg.get("device")
                if name not in devices and f"out:{name}" in devices:
                    name = f"out:{name}"  # windows saved before inputs existed
                if name not in devices:
                    name = default
                if name == current:
                    continue
                if current:
                    unsubscribe(ws, current)
                current = name
                await subscribe(ws, name)
        except ConnectionClosed:
            pass  # window closed or Designer reloaded the plugin: not an error
        finally:
            if current:
                unsubscribe(ws, current)

    def process_request(connection, request):
        if request.path.split("?", 1)[0] != "/ws":
            return static_response(request.path)
        return None

    async with serve(handler, args.host, args.port, process_request=process_request):
        print(f"UI:        http://localhost:{args.port}/")
        print(f"WebSocket: ws://localhost:{args.port}/ws")
        try:
            await asyncio.Future()
        finally:
            for stream in list(streams.values()):
                stream.stop.set()
            for stream in list(streams.values()):
                stream.thread.join(timeout=1)
            pa.terminate()


if __name__ == "__main__":
    # A cp1252 console must not choke on non-ASCII device names.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="list loopback devices and exit")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--blocksize", type=int, default=1024, help="frames per analysis block (~21 ms at 48k)")
    args = p.parse_args()

    if args.list:
        pa = pyaudio.PyAudio()
        default = f"out:{default_output_name(pa)}"
        for dev_id, (kind, d) in list_devices(pa).items():
            mark = "*" if dev_id == default else " "
            print(f"{mark} {dev_id}  ({d['maxInputChannels']} ch, {int(d['defaultSampleRate'])} Hz)")
        pa.terminate()
        raise SystemExit

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
