"""Fábrica TK — Automação Multi-Nicho de Vídeos Verticais 9:16 para TikTok."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

__version__ = "2.0.0"

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"
NICHES_DIR = BASE_DIR / "nichos"
ASSETS_DIR = BASE_DIR / "assets"
MUSIC_DIR = ASSETS_DIR / "music"
INTROS_DIR = ASSETS_DIR / "intros"
BRANDING_DIR = ASSETS_DIR / "branding"
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Carrega as configurações globais do config.json."""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict[str, Any], config_path: Path = CONFIG_PATH) -> None:
    """Salva as configurações globais no config.json."""
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def list_niches() -> list[dict[str, Any]]:
    """Descobre todos os nichos configurados em nichos/*/niche.json."""
    niches: list[dict[str, Any]] = []
    if not NICHES_DIR.exists():
        return niches
    for folder in sorted(NICHES_DIR.iterdir()):
        niche_file = folder / "niche.json"
        if folder.is_dir() and niche_file.is_file():
            try:
                with open(niche_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("id", folder.name)
                data.setdefault("nome", folder.name)
                niches.append(data)
            except Exception:
                continue
    return niches


def get_niche_paths(niche_id: str) -> dict[str, Path]:
    """Retorna todos os diretórios e arquivos dedicados de um nicho."""
    root = NICHES_DIR / niche_id
    assets = root / "assets"
    return {
        "root": root,
        "niche_json": root / "niche.json",
        "history_path": root / "history.json",
        "assets_dir": assets,
        "music_dir": assets / "music",
        "intros_dir": assets / "intros",
        "broll_dir": assets / "broll",
        "branding_dir": assets / "branding",
        "output_dir": root / "output",
    }


def load_niche_config(niche_id: str) -> dict[str, Any]:
    """Carrega o niche.json de um nicho específico."""
    paths = get_niche_paths(niche_id)
    with open(paths["niche_json"], "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("id", niche_id)
    return data


def save_niche_config(niche_id: str, niche_data: dict[str, Any]) -> None:
    """Salva alterações no niche.json do nicho."""
    paths = get_niche_paths(niche_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    with open(paths["niche_json"], "w", encoding="utf-8") as f:
        json.dump(niche_data, f, ensure_ascii=False, indent=2)


def build_runtime_config(
    global_cfg: dict[str, Any] | None = None,
    niche_id: str | None = None,
) -> dict[str, Any]:
    """
    Combina config.json global + nichos/<nicho>/niche.json em um dicionário pronto
    para todos os módulos da pipeline, incluindo caminhos isolados por nicho.
    """
    base = copy.deepcopy(global_cfg) if global_cfg is not None else load_config()
    active_id = niche_id or base.get("nicho_ativo") or "futebol_mexicano"
    niche_paths = get_niche_paths(active_id)

    if not niche_paths["niche_json"].is_file():
        available = list_niches()
        if not available:
            raise FileNotFoundError("Nenhum nicho encontrado na pasta nichos/.")
        active_id = available[0]["id"]
        niche_paths = get_niche_paths(active_id)

    niche = load_niche_config(active_id)
    for key in ("music_dir", "intros_dir", "broll_dir", "branding_dir", "output_dir"):
        niche_paths[key].mkdir(parents=True, exist_ok=True)

    foco = niche.get("foco_padrao", "")
    presets = niche.get("presets_foco", {})
    palavras = niche.get("palavras_chave") or presets.get(foco) or []

    vozes = niche.get("vozes") or []
    voz_padrao = niche.get("voz_padrao") or (vozes[0]["id"] if vozes else "pt-BR-AntonioNeural")
    idioma = niche.get("idioma_padrao", "pt-BR")
    for v in vozes:
        if v.get("id") == voz_padrao and v.get("idioma"):
            idioma = v["idioma"]
            break

    merged = copy.deepcopy(base)
    if isinstance(niche.get("video_override"), dict):
        merged_video = dict(merged.get("video") or {})
        merged_video.update(niche["video_override"])
        merged["video"] = merged_video
    if niche.get("roteiro_palavras_min"):
        merged["roteiro_palavras_min"] = int(niche["roteiro_palavras_min"])
    if niche.get("roteiro_palavras_max"):
        merged["roteiro_palavras_max"] = int(niche["roteiro_palavras_max"])
    merged["nicho_ativo"] = active_id
    merged["nicho_nome"] = niche.get("nome", active_id)
    merged["nicho_etiqueta"] = niche.get("etiqueta", f"{active_id.upper()} • 9:16")
    merged["canal_nome"] = niche.get("canal_nome", "")
    merged["moldura_escura"] = bool(niche.get("moldura_escura", False))
    merged["idioma"] = idioma
    merged["clube_foco"] = foco
    merged["palavras_chave"] = list(palavras)
    merged["presets_foco"] = presets
    merged["vozes"] = vozes
    merged["tts_voz"] = voz_padrao
    merged["rss_feeds"] = niche.get("rss_feeds", [])
    merged["ddgs_regiao"] = niche.get("ddgs_regiao", "br-pt")
    merged["ddgs_news_sufixo"] = niche.get("ddgs_news_sufixo", "noticias")
    merged["legenda"] = niche.get("legenda", merged.get("legenda", {}))
    merged["imagem_ancoras"] = niche.get("imagem_ancoras", [])
    merged["imagem_sufixo_padrao"] = niche.get("imagem_sufixo_padrao", "")
    merged["imagem_fallbacks"] = niche.get("imagem_fallbacks", [])
    merged["prompt_contexto"] = niche.get("prompt_contexto", {})
    merged["top5_banco"] = niche.get("top5_banco", [])
    merged["modo_padrao_nicho"] = niche.get("modo_padrao", merged.get("modo_conteudo", "noticia"))
    merged["canais_adaptador"] = niche.get("canais_adaptador", [])
    merged["canais_corte"] = niche.get("canais_corte", [])
    merged["yt_busca_adaptador"] = niche.get("yt_busca_adaptador", "")
    merged["yt_busca_corte"] = niche.get("yt_busca_corte", "")
    merged["paths"] = {k: str(v) for k, v in niche_paths.items()}
    return merged


def load_history(history_path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    """Carrega o histórico de pautas processadas."""
    if not history_path.exists():
        return []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def append_history(
    url: str,
    title: str,
    source: str = "",
    output_file: str = "",
    history_path: Path = HISTORY_PATH,
) -> None:
    """Adiciona uma pauta concluída ao history.json do nicho."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
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
        slug = "video_fabrica_tk"
    return slug[:max_length].rstrip("_")
