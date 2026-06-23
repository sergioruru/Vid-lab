#!/usr/bin/env python3
"""vid-lab DL server — редирект с нашего домена на presigned S3 URL."""
import os, sys
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3_upload import get_presigned_url

app = FastAPI(title="vid-lab-dl")


@app.get("/dl/vid-lab/{token}")
async def download(token: str):
    url = get_presigned_url(token)
    if not url:
        raise HTTPException(status_code=404, detail="Link expired or not found")
    return RedirectResponse(url=url)
