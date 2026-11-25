"""Módulo de busca, validação e normalização (1080x1920) de imagens para o vídeo."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

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
    "tiktok.com",
    "facebook.com",
    "instagram.com",
)


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


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


def _is_blocked_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return True
    if not host:
        return True
    if any(bad in host for bad in BLOCKED_DOMAINS):
        return True
    lower = url.lower()
    if any(ext in lower for ext in (".svg", ".gif", ".ico")):
        return True
    return False


def _download_and_validate_image(url: str, timeout: int = 12) -> Image.Image | None:
    """Baixa uma imagem e valida integridade e resolução mínima com Pillow."""
    if not url or not url.startswith("http") or _is_blocked_url(url):
        return None

    try:
        resp = requests.get(url, headers=IMAGE_HEADERS, timeout=timeout, stream=True)
        if resp.status_code != 200:
            return None

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if content_type and not ("image" in content_type or "octet-stream" in content_type):
            return None

        # Limite de 12 MB por imagem
        raw_bytes = resp.content
        if len(raw_bytes) < 8_000 or len(raw_bytes) > 12_000_000:
            return None

        # Valida integridade com verify() e reabre para uso
        with Image.open(io.BytesIO(raw_bytes)) as test_img:
            test_img.verify()

        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        if w < 420 or h < 280:
            return None

        aspect = w / h
        if aspect > 3.2 or aspect < 0.32:
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
    Normaliza qualquer imagem para 1080x1920 com preenchimento inteligente:
    - Fotos verticais ou quase quadradas: cover crop focado no terço superior (rostos).
    - Fotos horizontais (comuns em portais esportivos): fundo borrado cinematográfico
      em tela cheia + destaque central nítido com bordas suavizadas (sem cortes bruscos
      nos jogadores e sem barras pretas).
    """
    w, h = img.size
    aspect = w / h

    # Vertical ou quase quadrada -> cover crop direto
    if aspect <= 1.15:
        return ImageOps.fit(
            img,
            (target_w, target_h),
            method=Image.Resampling.LANCZOS,
            centering=(variant_pan, 0.32),
        )

    # Horizontal -> fundo borrado + primeiro plano de alto impacto com transição suave
    bg = ImageOps.fit(
        img,
        (target_w, target_h),
        method=Image.Resampling.BILINEAR,
        centering=(variant_pan, 0.5),
    )
    bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)
    bg = ImageEnhance.Color(bg).enhance(1.15)

    # Primeiro plano: amplia levemente para preencher bem a vertical sem decepar sujeitos
    fg_target_h = int(min(target_h * 0.66, max(target_w / aspect, target_h * 0.52)))
    fg = ImageOps.fit(
        img,
        (target_w, fg_target_h),
        method=Image.Resampling.LANCZOS,
        centering=(variant_pan, 0.38),
    )
    fg = ImageEnhance.Contrast(fg).enhance(1.06)
    fg = ImageEnhance.Sharpness(fg).enhance(1.08)

    # Máscara vertical com feather suave no topo e base do primeiro plano
    feather = min(90, fg_target_h // 6)
    mask = Image.new("L", (target_w, fg_target_h), 255)
    mask_pixels = mask.load()
    for y in range(feather):
        alpha = int(255 * (y / feather))
        for x in range(target_w):
            mask_pixels[x, y] = alpha
            mask_pixels[x, fg_target_h - 1 - y] = alpha

    canvas = bg.copy()
    y_offset = (target_h - fg_target_h) // 2 - 40  # Levemente acima para abrir espaço à legenda
    canvas.paste(fg, (0, y_offset), mask)
    return canvas


def _search_ddg_images(query: str, max_results: int = 10) -> list[str]:
    """Busca URLs de imagem via ddgs (ou duckduckgo-search) com proteção contra rate-limit."""
    urls: list[str] = []
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
                if img_url and isinstance(img_url, str) and img_url.startswith("http"):
                    urls.append(img_url)
    except Exception:
        pass
    return urls


def fetch_and_prepare_images(
    search_terms: list[str],
    output_dir: Path,
    config: dict[str, Any],
    og_image: str = "",
    article_images: list[str] | None = None,
    log_cb: LogCallback | None = None,
) -> list[Path]:
    """
    Baixa, valida, deduplica e normaliza entre 10 e 14 imagens em 1080x1920.
    """
    video_cfg = config.get("video", {})
    target_w = int(video_cfg.get("largura", 1080))
    target_h = int(video_cfg.get("altura", 1920))
    min_images = max(10, int(video_cfg.get("num_imagens_min", 10)))
    max_images = min(14, max(min_images, int(video_cfg.get("num_imagens_max", 14))))
    target_images = max(min_images, min(max_images, int(video_cfg.get("num_imagens_alvo", 12))))

    club_focus = config.get("clube_foco", "Club América")
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    candidate_urls: list[str] = []
    if og_image:
        candidate_urls.append(og_image)
    for extra in article_images or []:
        if extra and extra not in candidate_urls:
            candidate_urls.append(extra)

    _log(log_cb, f"[MediaFetcher] Buscando imagens (alvo: {target_images}, mín: {min_images}, máx: {max_images})...")

    # Coleta URLs a partir dos 5 termos gerados pelo Ollama
    for idx, term in enumerate(search_terms, start=1):
        _log(log_cb, f"[MediaFetcher] Pesquisando ({idx}/{len(search_terms)}): '{term}'")
        found = _search_ddg_images(term, max_results=8)
        for u in found:
            if u not in candidate_urls:
                candidate_urls.append(u)
        time.sleep(0.45)  # Pausa curta para evitar bloqueio do DuckDuckGo

    valid_raw_images: list[Image.Image] = []
    seen_hashes: list[int] = []
    tried_urls: set[str] = set()

    def _consume_urls(urls: list[str]) -> None:
        for url in urls:
            if len(valid_raw_images) >= target_images:
                return
            if url in tried_urls:
                continue
            tried_urls.add(url)

            img = _download_and_validate_image(url)
            if img is None:
                continue

            dhash = _compute_dhash(img)
            if any(_hamming_distance(dhash, prev) <= 6 for prev in seen_hashes):
                continue

            seen_hashes.append(dhash)
            valid_raw_images.append(img)
            _log(
                log_cb,
                f"[MediaFetcher] Imagem válida {len(valid_raw_images)}/{target_images} "
                f"({img.width}x{img.height})",
            )

    _consume_urls(candidate_urls)

    # Queries de fallback caso faltem imagens para atingir o mínimo (10)
    if len(valid_raw_images) < min_images:
        fallback_queries = [
            f"{club_focus} Liga MX partido",
            f"{club_focus} aficion estadio Mexico",
            f"{club_focus} jugadores futbol",
            "Liga MX futbol mexicano estadio",
            "Futbol mexicano partido goles Liga MX",
        ]
        for fq in fallback_queries:
            if len(valid_raw_images) >= target_images:
                break
            _log(log_cb, f"[MediaFetcher] Busca complementar: '{fq}'")
            more_urls = _search_ddg_images(fq, max_results=10)
            _consume_urls(more_urls)
            time.sleep(0.5)

    if not valid_raw_images:
        raise RuntimeError(
            "Não foi possível baixar nenhuma imagem válida. "
            "Verifique sua conexão com a internet ou tente novamente."
        )

    # Normaliza todas as imagens coletadas para 1080x1920
    saved_paths: list[Path] = []
    for idx, raw_img in enumerate(valid_raw_images[:max_images]):
        norm_img = normalize_image_to_vertical(raw_img, target_w=target_w, target_h=target_h)
        out_path = images_dir / f"img_{idx:02d}.jpg"
        norm_img.save(out_path, format="JPEG", quality=93, optimize=True)
        saved_paths.append(out_path)

    # Garantia final: se mesmo após fallbacks houver menos que min_images (ex: rate-limit momentâneo),
    # cria variações de enquadramento lateral/zoom das melhores imagens já validadas.
    if len(saved_paths) < min_images:
        _log(
            log_cb,
            f"[MediaFetcher] Aviso: apenas {len(saved_paths)} imagens únicas encontradas. "
            f"Gerando variações de enquadramento para garantir {min_images} cenas.",
        )
        pan_offsets = [0.25, 0.75, 0.35, 0.65]
        src_idx = 0
        while len(saved_paths) < min_images:
            base_img = valid_raw_images[src_idx % len(valid_raw_images)]
            pan = pan_offsets[(len(saved_paths)) % len(pan_offsets)]
            # Recorte ligeiramente mais fechado para parecer outro ângulo
            bw, bh = base_img.size
            crop_w = int(bw * 0.84)
            crop_h = int(bh * 0.84)
            left = int((bw - crop_w) * pan)
            top = int((bh - crop_h) * 0.35)
            cropped = base_img.crop((left, top, left + crop_w, top + crop_h))
            norm_img = normalize_image_to_vertical(cropped, target_w=target_w, target_h=target_h, variant_pan=pan)
            out_path = images_dir / f"img_{len(saved_paths):02d}.jpg"
            norm_img.save(out_path, format="JPEG", quality=93, optimize=True)
            saved_paths.append(out_path)
            src_idx += 1

    _log(log_cb, f"[MediaFetcher] {len(saved_paths)} imagens normalizadas em {target_w}x{target_h}.")
    return saved_paths
