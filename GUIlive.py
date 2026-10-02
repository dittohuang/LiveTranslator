import asyncio
import base64
import ctypes
import json
import math
import os
import queue
import threading
import unicodedata
from ctypes import wintypes
import tkinter as tk
from tkinter import messagebox, ttk
import numpy as np
import pyaudiowpatch as pyaudio
from google import genai
from google.genai import types

TARGET_SAMPLE_RATE = 16000
MODEL_ID = "gemini-3.5-live-translate-preview"
SILENCE_CHUNK = bytes(TARGET_SAMPLE_RATE // 10 * 2)  # 100 ms of int16 silence

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e5e7eb"
TEXT = "#111827"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
UI_FONT = "Microsoft YaHei UI"

CONFIG_PATH = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "LiveTranslate", "config.json")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Encrypt/decrypt with Windows DPAPI, bound to the current Windows user."""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not func(ctypes.byref(blob_in), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_api_key() -> str:
    try:
        encrypted = base64.b64decode(load_config()["api_key"])
        return _dpapi(encrypted, protect=False).decode("utf-8")
    except Exception:
        return ""


def save_api_key(api_key: str):
    cfg = load_config()
    cfg["api_key"] = base64.b64encode(_dpapi(api_key.encode("utf-8"), protect=True)).decode("ascii")
    save_config(cfg)


def apply_proxy(proxy_url: str | None):
    # websockets (used by the Live API) reads proxies from these env vars.
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if proxy_url:
            os.environ[name] = proxy_url
        else:
            os.environ.pop(name, None)


def loopback_producer(audio_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, stop_event: threading.Event, emit):
    """Capture Windows default speaker loopback using WASAPI."""
    p = pyaudio.PyAudio()
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        
        if not default_speakers["isLoopbackDevice"]:
            for loopback in p.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    default_speakers = loopback
                    break

        src_rate = int(default_speakers["defaultSampleRate"])
        channels = default_speakers["maxInputChannels"]
        emit(f"[音频] 正在采集：{default_speakers['name']}（{src_rate}Hz，{channels} 声道）", "sys")

        def audio_callback(in_data, frame_count, time_info, status):
            raw_audio = np.frombuffer(in_data, dtype=np.int16)
            if channels > 1:
                raw_audio = raw_audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
            
            # Resample to 16000Hz
            if src_rate != TARGET_SAMPLE_RATE:
                num_target_samples = int(len(raw_audio) * TARGET_SAMPLE_RATE / src_rate)
                indices = np.linspace(0, len(raw_audio) - 1, num_target_samples)
                resampled_audio = np.interp(indices, np.arange(len(raw_audio)), raw_audio).astype(np.int16)
            else:
                resampled_audio = raw_audio

            pcm_bytes = resampled_audio.tobytes()

            def safe_put():
                if audio_queue.full():
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    audio_queue.put_nowait(pcm_bytes)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(safe_put)
            return (None, pyaudio.paContinue)

        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=src_rate,
            input=True,
            input_device_index=default_speakers["index"],
            frames_per_buffer=4800,
            stream_callback=audio_callback
        )
        stream.start_stream()
        stop_event.wait()
        stream.stop_stream()
        stream.close()
    except Exception as e:
        emit(f"[音频错误] {e}", "err")
    finally:
        p.terminate()

async def capture_and_send_loopback(session, audio_queue: asyncio.Queue, stop_event: asyncio.Event, emit):
    """Stream captured audio chunks to Gemini Live Translate."""
    idle_ticks = 0
    while not stop_event.is_set():
        try:
            pcm_bytes = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
            idle_ticks = 0
        except asyncio.TimeoutError:
            # WASAPI loopback delivers nothing while no audio plays; keep the stream continuous.
            idle_ticks += 1
            if idle_ticks < 3:
                continue
            pcm_bytes = SILENCE_CHUNK

        try:
            await session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
        except Exception as e:
            if not stop_event.is_set():
                emit(f"[发送错误] {e}", "err")
            break

async def receive_translation(session, stop_event: asyncio.Event, emit):
    """Emit translated text immediately, replacing punctuation with line breaks."""
    line_open = False
    try:
        while not stop_event.is_set():
            async for response in session.receive():
                if stop_event.is_set():
                    break

                # Server warns the session is about to expire; close now and reconnect.
                if getattr(response, "go_away", None) is not None:
                    if line_open:
                        emit("\n")
                        line_open = False
                    emit("[系统] 会话即将过期，正在重新连接…", "sys")
                    stop_event.set()
                    return

                server_content = response.server_content
                if not server_content or not server_content.output_transcription:
                    continue

                text = server_content.output_transcription.text or ""
                for character in text:
                    if unicodedata.category(character).startswith("P"):
                        if line_open:
                            emit("\n")
                            line_open = False
                    elif character.isspace():
                        if line_open:
                            emit(character)
                    else:
                        emit(character)
                        line_open = True

                if server_content.turn_complete and line_open:
                    emit("\n")
                    line_open = False

    except asyncio.CancelledError:
        pass
    except Exception as e:
        if not stop_event.is_set():
            emit(f"[翻译错误] {e}", "err")
    finally:
        # Stop the sender too so run_session returns and a new session is opened.
        stop_event.set()

async def run_session(client, audio_queue: asyncio.Queue, translation_config, emit):
    """Run one Live API session until it ends or expires."""
    session_stop_event = asyncio.Event()

    async with client.aio.live.connect(model=MODEL_ID, config=translation_config) as session:
        emit("[系统] 已连接，实时翻译进行中", "sys")

        send_task = asyncio.create_task(capture_and_send_loopback(session, audio_queue, session_stop_event, emit))
        translation_task = asyncio.create_task(receive_translation(session, session_stop_event, emit))

        try:
            await asyncio.gather(send_task, translation_task)
        finally:
            session_stop_event.set()
            send_task.cancel()
            translation_task.cancel()
            await asyncio.gather(send_task, translation_task, return_exceptions=True)


async def translate_main(api_key: str, emit):
    client = genai.Client(api_key=api_key)
    translation_config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(
            target_language_code="zh-Hans",
        ),
    )
    loop = asyncio.get_running_loop()
    audio_queue = asyncio.Queue(maxsize=30)
    producer_stop_event = threading.Event()

    capture_thread = threading.Thread(
        target=loopback_producer,
        args=(audio_queue, loop, producer_stop_event, emit),
        daemon=True
    )
    capture_thread.start()

    emit("[系统] 正在连接 Gemini Live API…", "sys")

    retry_delay = 1.0
    try:
        while True:
            try:
                await run_session(client, audio_queue, translation_config, emit)
                retry_delay = 1.0
            except asyncio.CancelledError:
                emit("[系统] 已停止实时翻译", "sys")
                break
            except Exception as e:
                emit(f"[系统] 连接已断开（{e}），{retry_delay:.0f} 秒后重新连接…", "err")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

            # Drop stale audio buffered during the reconnect gap.
            while not audio_queue.empty():
                audio_queue.get_nowait()
    finally:
        producer_stop_event.set()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui_queue = queue.Queue()
        self.loop = None
        self.task = None
        self.worker = None

        root.title("Live Translate")
        root.geometry("720x420")
        root.minsize(480, 240)
        root.configure(bg=BG)
        self.setup_style()

        container = ttk.Frame(root, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(toolbar, text="Google AI Studio API Key", style="Caption.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        self.key_var = tk.StringVar(value=load_api_key() or os.getenv("GEMINI_API_KEY", ""))
        self.key_entry = ttk.Entry(toolbar, textvariable=self.key_var, show="•", font=(UI_FONT, 9))
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.run_btn = ttk.Button(toolbar, text="启动", style="Accent.TButton", command=self.toggle_running)
        self.run_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Button(toolbar, text="清空", command=lambda: self.text.delete("1.0", tk.END)).pack(side=tk.LEFT, padx=(4, 0))

        self.topmost = False
        self.ui_scale = root.winfo_fpixels("1i") / 96
        pin_size = round(24 * self.ui_scale)
        self.pin = tk.Canvas(toolbar, width=pin_size, height=pin_size, bg=BG, highlightthickness=0, cursor="hand2")
        self.pin.pack(side=tk.LEFT, padx=(4, 0))
        self.pin.bind("<Button-1>", lambda e: self.toggle_topmost())
        self.pin.bind("<Enter>", lambda e: self.draw_pin(hover=True))
        self.pin.bind("<Leave>", lambda e: self.draw_pin())
        self.draw_pin()

        cfg = load_config()
        proxy_row = ttk.Frame(container)
        proxy_row.pack(fill=tk.X, pady=(0, 8))
        self.proxy_enabled = tk.BooleanVar(value=bool(cfg.get("proxy_enabled", False)))
        self.proxy_check = ttk.Checkbutton(proxy_row, text="HTTP 代理", variable=self.proxy_enabled,
                                           command=self.update_proxy_state)
        self.proxy_check.pack(side=tk.LEFT, padx=(0, 6))
        self.proxy_host_var = tk.StringVar(value=cfg.get("proxy_host", "127.0.0.1"))
        self.proxy_host_entry = ttk.Entry(proxy_row, textvariable=self.proxy_host_var, width=18, font=(UI_FONT, 9))
        self.proxy_host_entry.pack(side=tk.LEFT)
        ttk.Label(proxy_row, text=":", style="Caption.TLabel").pack(side=tk.LEFT, padx=2)
        self.proxy_port_var = tk.StringVar(value=str(cfg.get("proxy_port", "7890")))
        self.proxy_port_entry = ttk.Entry(proxy_row, textvariable=self.proxy_port_var, width=7, font=(UI_FONT, 9))
        self.proxy_port_entry.pack(side=tk.LEFT)
        self.update_proxy_state()

        text_frame = tk.Frame(container, bg=CARD, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=BORDER)
        text_frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(text_frame, wrap=tk.WORD, font=(UI_FONT, 14), bg=CARD, fg=TEXT,
                            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
                            padx=12, pady=8, spacing3=4, insertbackground=TEXT,
                            selectbackground="#bfdbfe", selectforeground=TEXT)
        scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.text.tag_configure("sys", foreground=MUTED, font=(UI_FONT, 10))
        self.text.tag_configure("err", foreground=DANGER, font=(UI_FONT, 10))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll_queue)

    def setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=(UI_FONT, 9))
        style.configure("TFrame", background=BG)
        style.configure("Caption.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 9))
        style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=4)
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)], fieldbackground=[("disabled", BG)])

        flat = dict(borderwidth=0, relief=tk.FLAT, focusthickness=0, padding=(10, 3), width=-4)
        style.configure("TButton", background="#e5e7eb", foreground=TEXT, **flat)
        style.map("TButton", background=[("pressed", "#cbd5e1"), ("active", "#d1d5db")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", **flat)
        style.map("Accent.TButton", background=[("disabled", "#93c5fd"), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", "white")])
        style.configure("Danger.TButton", background=DANGER, foreground="white", **flat)
        style.map("Danger.TButton", background=[("disabled", "#fca5a5"), ("active", DANGER_HOVER)],
                  foreground=[("disabled", "white")])

        scale = self.root.winfo_fpixels("1i") / 96
        style.configure("TCheckbutton", background=BG, foreground=TEXT, focusthickness=0,
                        indicatorsize=round(14 * scale), indicatormargin=(0, 0, round(4 * scale), 0),
                        indicatorbackground=CARD, indicatorforeground="white",
                        upperbordercolor="#9ca3af", lowerbordercolor="#9ca3af")
        style.map("TCheckbutton", background=[("active", BG)],
                  indicatorbackground=[("selected", ACCENT), ("active", "#f9fafb")],
                  upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        style.configure("Vertical.TScrollbar", background="#d1d5db", troughcolor=CARD,
                        bordercolor=CARD, lightcolor="#d1d5db", darkcolor="#d1d5db",
                        arrowsize=0, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", "#9ca3af")])

    def emit(self, text: str, tag: str = "text"):
        """tag: 'text' for translation output, 'sys' for status, 'err' for errors."""
        self.ui_queue.put((tag, text))

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                at_line_start = self.text.get("end-2c", "end-1c") in ("", "\n")
                if kind == "text":
                    if payload == "\n" and at_line_start:
                        continue
                    self.text.insert(tk.END, payload)
                elif kind in ("sys", "err"):
                    prefix = "" if at_line_start else "\n"
                    self.text.insert(tk.END, f"{prefix}{payload}\n", kind)
                elif kind == "stopped":
                    self.on_stopped()
                    continue
                self.text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(50, self.poll_queue)

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        self.draw_pin(hover=True)

    def draw_pin(self, hover: bool = False):
        c = self.pin
        c.delete("all")
        fg = ACCENT if self.topmost else MUTED
        c.configure(bg="#dbeafe" if self.topmost else ("#e5e7eb" if hover else BG))
        # Upright when pinned, tilted 45° when not.
        angle = 0 if self.topmost else math.radians(45)
        cos, sin = math.cos(angle), math.sin(angle)

        def rot(points):
            out = []
            for x, y in points:
                dx, dy = x - 12, y - 12
                out += [12 + dx * cos - dy * sin, 12 + dx * sin + dy * cos]
            return out

        for poly in ([(9, 3), (15, 3), (15, 6), (9, 6)],
                     [(10, 6), (14, 6), (14, 12), (10, 12)],
                     [(7, 12), (17, 12), (17, 14), (7, 14)]):
            c.create_polygon(rot(poly), fill=fg, outline=fg)
        c.create_line(rot([(12, 14), (12, 21)]), fill=fg, width=2 * self.ui_scale)
        c.scale("all", 0, 0, self.ui_scale, self.ui_scale)

    def update_proxy_state(self):
        running = self.worker is not None and self.worker.is_alive()
        self.proxy_check.config(state=tk.DISABLED if running else tk.NORMAL)
        state = tk.NORMAL if self.proxy_enabled.get() and not running else tk.DISABLED
        self.proxy_host_entry.config(state=state)
        self.proxy_port_entry.config(state=state)

    def get_proxy_url(self) -> str | None:
        """Return the proxy URL, None if disabled, or raise ValueError if invalid."""
        if not self.proxy_enabled.get():
            return None
        host = self.proxy_host_var.get().strip().removeprefix("http://").rstrip("/")
        port = self.proxy_port_var.get().strip()
        if not host or any(ch in host for ch in " /:@"):
            raise ValueError("请输入有效的代理 IP 或主机名")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("代理端口应为 1-65535 之间的数字")
        self.proxy_host_var.set(host)
        return f"http://{host}:{port}"

    def toggle_running(self):
        if self.worker and self.worker.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        api_key = self.key_var.get().strip()
        if not api_key:
            messagebox.showwarning("提示", "请先输入 API Key")
            return
        try:
            proxy_url = self.get_proxy_url()
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        try:
            if api_key != load_api_key():
                save_api_key(api_key)
            cfg = load_config()
            cfg.update(proxy_enabled=self.proxy_enabled.get(),
                       proxy_host=self.proxy_host_var.get().strip(),
                       proxy_port=self.proxy_port_var.get().strip())
            save_config(cfg)
        except Exception as e:
            self.emit(f"[系统] 保存设置失败：{e}", "err")
        apply_proxy(proxy_url)
        self.emit(f"[系统] 代理：{proxy_url}" if proxy_url else "[系统] 代理：未启用", "sys")
        self.run_btn.config(text="停止", style="Danger.TButton")
        self.key_entry.config(state=tk.DISABLED)
        self.loop = asyncio.new_event_loop()
        self.task = self.loop.create_task(translate_main(api_key, self.emit))
        self.worker = threading.Thread(target=self.run_worker, daemon=True)
        self.worker.start()
        self.update_proxy_state()

    def run_worker(self):
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.emit(f"[错误] {e}", "err")
        finally:
            self.loop.close()
            self.ui_queue.put(("stopped", None))

    def stop(self):
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.run_btn.config(text="停止中...", state=tk.DISABLED)

    def on_stopped(self):
        self.loop = None
        self.task = None
        self.worker = None
        self.run_btn.config(text="启动", state=tk.NORMAL, style="Accent.TButton")
        self.key_entry.config(state=tk.NORMAL)
        self.update_proxy_state()

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            self.worker.join(timeout=3)
        self.root.destroy()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp rendering on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()