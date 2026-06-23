#!/usr/bin/env python3
"""vid-lab — ядро скачивания видео
YT/IG/TT через yt-dlp. Единый формат вывода.
Поддерживает выбор качества: 360p, 480p, 720p, 1080p, best.
"""
import os, sys, json, subprocess, time
from pathlib import Path

DOWNLOAD_DIR = os.path.expanduser("~/vid-lab/downloads")
YT_DLP = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/yt-dlp")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Форматы yt-dlp по качеству
QUALITY_FORMATS = {
    "360":  "best[height<=360]",
    "480":  "best[height<=480]",
    "720":  "best[height<=720]",
    "1080": "best[height<=1080]",
    "best": "best",
}

def resolve_format(quality: str, is_premium: bool = False) -> str:
    """Вернуть yt-dlp формат для запрошенного качества.
    Проверяет права доступа для 1080p и best."""
    quality = quality or "720"
    if quality not in QUALITY_FORMATS:
        quality = "720"
    # 1080p и best — только для premium
    if quality in ("1080", "best") and not is_premium:
        quality = "720"
    return QUALITY_FORMATS[quality]


def estimate_sizes(url: str) -> dict:
    """Оценить размер видео для всех качеств без скачивания.
    Возвращает {quality: size_mb_or_none} или {} при ошибке."""
    try:
        info = subprocess.run(
            [YT_DLP, "--dump-json", "--no-warnings", url],
            capture_output=True, text=True, timeout=30
        )
        if info.returncode != 0:
            return {}
        meta = json.loads(info.stdout)
    except Exception:
        return {}

    formats = meta.get("formats", [])
    duration = meta.get("duration", 0)

    # Разделяем форматы
    muxed = [
        f for f in formats
        if f.get("vcodec") and f["vcodec"] != "none"
        and f.get("acodec") and f["acodec"] != "none"
        and f.get("height")
    ]
    video_only = [
        f for f in formats
        if f.get("vcodec") and f["vcodec"] != "none"
        and (not f.get("acodec") or f["acodec"] == "none")
        and f.get("height")
    ]
    audio_only = [
        f for f in formats
        if (not f.get("vcodec") or f["vcodec"] == "none")
        and f.get("acodec") and f["acodec"] != "none"
    ]

    def _filesize(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    result = {}
    for q in ["360", "480", "720", "1080", "best"]:
        if q == "best":
            target_h = 99999
        else:
            target_h = int(q)

        # 1. Muxed формат — только если его высота близка к target_h (≥80%)
        best_muxed = None
        for f in sorted(muxed, key=lambda x: x.get("height", 0), reverse=True):
            h = f.get("height", 0)
            if h <= target_h and h >= target_h * 0.8:
                best_muxed = f
                break

        # 2. Video-only — максимальное разрешение ≤ target_h, по лучшему битрейту
        best_video = None
        best_video_alt = None  # запасной вариант с меньшим разрешением
        for f in sorted(video_only, key=lambda x: x.get("tbr", 0) or x.get("vbr", 0) or 0, reverse=True):
            h = f.get("height", 0)
            if h <= target_h:
                if h == target_h or (target_h - h) <= 100:
                    best_video = f
                    break
                if not best_video_alt:
                    best_video_alt = f

        if not best_video:
            best_video = best_video_alt

        # 3. Лучшее audio
        best_audio = None
        for f in sorted(audio_only, key=lambda x: x.get("tbr", 0) or x.get("abr", 0) or 0, reverse=True):
            best_audio = f
            break

        size = None
        # Приоритет: muxed (если есть) > video+audio
        if best_muxed:
            s = _filesize(best_muxed)
            if s:
                size = s
        if not size and best_video and best_audio:
            vs = _filesize(best_video)
            a_s = _filesize(best_audio)
            if vs and a_s:
                size = vs + a_s
        # Fallback: по битрейту
        if not size and best_video and duration:
            tbr = best_video.get("tbr", 0) or best_video.get("vbr", 0) or 0
            abr = best_audio.get("abr", 0) or best_audio.get("tbr", 0) or 0 if best_audio else 0
            if tbr:
                size = (tbr + abr) * duration / 8

        if size:
            result[q] = round(size / (1024 * 1024), 1)

    return result


def download_video(url: str, quality: str = "720", is_premium: bool = False,
                   progress_callback=None) -> dict:
    """
    Скачать видео с указанным качеством.
    Возвращает {path, title, size_mb, error}.

    quality: 360, 480, 720, 1080, best
    is_premium: True — доступны 1080p/best
    progress_callback(msg) — вызывается для статусов.
    """
    result = {"path": None, "title": None, "size_mb": 0, "error": None, "quality_used": quality}

    fmt = resolve_format(quality, is_premium)
    # Фактически используемое качество (может отличаться, если 1080p заблокирован)
    actual_quality = quality
    if quality in ("1080", "best") and not is_premium:
        actual_quality = "720"

    def log(msg):
        if progress_callback:
            progress_callback(msg)

    try:
        # 1. Мета-инфа (2 попытки)
        meta = None
        for attempt in range(2):
            info = subprocess.run(
                [YT_DLP, "--dump-json", "--no-warnings", url],
                capture_output=True, text=True, timeout=30
            )
            if info.returncode == 0:
                meta = json.loads(info.stdout)
                break
            log(f"⚠️ Попытка {attempt+1} не удалась, повтор...")
            time.sleep(2)

        if not meta:
            result["error"] = "YouTube временно недоступен. Попробуй позже."
            return result

        result["title"] = meta.get("title", "video")
        video_id = meta.get("id", "unknown")

        # 2. Формат скачивания
        outtmpl = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

        log(f"⏳ Скачиваю ({actual_quality}p)...")
        download = subprocess.run(
            [YT_DLP, "-f", fmt, "-o", outtmpl,
             "--no-warnings", "--no-mtime", url],
            capture_output=True, text=True, timeout=300
        )

        if download.returncode != 0:
            stderr = download.stderr.strip()
            if "Private video" in stderr:
                result["error"] = "Видео приватное — нет доступа"
            elif "Video unavailable" in stderr:
                result["error"] = "Видео недоступно"
            else:
                result["error"] = stderr[:300]
            return result

        # 3. Найти файл
        mp4_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp4")
        if os.path.exists(mp4_path):
            result["path"] = mp4_path
        else:
            for ext in ["webm", "mkv", "mov"]:
                p = os.path.join(DOWNLOAD_DIR, f"{video_id}.{ext}")
                if os.path.exists(p):
                    result["path"] = p
                    break
        if not result["path"]:
            files = sorted(Path(DOWNLOAD_DIR).iterdir(), key=os.path.getmtime, reverse=True)
            if files:
                result["path"] = str(files[0])

        if result["path"]:
            size_mb = os.path.getsize(result["path"]) / (1024 * 1024)
            result["size_mb"] = round(size_mb, 1)

        result["quality_used"] = actual_quality
        return result

    except subprocess.TimeoutExpired:
        result["error"] = "Таймаут скачивания (видео слишком большое)"
        return result
    except Exception as e:
        result["error"] = str(e)[:200]
        return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 core.py <url> [quality]")
        sys.exit(1)

    url = sys.argv[1]
    quality = sys.argv[2] if len(sys.argv) > 2 else "720"
    print(f"🎬 Скачиваю ({quality}p): {url}")
    r = download_video(url, quality=quality)
    if r["error"]:
        print(f"❌ {r['error']}")
        sys.exit(1)
    print(f"✅ {r['title']} — {r['size_mb']} MiB ({r['quality_used']}p)")
    print(f"📁 {r['path']}")
