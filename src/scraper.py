"""Módulo de coleta de notícias RSS + busca de notícias recentes e scraping completo da Liga MX."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.5",
}

DEFAULT_RSS_FEEDS = [
    {
        "nome": "Mediotiempo / Récord / ESPN MX",
        "url": "https://news.google.com/rss/search?q=(site:mediotiempo.com+OR+site:record.com.mx+OR+site:espn.com.mx)+Liga+MX+when:5d&hl=es-419&gl=MX&ceid=MX:es-419",
    },
    {
        "nome": "Google News Liga MX",
        "url": "https://news.google.com/rss/search?q=Liga+MX+futbol+when:4d&hl=es-419&gl=MX&ceid=MX:es-419",
    },
    {
        "nome": "ESPN Deportes MX",
        "url": "https://espndeportes.espn.com/espn/rss/futbol/mexico/news",
    },
]

BOILERPLATE_PHRASES = (
    "cookies",
    "suscríbete",
    "suscribete",
    "inicia sesión",
    "inicia sesion",
    "derechos reservados",
    "política de privacidad",
    "politica de privacidad",
    "te puede interesar",
    "también puedes leer",
    "tambien puedes leer",
    "lee también",
    "lee tambien",
    "descarga la app",
    "síguenos en",
    "siguenos en",
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
)


@dataclass
class NewsArticle:
    """Representa uma notícia coletada e enriquecida com texto completo."""

    title: str
    url: str
    source: str
    published: str = ""
    published_ts: float = 0.0
    summary: str = ""
    full_text: str = ""
    og_image: str = ""
    article_images: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    score: int = 0


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _canonical_url(url: str) -> str:
    """Normaliza URL removendo query/fragment desnecessários para deduplicação."""
    parsed = urlparse((url or "").strip())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()


def _parse_date_ts(raw_date: str) -> float:
    """Converte string RFC2822 ou ISO8601 em timestamp UTC; retorna 0 se inválido."""
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
    age_seconds = datetime.now(timezone.utc).timestamp() - published_ts
    return age_seconds > max_days * 86400


def _extract_history_sets(
    history_path=HISTORY_PATH,
) -> tuple[set[str], set[str]]:
    """Extrai conjuntos de URLs e títulos já processados do history.json."""
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
    """Tenta obter imagem diretamente dos campos de mídia do item RSS."""
    media_content = entry.get("media_content") or []
    for media in media_content:
        url = media.get("url")
        if url and isinstance(url, str) and url.startswith("http"):
            return url

    media_thumb = entry.get("media_thumbnail") or []
    for thumb in media_thumb:
        url = thumb.get("url")
        if url and isinstance(url, str) and url.startswith("http"):
            return url

    enclosures = entry.get("enclosures") or []
    for enc in enclosures:
        href = enc.get("href") or enc.get("url")
        mime = enc.get("type", "")
        if href and ("image" in mime or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href

    return ""


def scrape_full_article(
    url: str,
    fallback_summary: str = "",
    timeout: int = 18,
    log_cb: LogCallback | None = None,
) -> tuple[str, str, list[str]]:
    """
    Faz scraping do texto completo da matéria e extrai og:image + imagens relevantes.

    Retorna: (full_text, og_image, extra_images)
    """
    og_image = ""
    extra_images: list[str] = []

    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
        final_url = resp.url
    except Exception as exc:
        _log(log_cb, f"[Scraper] Aviso: não foi possível abrir matéria ({exc}). Usando resumo.")
        return fallback_summary, "", []

    # Se caiu em página de consentimento/redirect do Google News sem corpo de notícia
    if "news.google.com" in urlparse(final_url).netloc.lower():
        return fallback_summary, "", []

    soup = BeautifulSoup(html, "html.parser")

    # 1) Extrai og:image / twitter:image
    meta_selectors = [
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:secure_url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
    ]
    for tag_name, attrs in meta_selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get("content"):
            candidate = urljoin(final_url, tag["content"].strip())
            if candidate.startswith("http"):
                if not og_image:
                    og_image = candidate
                elif candidate not in extra_images:
                    extra_images.append(candidate)

    # 2) Coleta imagens de dentro do corpo principal antes de limpar tags
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=lambda c: c and any(
            k in str(c).lower() for k in ("article", "story", "content", "nota", "body")
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
            if len(extra_images) >= 5:
                break

    # 3) Remove ruído para extrair texto limpo
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

    return full_text, og_image, extra_images


def _score_article(
    title: str,
    summary: str,
    url: str,
    norm_keywords: list[tuple[str, str]],
    club_focus: str,
) -> tuple[list[str], int]:
    """Calcula relevância da notícia, descartando guias de TV/apostas e priorizando notícias reais em espanhol."""
    generic_terms = {"liga mx", "futbol mexicano", "seleccion mexicana", "liguilla", "clausura", "apertura"}
    searchable_title = normalize_text(title)
    searchable_body = normalize_text(f"{title} {summary}")

    # Descarta guias de transmissão ("cómo ver", "how to watch", "dónde ver", apostas)
    if any(pat in searchable_title for pat in LOW_VALUE_TITLE_PATTERNS):
        return [], 0

    matched: list[str] = []
    score = 0
    for orig_kw, norm_kw in norm_keywords:
        if not norm_kw:
            continue
        if norm_kw in searchable_body:
            matched.append(orig_kw)
            is_generic = norm_kw in generic_terms
            weight = 1 if is_generic else 5
            if norm_kw in searchable_title:
                weight += 4
            if club_focus and club_focus in norm_kw:
                weight += 3
            score += weight

    if matched:
        # Bônus para portais esportivos em espanhol e URLs diretas
        host = urlparse(url or "").netloc.lower()
        if any(dom in host for dom in (".mx", "espndeportes", "mediotiempo", "record.com", "tudn", "bolavip", "marca", "as.com", "clarosports", "esto.com")):
            score += 6
        if "news.google.com" not in host:
            score += 4

    return matched, score


def _fetch_ddgs_news(
    club_focus: str,
    keywords: list[str],
    log_cb: LogCallback | None = None,
) -> list[dict[str, str]]:
    """Busca notícias frescas da última semana com links diretos via DDGS."""
    if DDGS is None:
        return []

    query = (
        f"{club_focus} Liga MX noticias"
        if club_focus and "geral" not in normalize_text(club_focus)
        else "Liga MX futbol mexicano noticias"
    )
    _log(log_cb, f"[Scraper] Buscando notícias recentes diretas: '{query}'...")
    try:
        with DDGS() as ddgs:
            results = ddgs.news(query, region="mx-es", safesearch="moderate", timelimit="w", max_results=20)
            return results or []
    except Exception as exc:
        _log(log_cb, f"[Scraper] Aviso na busca direta de notícias ({exc}).")
        return []


def fetch_news_candidates(
    config: dict[str, Any],
    max_candidates: int = 12,
    exclude_urls: set[str] | None = None,
    log_cb: LogCallback | None = None,
) -> list[NewsArticle]:
    """
    Coleta notícias frescas (DDGS News direto + RSS mexicanos), filtra por palavras-chave,
    descarta matérias antigas (>14 dias) ou presentes em history.json e ordena por relevância.
    """
    feeds = list(config.get("rss_feeds") or DEFAULT_RSS_FEEDS)
    keywords = [k.strip() for k in config.get("palavras_chave", []) if k and k.strip()]
    if not keywords:
        keywords = ["Club América", "Águilas", "Liga MX"]

    club_raw = config.get("clube_foco", "Club América")
    club_focus = normalize_text(club_raw)

    # Adiciona feed dinâmico do clube foco no Google News MX (últimos 4 dias)
    if club_raw and "geral" not in club_focus:
        club_query = quote_plus(f'"{club_raw}" Liga MX when:4d')
        feeds.insert(
            0,
            {
                "nome": f"Google News ({club_raw})",
                "url": f"https://news.google.com/rss/search?q={club_query}&hl=es-419&gl=MX&ceid=MX:es-419",
            },
        )

    seen_urls, seen_titles = _extract_history_sets()
    if exclude_urls:
        seen_urls.update(_canonical_url(u) for u in exclude_urls)

    session_urls: set[str] = set()
    session_titles: set[str] = set()
    candidates: list[NewsArticle] = []
    norm_keywords = [(kw, normalize_text(kw)) for kw in keywords]

    # 1) Busca direta de notícias recentes (links diretos -> scraping completo garantido)
    ddgs_items = _fetch_ddgs_news(club_raw, keywords, log_cb=log_cb)
    if ddgs_items:
        _log(log_cb, f"[Scraper] Busca direta: {len(ddgs_items)} notícias recentes encontradas.")
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
        matched, score = _score_article(title, summary, link, norm_keywords, club_focus)
        if not matched:
            continue

        session_urls.add(canon_url)
        session_titles.add(norm_title)
        candidates.append(
            NewsArticle(
                title=title,
                url=link,
                source=str(item.get("source") or "Portal MX"),
                published=pub_str,
                published_ts=pub_ts,
                summary=summary,
                og_image=str(item.get("image") or ""),
                matched_keywords=matched,
                score=score,
            )
        )

    # 2) Feeds RSS configurados (Mediotiempo/Récord/ESPN via Google News MX + RSS diretos)
    for feed_idx, feed_info in enumerate(feeds):
        feed_name = feed_info.get("nome", "RSS")
        feed_url = feed_info.get("url", "")
        if not feed_url:
            continue

        if feed_idx > 0 and "news.google.com" in feed_url:
            time.sleep(0.6)

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
            matched, score = _score_article(title, summary, link, norm_keywords, club_focus)
            if not matched:
                continue

            session_urls.add(canon_url)
            session_titles.add(norm_title)

            rss_img = _extract_rss_image(entry)
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
                    og_image=rss_img,
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
    """
    Busca a melhor notícia recente ainda não processada e faz o scraping do texto completo.
    """
    candidates = fetch_news_candidates(
        config=config,
        max_candidates=12,
        exclude_urls=exclude_urls,
        log_cb=log_cb,
    )

    if not candidates:
        raise RuntimeError(
            "Nenhuma notícia nova encontrada para as palavras-chave configuradas. "
            "Tente ampliar as palavras-chave ou selecionar 'Liga MX Geral'."
        )

    # Tenta os top 3 candidatos caso o primeiro seja bloqueado por paywall/redirect vazio
    chosen = candidates[0]
    for cand in candidates[:3]:
        full_text, og_image, extra_images = scrape_full_article(
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
        f"og:image: {'sim' if chosen.og_image else 'não'} | "
        f"imagens extras: {len(chosen.article_images)}",
    )
    return chosen
