"""Módulo de busca rigorosa, validação anti-lixo e normalização full-bleed 1080x1920 de fotos de futebol."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageEnhance, ImageOps, ImageStat

from src import normalize_text

try:
    from ddgs import DDGS
    _DDGS_USE_QUERY_ARG = True
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS  # type: ignore
    _DDGS_USE_QUERY_ARG = False

LogCallback = Callable[[str], None]

IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

BLOCKED_DOMAINS = (
    "shutterstock.com",
    "gettyimages.com",
    "alamy.com",
    "istockphoto.com",
    "dreamstime.com",
    "123rf.com",
    "depositphotos.com",
    "vecteezy.com",
    "stock.adobe.com",
    "freepik.com",
    "pngtree.com",
    "pngwing.com",
    "cleanpng.com",
    "seekpng.com",
    "klipartz.com",
    "clipart",
    "pinterest.",
    "tiktok.com",
    "facebook.com",
    "instagram.com",
    "amazon.",
    "mercadolibre.",
    "ebay.",
    "etsy.",
    "aliexpress.",
    "redbubble.",
    "teepublic.",
    "walmart.",
    "worldatlas.com",
    "mapsofworld.com",
    "britannica.com",
    "nationalgeographic.",
)

# Palavras proibidas em URL ou título da imagem (elimina mapas, iluminati, vetores, animais, lojas)
BANNED_IMAGE_TOKENS = (
    "mapa",
    "map of",
    "world map",
    "continent",
    "continente",
    "geografia",
    "atlas geo",
    "illuminati",
    "iluminati",
    "freemason",
    "masoneria",
    "pyramid",
    "piramide",
    "conspiracy",
    "all seeing eye",
    "ojo que todo lo ve",
    "vector",
    "clipart",
    "cartoon",
    "caricatura",
    "dibujo",
    "coloring",
    "silhouette",
    "silueta",
    "bandera",
    "flag of",
    "coat of arms",
    "infographic",
    "infografia",
    "diagram",
    "chart",
    "graph",
    "screenshot",
    "captura",
    "wallpaper logo",
    "escudo png",
    "logo png",
    "transparent png",
    "mockup",
    "tienda",
    "comprar",
    "precio",
    "playera venta",
    "animal",
    "wildlife",
    "zoo",
    "bengal tiger",
    "tigre animal",
    "bald eagle",
    "aguila ave",
    "goat animal",
    "chiva animal",
    "puma animal",
    "cougar",
    "lion animal",
    "meme",
    "cemetery",
    "cementerio",
    "grave",
    "tombstone",
    "lapida",
    "crucifix",
    "crucifijo",
    "church",
    "iglesia",
    "religion",
    "bible",
    "biblia",
    "jesus",
    "cross on hill",
    "wooden cross",
)

CLUB_DISAMBIGUATION = [
    (r"\bcross\s+azul\b", "Cruz Azul"),
    (r"\bam[eé]rica\b", "Club America"),
    (r"\btigres\b", "Tigres UANL"),
    (r"\bchivas\b", "Chivas Guadalajara"),
    (r"\bpumas\b", "Pumas UNAM"),
    (r"\ble[oó]n\b", "Club Leon"),
    (r"\batlas\b", "Atlas FC"),
    (r"\bsantos\b", "Santos Laguna"),
    (r"\btoluca\b", "Deportivo Toluca"),
    (r"\bmonterrey\b", "Rayados Monterrey"),
]

FOOTBALL_ANCHORS = (
    "futbol",
    "soccer",
    "liga mx",
    "estadio",
    "partido",
    "jugador",
    "aficion",
    "cancha",
    "gol",
    "portero",
    "entrenador",
    "clasico",
)


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def sanitize_football_query(raw_query: str, club_focus: str = "Club América") -> str:
    """
    Garante que toda busca de imagem seja 100% ancorada em fotografia real de futebol mexicano,
    evitando resultados absurdos (mapas da América, tigres animais, símbolos iluminati, etc.).
    """
    q = re.sub(r"\s+", " ", (raw_query or "").strip())
    norm = normalize_text(q)

    # Se vier uma palavra genérica demais ("clubes", "futbol mexicano"), ancora no clube foco
    if len(norm.split()) <= 1 or norm in ("clubes", "equipos", "mexico", "futbol", "liga mx"):
        q = f"{club_focus} jugadores estadio partido Liga MX"
        norm = normalize_text(q)

    # Desambigua nomes de clubes que têm homônimos (América=continente, Tigres=animal, Atlas=mapa!)
    for pattern, replacement in CLUB_DISAMBIGUATION:
        if re.search(pattern, norm) and normalize_text(replacement) not in norm:
            q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
            norm = normalize_text(q)

    # Garante âncora de futebol mexicano (Liga MX) em toda busca
    if "liga mx" not in norm:
        if any(anchor in norm for anchor in FOOTBALL_ANCHORS):
            q = f"{q} Liga MX"
        else:
            q = f"{q} futbol partido Liga MX"

    return q.strip()


def _compute_dhash(img: Image.Image, hash_size: int = 8) -> int:
    """Calcula perceptual dHash rápido para evitar imagens duplicadas."""
    resized = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    getter = getattr(resized, "get_flattened_data", resized.getdata)
    pixels = list(getter())
    bits = 0
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            if pixels[row_start + col] > pixels[row_start + col + 1]:
                bits |= 1 << (row * hash_size + col)
    return bits


def _hamming_distance(h1: int, h2: int) -> int:
    return (h1 ^ h2).bit_count()


def _is_blocked_candidate(url: str, title: str = "") -> bool:
    """Filtra URLs e títulos que indiquem mapas, vetores, lojas, símbolos ou não-futebol."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return True
    if not host:
        return True
    if any(bad in host for bad in BLOCKED_DOMAINS):
        return True

    lower_url = url.lower()
    if any(ext in lower_url for ext in (".svg", ".gif", ".ico", ".bmp")):
        return True

    combined = normalize_text(f"{url} {title}")
    if any(tok in combined for tok in BANNED_IMAGE_TOKENS):
        return True

    return False


def _looks_like_real_photo(img: Image.Image) -> bool:
    """
    Heurística leve para descartar gráficos chapados, logos sobre fundo branco
    ou mapas vetoriais simples que escapem pelo filtro textual.
    """
    small = img.resize((64, 64), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)

    # Desvio padrão muito baixo em todos os canais => imagem chapada/texto/gráfico simples
    avg_stddev = sum(stat.stddev) / 3.0
    if avg_stddev < 24.0:
        return False

    # Verifica se os 4 cantos são praticamente branco puro (recorte de loja/logo/mapa)
    corners = [
        small.getpixel((2, 2)),
        small.getpixel((61, 2)),
        small.getpixel((2, 61)),
        small.getpixel((61, 61)),
    ]
    white_corners = sum(1 for r, g, b in corners if r > 242 and g > 242 and b > 242)
    if white_corners >= 3:
        return False

    return True


def _download_and_validate_image(url: str, title: str = "", timeout: int = 12) -> Image.Image | None:
    """Baixa uma imagem e valida integridade, resolução mínima e aparência fotográfica com Pillow."""
    if not url or not url.startswith("http") or _is_blocked_candidate(url, title):
        return None

    try:
        resp = requests.get(url, headers=IMAGE_HEADERS, timeout=timeout, stream=True)
        if resp.status_code != 200:
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not ("image" in content_type or "octet-stream" in content_type):
            return None

        raw_bytes = resp.content
        if len(raw_bytes) < 12_000 or len(raw_bytes) > 14_000_000:
            return None

        with Image.open(io.BytesIO(raw_bytes)) as test_img:
            test_img.verify()

        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)

        # Descarta PNGs com transparência dominante (logos/ícones)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            alpha = rgba.getchannel("A")
            alpha_stat = ImageStat.Stat(alpha)
            if alpha_stat.mean[0] < 245:
                return None
            img = rgba.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        if w < 480 or h < 320:
            return None

        aspect = w / h
        if aspect > 2.6 or aspect < 0.35:
            return None

        if not _looks_like_real_photo(img):
            return None

        return img
    except Exception:
        return None


def normalize_image_to_vertical(
    img: Image.Image,
    target_w: int = 1080,
    target_h: int = 1920,
    variant_pan: float = 0.5,
) -> Image.Image:
    """
    Normaliza para 1080x1920 FULL-BLEED estilo @yaguilas1 (tela cheia sem barras nem molduras borradas),
    com enquadramento focado no terço superior (preserva rostos, jogadores e arquibancadas)
    e color grading esportivo vibrante.
    """
    fitted = ImageOps.fit(
        img,
        (target_w, target_h),
        method=Image.Resampling.LANCZOS,
        centering=(variant_pan, 0.28),
    )
    fitted = ImageEnhance.Contrast(fitted).enhance(1.09)
    fitted = ImageEnhance.Color(fitted).enhance(1.14)
    fitted = ImageEnhance.Sharpness(fitted).enhance(1.10)
    return fitted


def _search_ddg_images(query: str, max_results: int = 14) -> list[tuple[str, str]]:
    """Busca lista de (url, titulo) via ddgs com tratamento de erros."""
    results_out: list[tuple[str, str]] = []
    try:
        with DDGS() as ddgs:
            if _DDGS_USE_QUERY_ARG:
                results = ddgs.images(
                    query=query,
                    region="mx-es",
                    safesearch="moderate",
                    max_results=max_results,
                )
            else:
                results = ddgs.images(
                    keywords=query,
                    region="mx-es",
                    safesearch="moderate",
                    max_results=max_results,
                )
            for item in results or []:
                img_url = item.get("image") or item.get("thumbnail")
                title = str(item.get("title") or "")
                if img_url and isinstance(img_url, str) and img_url.startswith("http"):
                    results_out.append((img_url, title))
    except Exception:
        pass
    return results_out


def fetch_and_prepare_images(
    search_terms: list[str],
    output_dir: Path,
    config: dict[str, Any],
    og_image: str = "",
    article_images: list[str] | None = None,
    log_cb: LogCallback | None = None,
) -> list[Path]:
    """
    Baixa, valida (anti-mapas/anti-vetores/anti-duplicatas) e normaliza 14 a 18 fotos reais
    em full-bleed 1080x1920 distribuídas na ordem dos termos do roteiro.
    """
    video_cfg = config.get("video", {})
    target_w = int(video_cfg.get("largura", 1080))
    target_h = int(video_cfg.get("altura", 1920))
    min_images = max(10, int(video_cfg.get("num_imagens_min", 14)))
    max_images = max(min_images, int(video_cfg.get("num_imagens_max", 18)))
    target_images = max(min_images, min(max_images, int(video_cfg.get("num_imagens_alvo", 16))))

    club_focus = config.get("clube_foco", "Club América")
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    valid_raw_images: list[Image.Image] = []
    seen_hashes: list[int] = []
    tried_urls: set[str] = set()

    def _try_add(url: str, title: str = "") -> bool:
        if url in tried_urls:
            return False
        tried_urls.add(url)
        img = _download_and_validate_image(url, title=title)
        if img is None:
            return False
        dhash = _compute_dhash(img)
        if any(_hamming_distance(dhash, prev) <= 6 for prev in seen_hashes):
            return False
        seen_hashes.append(dhash)
        valid_raw_images.append(img)
        _log(
            log_cb,
            f"[MediaFetcher] Foto real válida {len(valid_raw_images)}/{target_images} "
            f"({img.width}x{img.height})",
        )
        return True

    _log(
        log_cb,
        f"[MediaFetcher] Buscando fotos reais de futebol (alvo: {target_images}, mín: {min_images}, máx: {max_images})...",
    )

    # 1) Primeiro tenta a imagem principal e extras da própria matéria
    for direct_url in [og_image, *(article_images or [])]:
        if len(valid_raw_images) >= target_images:
            break
        if direct_url:
            _try_add(direct_url, title=f"{club_focus} Liga MX")

    # 2) Distribui cotas por termo de busca na ordem do roteiro (ideal para Top 5 e notícias!)
    clean_terms = [sanitize_football_query(t, club_focus) for t in search_terms if t and t.strip()]
    per_term_quota = max(2, (target_images - len(valid_raw_images) + len(clean_terms) - 1) // max(1, len(clean_terms)))

    for idx, term in enumerate(clean_terms, start=1):
        if len(valid_raw_images) >= target_images:
            break
        _log(log_cb, f"[MediaFetcher] Pesquisando ({idx}/{len(clean_terms)}): '{term}'")
        found = _search_ddg_images(term, max_results=14)
        added_this_term = 0
        for img_url, img_title in found:
            if len(valid_raw_images) >= target_images or added_this_term >= per_term_quota:
                break
            if _try_add(img_url, title=img_title):
                added_this_term += 1
        time.sleep(0.35)

    # 3) Buscas complementares de estádios/torcidas/partidas da Liga MX caso ainda falte
    if len(valid_raw_images) < target_images:
        fallback_queries = [
            f"{club_focus} aficion estadio lleno futbol Liga MX",
            f"{club_focus} jugadores celebrando gol partido Liga MX",
            "Estadio Azteca aficion noche futbol Liga MX",
            "Liga MX partido estadio lleno torcida futbol mexicano",
            "Clásico Nacional Club America vs Chivas partido futbol",
        ]
        for fq in fallback_queries:
            if len(valid_raw_images) >= target_images:
                break
            sanitized_fq = sanitize_football_query(fq, club_focus)
            _log(log_cb, f"[MediaFetcher] Busca complementar: '{sanitized_fq}'")
            for img_url, img_title in _search_ddg_images(sanitized_fq, max_results=14):
                if len(valid_raw_images) >= target_images:
                    break
                _try_add(img_url, title=img_title)
            time.sleep(0.35)

    if not valid_raw_images:
        raise RuntimeError(
            "Não foi possível baixar nenhuma foto válida de futebol. "
            "Verifique sua conexão com a internet ou tente novamente."
        )

    # Normaliza todas para 1080x1920 full-bleed
    saved_paths: list[Path] = []
    for idx, raw_img in enumerate(valid_raw_images[:max_images]):
        norm_img = normalize_image_to_vertical(raw_img, target_w=target_w, target_h=target_h)
        out_path = images_dir / f"img_{idx:02d}.jpg"
        norm_img.save(out_path, format="JPEG", quality=93, optimize=True)
        saved_paths.append(out_path)

    # Garantia final caso falte alguma cena para atingir o mínimo
    if len(saved_paths) < min_images:
        _log(
            log_cb,
            f"[MediaFetcher] Complementando enquadramentos ({len(saved_paths)} -> {min_images} cenas)...",
        )
        pan_offsets = [0.22, 0.78, 0.35, 0.65]
        src_idx = 0
        while len(saved_paths) < min_images:
            base_img = valid_raw_images[src_idx % len(valid_raw_images)]
            pan = pan_offsets[len(saved_paths) % len(pan_offsets)]
            norm_img = normalize_image_to_vertical(base_img, target_w=target_w, target_h=target_h, variant_pan=pan)
            out_path = images_dir / f"img_{len(saved_paths):02d}.jpg"
            norm_img.save(out_path, format="JPEG", quality=93, optimize=True)
            saved_paths.append(out_path)
            src_idx += 1

    _log(log_cb, f"[MediaFetcher] {len(saved_paths)} fotos full-bleed normalizadas em {target_w}x{target_h}.")
    return saved_paths
