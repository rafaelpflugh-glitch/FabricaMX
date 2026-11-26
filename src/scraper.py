"""Módulo multi-nicho de coleta de notícias recentes (DDGS News + RSS) e scraping completo."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from src import HISTORY_PATH, load_history, normalize_text

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore

LogCallback = Callable[[str], None]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,es-MX;q=0.8,en;q=0.6",
}

BOILERPLATE_PHRASES = (
    "cookies",
    "suscríbete",
    "suscribete",
    "inscreva-se",
    "inicia sesión",
    "derechos reservados",
    "direitos reservados",
    "política de privacidad",
    "politica de privacidade",
    "te puede interesar",
    "leia também",
    "descarga la app",
    "whatsapp",
    "newsletter",
)

MAX_ARTICLE_AGE_DAYS = 14

LOW_VALUE_TITLE_PATTERNS = (
    "how to watch",
    "where to watch",
    "live stream",
    "tv channel",
    "what channel",
    "donde ver",
    "onde assistir",
    "a que hora",
    "horario y transmision",
    "horario y donde",
    "canal y horario",
    "minuto a minuto",
    "en vivo:",
    "en vivo hoy",
    "ver en vivo",
    "momios",
    "apuestas",
    "pronostico",
    "odds",
    "prediction",
    "palpite",
)


@dataclass
class NewsArticle:
    """Representa uma pauta coletada e enriquecida com texto completo e vídeos embutidos."""

    title: str
    url: str
    source: str
    published: str = ""
    published_ts: float = 0.0
    summary: str = ""
    full_text: str = ""
    og_image: str = ""
    article_images: list[str] = field(default_factory=list)
    embedded_videos: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    score: int = 0


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _canonical_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()


def _parse_date_ts(raw_date: str) -> float:
    if not raw_date:
        return 0.0
    raw = raw_date.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _is_too_old(published_ts: float, max_days: int = MAX_ARTICLE_AGE_DAYS) -> bool:
    if published_ts <= 0.0:
        return False
    return (datetime.now(timezone.utc).timestamp() - published_ts) > max_days * 86400


def _extract_history_sets(history_path: Path) -> tuple[set[str], set[str]]:
    history = load_history(history_path)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for item in history:
        if isinstance(item, str):
            if item.startswith("http"):
                seen_urls.add(_canonical_url(item))
            else:
                seen_titles.add(normalize_text(item))
        elif isinstance(item, dict):
            url = item.get("url", "")
            title = item.get("title", "")
            if url:
                seen_urls.add(_canonical_url(url))
            if title:
                seen_titles.add(normalize_text(title))

    return seen_urls, seen_titles


def _clean_html_summary(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _extract_rss_image(entry: Any) -> str:
    for media in entry.get("media_content") or []:
        url = media.get("url")
        if url and isinstance(url, str) and url.startswith("http"):
            return url
    for thumb in entry.get("media_thumbnail") or []:
        url = thumb.get("url")
        if url and isinstance(url, str) and url.startswith("http"):
            return url
    for enc in entry.get("enclosures") or []:
        href = enc.get("href") or enc.get("url")
        mime = enc.get("type", "")
        if href and ("image" in mime or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href
    return ""


def _extract_youtube_urls_from_soup(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Extrai links/embeds do YouTube encontrados dentro da própria matéria jornalística."""
    import re

    yt_ids: list[str] = []
    patterns = (
        r"(?:youtube\.com/embed/|youtube-nocookie\.com/embed/|youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})",
    )

    def _scan(text: str) -> None:
        for pat in patterns:
            for m in re.findall(pat, text or ""):
                if m not in yt_ids:
                    yt_ids.append(m)

    # Se a própria URL já é YouTube (ex.: RSS do Gigaton Games)
    _scan(page_url)

    for iframe in soup.find_all("iframe"):
        _scan(iframe.get("src") or "")
        _scan(iframe.get("data-src") or "")
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        if "youtu" in href.lower():
            _scan(href)

    return [f"https://www.youtube.com/watch?v={vid}" for vid in yt_ids[:3]]


def scrape_full_article(
    url: str,
    fallback_summary: str = "",
    timeout: int = 18,
    log_cb: LogCallback | None = None,
) -> tuple[str, str, list[str], list[str]]:
    """Faz scraping do texto completo da matéria e extrai og:image, imagens e vídeos YouTube embutidos."""
    og_image = ""
    extra_images: list[str] = []
    embedded_videos: list[str] = []

    if "youtube.com/watch" in (url or "").lower() or "youtu.be/" in (url or "").lower():
        embedded_videos.append(url)
        return fallback_summary, "", [], embedded_videos

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
        final_url = resp.url
    except Exception as exc:
        _log(log_cb, f"[Scraper] Aviso: não foi possível abrir matéria ({exc}). Usando resumo.")
        return fallback_summary, "", [], embedded_videos

    if "news.google.com" in urlparse(final_url).netloc.lower():
        return fallback_summary, "", [], embedded_videos

    soup = BeautifulSoup(html, "html.parser")
    embedded_videos = _extract_youtube_urls_from_soup(soup, final_url)

    for tag_name, attrs in (
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:secure_url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
    ):
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get("content"):
            candidate = urljoin(final_url, tag["content"].strip())
            if candidate.startswith("http"):
                if not og_image:
                    og_image = candidate
                elif candidate not in extra_images:
                    extra_images.append(candidate)

    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=lambda c: c and any(
            k in str(c).lower() for k in ("article", "story", "content", "nota", "body", "post")
        ))
        or soup.body
    )

    if container:
        for img in container.find_all("img"):
            src = (
                img.get("data-src")
                or img.get("data-original")
                or img.get("data-lazy-src")
                or img.get("src")
                or ""
            ).strip()
            if not src or src.startswith("data:"):
                continue
            full_img_url = urljoin(final_url, src)
            lower_url = full_img_url.lower()
            if any(skip in lower_url for skip in ("logo", "icon", "avatar", "sprite", "pixel", ".svg", ".gif")):
                continue
            if full_img_url.startswith("http") and full_img_url != og_image and full_img_url not in extra_images:
                extra_images.append(full_img_url)
            if len(extra_images) >= 6:
                break

    for tag in soup.find_all(
        ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe", "button"]
    ):
        tag.decompose()

    target = soup.find("article") or soup.find("main") or container or soup.body
    paragraphs: list[str] = []
    if target:
        for p in target.find_all("p"):
            text = " ".join(p.get_text(" ", strip=True).split())
            if len(text) < 35:
                continue
            norm = normalize_text(text)
            if any(phrase in norm for phrase in BOILERPLATE_PHRASES):
                continue
            if text not in paragraphs:
                paragraphs.append(text)

    full_text = "\n\n".join(paragraphs[:18]).strip()
    if len(full_text) < 180 and fallback_summary:
        full_text = f"{fallback_summary}\n\n{full_text}".strip()

    if len(full_text) > 4500:
        full_text = full_text[:4500].rsplit(" ", 1)[0] + "..."

    return full_text, og_image, extra_images, embedded_videos


def _score_article(
    title: str,
    summary: str,
    url: str,
    norm_keywords: list[tuple[str, str]],
    foco_norm: str,
    idioma: str,
) -> tuple[list[str], int]:
    """Pontua relevância da notícia descartando guias de TV/apostas."""
    searchable_title = normalize_text(title)
    searchable_body = normalize_text(f"{title} {summary}")

    if any(pat in searchable_title for pat in LOW_VALUE_TITLE_PATTERNS):
        return [], 0

    matched: list[str] = []
    score = 0
    for orig_kw, norm_kw in norm_keywords:
        if not norm_kw:
            continue
        if norm_kw in searchable_body:
            matched.append(orig_kw)
            weight = 4
            if norm_kw in searchable_title:
                weight += 4
            if foco_norm and foco_norm in norm_kw:
                weight += 3
            score += weight

    if matched:
        host = urlparse(url or "").netloc.lower()
        if "news.google.com" not in host:
            score += 4
        if idioma.startswith("es") and any(
            d in host for d in (".mx", "espndeportes", "mediotiempo", "record.com", "tudn", "bolavip", "marca", "clarosports")
        ):
            score += 6
        elif idioma.startswith("pt") and any(
            d in host for d in (".br", "ign.com", "omelete", "tecmundo", "voxel", "theenemy", "meups", "flowgames", "eurogamer.pt", "uol.com")
        ):
            score += 6

    return matched, score


def _fetch_ddgs_news(
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> list[dict[str, str]]:
    """Busca notícias frescas com links diretos via DDGS News adaptado ao nicho e idioma."""
    if DDGS is None:
        return []

    foco = str(config.get("clube_foco") or config.get("nicho_nome") or "").strip()
    sufixo = str(config.get("ddgs_news_sufixo") or "noticias").strip()
    regiao = str(config.get("ddgs_regiao") or "br-pt")

    if "geral" in normalize_text(foco):
        query = sufixo
    else:
        query = f"{foco} {sufixo}".strip()

    _log(log_cb, f"[Scraper] Buscando notícias recentes diretas ({regiao}): '{query}'...")
    try:
        with DDGS() as ddgs:
            results = ddgs.news(query, region=regiao, safesearch="moderate", timelimit="w", max_results=20)
            return results or []
    except Exception as exc:
        _log(log_cb, f"[Scraper] Aviso na busca direta ({exc}).")
        return []


def fetch_news_candidates(
    config: dict[str, Any],
    max_candidates: int = 12,
    exclude_urls: set[str] | None = None,
    log_cb: LogCallback | None = None,
) -> list[NewsArticle]:
    """Coleta notícias recentes para o nicho ativo ignorando itens já presentes no history.json do nicho."""
    feeds = list(config.get("rss_feeds") or [])
    keywords = [k.strip() for k in config.get("palavras_chave", []) if k and k.strip()]
    if not keywords:
        keywords = [str(config.get("nicho_nome", "noticias"))]

    foco_raw = str(config.get("clube_foco") or config.get("nicho_nome") or "")
    foco_norm = normalize_text(foco_raw)
    idioma = str(config.get("idioma", "pt-BR"))

    # Feed dinâmico do foco no Google News no idioma do nicho
    if foco_raw and "geral" not in foco_norm:
        if idioma.startswith("es"):
            hl, gl, ceid = "es-419", "MX", "MX:es-419"
        elif idioma.startswith("en"):
            hl, gl, ceid = "en-US", "US", "US:en"
        else:
            hl, gl, ceid = "pt-BR", "BR", "BR:pt-419"
        q_str = quote_plus(f'"{foco_raw}" when:5d')
        feeds.insert(
            0,
            {
                "nome": f"Google News ({foco_raw})",
                "url": f"https://news.google.com/rss/search?q={q_str}&hl={hl}&gl={gl}&ceid={ceid}",
            },
        )

    paths_dict = config.get("paths") or {}
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    seen_urls, seen_titles = _extract_history_sets(history_path)
    if exclude_urls:
        seen_urls.update(_canonical_url(u) for u in exclude_urls)

    session_urls: set[str] = set()
    session_titles: set[str] = set()
    candidates: list[NewsArticle] = []
    norm_keywords = [(kw, normalize_text(kw)) for kw in keywords]

    # 1) DDGS News direto
    ddgs_items = _fetch_ddgs_news(config, log_cb=log_cb)
    if ddgs_items:
        _log(log_cb, f"[Scraper] Busca direta: {len(ddgs_items)} notícias encontradas.")
    for item in ddgs_items:
        title = str(item.get("title") or "").strip()
        link = str(item.get("url") or "").strip()
        if not title or not link:
            continue

        canon_url = _canonical_url(link)
        norm_title = normalize_text(title)
        if canon_url in seen_urls or norm_title in seen_titles:
            continue
        if canon_url in session_urls or norm_title in session_titles:
            continue

        pub_str = str(item.get("date") or "")
        pub_ts = _parse_date_ts(pub_str)
        if _is_too_old(pub_ts):
            continue

        summary = str(item.get("body") or "").strip()
        matched, score = _score_article(title, summary, link, norm_keywords, foco_norm, idioma)
        if not matched:
            continue

        session_urls.add(canon_url)
        session_titles.add(norm_title)
        candidates.append(
            NewsArticle(
                title=title,
                url=link,
                source=str(item.get("source") or "Portal"),
                published=pub_str,
                published_ts=pub_ts,
                summary=summary,
                og_image=str(item.get("image") or ""),
                matched_keywords=matched,
                score=score,
            )
        )

    # 2) Feeds RSS do nicho
    for feed_idx, feed_info in enumerate(feeds):
        feed_name = feed_info.get("nome", "RSS")
        feed_url = feed_info.get("url", "")
        if not feed_url:
            continue

        if feed_idx > 0 and "news.google.com" in feed_url:
            time.sleep(0.55)

        _log(log_cb, f"[Scraper] Lendo feed {feed_name}...")
        try:
            resp = requests.get(feed_url, headers=DEFAULT_HEADERS, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            _log(log_cb, f"[Scraper] Feed {feed_name} indisponível ({exc}). Pulando.")
            continue

        entries = getattr(parsed, "entries", []) or []
        _log(log_cb, f"[Scraper] {feed_name}: {len(entries)} itens encontrados.")

        for entry in entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            canon_url = _canonical_url(link)
            norm_title = normalize_text(title)
            if canon_url in seen_urls or norm_title in seen_titles:
                continue
            if canon_url in session_urls or norm_title in session_titles:
                continue

            published = (entry.get("published") or entry.get("updated") or "").strip()
            pub_ts = _parse_date_ts(published)
            if _is_too_old(pub_ts):
                continue

            summary = _clean_html_summary(entry.get("summary") or entry.get("description") or "")
            matched, score = _score_article(title, summary, link, norm_keywords, foco_norm, idioma)
            if not matched:
                continue

            session_urls.add(canon_url)
            session_titles.add(norm_title)

            source_obj = entry.get("source")
            entry_source = (
                source_obj.get("title") if isinstance(source_obj, dict) and source_obj.get("title") else feed_name
            )
            candidates.append(
                NewsArticle(
                    title=title,
                    url=link,
                    source=str(entry_source),
                    published=published,
                    published_ts=pub_ts,
                    summary=summary,
                    og_image=_extract_rss_image(entry),
                    matched_keywords=matched,
                    score=score,
                )
            )

    candidates.sort(key=lambda a: (a.score, a.published_ts), reverse=True)
    return candidates[:max_candidates]


def fetch_next_news(
    config: dict[str, Any],
    exclude_urls: set[str] | None = None,
    log_cb: LogCallback | None = None,
) -> NewsArticle:
    """Seleciona a melhor notícia recente do nicho ativo e extrai o texto completo."""
    candidates = fetch_news_candidates(
        config=config,
        max_candidates=12,
        exclude_urls=exclude_urls,
        log_cb=log_cb,
    )

    if not candidates:
        raise RuntimeError(
            "Nenhuma notícia nova encontrada para as palavras-chave configuradas neste nicho. "
            "Tente ampliar as palavras-chave ou alternar para o modo 'Top 5 / Segredos Virais'."
        )

    chosen = candidates[0]
    for cand in candidates[:3]:
        full_text, og_image, extra_images, embedded_videos = scrape_full_article(
            url=cand.url,
            fallback_summary=cand.summary,
            log_cb=log_cb,
        )
        cand.full_text = full_text or cand.summary or cand.title
        if og_image:
            if cand.og_image and cand.og_image != og_image:
                extra_images.insert(0, cand.og_image)
            cand.og_image = og_image
        cand.article_images = extra_images
        cand.embedded_videos = embedded_videos
        chosen = cand
        if len(cand.full_text) >= 280:
            break

    _log(
        log_cb,
        f"[Scraper] Matéria selecionada ({chosen.source}): {chosen.title} "
        f"[tags: {', '.join(chosen.matched_keywords)}]",
    )
    _log(
        log_cb,
        f"[Scraper] Texto extraído: {len(chosen.full_text)} caracteres | "
        f"vídeos YouTube embutidos: {len(chosen.embedded_videos)} | "
        f"og:image: {'sim' if chosen.og_image else 'não'}",
    )
    return chosen
