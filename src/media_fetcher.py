"""Módulo multi-nicho de busca de fotos de ação, filtro anti-lixo e normalização full-bleed 1080x1920."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

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
    "mercadolivre.",
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
    "escudo png",
    "logo png",
    "transparent png",
    "mockup",
    "tienda",
    "comprar",
    "precio",
    "playera venta",
    "camiseta venda",
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

FOOTBALL_CLUB_DISAMBIGUATION = [
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


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def sanitize_niche_query(raw_query: str, config: dict[str, Any]) -> str:
    """
    Garante que toda busca de imagem esteja fortemente ancorada no nicho ativo
    (ex.: lances/jogadores de Liga MX ou cenas reais 4K de GTA VI), evitando mapas e lixo.
    """
    niche_id = str(config.get("nicho_ativo", "futebol_mexicano"))
    foco = str(config.get("clube_foco") or config.get("nicho_nome") or "")
    anchors = [normalize_text(a) for a in (config.get("imagem_ancoras") or []) if a]
    suffix = str(config.get("imagem_sufixo_padrao") or "").strip()

    q = re.sub(r"\s+", " ", (raw_query or "").strip())
    norm = normalize_text(q)

    if len(norm.split()) <= 1 or norm in ("clubes", "equipos", "mexico", "jogo", "game"):
        q = f"{foco} {suffix}".strip()
        norm = normalize_text(q)

    if niche_id == "futebol_mexicano":
        for pattern, replacement in FOOTBALL_CLUB_DISAMBIGUATION:
            if re.search(pattern, norm) and normalize_text(replacement) not in norm:
                q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
                norm = normalize_text(q)
        if "liga mx" not in norm:
            q = f"{q} partido Liga MX"
        if not any(w in norm for w in ("gol", "jugad", "celebr", "atajad", "partido", "cancha", "campeon", "trofeo")):
            q = f"{q} jugadores accion"
    elif niche_id == "gta_vi":
        if "gta" not in norm and "grand theft auto" not in norm:
            q = f"GTA 6 {q}"
        if "vice city" not in norm and "leonida" not in norm and "rockstar" not in norm:
            q = f"{q} Rockstar Games Vice City"
    else:
        if anchors and not any(a in norm for a in anchors) and suffix:
            q = f"{q} {suffix}"

    return q.strip()


def _compute_dhash(img: Image.Image, hash_size: int = 8) -> int:
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


def _is_blocked_candidate(url: str, title: str = "", niche_id: str = "") -> bool:
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

    # No nicho GTA VI, bloqueia capas/artes antigas do GTA V / GTA Online
    if niche_id == "gta_vi":
        has_gta6 = any(k in combined for k in ("gta 6", "gta6", "gta vi", "gtavi", "leonida", "lucia"))
        has_old_gta = any(k in combined for k in ("gta v", "gta 5", "gtav", "gta5", "gta-v", "gta-5", "gta online", "san andreas", "gta iv", "gta 4"))
        if has_old_gta and not has_gta6:
            return True

    return False


def _looks_like_real_scene(img: Image.Image) -> bool:
    """Descarta gráficos chapados, recortes de fundo branco ou mapas vetoriais simples."""
    small = img.resize((64, 64), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    if (sum(stat.stddev) / 3.0) < 22.0:
        return False

    corners = [
        small.getpixel((2, 2)),
        small.getpixel((61, 2)),
        small.getpixel((2, 61)),
        small.getpixel((61, 61)),
    ]
    white_corners = sum(1 for r, g, b in corners if r > 242 and g > 242 and b > 242)
    return white_corners < 3


def _download_and_validate_image(
    url: str,
    title: str = "",
    niche_id: str = "",
    strict_hd: bool = True,
    timeout: int = 12,
) -> Image.Image | None:
    if not url or not url.startswith("http") or _is_blocked_candidate(url, title, niche_id=niche_id):
        return None

    try:
        resp = requests.get(url, headers=IMAGE_HEADERS, timeout=timeout, stream=True)
        if resp.status_code != 200:
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not ("image" in content_type or "octet-stream" in content_type):
            return None

        raw_bytes = resp.content
        min_bytes = 45_000 if strict_hd else 28_000
        if len(raw_bytes) < min_bytes or len(raw_bytes) > 18_000_000:
            return None

        with Image.open(io.BytesIO(raw_bytes)) as test_img:
            test_img.verify()

        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            alpha_stat = ImageStat.Stat(rgba.getchannel("A"))
            if alpha_stat.mean[0] < 245:
                return None
            img = rgba.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        # Exige resolução HD real para evitar borrão no crop vertical 1080x1920
        if strict_hd:
            # Aceita Full HD / 1440p / 4K (horizontal >= 1000px altura ou vertical >= 720x1080)
            is_hd_landscape = h >= 1000 and w >= 1200
            is_hd_portrait = w >= 720 and h >= 1080
            if not (is_hd_landscape or is_hd_portrait):
                return None
        else:
            # Fallback mínimo 720p (jamais aceita sub-HD borrado como 768x384)
            if h < 720 or w < 720:
                return None

        aspect = w / h
        if aspect > 2.5 or aspect < 0.35:
            return None

        if not _looks_like_real_scene(img):
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
    """Normaliza 100% full-bleed 1080x1920 HD com nitidez UnsharpMask e contraste vibrante."""
    fitted = ImageOps.fit(
        img,
        (target_w, target_h),
        method=Image.Resampling.LANCZOS,
        centering=(variant_pan, 0.28),
    )
    fitted = fitted.filter(ImageFilter.UnsharpMask(radius=1.3, percent=125, threshold=3))
    fitted = ImageEnhance.Contrast(fitted).enhance(1.08)
    fitted = ImageEnhance.Color(fitted).enhance(1.14)
    return fitted


def _parse_int(val: Any) -> int:
    try:
        return int(str(val).strip())
    except Exception:
        return 0


def _hd_priority_score(w: int, h: int) -> float:
    """Pontua imagens priorizando 4K/Full HD e menor fator de ampliação para 1080x1920."""
    if w <= 0 or h <= 0:
        return 0.5
    upscale = max(1080.0 / w, 1920.0 / h)
    # Menor upscale = mais nítida no frame vertical; bônus para fotos verticais/4K
    score = 10.0 / max(0.5, upscale)
    if h >= 1440:
        score += 4.0
    elif h >= 1080:
        score += 2.5
    if h >= w:
        score += 1.5  # Fotos verticais precisam de menos recorte lateral
    return score


def _search_ddg_images(
    query: str,
    region: str = "br-pt",
    max_results: int = 30,
) -> list[tuple[str, str, int, int]]:
    """
    Busca imagens via ddgs e ordena colocando as de maior resolução (4K / Full HD / Verticais HD)
    no topo ANTES mesmo de baixar!
    """
    results_out: list[tuple[str, str, int, int]] = []
    try:
        with DDGS() as ddgs:
            if _DDGS_USE_QUERY_ARG:
                results = ddgs.images(
                    query=query,
                    region=region,
                    safesearch="moderate",
                    size="Large",
                    max_results=max_results,
                )
            else:
                results = ddgs.images(
                    keywords=query,
                    region=region,
                    safesearch="moderate",
                    size="Large",
                    max_results=max_results,
                )
            for item in results or []:
                img_url = item.get("image") or item.get("thumbnail")
                title = str(item.get("title") or "")
                w = _parse_int(item.get("width"))
                h = _parse_int(item.get("height"))
                # Descarta thumbnails minúsculos já nos metadados sem gastar banda
                if w > 0 and h > 0 and (h < 720 or w < 720):
                    continue
                if img_url and isinstance(img_url, str) and img_url.startswith("http"):
                    results_out.append((img_url, title, w, h))
    except Exception:
        pass

    results_out.sort(key=lambda r: _hd_priority_score(r[2], r[3]), reverse=True)
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
    Baixa, valida (anti-mapas/anti-lixo/anti-duplicatas) e normaliza 18 a 24 cenas full-bleed
    em 1080x1920 para cortes rápidos de 2.4–3.2s com retenção máxima.
    """
    video_cfg = config.get("video", {})
    target_w = int(video_cfg.get("largura", 1080))
    target_h = int(video_cfg.get("altura", 1920))
    min_images = max(12, int(video_cfg.get("num_cenas_min", video_cfg.get("num_imagens_min", 18))))
    max_images = max(min_images, int(video_cfg.get("num_cenas_max", video_cfg.get("num_imagens_max", 24))))
    target_images = max(
        min_images,
        min(max_images, int(video_cfg.get("num_cenas_alvo", video_cfg.get("num_imagens_alvo", 20)))),
    )

    region = str(config.get("ddgs_regiao", "br-pt"))
    niche_id = str(config.get("nicho_ativo", ""))
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    valid_raw_images: list[Image.Image] = []
    seen_hashes: list[int] = []
    tried_urls: set[str] = set()

    def _try_add(url: str, title: str = "", strict_hd: bool = True) -> bool:
        if url in tried_urls:
            return False
        tried_urls.add(url)
        img = _download_and_validate_image(url, title=title, niche_id=niche_id, strict_hd=strict_hd)
        if img is None:
            return False
        dhash = _compute_dhash(img)
        if any(_hamming_distance(dhash, prev) <= 6 for prev in seen_hashes):
            return False
        seen_hashes.append(dhash)
        valid_raw_images.append(img)
        quality_tag = "4K" if img.height >= 1800 else ("FullHD" if img.height >= 1000 else "HD 720p")
        _log(
            log_cb,
            f"[MediaFetcher] Cena {quality_tag} {len(valid_raw_images)}/{target_images} ({img.width}x{img.height})",
        )
        return True

    _log(
        log_cb,
        f"[MediaFetcher] Buscando cenas em HD/4K ({config.get('nicho_nome', '')} | alvo: {target_images})...",
    )

    # 1) Imagem principal e extras da matéria (aceita >= 720p por ser foto exata da matéria)
    for direct_url in [og_image, *(article_images or [])]:
        if len(valid_raw_images) >= target_images:
            break
        if direct_url and direct_url.startswith("http"):
            _try_add(direct_url, title=str(config.get("nicho_nome", "")), strict_hd=False)

    # 2) Distribui cotas equilibradas por termo (priorizando 4K/FullHD primeiro, depois 720p)
    clean_terms = [sanitize_niche_query(t, config) for t in search_terms if t and t.strip()]
    per_term_quota = max(3, (target_images - len(valid_raw_images) + len(clean_terms) - 1) // max(1, len(clean_terms)))
    fallback_720p_pool: list[tuple[str, str]] = []

    for idx, term in enumerate(clean_terms, start=1):
        if len(valid_raw_images) >= target_images:
            break
        _log(log_cb, f"[MediaFetcher] Pesquisando HD ({idx}/{len(clean_terms)}): '{term}'")
        found = _search_ddg_images(term, region=region, max_results=30)
        added_this = 0
        # 1ª passada: apenas Full HD / 4K (altura >= 1000px ou vertical >= 720x1080)
        for img_url, img_title, mw, mh in found:
            if len(valid_raw_images) >= target_images or added_this >= per_term_quota:
                break
            if mw > 0 and mh > 0 and not ((mh >= 1000 and mw >= 1200) or (mw >= 720 and mh >= 1080)):
                fallback_720p_pool.append((img_url, img_title))
                continue
            if _try_add(img_url, title=img_title, strict_hd=True):
                added_this += 1
        # Se este termo não bateu a cota em 1080p+, completa com 720p do próprio termo
        if added_this < per_term_quota and len(valid_raw_images) < target_images:
            for img_url, img_title, _, _ in found:
                if len(valid_raw_images) >= target_images or added_this >= per_term_quota:
                    break
                if _try_add(img_url, title=img_title, strict_hd=False):
                    added_this += 1
        time.sleep(0.35)

    # 3) Buscas complementares do nicho em Full HD / 4K
    if len(valid_raw_images) < target_images:
        for fq in config.get("imagem_fallbacks") or []:
            if len(valid_raw_images) >= target_images:
                break
            sanitized_fq = sanitize_niche_query(fq, config)
            _log(log_cb, f"[MediaFetcher] Busca complementar HD: '{sanitized_fq}'")
            for img_url, img_title, _, _ in _search_ddg_images(sanitized_fq, region=region, max_results=28):
                if len(valid_raw_images) >= target_images:
                    break
                if not _try_add(img_url, title=img_title, strict_hd=True):
                    fallback_720p_pool.append((img_url, img_title))
            time.sleep(0.35)

    # 4) Completa com pool 720p se ainda faltar para atingir o alvo
    if len(valid_raw_images) < target_images:
        for img_url, img_title in fallback_720p_pool:
            if len(valid_raw_images) >= target_images:
                break
            _try_add(img_url, title=img_title, strict_hd=False)

    if not valid_raw_images:
        raise RuntimeError(
            "Não foi possível baixar nenhuma imagem HD válida para o vídeo. "
            "Verifique sua conexão com a internet ou tente novamente."
        )

    saved_paths: list[Path] = []
    for idx, raw_img in enumerate(valid_raw_images[:max_images]):
        norm_img = normalize_image_to_vertical(raw_img, target_w=target_w, target_h=target_h)
        out_path = images_dir / f"img_{idx:02d}.jpg"
        norm_img.save(out_path, format="JPEG", quality=96, subsampling=0, optimize=True)
        saved_paths.append(out_path)

    if len(saved_paths) < min_images:
        pan_offsets = [0.22, 0.78, 0.35, 0.65]
        src_idx = 0
        while len(saved_paths) < min_images:
            base_img = valid_raw_images[src_idx % len(valid_raw_images)]
            pan = pan_offsets[len(saved_paths) % len(pan_offsets)]
            norm_img = normalize_image_to_vertical(base_img, target_w=target_w, target_h=target_h, variant_pan=pan)
            out_path = images_dir / f"img_{len(saved_paths):02d}.jpg"
            norm_img.save(out_path, format="JPEG", quality=96, subsampling=0, optimize=True)
            saved_paths.append(out_path)
            src_idx += 1

    _log(log_cb, f"[MediaFetcher] {len(saved_paths)} cenas full-bleed prontas em {target_w}x{target_h}.")
    return saved_paths
