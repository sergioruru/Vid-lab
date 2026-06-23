#!/usr/bin/env python3
"""dl_server — прокси S3-контента без редиректа (X-Accel-Redirect style)."""
import os, sys, re
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3_upload import get_presigned_url

app = FastAPI(title="vid-lab dl-server")


@app.get("/dl/vid-lab/{token}")
async def proxy(token: str):
    """Потоково проксировать видео из S3. Клиент видит только наш домен."""
    # Валидация токена
    if not re.match(r'^[a-f0-9]{12}$', token):
        raise HTTPException(status_code=404, detail="Invalid token")

    url = get_presigned_url(token, expire=3600)
    if not url:
        raise HTTPException(status_code=404, detail="Link expired or not found")

    try:
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "video/mp4")
        content_length = resp.headers.get("content-length", "0")

        return StreamingResponse(
            resp.iter_content(chunk_size=65536),
            media_type=content_type,
            headers={
                "Content-Disposition": "inline",
                "Content-Length": content_length,
                "Cache-Control": "private, max-age=300",
            }
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Failed to fetch from storage")


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5013
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
