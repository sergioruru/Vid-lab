#!/usr/bin/env python3
"""S3 upload для vid-lab — presigned URL + токен-маппинг + дата в имени."""
import os, uuid, datetime, re, sqlite3
import boto3
from botocore.config import Config
import requests

ENV = os.path.expanduser("~/.hermes/config/s3-b2b-contact.env")
DL_DOMAIN = "https://agent.rusinov-s.ru"

def load_config():
    cfg = {}
    with open(ENV) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                cfg[k] = v
    return cfg

cfg = load_config()

s3 = boto3.session.Session().client(
    "s3",
    endpoint_url=cfg["S3_ENDPOINT"],
    aws_access_key_id=cfg["S3_ACCESS_KEY"],
    aws_secret_access_key=cfg["S3_SECRET_KEY"],
    region_name="ru1",
    config=Config(signature_version="s3v4"),
)

BUCKET = cfg["S3_BUCKET"]
PREFIX = "vid-lab"


def _init_link_db():
    conn = sqlite3.connect(_db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            token TEXT PRIMARY KEY,
            s3_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            original_filename TEXT
        )
    """)
    conn.commit()
    conn.close()


def _db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "links.db")


def upload_file(local_path: str, original_filename: str, expire: int = 86400) -> str | None:
    """Загрузить в S3, сохранить токен. Вернуть URL вида dl.kserv.pro/vid-lab/<token>."""
    _init_link_db()

    today = datetime.date.today().isoformat()
    safe_name = re.sub(r'[^\w\.-]', '_', original_filename)
    key = f"{PREFIX}/{today}/{safe_name}"

    content_type = "video/mp4" if key.endswith(".mp4") else "application/octet-stream"

    try:
        # PUT — presigned URL для загрузки
        put_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=3600,
        )

        with open(local_path, "rb") as f:
            data = f.read()

        r = requests.put(put_url, data=data, headers={"Content-Type": content_type}, timeout=300)
        if r.status_code >= 400:
            return None

        # Токен
        token = uuid.uuid4().hex[:12]
        now = datetime.datetime.utcnow().isoformat()

        conn = sqlite3.connect(_db_path())
        conn.execute(
            "INSERT OR REPLACE INTO links (token, s3_key, created_at, original_filename) VALUES (?, ?, ?, ?)",
            (token, key, now, safe_name)
        )
        conn.commit()
        conn.close()

        # Ссылка через наш домен
        return f"{DL_DOMAIN}/dl/vid-lab/{token}"

    except Exception as e:
        return None


def get_presigned_url(token: str, expire: int = 300) -> str | None:
    """Сгенерировать свежий presigned URL для скачивания (живёт 5 мин)."""
    conn = sqlite3.connect(_db_path())
    row = conn.execute("SELECT s3_key FROM links WHERE token=?", (token,)).fetchone()
    conn.close()
    if not row:
        return None

    key = row[0]
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=expire,
        )
    except Exception:
        return None


def delete_file(filename: str):
    key = f"{PREFIX}/{filename}"
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except:
        pass


def cleanup_expired(older_than_days: int = 1):
    """Удалить токены старше N дней (S3 не трогаем — объекты живут по TTL Beget)."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=older_than_days)).isoformat()
    conn = sqlite3.connect(_db_path())
    conn.execute("DELETE FROM links WHERE created_at < ?", (cutoff,))
    conn.commit()
    conn.close()
