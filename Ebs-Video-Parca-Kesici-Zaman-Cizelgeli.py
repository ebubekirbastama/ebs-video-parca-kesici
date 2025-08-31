#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ebs Video Parça Kesici (Metro GUI)
- Video önizleme (OpenCV + Canvas, UI-thread güvenli)
- Ses (opsiyonel): ffplay ile senkron oynatma
- Ayrı Oynatma Çizgisi (SeekBar): tıkla/çek → ileri sar (scrub)
- Parça Seçim Timeline: sürükle-bırak ile çoklu aralık
- HIZLI (stream copy) / HASSAS (yeniden kodlama) kesim
- Atomik seek: read() ve set(POS) aynı lock ile korundu (assert fix)

Gereksinimler:
  pip install customtkinter opencv-python pillow
  ffmpeg, ffprobe, ffplay PATH'te (Windows: C:\\ffmpeg\\bin'i PATH'e ekleyin)
"""

import os
import threading
import queue
import subprocess
import shlex
import shutil
from dataclasses import dataclass
from typing import List, Tuple, Optional, Callable, Dict

# GUI
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

# Video/görüntü
import cv2
from PIL import Image, ImageTk

APP_TITLE = "Ebs Video Parça Kesici (Metro GUI)"
VERSION = "1.5.0"  # scrub + atomik seek + seekbar

# ---------------- Yardımcılar ----------------

def which_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")

def which_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")

def which_ffplay() -> Optional[str]:
    return shutil.which("ffplay")

def probe_duration(path: str) -> Optional[float]:
    """ffprobe ile toplam süre (saniye) okur."""
    if not which_ffprobe():
        return None
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        dur = float(out)
        if dur > 0:
            return dur
    except Exception:
        pass
    return None

def parse_time_to_seconds(text: str) -> float:
    """
    Desteklenenler: "hh:mm:ss(.ms)", "mm:ss", "ss(.ms)"
    Ondalıkta virgül de kabul: "12,5"
    """
    s = text.strip().replace(",", ".")
    if not s:
        raise ValueError("Boş zaman")
    if ":" not in s:
        return float(s)
    parts = s.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    if len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    raise ValueError(f"Zaman formatı hatalı: {text}")

def seconds_to_hms(seconds: float, ms: bool = True) -> str:
    if seconds < 0:
        seconds = 0
    ms_part = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms_part:03d}" if ms else f"{h:02d}:{m:02d}:{s:02d}"

# ---------------- Veri Modeli ----------------

@dataclass
class Segment:
    start_str: str
    end_str: str
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    def to_label(self) -> str:
        return f"{self.start_str} → {self.end_str}"

# ---------------- FFmpeg İşçisi ----------------

class CutterWorker(threading.Thread):
    """Kesimleri arka planda yapan iş parçacığı (UI’yi bloklamaz)."""
    def __init__(self, video_path: str, segments: List[Segment], out_dir: str,
                 mode: str, codec: str, crf: int, preset: str,
                 audio_mode: str, audio_bitrate: str, message_queue: queue.Queue):
        super().__init__(daemon=True)
        self.video_path = video_path
        self.segments = segments
        self.out_dir = out_dir
        self.mode = mode
        self.codec = codec
        self.crf = crf
        self.preset = preset
        self.audio_mode = audio_mode
        self.audio_bitrate = audio_bitrate
        self.q = message_queue
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def run(self):
        try:
            base = os.path.splitext(os.path.basename(self.video_path))[0]
            total = len(self.segments)
            self.q.put(("overall", f"Toplam {total} parça kesilecek…"))

            for idx, seg in enumerate(self.segments, start=1):
                if self.stop_requested:
                    self.q.put(("log", "İşlem kullanıcı tarafından durduruldu."))
                    break

                duration = seg.duration_sec
                if duration <= 0:
                    self.q.put(("segment_status", (idx - 1, "HATA: süre <= 0")))
                    continue

                start_hms = seconds_to_hms(seg.start_sec)
                dur_hms = seconds_to_hms(duration)

                safe_start = seg.start_str.replace(":", "-")
                safe_end = seg.end_str.replace(":", "-")
                out_name = f"{base}_parca{idx:02d}_{safe_start}_to_{safe_end}.mp4"
                out_path = os.path.join(self.out_dir, out_name)

                if self.mode == "FAST":
                    cmd = ["ffmpeg", "-y", "-ss", start_hms, "-i", self.video_path, "-t", dur_hms,
                           "-c", "copy", "-avoid_negative_ts", "1", out_path]
                else:
                    cmd = ["ffmpeg", "-y", "-i", self.video_path, "-ss", start_hms, "-t", dur_hms,
                           "-map", "0", "-c:v", self.codec, "-preset", self.preset, "-crf", str(self.crf)]
                    if self.audio_mode == "copy":
                        cmd += ["-c:a", "copy"]
                    else:
                        cmd += ["-c:a", "aac", "-b:a", self.audio_bitrate]
                    cmd += [out_path]

                self.q.put(("segment_status", (idx - 1, "ÇALIŞIYOR…")))
                self.q.put(("log", f"[{idx}/{total}] Çıktı: {out_name}"))
                self.q.put(("log", "Komut: " + " ".join(shlex.quote(c) for c in cmd)))

                try:
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if proc.returncode == 0:
                        self.q.put(("segment_status", (idx - 1, "BİTTİ")))
                    else:
                        self.q.put(("segment_status", (idx - 1, f"HATA (kod {proc.returncode})")))
                        self.q.put(("log", proc.stdout[-1000:] if proc.stdout else "Hata çıktısı yok."))
                except Exception as e:
                    self.q.put(("segment_status", (idx - 1, f"HATA: {e}")))

                self.q.put(("progress", idx / total))

            self.q.put(("done", None))
        except Exception as e:
            self.q.put(("fatal", str(e)))

# ---------------- Video Player (Canvas + ffplay ses + atomik seek) ----------------

class VideoPlayer(ctk.CTkFrame):
    """OpenCV ile video görüntüleme (Canvas), ffplay ile opsiyonel ses, UI-thread güvenli ve atomik seek."""
    def __init__(self, master, on_seek: Callable[[float], None]):
        super().__init__(master, corner_radius=12)
        self.on_seek = on_seek
        self.cap: Optional[cv2.VideoCapture] = None
        self.path: Optional[str] = None
        self.duration: float = 0.0
        self.fps: float = 30.0
        self.playing = False
        self.current_sec: float = 0.0

        # Thread güvenliği ve scrub durumu
        self._cap_lock = threading.RLock()
        self._scrubbing = False
        self._was_playing_before_scrub = False

        # Görüntüleme (Canvas)
        frame = ctk.CTkFrame(self, corner_radius=12)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas = tk.Canvas(frame, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._image_id = None
        self._imgtk = None
        self.max_preview_w, self.max_preview_h = 960, 540  # ağır dosyalarda 720x405 yapabilirsin

        # Kontroller
        ctrl = ctk.CTkFrame(self, corner_radius=12)
        ctrl.pack(fill="x", padx=8, pady=(0, 8))
        self.play_btn = ctk.CTkButton(ctrl, text="▶ Play", width=80, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=6, pady=6)
        self.time_var = tk.StringVar(value="00:00:00 / 00:00:00")
        ctk.CTkLabel(ctrl, textvariable=self.time_var).pack(side="left", padx=10)

        # Arka plan okuma
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._pending_frame = None

        # Ses (ffplay)
        self._ffplay_proc: Optional[subprocess.Popen] = None
        self._ffplay_available = which_ffplay() is not None
        self._warned_ffplay = False

    # ---------- Yaşam döngüsü ----------
    def load(self, path: str, duration: float):
        self.close()
        self.path = path
        self.duration = max(0.01, duration or 0.01)
        with self._cap_lock:
            self.cap = cv2.VideoCapture(path)
            if not self.cap or not self.cap.isOpened():
                messagebox.showerror("Video", "Video açılamadı.")
                self.cap = None
                return
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            self.fps = fps if fps and fps > 1 else 30.0
        self.seek(0.0, emit=False)  # ilk kare
        self.update_time_label()
        self.playing = False
        self.play_btn.configure(text="▶ Play")

    def close(self):
        self.stop_audio()
        self.stop_thread()
        with self._cap_lock:
            if self.cap:
                self.cap.release()
            self.cap = None
        self._clear_canvas()
        self.current_sec = 0.0

    # ---------- Play/Pause ----------
    def toggle_play(self):
        if not self.cap:
            return
        if self.playing:
            self.playing = False
            self.play_btn.configure(text="▶ Play")
            self.stop_audio()
            self.stop_thread()
        else:
            self.playing = True
            self.play_btn.configure(text="⏸ Pause")
            self.start_thread()
            self.start_audio(self.current_sec)

    # ---------- Scrub API (seekbar sürükleme) ----------
    def begin_scrub(self):
        if self._scrubbing:
            return
        self._was_playing_before_scrub = self.playing
        # durdur
        self.playing = False
        self.play_btn.configure(text="▶ Play")
        self.stop_audio()
        self.stop_thread()
        self._scrubbing = True

    def scrub_to(self, sec: float):
        # sürükleme esnasında hızlı seek (ses yok, thread yok)
        self.seek(sec, emit=False)

    def end_scrub(self):
        if not self._scrubbing:
            return
        self._scrubbing = False
        if self._was_playing_before_scrub:
            self.playing = True
            self.play_btn.configure(text="⏸ Pause")
            self.start_thread()
            self.start_audio(self.current_sec)

    # ---------- Thread yönetimi ----------
    def start_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def stop_thread(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        self._thread = None

    def _reader_loop(self):
        import time
        frame_interval = 1.0 / max(self.fps, 1.0)
        next_t = time.time()
        while not self._stop_evt.is_set() and self.playing:
            with self._cap_lock:
                if not self.cap:
                    break
                ok, frame = self.cap.read()
                pos_ms = self.cap.get(cv2.CAP_PROP_POS_MSEC) if ok else 0.0
            if not ok:
                break  # video sonu
            self.current_sec = float(pos_ms) / 1000.0
            self._pending_frame = frame
            self.after(0, self._deliver_frame, self.current_sec)

            next_t += frame_interval
            sleep_t = next_t - time.time()
            if sleep_t > 0:
                time.sleep(sleep_t)
            else:
                next_t = time.time()

        # döngü bitti
        self.after(0, lambda: self.play_btn.configure(text="▶ Play") if not self.playing else None)
        self.stop_audio()

    def _deliver_frame(self, sec: float):
        if self._pending_frame is None:
            return
        frame = self._pending_frame
        self._pending_frame = None
        self._show_frame(frame)
        self.current_sec = sec
        self.update_time_label()
        if self.on_seek:
            self.on_seek(sec)

    # ---------- Çizim ----------
    def _clear_canvas(self):
        if self._image_id:
            try:
                self.canvas.delete(self._image_id)
            except Exception:
                pass
        self._image_id = None
        self._imgtk = None

    def _show_frame(self, frame):
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        cw = self.canvas.winfo_width() or self.max_preview_w
        ch = self.canvas.winfo_height() or self.max_preview_h
        cw = min(cw, self.max_preview_w)
        ch = min(ch, self.max_preview_h)
        fh, fw = frame.shape[:2]
        scale = min(cw / fw, ch / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        if (nw, nh) != (fw, fh):
            frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        img = ImageTk.PhotoImage(Image.fromarray(frame))
        if self._image_id is None:
            self._image_id = self.canvas.create_image(cw // 2, ch // 2, image=img)
        else:
            self.canvas.coords(self._image_id, cw // 2, ch // 2)
            self.canvas.itemconfigure(self._image_id, image=img)
        self._imgtk = img

    # ---------- Seek (ATOMİK) ----------
    def seek(self, sec: float, emit: bool = True):
        if not self.cap:
            return
        sec = max(0.0, min(sec, self.duration))
        ok = False
        frame = None
        with self._cap_lock:
            if not self.cap:
                return
            self.cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
            ok, frame = self.cap.read()
            if ok:
                # Pozisyonu kilitle (bazı kodeklerde read sonrası ileri kayabilir)
                self.cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
        if ok and frame is not None:
            self._pending_frame = frame
            self.after(0, self._deliver_frame, sec)
        # Sadece normal seek’te sesi güncelle (scrub değil)
        if self.playing and (not self._scrubbing):
            self.restart_audio(sec)

    def update_time_label(self):
        self.time_var.set(f"{seconds_to_hms(self.current_sec, ms=False)} / {seconds_to_hms(self.duration, ms=False)}")

    # ---------- Ses (ffplay) ----------
    def start_audio(self, start_sec: float):
        if not self._ffplay_available or not self.path:
            if not self._ffplay_available and not self._warned_ffplay:
                self._warned_ffplay = True
                try:
                    messagebox.showinfo("Bilgi", "Ses için 'ffplay' bulunamadı. FFmpeg paketinizde 'ffplay.exe' olduğundan ve PATH'e eklendiğinden emin olun.")
                except Exception:
                    pass
            return
        self.stop_audio()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._ffplay_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-ss", f"{start_sec:.3f}", self.path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
            )
        except Exception:
            # sessizce geç
            pass

    def stop_audio(self):
        if self._ffplay_proc and self._ffplay_proc.poll() is None:
            try:
                self._ffplay_proc.terminate()
            except Exception:
                pass
        self._ffplay_proc = None

    def restart_audio(self, sec: float):
        self.stop_audio()
        self.start_audio(sec)

# ---------------- SeekBar (oynatma çizgisi) ----------------

class SeekBar(ctk.CTkFrame):
    """Basit oynatma çizgisi: tıkla/çek → seek, playhead gösterimi, scrub başlangıç/bitiş callback."""
    def __init__(self, master, on_seek_request: Callable[[float], None],
                 on_scrub_start: Optional[Callable[[], None]] = None,
                 on_scrub_end: Optional[Callable[[], None]] = None):
        super().__init__(master, corner_radius=12)
        self.on_seek_request = on_seek_request
        self.on_scrub_start = on_scrub_start
        self.on_scrub_end = on_scrub_end

        self.duration_sec: float = 0.0
        self.padding = 12
        self.height = 40
        self.canvas_width = 800

        self.canvas = tk.Canvas(self, height=self.height, bg="#181818", highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="x", expand=True, padx=8, pady=6)
        self.canvas.bind("<Configure>", self._on_resize)

        # Etkileşim
        self._mouse_down = False
        self.canvas.bind("<Button-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_up)

        self.playhead_id: Optional[int] = None
        self.playhead_sec: float = 0.0

        self._draw_base()

    def set_duration(self, seconds: float):
        self.duration_sec = max(0.01, float(seconds))
        self._redraw()

    def set_playhead(self, sec: float):
        if self.duration_sec <= 0:
            return
        self.playhead_sec = max(0.0, min(sec, self.duration_sec))
        x = self._sec_to_x(self.playhead_sec)
        if self.playhead_id:
            self.canvas.coords(self.playhead_id, x, self.padding-4, x, self.height - self.padding + 4)
        else:
            self.playhead_id = self.canvas.create_line(x, self.padding-4, x, self.height - self.padding + 4,
                                                       fill="#ffffff", width=2)
        self._raise_overlays()

    # --- mouse ---
    def _on_down(self, e):
        self._mouse_down = True
        if self.on_scrub_start:
            self.on_scrub_start()
        self._seek_to_event(e)

    def _on_drag(self, e):
        if not self._mouse_down:
            return
        self._seek_to_event(e)

    def _on_up(self, e):
        if self._mouse_down and self.on_scrub_end:
            self.on_scrub_end()
        self._mouse_down = False

    def _seek_to_event(self, e):
        if self.duration_sec <= 0:
            return
        x = self._clamp_x(e.x)
        sec = self._x_to_sec(x)
        self.set_playhead(sec)
        if self.on_seek_request:
            self.on_seek_request(sec)

    # --- çizim ---
    def _draw_base(self):
        self.canvas.delete("base")
        w = self.canvas.winfo_width() or self.canvas_width
        h = self.height
        self.canvas.create_rectangle(
            self.padding, self.padding, w - self.padding, h - self.padding,
            fill="#2a2a2a", outline="#3a3a3a", width=1, tags="base"
        )
        self._draw_ticks()

    def _draw_ticks(self):
        self.canvas.delete("ticks")
        if self.duration_sec <= 0:
            return
        total = self.duration_sec
        step = max(5, int(total / 6))
        sec = 0
        while sec <= total + 0.01:
            x = self._sec_to_x(sec)
            self.canvas.create_line(x, self.height - self.padding, x, self.height - self.padding - 8,
                fill="#777", width=1, tags="ticks")
            label = self._sec_label(sec)
            self.canvas.create_text(x, self.height - self.padding - 12, text=label, fill="#aaa",
                font=("Segoe UI", 9), tags="ticks")
            sec += step
        self._raise_overlays()

    def _raise_overlays(self):
        self.canvas.tag_raise("ticks")
        if self.playhead_id:
            self.canvas.tag_raise(self.playhead_id)

    def _on_resize(self, _e):
        self._redraw()

    def _redraw(self):
        self._draw_base()
        if self.playhead_id:
            self.canvas.delete(self.playhead_id)
            self.playhead_id = None
        if self.duration_sec > 0:
            self.set_playhead(self.playhead_sec)

    # dönüşümler
    def _sec_to_x(self, sec: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        left, right = self.padding, w - self.padding
        ratio = min(max(sec / self.duration_sec, 0.0), 1.0)
        return left + ratio * (right - left)

    def _x_to_sec(self, x: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        left, right = self.padding, w - self.padding
        ratio = min(max((x - left) / (right - left), 0.0), 1.0)
        return ratio * self.duration_sec

    def _clamp_x(self, x: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        return min(max(x, self.padding), w - self.padding)

    def _sec_label(self, sec: float) -> str:
        s = int(round(sec))
        hh = s // 3600; mm = (s % 3600) // 60; ss = s % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"

# ---------------- Timeline (çoklu aralık) ----------------

class Timeline(ctk.CTkFrame):
    """Zaman çizgisi: çoklu aralık + playhead + seek (sağ tık/çift tık)."""
    def __init__(self, master,
                 on_new_selection: Callable[[float, float], None],
                 on_delete_selection: Callable[[float, float], None],
                 on_seek_request: Callable[[float], None]):
        super().__init__(master, corner_radius=12)
        self.on_new_selection = on_new_selection
        self.on_delete_selection = on_delete_selection
        self.on_seek_request = on_seek_request

        self.duration_sec: float = 0.0
        self.padding = 12
        self.height = 80
        self.canvas_width = 800

        self.canvas = tk.Canvas(self, height=self.height, bg="#202020", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="x", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", self._on_resize)

        # Etkileşim (seçim)
        self.dragging = False
        self.drag_start_x = 0
        self.temp_rect_id = None

        self.canvas.bind("<Button-1>", self._on_down)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_up)

        # Seek: sağ tık veya çift sol tık
        self.canvas.bind("<Button-3>", self._on_seek_click)
        self.canvas.bind("<Double-Button-1>", self._on_seek_double)

        # Seçimler
        self.rects: List[int] = []
        self.data: Dict[int, Tuple[float, float]] = {}
        self.selected_rect: Optional[int] = None

        # Klavye: delete
        self.canvas.bind_all("<Delete>", self._on_delete)

        # Playhead
        self.playhead_id: Optional[int] = None
        self.playhead_sec: Optional[float] = None

        self._draw_base()

    def set_duration(self, seconds: float):
        self.duration_sec = max(0.01, float(seconds))
        self._redraw()

    def clear(self):
        for rid in list(self.rects):
            self.canvas.delete(rid)
        self.rects.clear()
        self.data.clear()
        self.selected_rect = None
        self._redraw()

    def add_segment(self, start_sec: float, end_sec: float):
        if end_sec <= start_sec or self.duration_sec <= 0:
            return
        x1 = self._sec_to_x(start_sec)
        x2 = self._sec_to_x(end_sec)
        rid = self.canvas.create_rectangle(
            x1, self.padding, x2, self.height - self.padding,
            fill="#2d7ef7", outline="#6fa8ff", width=1
        )
        self.rects.append(rid)
        self.data[rid] = (start_sec, end_sec)
        self._raise_overlays()
        self._draw_ticks()

    # ---- Playhead ----
    def set_playhead(self, sec: float):
        if self.duration_sec <= 0:
            return
        sec = max(0.0, min(sec, self.duration_sec))
        x = self._sec_to_x(sec)
        self.playhead_sec = sec
        if self.playhead_id:
            self.canvas.coords(self.playhead_id, x, self.padding-4, x, self.height - self.padding + 4)
        else:
            self.playhead_id = self.canvas.create_line(x, self.padding-4, x, self.height - self.padding + 4,
                                                       fill="#ffffff", width=2)
        self._raise_overlays()

    # ---- Etkileşim ----
    def _on_down(self, e):
        rid = self._find_rect_at(e.x, e.y)
        if rid:
            self._select_rect(rid)
            return
        self._select_rect(None)
        self.dragging = True
        self.drag_start_x = self._clamp_x(e.x)
        if self.temp_rect_id:
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None

    def _on_drag(self, e):
        if not self.dragging:
            return
        x1 = self.drag_start_x
        x2 = self._clamp_x(e.x)
        if self.temp_rect_id:
            self.canvas.coords(self.temp_rect_id, min(x1, x2), self.padding, max(x1, x2), self.height - self.padding)
        else:
            self.temp_rect_id = self.canvas.create_rectangle(
                min(x1, x2), self.padding, max(x1, x2), self.height - self.padding,
                fill="#1ea97c", outline="#7fe3c9", width=1, stipple="gray50"
            )
        self._raise_overlays()

    def _finalize_temp(self):
        if not self.temp_rect_id:
            return None
        x1, _, x2, _ = self.canvas.coords(self.temp_rect_id)
        self.canvas.delete(self.temp_rect_id)
        self.temp_rect_id = None
        if abs(x2 - x1) < 3:
            return None
        s = self._x_to_sec(min(x1, x2))
        t = self._x_to_sec(max(x1, x2))
        self.add_segment(s, t)
        if self.on_new_selection:
            self.on_new_selection(s, t)
        if self.on_seek_request:
            self.on_seek_request(s)
        return (s, t)

    def _on_up(self, _e):
        self.dragging = False
        self._finalize_temp()

    def _on_seek_click(self, e):
        sec = self._x_to_sec(self._clamp_x(e.x))
        if self.on_seek_request:
            self.on_seek_request(sec)

    def _on_seek_double(self, e):
        sec = self._x_to_sec(self._clamp_x(e.x))
        if self.on_seek_request:
            self.on_seek_request(sec)

    def _on_delete(self, _e=None):
        if not self.selected_rect:
            return
        s, t = self.data.get(self.selected_rect, (None, None))
        self.canvas.delete(self.selected_rect)
        self.rects.remove(self.selected_rect)
        del self.data[self.selected_rect]
        self.selected_rect = None
        self._draw_ticks()
        if s is not None and t is not None and self.on_delete_selection:
            self.on_delete_selection(s, t)

    # ---- Görsel ----
    def _draw_base(self):
        self.canvas.delete("base")
        w = self.canvas.winfo_width() or self.canvas_width
        h = self.height
        self.canvas.create_rectangle(
            self.padding, self.padding, w - self.padding, h - self.padding,
            fill="#2a2a2a", outline="#3a3a3a", width=1, tags="base"
        )
        self._draw_ticks()

    def _draw_ticks(self):
        self.canvas.delete("ticks")
        if self.duration_sec <= 0:
            return
        total = self.duration_sec
        step = max(5, int(total / 6))
        sec = 0
        while sec <= total + 0.01:
            x = self._sec_to_x(sec)
            self.canvas.create_line(x, self.height - self.padding, x, self.height - self.padding - 8,
                                    fill="#888", width=1, tags="ticks")
            label = self._sec_label(sec)
            self.canvas.create_text(x, self.height - self.padding - 12, text=label, fill="#aaa",
                                    font=("Segoe UI", 9), tags="ticks")
            sec += step
        if self.playhead_id:
            self._raise_overlays()

    def _raise_overlays(self):
        for rid in self.rects:
            self.canvas.tag_raise(rid)
        if self.temp_rect_id:
            self.canvas.tag_raise(self.temp_rect_id)
        self.canvas.tag_raise("ticks")
        if self.playhead_id:
            self.canvas.tag_raise(self.playhead_id)

    def _on_resize(self, _e):
        self._redraw()

    def _redraw(self):
        """Zaman çizgisini (taban, aralıklar, playhead) yeniden çizer."""
        self._draw_base()
        new_rects, new_data = [], {}
        for rid in list(self.rects):
            s, t = self.data[rid]
            try:
                self.canvas.delete(rid)
            except Exception:
                pass
            x1 = self._sec_to_x(s)
            x2 = self._sec_to_x(t)
            nr = self.canvas.create_rectangle(
                x1, self.padding, x2, self.height - self.padding,
                fill="#2d7ef7", outline="#6fa8ff", width=1
            )
            new_rects.append(nr)
            new_data[nr] = (s, t)
        self.rects, self.data = new_rects, new_data
        if self.playhead_sec is not None:
            self.set_playhead(self.playhead_sec)
        self._raise_overlays()

    def _select_rect(self, rid: Optional[int]):
        if self.selected_rect and self.selected_rect in self.rects:
            self.canvas.itemconfigure(self.selected_rect, outline="#6fa8ff", width=1)
        self.selected_rect = rid
        if rid:
            self.canvas.itemconfigure(rid, outline="#ffffff", width=2)

    # ---- Dönüşümler ----
    def _sec_to_x(self, sec: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        left, right = self.padding, w - self.padding
        ratio = min(max(sec / self.duration_sec, 0.0), 1.0)
        return left + ratio * (right - left)

    def _x_to_sec(self, x: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        left, right = self.padding, w - self.padding
        ratio = min(max((x - left) / (right - left), 0.0), 1.0)
        return ratio * self.duration_sec

    def _clamp_x(self, x: float) -> float:
        w = self.canvas.winfo_width() or self.canvas_width
        return min(max(x, self.padding), w - self.padding)

    def _find_rect_at(self, x: float, y: float) -> Optional[int]:
        ids = self.canvas.find_overlapping(x-1, y-1, x+1, y+1)
        for rid in ids:
            if rid in self.rects:
                return rid
        return None

    def _sec_label(self, sec: float) -> str:
        s = int(round(sec))
        hh = s // 3600
        mm = (s % 3600) // 60
        ss = s % 60
        return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"

# ---------------- Uygulama ----------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry("1200x880")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.worker: Optional[CutterWorker] = None
        self.msg_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self.after(100, self._poll_queue)

        # Pencere kapanışında player'ı düzgün kapat
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Üst şerit
        top = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        top.pack(side="top", fill="x", padx=12, pady=(12, 6))

        self.video_var = tk.StringVar(value="")
        ctk.CTkLabel(top, text="Video Dosyası:", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(top, textvariable=self.video_var, width=600).grid(row=0, column=1, padx=10, pady=10, sticky="we")
        ctk.CTkButton(top, text="Gözat…", command=self._browse_video, width=100).grid(row=0, column=2, padx=10, pady=10)

        self.outdir_var = tk.StringVar(value="")
        ctk.CTkLabel(top, text="Çıkış Klasörü:", font=ctk.CTkFont(size=13, weight="bold")).grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(top, textvariable=self.outdir_var, width=600).grid(row=1, column=1, padx=10, pady=10, sticky="we")
        ctk.CTkButton(top, text="Seç…", command=self._browse_outdir, width=100).grid(row=1, column=2, padx=10, pady=10)

        ffmpeg_path = which_ffmpeg()
        ffprobe_path = which_ffprobe()
        ffplay_path = which_ffplay()
        ctk.CTkLabel(top, text=("FFmpeg: Bulundu" if ffmpeg_path else "FFmpeg: Bulunamadı"), text_color=("green" if ffmpeg_path else "red")).grid(row=2, column=0, padx=10, pady=(0, 10), sticky="w")
        ctk.CTkLabel(top, text=("ffprobe: Bulundu" if ffprobe_path else "ffprobe: Bulunamadı"), text_color=("green" if ffprobe_path else "red")).grid(row=2, column=1, padx=10, pady=(0, 10), sticky="w")
        ctk.CTkLabel(top, text=("ffplay (ses): Bulundu" if ffplay_path else "ffplay (ses): Bulunamadı"), text_color=("green" if ffplay_path else "orange")).grid(row=2, column=2, padx=10, pady=(0, 10), sticky="w")

        top.grid_columnconfigure(1, weight=1)

        # Orta: Sol (player + seekbar + timeline + tablo), Sağ (ayarlar)
        mid = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        mid.pack(side="top", fill="both", expand=True, padx=12, pady=6)

        left = ctk.CTkFrame(mid, corner_radius=12)
        left.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        # Player
        ctk.CTkLabel(left, text="Video Önizleme", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=8, pady=(6, 0))
        self.player = VideoPlayer(left, on_seek=self._on_player_seek_emit)
        self.player.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # SeekBar
        ctk.CTkLabel(left, text="Oynatma Çizgisi (Tıkla/Çek → Sar)", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=8, pady=(2, 0))
        self.seekbar = SeekBar(
            left,
            on_seek_request=self._on_scrub_seek_request,
            on_scrub_start=self._on_scrub_start,
            on_scrub_end=self._on_scrub_end,
        )
        self.seekbar.pack(fill="x", padx=8, pady=(4, 8))

        # Timeline (çoklu aralık)
        ctk.CTkLabel(left, text="Parça Seçim Zaman Çizgisi", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=8, pady=(2, 0))
        self.timeline = Timeline(
            left,
            on_new_selection=self._timeline_new_selection,
            on_delete_selection=self._timeline_delete_selection,
            on_seek_request=self._on_timeline_seek_request
        )
        self.timeline.pack(fill="x", padx=8, pady=(4, 8))

        # Tablo
        self.tree = ttk.Treeview(left, columns=("start", "end", "status"), show="headings", height=10)
        self.tree.heading("start", text="Başlangıç")
        self.tree.heading("end", text="Bitiş")
        self.tree.heading("status", text="Durum")
        self.tree.column("start", width=120, anchor="center")
        self.tree.column("end", width=120, anchor="center")
        self.tree.column("status", width=180, anchor="w")
        self.tree.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))

        # Manuel aralık ekleme
        input_frame = ctk.CTkFrame(left, corner_radius=12)
        input_frame.pack(side="top", fill="x", padx=8, pady=8)
        self.start_var = tk.StringVar(value="00:00")
        self.end_var = tk.StringVar(value="05:00")
        ctk.CTkLabel(input_frame, text="Başlangıç:").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkEntry(input_frame, textvariable=self.start_var, width=120).grid(row=0, column=1, padx=6, pady=6)
        ctk.CTkLabel(input_frame, text="Bitiş:").grid(row=0, column=2, padx=6, pady=6, sticky="e")
        ctk.CTkEntry(input_frame, textvariable=self.end_var, width=120).grid(row=0, column=3, padx=6, pady=6)
        ctk.CTkButton(input_frame, text="Ekle", command=self._add_range).grid(row=0, column=4, padx=6, pady=6)

        btns = ctk.CTkFrame(left, corner_radius=12)
        btns.pack(side="top", fill="x", padx=8, pady=(0, 10))
        ctk.CTkButton(btns, text="Seçiliyi Sil", command=self._remove_selected, width=120).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(btns, text="Tümünü Temizle", command=self._clear_all, width=140).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(btns, text="Txt'den İçe Aktar…", command=self._import_from_text, width=160).pack(side="left", padx=6, pady=6)

        # Sağ (ayarlar)
        right = ctk.CTkFrame(mid, corner_radius=12)
        right.pack(side="left", fill="y", padx=(0, 10), pady=10)

        ctk.CTkLabel(right, text="Kesim Modu", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(10, 6))
        self.mode_var = tk.StringVar(value="FAST")
        ctk.CTkRadioButton(right, text="HIZLI (stream copy)", variable=self.mode_var, value="FAST", command=self._toggle_encode_opts).pack(anchor="w", padx=16, pady=2)
        ctk.CTkRadioButton(right, text="HASSAS (yeniden kodlama)", variable=self.mode_var, value="ACCURATE", command=self._toggle_encode_opts).pack(anchor="w", padx=16, pady=2)

        self.encode_frame = ctk.CTkFrame(right, corner_radius=12)
        self.encode_frame.pack(fill="x", padx=10, pady=(8, 10))
        self.codec_var = tk.StringVar(value="libx264")
        self.crf_var = tk.IntVar(value=18)
        self.preset_var = tk.StringVar(value="veryfast")
        self.audio_mode_var = tk.StringVar(value="copy")
        self.audio_bitrate_var = tk.StringVar(value="192k")

        ctk.CTkLabel(self.encode_frame, text="Video codec:").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkOptionMenu(self.encode_frame, values=["libx264", "libx265"], variable=self.codec_var, width=140).grid(row=0, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(self.encode_frame, text="CRF:").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        self.crf_slider = ctk.CTkSlider(self.encode_frame, from_=14, to=28, number_of_steps=14, command=lambda v: self.crf_var.set(int(float(v))))
        self.crf_slider.set(self.crf_var.get()); self.crf_slider.grid(row=1, column=1, padx=6, pady=6, sticky="we")
        ctk.CTkLabel(self.encode_frame, text="Preset:").grid(row=2, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkOptionMenu(self.encode_frame, values=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"], variable=self.preset_var, width=140).grid(row=2, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(self.encode_frame, text="Ses:").grid(row=3, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkOptionMenu(self.encode_frame, values=["copy", "aac"], variable=self.audio_mode_var, width=140).grid(row=3, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(self.encode_frame, text="AAC bitrate:").grid(row=4, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkOptionMenu(self.encode_frame, values=["128k", "160k", "192k", "256k", "320k"], variable=self.audio_bitrate_var, width=140).grid(row=4, column=1, padx=6, pady=6)
        self.encode_frame.grid_columnconfigure(1, weight=1)

        self._toggle_encode_opts()

        # Alt: başlat/durdur + ilerleme + log
        bottom = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        bottom.pack(side="bottom", fill="x", padx=12, pady=(6, 12))
        self.start_btn = ctk.CTkButton(bottom, text="Toplu Dışa Aktar", command=self._start_cutting, width=160, height=36)
        self.start_btn.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.stop_btn = ctk.CTkButton(bottom, text="Durdur", command=self._stop_cutting, width=100, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=10, pady=10, sticky="w")
        self.progress = ctk.CTkProgressBar(bottom, height=14); self.progress.set(0.0)
        self.progress.grid(row=0, column=2, padx=10, pady=10, sticky="we")
        self.status_label = ctk.CTkLabel(bottom, text="Hazır")
        self.status_label.grid(row=0, column=3, padx=10, pady=10, sticky="e")
        bottom.grid_columnconfigure(2, weight=1)

        # Log
        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.pack(side="bottom", fill="both", expand=False, padx=12, pady=(0, 12))
        ctk.CTkLabel(log_frame, text="Günlük (Log):", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=7, wrap="word"); self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

        style = ttk.Style(self)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    # ----- Player <-> Çizgiler senkron -----
    def _on_player_seek_emit(self, sec: float):
        """Player oynarken/seek yapılırken playhead'leri güncelle."""
        self.timeline.set_playhead(sec)
        self.seekbar.set_playhead(sec)

    def _on_scrub_start(self):
        self.player.begin_scrub()

    def _on_scrub_end(self):
        self.player.end_scrub()

    def _on_scrub_seek_request(self, sec: float):
        """SeekBar tıklayınca/çekince video sar (scrub)."""
        self.player.scrub_to(sec)
        self.timeline.set_playhead(sec)
        self.seekbar.set_playhead(sec)

    def _on_timeline_seek_request(self, sec: float):
        """Parça timeline’ında sağ tık/çift tık ile sar (normal seek)."""
        self.player.seek(sec, emit=False)
        self.timeline.set_playhead(sec)
        self.seekbar.set_playhead(sec)

    # ----- Timeline Callbacks -----
    def _timeline_new_selection(self, start_sec: float, end_sec: float):
        self.tree.insert("", "end", values=(seconds_to_hms(start_sec), seconds_to_hms(end_sec), "Beklemede"))
        self.player.seek(start_sec, emit=False)
        self.timeline.set_playhead(start_sec)
        self.seekbar.set_playhead(start_sec)

    def _timeline_delete_selection(self, start_sec: float, end_sec: float):
        for iid in self.tree.get_children():
            st, en, _ = self.tree.item(iid, "values")
            try:
                s2 = parse_time_to_seconds(st); e2 = parse_time_to_seconds(en)
            except Exception:
                continue
            if abs(s2 - start_sec) < 0.5 and abs(e2 - end_sec) < 0.5:
                self.tree.delete(iid)
                break

    # ----- UI event'leri -----
    def _toggle_encode_opts(self):
        state = "normal" if self.mode_var.get() == "ACCURATE" else "disabled"
        for child in self.encode_frame.winfo_children():
            try: child.configure(state=state)
            except tk.TclError: pass

    def _browse_video(self):
        path = filedialog.askopenfilename(
            title="Video dosyası seçin",
            filetypes=[("Video dosyaları", "*.mp4 *.mov *.mkv *.avi *.m4v *.ts *.mts *.m2ts"), ("Tüm dosyalar", "*.*")]
        )
        if not path: return
        self.video_var.set(path)
        dur = probe_duration(path) or 0.0
        self.timeline.set_duration(dur)
        self.seekbar.set_duration(dur)
        self.player.load(path, dur)
        self.seekbar.set_playhead(0.0)
        self.timeline.set_playhead(0.0)
        self._log(f"Video süresi: {seconds_to_hms(dur)}")

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="Çıkış klasörü seçin")
        if path: self.outdir_var.set(path)

    def _add_range(self):
        start = self.start_var.get().strip(); end = self.end_var.get().strip()
        try:
            s = parse_time_to_seconds(start); e = parse_time_to_seconds(end)
            if e <= s: raise ValueError("Bitiş, başlangıçtan büyük olmalı")
        except Exception as e:
            messagebox.showerror("Hatalı zaman", f"Zamanları kontrol edin:\n{e}"); return
        self.tree.insert("", "end", values=(seconds_to_hms(s), seconds_to_hms(e), "Beklemede"))
        if self.timeline.duration_sec > 0: self.timeline.add_segment(s, e)

    def _remove_selected(self):
        sels = list(self.tree.selection())
        if not sels: return
        for iid in sels:
            self.tree.delete(iid)
        self._rebuild_timeline_from_table()

    def _clear_all(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.timeline.clear()

    def _import_from_text(self):
        path = filedialog.askopenfilename(
            title="Zaman aralıkları dosyası (txt)",
            filetypes=[("Metin dosyaları", "*.txt"), ("Tüm dosyalar", "*.*")]
        )
        if not path: return
        added = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                tokens = None
                if "-" in line: tokens = line.split("-")
                elif " to " in line.lower(): tokens = line.lower().split(" to ")
                elif "," in line: tokens = line.split(",")
                else:
                    parts = line.split()
                    if len(parts) == 2: tokens = parts
                if not tokens or len(tokens) != 2:
                    if ".." in line: tokens = line.split("..")
                    else: continue
                start, end = tokens[0].strip(), tokens[1].strip()
                try:
                    s = parse_time_to_seconds(start); e = parse_time_to_seconds(end)
                    if e <= s: continue
                    self.tree.insert("", "end", values=(seconds_to_hms(s), seconds_to_hms(e), "Beklemede"))
                    added += 1
                except Exception:
                    continue
        self._rebuild_timeline_from_table()
        messagebox.showinfo("İçe aktarma", f"{added} adet aralık eklendi.")

    def _collect_segments(self) -> List[Segment]:
        segs: List[Segment] = []
        for iid in self.tree.get_children():
            start, end, _ = self.tree.item(iid, "values")
            s = parse_time_to_seconds(start); e = parse_time_to_seconds(end)
            segs.append(Segment(start, end, s, e))
        # sırayı koru (UI indexleri)
        return segs

    def _rebuild_timeline_from_table(self):
        self.timeline.clear()
        for seg in self._collect_segments():
            if self.timeline.duration_sec > 0:
                self.timeline.add_segment(seg.start_sec, seg.end_sec)

    def _start_cutting(self):
        video_path = self.video_var.get().strip()
        out_dir = self.outdir_var.get().strip()
        if not video_path or not os.path.isfile(video_path):
            messagebox.showerror("Hata", "Geçerli bir video dosyası seçin."); return
        if not out_dir:
            messagebox.showerror("Hata", "Çıkış klasörü seçin."); return
        if not os.path.isdir(out_dir):
            try: os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Hata", f"Çıkış klasörü oluşturulamadı:\n{e}"); return
        if which_ffmpeg() is None:
            messagebox.showerror("FFmpeg yok", "FFmpeg bulunamadı. PATH'e ekleyin."); return

        segments = self._collect_segments()
        if not segments:
            messagebox.showerror("Hata", "En az bir zaman aralığı ekleyin."); return

        # Durumları sıfırla
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values")); vals[2] = "Beklemede"; self.tree.item(iid, values=vals)

        mode = self.mode_var.get()
        codec = self.codec_var.get()
        crf = self.crf_var.get()
        preset = self.preset_var.get()
        audio_mode = self.audio_mode_var.get()
        audio_bitrate = self.audio_bitrate_var.get()

        self._log_clear()
        self._log(f"Video: {video_path}")
        self._log(f"Çıkış klasörü: {out_dir}")
        self._log(f"Mod: {mode}")
        if mode == "ACCURATE":
            self._log(f"Codec: {codec}, CRF: {crf}, Preset: {preset}, Ses: {audio_mode} {self.audio_bitrate_var.get() if self.audio_mode_var.get()=='aac' else ''}")

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Kesim başladı…")
        self.progress.set(0.0)

        self.worker = CutterWorker(
            video_path=video_path, segments=segments, out_dir=out_dir,
            mode=mode, codec=codec, crf=int(crf), preset=preset,
            audio_mode=audio_mode, audio_bitrate=audio_bitrate,
            message_queue=self.msg_queue
        )
        self.worker.start()

    def _stop_cutting(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self._log("Durduruluyor…")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "overall":
                    self.status_label.configure(text=str(payload))
                elif kind == "segment_status":
                    index, status = payload; self._set_row_status(index, status)
                elif kind == "progress":
                    self.progress.set(float(payload))
                elif kind == "fatal":
                    messagebox.showerror("Hata", str(payload)); self._cleanup_after_done()
                elif kind == "done":
                    self._cleanup_after_done()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _set_row_status(self, row_index: int, status_text: str):
        children = self.tree.get_children()
        if 0 <= row_index < len(children):
            iid = children[row_index]
            vals = list(self.tree.item(iid, "values"))
            vals[2] = status_text
            self.tree.item(iid, values=vals)

    def _cleanup_after_done(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="Tamamlandı")
        self._log("Tüm işler tamamlandı.")

    def _log(self, text: str):
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _log_clear(self):
        self.log_text.delete("1.0", "end")

    def _on_close(self):
        try:
            self.player.close()
        except Exception:
            pass
        self.destroy()

# ---------------- Çalıştırıcı ----------------

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
