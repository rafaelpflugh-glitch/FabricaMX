"""FábricaMX - Automação de Vídeos Verticais de Liga MX para TikTok."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

__version__ = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"
ASSETS_DIR = BASE_DIR / "assets"
MUSIC_DIR = ASSETS_DIR / "music"
INTROS_DIR = ASSETS_DIR / "intros"
BRANDING_DIR = ASSETS_DIR / "branding"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Carrega as configurações do config.json."""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict[str, Any], config_path: Path = CONFIG_PATH) -> None:
    """Salva as configurações no config.json."""
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_history(history_path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    """Carrega o histórico de notícias processadas."""
    if not history_path.exists():
        return []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def append_history(
    url: str,
    title: str,
    source: str = "",
    output_file: str = "",
    history_path: Path = HISTORY_PATH,
) -> None:
    """Adiciona uma notícia concluída ao history.json."""
    history = load_history(history_path)
    entry = {
        "url": url.strip(),
        "title": title.strip(),
        "source": source.strip(),
        "output_file": output_file,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    history.append(entry)
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def normalize_text(text: str) -> str:
    """Remove acentos e normaliza texto para comparações case-insensitive."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def slugify(text: str, max_length: int = 48) -> str:
    """Gera um slug seguro para nomes de arquivo no Windows."""
    norm = normalize_text(text)
    slug = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")
    if not slug:
        slug = "video_ligamx"
    return slug[:max_length].rstrip("_")
