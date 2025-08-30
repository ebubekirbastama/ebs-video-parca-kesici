#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ebs Video Parça Kesici (Metro tarzı GUI)

Gereksinimler:
  - Python 3.8+
  - pip install customtkinter
  - ffmpeg kurulu ve PATH içinde erişilebilir olmalı

Özellikler:
  - Video dosyası seçme
  - Birden çok başlangıç/bitiş aralığı ekleme (hh:mm:ss, mm:ss veya saniye olarak)
  - Zaman aralıklarını metin dosyasından içe aktarma (satır başına: "00:00-05:15" ya da "0:00 to 5:15" veya "0:00,5:15")
  - Çıkış klasörü seçme
  - Kesim modu seçimi:
      * HIZLI (stream copy, keyframe doğruluğunda; anahtar kare değilse birkaç kare kayma olabilir)
      * HASSAS (H.264/H.265 ile yeniden kodlama; kare doğruluğunda, daha yavaş)
  - Parça bazlı durum + genel ilerleme çubuğu
  - customtkinter ile basit ve temiz "Metro" görünümü

"""

import os
import sys
import threading
import queue
import subprocess
import shlex
import shutil
from dataclasses import dataclass
from typing import List, Tuple, Optional

# GUI modülleri
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

# Uygulama başlığına istenildiği gibi "Ebs" eklendi
APP_TITLE = "Ebs Video Parça Kesici (Metro GUI)"
VERSION = "1.0.0"


def which_ffmpeg() -> Optional[str]:
    """Sistemde ffmpeg var mı kontrol eder; varsa yolunu, yoksa None döndürür."""
    return shutil.which("ffmpeg")


def parse_time_to_seconds(text: str) -> float:
    """
    Zaman metnini saniyeye çevirir.
    Desteklenen formatlar:
      - "hh:mm:ss(.ms)"
      - "mm:ss"
      - "ss" veya "123.45"
    Virgül ondalık ayırıcı olarak da kabul edilir (örn. "12,5").
    """
    s = text.strip().replace(",", ".")
    if not s:
        raise ValueError("Boş zaman değeri")

    if ":" not in s:
        # Ham saniye (float olarak da olabilir)
        return float(s)

    parts = s.split(":")
    if len(parts) == 2:
        mm, ss = parts
        return int(mm) * 60 + float(ss)
    elif len(parts) == 3:
        hh, mm, ss = parts
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    else:
        raise ValueError(f"Zaman formatı hatalı: {text}")


def seconds_to_hms(seconds: float) -> str:
    """Saniye cinsinden değeri FFmpeg’in sevdiği 'HH:MM:SS.mmm' biçimine çevirir."""
    if seconds < 0:
        seconds = 0
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


@dataclass
class Segment:
    """Bir video kesim aralığını temsil eder."""
    start_str: str
    end_str: str
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        """Aralığın süresini saniye cinsinden döndürür (negatifse 0 yapılır)."""
        return max(0.0, self.end_sec - self.start_sec)

    def to_label(self) -> str:
        """UI’da gösterilecek kısa etiket metni."""
        return f"{self.start_str} → {self.end_str}"


class CutterWorker(threading.Thread):
    """
    Asıl kesim işini yapan işçi thread.
    UI’yi kitlememek için kesim bu ayrı iş parçacığında yürütülür.
    İlerleme ve durum mesajları, ana UI’ya bir 'queue' üzerinden iletilir.
    """
    def __init__(
        self,
        video_path: str,
        segments: List[Segment],
        out_dir: str,
        mode: str,  # "FAST" veya "ACCURATE"
        codec: str,  # ACCURATE için "libx264" veya "libx265"
        crf: int,
        preset: str,
        audio_mode: str,  # "copy" veya "aac"
        audio_bitrate: str,  # Örn: "192k"
        message_queue: queue.Queue,
    ):
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
        """Kullanıcı durdur düğmesine basarsa iptal bayrağını kaldırır."""
        self.stop_requested = True

    def run(self):
        """
        Her bir aralık için uygun FFmpeg komutunu kurup çalıştırır.
        Hızlı modda (-c copy) anahtar kareye hizalı kesim yapar;
        Hassas modda yeniden kodlama ile kare hassasiyetinde kesim yapılır.
        """
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

                # Çıkış dosya adı (geçerli bir isim için ':' yerine '-' kullan)
                safe_start = seg.start_str.replace(":", "-")
                safe_end = seg.end_str.replace(":", "-")
                out_name = f"{base}_parca{idx:02d}_{safe_start}_to_{safe_end}.mp4"
                out_path = os.path.join(self.out_dir, out_name)

                if self.mode == "FAST":
                    # Hızlı: stream copy (keyframe doğruluğu). -ss girişten önce, hızlıdır ama
                    # başlangıç kare anahtar değilse birkaç kare kayabilir.
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", start_hms,
                        "-i", self.video_path,
                        "-t", dur_hms,
                        "-c", "copy",
                        "-avoid_negative_ts", "1",
                        out_path
                    ]
                else:
                    # Hassas: yeniden kodlama. -ss girişten sonra. Video yeniden kodlanır,
                    # ses kopyalanabilir veya AAC olarak yeniden kodlanabilir.
                    cmd = ["ffmpeg", "-y", "-i", self.video_path, "-ss", start_hms, "-t", dur_hms,
                           "-map", "0", "-c:v", self.codec, "-preset", self.preset, "-crf", str(self.crf)]
                    if self.audio_mode == "copy":
                        cmd += ["-c:a", "copy"]
                    else:
                        cmd += ["-c:a", "aac", "-b:a", self.audio_bitrate]
                    cmd += [out_path]

                # UI’a durum/ileri bilgi gönder
                self.q.put(("segment_status", (idx - 1, "ÇALIŞIYOR…")))
                self.q.put(("log", f"[{idx}/{total}] Çıktı: {out_name}"))
                self.q.put(("log", f"Komut: {' '.join(shlex.quote(c) for c in cmd)}"))

                try:
                    # stdout+stderr’i yakalayıp hata ayıklamayı kolaylaştırıyoruz
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if proc.returncode == 0:
                        self.q.put(("segment_status", (idx - 1, "BİTTİ")))
                    else:
                        self.q.put(("segment_status", (idx - 1, f"HATA (kod {proc.returncode})")))
                        # Son ~1000 karakteri log’a bas (çoğunlukla hata özeti için yeterli)
                        self.q.put(("log", proc.stdout[-1000:] if proc.stdout else "Hata çıktısı yok."))
                except Exception as e:
                    self.q.put(("segment_status", (idx - 1, f"HATA: {e}")))

                # Genel ilerleme
                self.q.put(("progress", idx / total))

            self.q.put(("done", None))
        except Exception as e:
            self.q.put(("fatal", str(e)))


class App(ctk.CTk):
    """Ana uygulama penceresi ve tüm UI bileşenleri."""
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry("1000x700")
        # Görünüm ve tema (kullanıcı sistemine uyumlu)
        ctk.set_appearance_mode("System")  # "Light" / "Dark" / "System"
        ctk.set_default_color_theme("blue")  # "blue", "green", "dark-blue"

        self.worker: Optional[CutterWorker] = None
        self.msg_queue: queue.Queue = queue.Queue()

        self._build_ui()
        # İşçi thread’den gelecek mesajları düzenli aralıklarla yokla
        self.after(100, self._poll_queue)

    # ---------- UI OLUŞTURMA ----------

    def _build_ui(self):
        # Üst alan: Dosya + Çıkış + FFmpeg durumu
        top = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        top.pack(side="top", fill="x", padx=12, pady=(12, 6))

        # Giriş videosu
        self.video_var = tk.StringVar(value="")
        lbl_in = ctk.CTkLabel(top, text="Video Dosyası:", font=ctk.CTkFont(size=13, weight="bold"))
        lbl_in.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.entry_video = ctk.CTkEntry(top, textvariable=self.video_var, width=600)
        self.entry_video.grid(row=0, column=1, padx=10, pady=10, sticky="we")
        btn_browse_in = ctk.CTkButton(top, text="Gözat…", command=self._browse_video, width=100)
        btn_browse_in.grid(row=0, column=2, padx=10, pady=10)

        # Çıkış klasörü
        self.outdir_var = tk.StringVar(value="")
        lbl_out = ctk.CTkLabel(top, text="Çıkış Klasörü:", font=ctk.CTkFont(size=13, weight="bold"))
        lbl_out.grid(row=1, column=0, padx=10, pady=10, sticky="w")
        self.entry_out = ctk.CTkEntry(top, textvariable=self.outdir_var, width=600)
        self.entry_out.grid(row=1, column=1, padx=10, pady=10, sticky="we")
        btn_browse_out = ctk.CTkButton(top, text="Seç…", command=self._browse_outdir, width=100)
        btn_browse_out.grid(row=1, column=2, padx=10, pady=10)

        # FFmpeg var/yok bilgisi
        ffmpeg_path = which_ffmpeg()
        self.ffmpeg_label = ctk.CTkLabel(
            top,
            text=("FFmpeg: Bulundu" if ffmpeg_path else "FFmpeg: Bulunamadı (PATH'e ekleyin)"),
            text_color=("green" if ffmpeg_path else "red")
        )
        self.ffmpeg_label.grid(row=2, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")

        top.grid_columnconfigure(1, weight=1)

        # Orta alan: aralık listesi + kontroller
        mid = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        mid.pack(side="top", fill="both", expand=True, padx=12, pady=6)

        left = ctk.CTkFrame(mid, corner_radius=12)
        left.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        # Aralıklar için tablo (Treeview)
        self.tree = ttk.Treeview(left, columns=("start", "end", "status"), show="headings", height=12)
        self.tree.heading("start", text="Başlangıç")
        self.tree.heading("end", text="Bitiş")
        self.tree.heading("status", text="Durum")
        self.tree.column("start", width=120, anchor="center")
        self.tree.column("end", width=120, anchor="center")
        self.tree.column("status", width=180, anchor="w")
        self.tree.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))

        # Aralık ekleme girişleri
        input_frame = ctk.CTkFrame(left, corner_radius=12)
        input_frame.pack(side="top", fill="x", padx=8, pady=8)

        self.start_var = tk.StringVar(value="00:00")
        self.end_var = tk.StringVar(value="05:15")
        ctk.CTkLabel(input_frame, text="Başlangıç (hh:mm:ss):").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        ctk.CTkEntry(input_frame, textvariable=self.start_var, width=140).grid(row=0, column=1, padx=6, pady=6)
        ctk.CTkLabel(input_frame, text="Bitiş (hh:mm:ss):").grid(row=0, column=2, padx=6, pady=6, sticky="e")
        ctk.CTkEntry(input_frame, textvariable=self.end_var, width=140).grid(row=0, column=3, padx=6, pady=6)
        ctk.CTkButton(input_frame, text="Ekle", command=self._add_range).grid(row=0, column=4, padx=6, pady=6)

        # Aralık yönetim butonları
        btns = ctk.CTkFrame(left, corner_radius=12)
        btns.pack(side="top", fill="x", padx=8, pady=(0, 10))

        ctk.CTkButton(btns, text="Seçiliyi Sil", command=self._remove_selected, width=120).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(btns, text="Tümünü Temizle", command=self._clear_all, width=140).pack(side="left", padx=6, pady=6)
        ctk.CTkButton(btns, text="Txt'den İçe Aktar…", command=self._import_from_text, width=160).pack(side="left", padx=6, pady=6)

        # Sağ panel: seçenekler
        right = ctk.CTkFrame(mid, corner_radius=12)
        right.pack(side="left", fill="y", padx=(0, 10), pady=10)

        # Kesim modu
        ctk.CTkLabel(right, text="Kesim Modu", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(10, 6))
        self.mode_var = tk.StringVar(value="FAST")
        ctk.CTkRadioButton(right, text="HIZLI (stream copy, -c copy)", variable=self.mode_var, value="FAST", command=self._toggle_encode_opts).pack(anchor="w", padx=16, pady=2)
        ctk.CTkRadioButton(right, text="HASSAS (yeniden kodlama)", variable=self.mode_var, value="ACCURATE", command=self._toggle_encode_opts).pack(anchor="w", padx=16, pady=2)

        # Kodlama seçenekleri (HASSAS modda aktif)
        self.encode_frame = ctk.CTkFrame(right, corner_radius=12)
        self.encode_frame.pack(fill="x", padx=10, pady=(8, 10))

        ctk.CTkLabel(self.encode_frame, text="Video codec:").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        self.codec_var = tk.StringVar(value="libx264")
        ctk.CTkOptionMenu(self.encode_frame, values=["libx264", "libx265"], variable=self.codec_var, width=140).grid(row=0, column=1, padx=6, pady=6, sticky="w")

        ctk.CTkLabel(self.encode_frame, text="CRF (kalite):").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        self.crf_var = tk.IntVar(value=18)
        # Slider’dan gelen float’ı IntVar’a yuvarlayarak aktar
        self.crf_slider = ctk.CTkSlider(self.encode_frame, from_=14, to=28, number_of_steps=14, command=lambda v: self.crf_var.set(int(float(v))))
        self.crf_slider.set(self.crf_var.get())
        self.crf_slider.grid(row=1, column=1, padx=6, pady=6, sticky="we")

        ctk.CTkLabel(self.encode_frame, text="Preset:").grid(row=2, column=0, padx=6, pady=6, sticky="e")
        self.preset_var = tk.StringVar(value="veryfast")
        ctk.CTkOptionMenu(self.encode_frame, values=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"], variable=self.preset_var, width=140).grid(row=2, column=1, padx=6, pady=6, sticky="w")

        # Ses seçenekleri
        ctk.CTkLabel(self.encode_frame, text="Ses:").grid(row=3, column=0, padx=6, pady=6, sticky="e")
        self.audio_mode_var = tk.StringVar(value="copy")
        ctk.CTkOptionMenu(self.encode_frame, values=["copy", "aac"], variable=self.audio_mode_var, width=140).grid(row=3, column=1, padx=6, pady=6, sticky="w")

        ctk.CTkLabel(self.encode_frame, text="AAC bitrate:").grid(row=4, column=0, padx=6, pady=6, sticky="e")
        self.audio_bitrate_var = tk.StringVar(value="192k")
        ctk.CTkOptionMenu(self.encode_frame, values=["128k", "160k", "192k", "256k", "320k"], variable=self.audio_bitrate_var, width=140).grid(row=4, column=1, padx=6, pady=6, sticky="w")

        for col in range(2):
            self.encode_frame.grid_columnconfigure(col, weight=1)

        self._toggle_encode_opts()

        # Alt alan: başlat/durdur + ilerleme + durum + log
        bottom = ctk.CTkFrame(self, corner_radius=12, fg_color=("white", "#1e1e1e"))
        bottom.pack(side="bottom", fill="x", padx=12, pady=(6, 12))

        self.start_btn = ctk.CTkButton(bottom, text="Kesimi Başlat", command=self._start_cutting, width=160, height=36)
        self.start_btn.grid(row=0, column=0, padx=10, pady=10, sticky="w")

        self.stop_btn = ctk.CTkButton(bottom, text="Durdur", command=self._stop_cutting, width=100, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=10, pady=10, sticky="w")

        self.progress = ctk.CTkProgressBar(bottom, height=14)
        self.progress.set(0.0)
        self.progress.grid(row=0, column=2, padx=10, pady=10, sticky="we")

        self.status_label = ctk.CTkLabel(bottom, text="Hazır")
        self.status_label.grid(row=0, column=3, padx=10, pady=10, sticky="e")

        bottom.grid_columnconfigure(2, weight=1)

        # Günlük (Log) alanı
        log_frame = ctk.CTkFrame(self, corner_radius=12)
        log_frame.pack(side="bottom", fill="both", expand=False, padx=12, pady=(0, 12))

        ctk.CTkLabel(log_frame, text="Günlük (Log):", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

        # Treeview stilini biraz "metro"ya yaklaştır
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    # ---------- UI OLAY İŞLEMCİLERİ ----------

    def _toggle_encode_opts(self):
        """
        HIZLI modda kodlama seçenekleri devre dışı kalır.
        HASSAS modda etkinleşir.
        """
        mode = self.mode_var.get()
        state = "normal" if mode == "ACCURATE" else "disabled"
        for child in self.encode_frame.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                # Bazı widget’lar bu özelliği desteklemeyebilir
                pass

    def _browse_video(self):
        """Video dosyası seçme iletişim kutusu."""
        path = filedialog.askopenfilename(
            title="Video dosyası seçin",
            filetypes=[("Video dosyaları", "*.mp4 *.mov *.mkv *.avi *.m4v *.ts *.mts *.m2ts"), ("Tüm dosyalar", "*.*")]
        )
        if path:
            self.video_var.set(path)

    def _browse_outdir(self):
        """Çıkış klasörü seçme iletişim kutusu."""
        path = filedialog.askdirectory(title="Çıkış klasörü seçin")
        if path:
            self.outdir_var.set(path)

    def _add_range(self):
        """Girdi alanlarından başlangıç/bitiş alıp tabloya bir kesim aralığı ekler."""
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        try:
            s_sec = parse_time_to_seconds(start)
            e_sec = parse_time_to_seconds(end)
            if e_sec <= s_sec:
                raise ValueError("Bitiş, başlangıçtan büyük olmalı")
        except Exception as e:
            messagebox.showerror("Hatalı zaman", f"Zamanları kontrol edin:\n{e}")
            return
        self.tree.insert("", "end", values=(start, end, "Beklemede"))

    def _remove_selected(self):
        """Seçili aralık(lar)ı siler."""
        for iid in self.tree.selection():
            self.tree.delete(iid)

    def _clear_all(self):
        """Tüm aralıkları temizler."""
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _import_from_text(self):
        """
        Dışarıdan .txt dosyasından aralıkları içe aktarır.
        Desteklenen satır örnekleri:
          "00:00-05:15"
          "0:00 to 5:15"
          "0:00,5:15"
          "0:00 5:15"
          "0:00..5:15"
        """
        path = filedialog.askopenfilename(
            title="Zaman aralıkları dosyası (txt)",
            filetypes=[("Metin dosyaları", "*.txt"), ("Tüm dosyalar", "*.*")]
        )
        if not path:
            return
        added = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Kabul edilen ayırıcılar
                tokens = None
                if "-" in line:
                    tokens = line.split("-")
                elif " to " in line.lower():
                    tokens = line.lower().split(" to ")
                elif "," in line:
                    tokens = line.split(",")
                else:
                    parts = line.split()
                    if len(parts) == 2:
                        tokens = parts

                if not tokens or len(tokens) != 2:
                    # Alternatif: "start..end"
                    if ".." in line:
                        tokens = line.split("..")
                    else:
                        continue

                start = tokens[0].strip()
                end = tokens[1].strip()
                try:
                    s_sec = parse_time_to_seconds(start)
                    e_sec = parse_time_to_seconds(end)
                    if e_sec <= s_sec:
                        continue
                    self.tree.insert("", "end", values=(start, end, "Beklemede"))
                    added += 1
                except Exception:
                    continue

        messagebox.showinfo("İçe aktarma", f"{added} adet aralık eklendi.")

    def _collect_segments(self) -> List[Segment]:
        """Tablodaki tüm aralıkları Segment nesnelerine çevirip döndürür."""
        segs: List[Segment] = []
        for iid in self.tree.get_children():
            start, end, _ = self.tree.item(iid, "values")
            s_sec = parse_time_to_seconds(start)
            e_sec = parse_time_to_seconds(end)
            segs.append(Segment(start, end, s_sec, e_sec))
        return segs

    def _start_cutting(self):
        """Girdi doğrulamalarını yapar ve kesim işini başlatır."""
        video_path = self.video_var.get().strip()
        out_dir = self.outdir_var.get().strip()
        if not video_path or not os.path.isfile(video_path):
            messagebox.showerror("Hata", "Geçerli bir video dosyası seçin.")
            return
        if not out_dir:
            messagebox.showerror("Hata", "Çıkış klasörü seçin.")
            return
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Hata", f"Çıkış klasörü oluşturulamadı:\n{e}")
                return
        if which_ffmpeg() is None:
            messagebox.showerror("FFmpeg yok", "FFmpeg bulunamadı. Lütfen yükleyin ve PATH'e ekleyin.")
            return

        segments = self._collect_segments()
        if not segments:
            messagebox.showerror("Hata", "En az bir zaman aralığı ekleyin.")
            return

        # Durumları sıfırla
        for i, iid in enumerate(self.tree.get_children()):
            vals = list(self.tree.item(iid, "values"))
            vals[2] = "Beklemede"
            self.tree.item(iid, values=vals)

        # İşçi için seçenekleri topla
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
            self._log(f"Codec: {codec}, CRF: {crf}, Preset: {preset}, Ses: {audio_mode} {audio_bitrate if audio_mode=='aac' else ''}")

        # UI durumlarını güncelle
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Kesim başladı…")
        self.progress.set(0.0)

        # İşçiyi başlat
        self.worker = CutterWorker(
            video_path=video_path,
            segments=segments,
            out_dir=out_dir,
            mode=mode,
            codec=codec,
            crf=int(crf),
            preset=preset,
            audio_mode=audio_mode,
            audio_bitrate=audio_bitrate,
            message_queue=self.msg_queue,
        )
        self.worker.start()

    def _stop_cutting(self):
        """Kesim devam ediyorsa iptal ister."""
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self._log("Durduruluyor…")

    def _poll_queue(self):
        """İşçi thread’den gelen mesajları alıp UI’a uygular."""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind, payload = msg
                if kind == "log":
                    self._log(str(payload))
                elif kind == "overall":
                    self.status_label.configure(text=str(payload))
                elif kind == "segment_status":
                    index, status = payload
                    self._set_row_status(index, status)
                elif kind == "progress":
                    self.progress.set(float(payload))
                elif kind == "fatal":
                    messagebox.showerror("Hata", str(payload))
                    self._cleanup_after_done()
                elif kind == "done":
                    self._cleanup_after_done()
                else:
                    pass
        except queue.Empty:
            pass
        # Kısa aralıklarla tekrar kontrol et
        self.after(100, self._poll_queue)

    def _set_row_status(self, row_index: int, status_text: str):
        """Belirli satırın durum sütununu günceller."""
        children = self.tree.get_children()
        if 0 <= row_index < len(children):
            iid = children[row_index]
            vals = list(self.tree.item(iid, "values"))
            vals[2] = status_text
            self.tree.item(iid, values=vals)

    def _cleanup_after_done(self):
        """İş bittiğinde/durduğunda UI’yı toparlar."""
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="Tamamlandı")
        self._log("Tüm işler tamamlandı.")

    def _log(self, text: str):
        """Log metnine satır ekler ve en alta kaydırır."""
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _log_clear(self):
        """Log alanını temizler."""
        self.log_text.delete("1.0", "end")


def main():
    """Uygulamayı başlatır."""
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
