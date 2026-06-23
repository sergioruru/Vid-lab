#!/usr/bin/env python3
"""
vid-lab — ядро скачивания видео
YT/IG/TT через yt-dlp. Единый формат вывода.
"""
import os, sys, json, subprocess, time
from pathlib import Path

DOWNLOAD_DIR = os.path.expanduser("~/vid-lab/downloads")
YT_DLP = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/yt-dlp")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def download_video(url: str, progress_callback=None) -> dict:
    """
    Скачать видео. Возвращает {path, title, size_mb, error}.

    progress_callback(msg) вызывается для статусов.
    """
    result = {"path": None, "title": None, "size_mb": 0, "error": None}

    def log(msg):
        if progress_callback:
            progress_callback(msg)

    try:
        # 1. Получаем мета-инфу (2 попытки)
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

        # 2. Определяем формат: best mp4 до 720p (быстро, совместимо)
        with_ext = meta.get("ext", "mp4")
        outtmpl = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

        log("⏳ Скачиваю...")
        download = subprocess.run(
            [YT_DLP, "-f", "best[height<=720]", "-o", outtmpl,
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

        # 3. Находим файл
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

        return result

    except subprocess.TimeoutExpired:
        result["error"] = "Таймаут скачивания (видео слишком большое)"
        return result
    except Exception as e:
        result["error"] = str(e)[:200]
        return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 core.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"🎬 Скачиваю: {url}")
    r = download_video(url)
    if r["error"]:
        print(f"❌ {r['error']}")
        sys.exit(1)
    print(f"✅ {r['title']} — {r['size_mb']} MiB")
    print(f"📁 {r['path']}")
