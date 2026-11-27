"""Módulo de Transcrição YouTube + Adaptador Sincronizado (IGN/Gameranx/Rig) + Cortador Viral (Streamers/TV)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import feedparser
import requests

from src import HISTORY_PATH, load_history, normalize_text
from src.scraper import NewsArticle
from src.scriptwriter import ScriptPackage, _call_ollama, _clean_script_text, _count_words, _language_pack, _normalize_hashtags, _parse_json_response
from src.tts_subtitles import WordTiming
from src.video_fetcher import (
    SPAM_VIDEO_TITLE_TOKENS,
    TRUSTED_VIDEO_CHANNELS,
    UNTRUSTED_VIDEO_CHANNELS,
    _find_ytdlp,
    download_video_hd,
    probe_video_info,
    run_subprocess_tree,
    ytdlp_base_args,
)

LogCallback = Callable[[str], None]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


@dataclass
class YouTubeCandidate:
    """Vídeo candidato encontrado via RSS de canal ou yt-dlp search."""

    video_id: str
    url: str
    title: str
    channel: str
    duration: int = 0


@dataclass
class TranscriptCue:
    """Trecho legendado do YouTube com marcação temporal."""

    start: float
    end: float
    text: str
    words: list[WordTiming] = field(default_factory=list)


@dataclass
class AdaptedPlan:
    """Plano do Modo Adaptador YouTube: roteiro reescrito + timestamps exatos do vídeo original."""

    video: YouTubeCandidate
    article: NewsArticle
    script: ScriptPackage
    matched_scenes: list[tuple[Path, float, int, int]]
    source_video_path: Path
    source_video_paths: list[Path] = field(default_factory=list)


@dataclass
class OriginalCutPlan:
    """Plano do Modo Cortador Viral (Áudio Original + Legenda): clipe contínuo de 62-74s."""

    video: YouTubeCandidate
    article: NewsArticle
    clip_path: Path
    clip_duration: float
    start_sec: float
    end_sec: float
    gancho_tela: str
    descricao_tiktok: str
    hashtags: list[str]
    word_timings: list[WordTiming]
    transcript_excerpt: str


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _extract_yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def _seen_video_ids(history_path: Path, exclude_urls: set[str] | None = None) -> set[str]:
    seen: set[str] = set()
    for item in load_history(history_path):
        url = item.get("url", "") if isinstance(item, dict) else str(item)
        vid = _extract_yt_id(url)
        if vid:
            seen.add(vid)
    for u in exclude_urls or set():
        vid = _extract_yt_id(u)
        if vid:
            seen.add(vid)
    return seen


def _probe_yt_metadata(video_url: str) -> tuple[str, str, int]:
    ytdlp = _find_ytdlp()
    try:
        out = subprocess.check_output(
            [
                ytdlp,
                "--no-playlist",
                "--no-warnings",
                "--print",
                "%(title)s|||%(channel)s|||%(duration)s",
                video_url,
            ],
            text=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        parts = out.splitlines()[0].split("|||")
        title = parts[0].strip() if len(parts) > 0 else "YouTube Video"
        channel = parts[1].strip() if len(parts) > 1 else "YouTube"
        dur = int(float(parts[2])) if len(parts) > 2 and parts[2].replace(".", "").isdigit() else 0
        return title, channel, dur
    except Exception:
        return "YouTube Video", "YouTube", 0


def discover_youtube_candidate(
    config: dict[str, Any],
    mode: str,
    exclude_urls: set[str] | None = None,
    custom_url: str = "",
    log_cb: LogCallback | None = None,
) -> YouTubeCandidate:
    """
    Descobre automaticamente um vídeo recente no YouTube para o nicho ativo:
    - Se `custom_url` foi informado -> usa ele diretamente.
    - Lê os feeds RSS dos canais configurados (`canais_adaptador` ou `canais_corte`) ignorando `history.json`.
    - Fallback via `yt-dlp ytsearch` caso os feeds não tenham correspondência para as palavras-chave.
    """
    if custom_url and custom_url.strip().startswith("http"):
        url = custom_url.strip()
        vid_id = _extract_yt_id(url) or "custom"
        title, channel, dur = _probe_yt_metadata(url)
        _log(log_cb, f"[YouTube] Usando link informado: '{title}' ({channel}, {dur}s)")
        return YouTubeCandidate(video_id=vid_id, url=url, title=title, channel=channel, duration=dur)

    paths_dict = config.get("paths") or {}
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    seen_ids = _seen_video_ids(history_path, exclude_urls)

    channels = (
        config.get("canais_corte")
        if mode == "corte_original"
        else config.get("canais_adaptador")
    ) or []

    keywords = [normalize_text(k) for k in (config.get("palavras_chave") or []) if k and k.strip()]
    foco_norm = normalize_text(str(config.get("clube_foco") or ""))
    candidates: list[tuple[int, YouTubeCandidate]] = []

    is_hl_pro = mode == "highlights_pro"
    hl_bad_tokens = (
        "jogo completo", "partido completo", "full match", "ao vivo", "en vivo", "live",
        "debate", "programa", "pos-jogo", "pos jogo", "pre-jogo", "pre jogo", "coletiva",
        "entrevista", "podcast", "react", "reaccion", "copa davis", "tenis", "basquete",
        "nba", "volei", "ufc", "boxe", "formula 1", "femenil", "sub-20", "sub 20",
    )
    hl_good_tokens = ("resumen", "highlights", "goles", "melhores momentos", "gols", "resumo", "compacto")

    for ch in channels:
        ch_name = str(ch.get("nome", "YouTube"))
        ch_id = str(ch.get("channel_id", "")).strip()
        if not ch_id:
            continue
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch_id}"
        _log(log_cb, f"[YouTube] Checando lançamentos de {ch_name}...")
        try:
            resp = requests.get(rss_url, headers=DEFAULT_HEADERS, timeout=12)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            _log(log_cb, f"[YouTube] Feed de {ch_name} indisponível ({exc}).")
            continue

        for entry in (parsed.entries or [])[:15]:
            link = str(entry.get("link") or "").strip()
            title = str(entry.get("title") or "").strip()
            if "/shorts/" in link.lower():
                continue  # Queremos vídeos horizontais com conteúdo rico para recortar
            vid_id = _extract_yt_id(link)
            if not vid_id or vid_id in seen_ids:
                continue

            norm_t = normalize_text(title)
            if is_hl_pro:
                if any(bad in norm_t for bad in hl_bad_tokens):
                    continue
                has_score = bool(re.search(r"\b\d\s*[x-]\s*\d\b", norm_t))
                has_hl_word = any(g in norm_t for g in hl_good_tokens)
                if not (has_hl_word or has_score):
                    continue

            score = 1
            if is_hl_pro:
                if any(g in norm_t for g in hl_good_tokens):
                    score += 8
                if re.search(r"\b\d\s*[x-]\s*\d\b", norm_t):
                    score += 6
            for kw in keywords:
                if kw and kw in norm_t:
                    score += 4
            if foco_norm and foco_norm in norm_t:
                score += 6
            candidates.append(
                (
                    score,
                    YouTubeCandidate(
                        video_id=vid_id,
                        url=f"https://www.youtube.com/watch?v={vid_id}",
                        title=title,
                        channel=ch_name,
                    ),
                )
            )

    min_dur_allowed = 140 if is_hl_pro else 75
    max_dur_allowed = 1200 if is_hl_pro else 3600
    niche_id = str(config.get("nicho_ativo", ""))
    require_keyword_match = (niche_id == "recap_comedia") and bool(keywords)

    # Em recap_comedia, se o foco pede um protagonista específico (ex.: Cirilo!), EXIGE o protagonista no título
    # para nunca pegar um vídeo só da Margarida/Valéria quando o roteiro é do Cirilo!
    required_protagonists: tuple[str, ...] = ()
    if niche_id == "recap_comedia":
        if "cirilo" in foco_norm:
            required_protagonists = ("cirilo",)
        elif "chaves" in foco_norm or "madruga" in foco_norm:
            required_protagonists = ("chaves", "madruga", "quico", "chris")
        elif "chiquititas" in foco_norm:
            required_protagonists = ("chiquititas", "mili", "mosca")

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        for score, cand in candidates[:12]:
            norm_cand_t = normalize_text(cand.title)
            if required_protagonists and not any(p in norm_cand_t for p in required_protagonists):
                continue
            if require_keyword_match and score < 5:
                continue
            _, _, dur = _probe_yt_metadata(cand.url)
            if min_dur_allowed <= dur <= max_dur_allowed:
                cand.duration = dur
                _log(log_cb, f"[YouTube] Vídeo selecionado ({cand.channel}): '{cand.title}' ({dur}s)")
                return cand
            elif dur > max_dur_allowed:
                _log(log_cb, f"[YouTube] Ignorando '{cand.title[:48]}...' (muito longo: {dur}s > {max_dur_allowed}s)")

    # Se exigimos protagonista específico (ex.: Cirilo!) e o RSS recente (15 vídeos) não trouxe,
    # usa a busca INTERNA do próprio canal oficial (/search?query=Cirilo) -> 100% acervo oficial limpo, zero canal pirata!
    if required_protagonists and channels:
        ytdlp = _find_ytdlp()
        q_term = required_protagonists[0]
        for ch in channels[:2]:
            ch_id = str(ch.get("channel_id", "")).strip()
            ch_name = str(ch.get("nome", "YouTube"))
            if not ch_id:
                continue
            try:
                _log(log_cb, f"[YouTube] Buscando cenas com '{q_term}' no acervo oficial de {ch_name}...")
                raw_ch = subprocess.check_output(
                    [
                        ytdlp,
                        "--flat-playlist",
                        "--playlist-end",
                        "45",
                        "--dump-single-json",
                        "--no-warnings",
                        f"https://www.youtube.com/channel/{ch_id}/search?query={q_term}",
                    ],
                    text=True,
                    timeout=25,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                ch_entries = json.loads(raw_ch).get("entries") or []
                for e in ch_entries:
                    vid_id = str(e.get("id") or "")
                    dur = int(e.get("duration") or 0)
                    title = str(e.get("title") or "")
                    norm_t = normalize_text(title)
                    if not vid_id or vid_id in seen_ids or dur < min_dur_allowed or dur > max_dur_allowed:
                        continue
                    if not any(p in norm_t for p in required_protagonists):
                        continue
                    if any(b in norm_t for b in ("musica", "clipe:", "clipe oficial", "canta ", "cantam ", "abertura", "trilha sonora")):
                        continue
                    cand = YouTubeCandidate(
                        video_id=vid_id,
                        url=f"https://www.youtube.com/watch?v={vid_id}",
                        title=title,
                        channel=ch_name,
                        duration=dur,
                    )
                    _log(log_cb, f"[YouTube] Cena oficial selecionada ({ch_name}): '{title}' ({dur}s)")
                    return cand
            except Exception:
                continue

    # Fallback ytsearch focado (em highlights_pro, busca resumos/gols de 3-18 min; em recap_comedia, busca cenas do foco escolhido!)
    if is_hl_pro:
        idioma = str(config.get("idioma", "pt-BR"))
        foco_clean = str(config.get("clube_foco") or "").strip()
        if "geral" in foco_norm or not foco_clean:
            search_q = str(config.get("yt_busca_adaptador") or "").strip() or (
                "Liga MX resumen y goles TUDN" if idioma.startswith("es") else "melhores momentos gols Brasileirao Libertadores"
            )
        else:
            search_q = (
                f"{foco_clean} resumen y goles Liga MX TUDN"
                if idioma.startswith("es")
                else f"{foco_clean} melhores momentos gols"
            )
    elif niche_id == "recap_comedia":
        if "cirilo" in foco_norm:
            search_q = "Carrossel Cirilo Maria Joaquina cena SBT Carrossel 2022"
        else:
            kw_top = [str(k).strip() for k in (config.get("palavras_chave") or []) if str(k).strip()][:3]
            search_q = (" ".join(kw_top) + " cena oficial SBT TV ZYN") if kw_top else str(
                config.get("yt_busca_adaptador") or "Carrossel Cirilo Maria Joaquina cena oficial SBT TV ZYN"
            ).strip()
    else:
        search_q = str(
            config.get("yt_busca_corte") if mode == "corte_original" else config.get("yt_busca_adaptador")
        ).strip() or str(config.get("nicho_nome", "GTA 6"))

    _log(log_cb, f"[YouTube] Buscando vídeo via yt-dlp: '{search_q}'...")
    ytdlp = _find_ytdlp()
    out = subprocess.check_output(
        [ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", f"ytsearch18:{search_q}"],
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    entries = json.loads(out).get("entries") or []
    recap_official_exact = {
        "carrossel", "tv zyn", "sbt", "sbt novelas", "sbt video", "sbt videos",
        "chiquititas", "turma do chaves oficial", "chaves", "comedy central",
        "paramount", "netflix", "warner", "universal", "dreamworks",
    }
    recap_pirate_channels = (
        "novelas tv", "teen", "fa clube", "fan club", "edits", "cortes", "react",
        "clube da", "cantinho", "novelaromance",
    )
    recap_bad_titles = (
        "musica", "clipe:", "clipe oficial", "meu clipe", "canta ", "cantam ", "abertura",
        "trilha sonora", "ao vivo", "live", "final de ano", "coming soon", "chamada",
    )
    scored_search: list[tuple[int, YouTubeCandidate]] = []
    for pos, e in enumerate(entries):
        vid_id = str(e.get("id") or "")
        dur = int(e.get("duration") or 0)
        title = str(e.get("title") or "YouTube Video")
        ch_raw = str(e.get("channel") or "YouTube")
        norm_t = normalize_text(title)
        norm_ch = normalize_text(ch_raw)
        if not vid_id or vid_id in seen_ids or dur < min_dur_allowed or dur > max_dur_allowed:
            continue
        if any(p in norm_ch for p in UNTRUSTED_VIDEO_CHANNELS):
            continue
        if any(sp in norm_t for sp in SPAM_VIDEO_TITLE_TOKENS):
            continue
        if is_hl_pro and any(bad in norm_t for bad in hl_bad_tokens):
            continue
        if niche_id == "recap_comedia":
            # Bloqueia canais piratas que colocam molduras/figurinhas estáticas por cima da novela!
            if any(p in norm_ch for p in recap_pirate_channels):
                continue
            if any(b in norm_t for b in recap_bad_titles):
                continue
            if required_protagonists and not any(p in norm_t for p in required_protagonists):
                continue
        s_score = max(1, 8 - (pos // 2))
        for kw in keywords:
            if kw and kw in norm_t:
                s_score += 5
        if any(tc in norm_ch for tc in TRUSTED_VIDEO_CHANNELS):
            s_score += 20  # Prioridade global para canais oficiais e emissoras limpas!
        if niche_id == "recap_comedia":
            if norm_ch in recap_official_exact:
                s_score += 25
            # Prefere cenas limpas de 1.5–12 min em canais oficiais
            if 90 <= dur <= 720:
                s_score += 8
        scored_search.append(
            (
                s_score,
                YouTubeCandidate(
                    video_id=vid_id,
                    url=f"https://www.youtube.com/watch?v={vid_id}",
                    title=title,
                    channel=ch_raw,
                    duration=dur,
                ),
            )
        )
    if scored_search:
        scored_search.sort(key=lambda x: x[0], reverse=True)
        cand = scored_search[0][1]
        _log(log_cb, f"[YouTube] Vídeo encontrado ({cand.channel}): '{cand.title}' ({cand.duration}s)")
        return cand

    raise RuntimeError("Nenhum vídeo novo do YouTube encontrado para este nicho.")


def _parse_json3_subtitles(json3_path: Path) -> list[TranscriptCue]:
    """Faz parse do formato json3 do YouTube preservando timestamps exatos por palavra (sem overlaps nem ruídos)."""
    data = json.loads(json3_path.read_text(encoding="utf-8", errors="replace"))
    raw_events = [ev for ev in (data.get("events") or []) if ev.get("segs") and "tStartMs" in ev]
    raw_events.sort(key=lambda ev: float(ev["tStartMs"]))

    cues: list[TranscriptCue] = []

    for ev_idx, ev in enumerate(raw_events):
        segs = ev.get("segs") or []
        base_s = float(ev["tStartMs"]) / 1000.0
        dur_s = float(ev.get("dDurationMs") or 2000.0) / 1000.0
        # YouTube envia dDurationMs de 5-8s (janela rolante) que sobrepõe o próximo evento!
        # Travamos o fim de cada cue no início do próximo evento para sincronia exata sem empilhamento.
        next_start_s = (
            float(raw_events[ev_idx + 1]["tStartMs"]) / 1000.0
            if ev_idx + 1 < len(raw_events)
            else (base_s + min(dur_s, 4.0))
        )
        end_s = min(base_s + max(0.3, min(dur_s, 4.5)), max(base_s + 0.25, next_start_s))

        full_parts: list[str] = []
        seg_entries: list[tuple[list[str], float]] = []
        for seg in segs:
            txt = str(seg.get("utf8") or "").replace("\n", " ").strip()
            txt = re.sub(r"\[[^\]]*\]", "", txt)
            txt = txt.replace(">>", "").replace("♪", "").replace("♫", "").strip()
            if not txt:
                continue
            off_s = float(seg.get("tOffsetMs") or 0.0) / 1000.0
            toks = [
                t.strip()
                for t in txt.split()
                if t.strip() and t.strip() not in (">", ">>", ">>>", "-", "—")
            ]
            if not toks:
                continue
            full_parts.append(" ".join(toks))
            seg_entries.append((toks, base_s + off_s))

        joined = " ".join(full_parts).strip()
        if not joined or not seg_entries:
            continue

        # Expande cada seg em palavras individuais com tempo proporcional quando um seg traz várias palavras
        words: list[WordTiming] = []
        for s_idx, (toks, s_start) in enumerate(seg_entries):
            s_end = seg_entries[s_idx + 1][1] if s_idx + 1 < len(seg_entries) else end_s
            s_end = max(s_start + 0.12 * len(toks), min(s_end, end_s + 0.15))
            if len(toks) == 1:
                words.append(WordTiming(text=toks[0], start=s_start, end=s_end))
            else:
                span = max(0.12 * len(toks), s_end - s_start)
                weights = [max(2, len(t)) for t in toks]
                wsum = sum(weights)
                cursor = s_start
                for tok, w in zip(toks, weights):
                    piece = span * (w / wsum)
                    words.append(WordTiming(text=tok, start=cursor, end=cursor + piece))
                    cursor += piece

        if words:
            cues.append(TranscriptCue(start=base_s, end=end_s, text=joined, words=words))

    return cues


def _parse_vtt_time(ts: str) -> float:
    parts = ts.strip().replace(",", ".").split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return 0.0


def _parse_vtt_subtitles(vtt_path: Path) -> list[TranscriptCue]:
    """Fallback de parser para legendas .vtt."""
    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\r?\n\r?\n", raw)
    cues: list[TranscriptCue] = []
    time_re = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})")

    for blk in blocks:
        m = time_re.search(blk)
        if not m:
            continue
        start_s = _parse_vtt_time(m.group(1))
        end_s = _parse_vtt_time(m.group(2))
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip() and "-->" not in ln and not ln.startswith("WEBVTT")]
        text = re.sub(r"<[^>]+>", "", " ".join(lines)).replace(">>", "").strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        if cues and cues[-1].text == text:
            cues[-1].end = max(cues[-1].end, end_s)
            continue

        tokens = text.split()
        seg_dur = max(0.2, end_s - start_s) / max(1, len(tokens))
        words = [
            WordTiming(text=tok, start=start_s + i * seg_dur, end=start_s + (i + 1) * seg_dur)
            for i, tok in enumerate(tokens)
        ]
        cues.append(TranscriptCue(start=start_s, end=end_s, text=text, words=words))

    return cues


def fetch_transcript_cues(
    video_url: str,
    work_dir: Path,
    preferred_langs: tuple[str, ...] = ("pt", "en", "es"),
    log_cb: LogCallback | None = None,
) -> list[TranscriptCue]:
    """
    Baixa a transcrição com timestamps do YouTube via yt-dlp (json3 ou vtt).
    Pede UM idioma por vez para evitar HTTP 429 do YouTube ao baixar múltiplas traduções.
    """
    ytdlp = _find_ytdlp()
    sub_dir = work_dir / "yt_subs"
    sub_dir.mkdir(parents=True, exist_ok=True)
    for old in sub_dir.glob("*"):
        old.unlink(missing_ok=True)

    # Deduplica preservando ordem e prioriza faixa original (sem wildcards que causam 429)
    ordered_langs: list[str] = []
    for lang in preferred_langs:
        base = lang.split("-")[0].lower()
        if base not in ordered_langs:
            ordered_langs.append(base)
    for fallback in ("en", "pt", "es"):
        if fallback not in ordered_langs:
            ordered_langs.append(fallback)

    _log(log_cb, "[YouTube] Baixando transcrição com timestamps exatos...")
    for lang in ordered_langs:
        for sub_fmt in ("json3", "vtt"):
            cmd = [
                ytdlp,
                "--skip-download",
                "--no-playlist",
                "--no-warnings",
                "--quiet",
                "--write-sub",
                "--write-auto-sub",
                "--sub-langs",
                lang,
                "--sub-format",
                sub_fmt,
                "-o",
                str(sub_dir / "sub"),
                video_url,
            ]
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                continue

            files = sorted(sub_dir.glob(f"*.{sub_fmt}"))
            if files:
                chosen = files[0]
                cues = _parse_json3_subtitles(chosen) if sub_fmt == "json3" else _parse_vtt_subtitles(chosen)
                if cues:
                    _log(log_cb, f"[YouTube] Transcrição carregada: {len(cues)} blocos temporais ({chosen.name}).")
                    return cues

    raise RuntimeError("Não foi possível obter a transcrição/legendas deste vídeo no YouTube.")


def _compress_transcript_digest(cues: list[TranscriptCue], block_seconds: int = 12, max_chars: int = 11000) -> str:
    """Agrupa a transcrição em blocos cronológicos [MM:SS - MM:SS] (em segundos) para o LLM ler tudo."""
    if not cues:
        return ""
    buckets: dict[int, list[str]] = {}
    for c in cues:
        b = int(c.start // block_seconds)
        buckets.setdefault(b, []).append(c.text)

    lines: list[str] = []
    total_len = 0
    for b in sorted(buckets.keys()):
        t0 = b * block_seconds
        t1 = t0 + block_seconds
        txt = " ".join(buckets[b]).strip()
        line = f"[{t0}s-{t1}s] {txt}"
        if total_len + len(line) > max_chars:
            break
        lines.append(line)
        total_len += len(line) + 1
    return "\n".join(lines)


def _download_clip_with_audio(
    video_url: str,
    start_sec: float,
    end_sec: float,
    output_path: Path,
    log_cb: LogCallback | None = None,
) -> Path:
    """Baixa EXATAMENTE o trecho [start_sec..end_sec] em 1080p HD COM ÁUDIO ORIGINAL."""
    ytdlp = _find_ytdlp()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    section = f"*{start_sec:.2f}-{end_sec:.2f}"
    fmt = (
        "bestvideo[height<=1080][height>=720][fps<=30][protocol^=m3u8][vcodec^=avc1]+bestaudio[protocol^=m3u8]/"
        "bestvideo[height<=1080][height>=720][protocol^=m3u8][vcodec^=avc1]+bestaudio[protocol^=m3u8]/"
        "bestvideo[height<=1080][height>=720][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
        "bestvideo[height<=1080][height>=720][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=1080][ext=mp4]/best[height<=1080]"
    )

    _log(log_cb, f"[YouTube] Baixando recorte HD com áudio original ({start_sec:.0f}s -> {end_sec:.0f}s)...")
    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        *ytdlp_base_args(),
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "--download-sections",
        section,
        "--force-keyframes-at-cuts",
        "-o",
        str(output_path),
        video_url,
    ]
    try:
        proc = run_subprocess_tree(cmd, timeout=150)
    except subprocess.TimeoutExpired:
        for stale in (output_path, output_path.with_name(output_path.name + ".part")):
            stale.unlink(missing_ok=True)
        raise RuntimeError("Timeout (150s) ao baixar trecho com áudio original via yt-dlp.")
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 100_000:
        raise RuntimeError(f"Falha ao baixar trecho com áudio original via yt-dlp: {proc.stderr[-300:]}")
    return output_path


def run_youtube_adapter_stage(
    config: dict[str, Any],
    work_dir: Path,
    exclude_urls: set[str] | None = None,
    custom_url: str = "",
    log_cb: LogCallback | None = None,
) -> AdaptedPlan:
    """
    MODO ADAPTADOR YOUTUBE (IGN / Gameranx / Gigaton / Premier Guitar / Rick Beato):
    1. Encontra vídeo novo do canal -> baixa transcrição com timestamps.
    2. qwen3:8b escreve roteiro viral de 62-72s no idioma escolhido + escolhe os timestamps exatos
       do próprio vídeo que mostram cada detalhe narrado!
    3. Baixa o vídeo em 1080p HD e retorna `matched_scenes` 100% sincronizadas!
    """
    ollama_url = str(config.get("ollama_url", "http://localhost:11434"))
    model = str(config.get("ollama_modelo", "qwen3:8b"))
    timeout = int(config.get("ollama_timeout", 180))
    lp = _language_pack(config)
    idioma = str(config.get("idioma", "pt-BR"))
    # Para canais gringos (gameranx, IGN, The Dodo, Daily Dose, Veritasium, Mark Rober), prioriza faixa original em inglês
    pref_langs = ("en", "pt", "es") if str(config.get("nicho_ativo", "")) in ("musica_gear", "gta_vi", "virais_mundo") else (
        ("pt", "en", "es") if idioma.startswith("pt") else ("es", "en", "pt")
    )

    tried_urls = set(exclude_urls or set())
    video: YouTubeCandidate | None = None
    cues: list[TranscriptCue] = []
    last_err: Exception | None = None
    pre_downloaded_video: Path | None = None
    vision_digest = ""

    for attempt in range(1, 4):
        try:
            video = discover_youtube_candidate(
                config=config,
                mode="adaptador_yt",
                exclude_urls=tried_urls,
                custom_url=custom_url if attempt == 1 else "",
                log_cb=log_cb,
            )
            tried_urls.add(video.url)
            try:
                cues = fetch_transcript_cues(video.url, work_dir, preferred_langs=pref_langs, log_cb=log_cb)
                if cues:
                    break
            except Exception as sub_exc:
                last_err = sub_exc
                if custom_url or attempt == 3:
                    _log(
                        log_cb,
                        "[AdaptadorYT] Vídeo sem legendas no YouTube — baixando HD e lendo as cenas via IA Visão!",
                    )
                    sec_cap = "*0-340" if (video.duration and video.duration > 360) else None
                    dl_v = download_video_hd(video.url, work_dir / "adapted_part_00.mp4", section=sec_cap, log_cb=log_cb)
                    if not dl_v and sec_cap:
                        dl_v = download_video_hd(video.url, work_dir / "adapted_part_00.mp4", section=None, log_cb=log_cb)
                    if not dl_v:
                        raise RuntimeError(f"Não foi possível baixar o vídeo HD sem legendas: {video.url}") from sub_exc
                    pre_downloaded_video = dl_v
                    v_dur, _, _ = probe_video_info(dl_v)
                    from src.scene_matcher import index_video_pool

                    try:
                        indexed = index_video_pool(
                            contextual_videos=[dl_v],
                            library_videos=[],
                            scene_dur=4.0,
                            work_dir=work_dir,
                            config=config,
                            log_cb=log_cb,
                        )
                        v_lines = [f"[{int(sc.start)}s] {sc.description}" for sc in sorted(indexed, key=lambda s: s.start)]
                    except Exception:
                        v_lines = []
                    if not v_lines:
                        step_v = max(12, int(max(30.0, v_dur) // 16))
                        v_lines = [f"[{t}s] Cena de '{video.title}'" for t in range(4, max(20, int(v_dur) - 6), step_v)]
                    vision_digest = "CENAS VISUAIS CRONOLÓGICAS DO VÍDEO (ANÁLISE POR IA VISÃO):\n" + "\n".join(v_lines)
                    video.duration = int(v_dur)
                    break
                _log(log_cb, f"[AdaptadorYT] Sem legendas; tentando próximo vídeo ({attempt}/3)...")
        except Exception as exc:
            last_err = exc
            if custom_url:
                raise
            _log(log_cb, f"[AdaptadorYT] Tentando próximo vídeo com transcrição ({attempt}/3)...")

    if video is None or (not cues and not vision_digest):
        raise RuntimeError(f"Não foi possível obter vídeo com transcrição ou análise visual: {last_err}")
    if cues:
        max_cue_t = cues[-1].end
        digest = _compress_transcript_digest(cues, block_seconds=12)
    else:
        max_cue_t = float(video.duration or 300)
        digest = vision_digest

    niche_id = str(config.get("nicho_ativo", ""))
    if niche_id == "recap_comedia":
        style_block = (
            "ESTILO OBRIGATÓRIO — PARÓDIA / COMÉDIA DE NOVELA (ESTILO @jvrecap — 100% COERENTE COM QUEM ESTÁ NA TELA!):\n"
            "- **REGRA DE OURO DE COERÊNCIA VISUAL (INVIOLÁVEL!)**: Use APENAS os personagens que REALMENTE aparecem no título "
            f'("{video.title}") e na timeline/transcrição abaixo!\n'
            "- **PROIBIDO INVENTAR PERSONAGENS AUSENTES**: Se o Cirilo NÃO estiver na cena/título, JAMAIS fale 'o Cirilo fez isso'! "
            "Da mesma forma, se a Maria Joaquina não aparecer, não invente ela na cena! A graça SÓ funciona quando a piada bate 100% com o rosto e a ação que o espectador está vendo na tela!\n"
            "- Quando o **Cirilo** estiver na cena (especialmente com a Maria Joaquina): faça a paródia clássica do **'Cirilo Safadão / Cafajeste'** — o Cirilo tentando conquistar/pegar a Maria Joaquina na cara de pau, fazendo joguinho de ciúmes, tomando fora lendário ou dando a volta por cima!\n"
            "- Quando a cena for de outros personagens (Valéria, Paulo, Davi, Margarida, Professora Helena, Chaves, Chiquititas): faça a mesma paródia cômica debochada em cima dos **PERSONAGENS REAIS DA CENA** (ex.: Valéria surtando de ciúmes, Paulo aprontando, Maria Joaquina perdendo a pose)!\n"
            "- Tom de BRINCADEIRA e PARÓDIA leve (humor inteligente e hilário que o TikTok ama, sem palavrão pesado), contando uma historinha completa com INÍCIO, MEIO, CLÍMAX e RESOLUÇÃO real!\n"
            "- PROIBIDO falar números de segundos ou timestamps dentro do 'roteiro' (JAMAIS escreva 'aos 12 segundos', 'aos 24', 'aos 48', 'aos 60' — use os segundos APENAS no campo 'segmentos_video'!).\n"
        )
    else:
        style_block = (
            "Escolha o assunto/segredo/detalhe MAIS VIRAL deste vídeo e transforme em um roteiro completo de 62 a 72 segundos "
            "com ARCO NARRATIVO REDONDO (INÍCIO → MEIO → CLÍMAX → RESOLUÇÃO) e SINCRONIA VISUAL PERFEITA usando os timestamps exatos!\n"
        )

    prompt = f"""{lp['persona']}
Você recebeu a TRANSCRIÇÃO/CENAS COM TIMESTAMPS de um vídeo real do YouTube ({video.channel}: "{video.title}").
{style_block}
TIMELINE DO VÍDEO (em segundos — use os segundos APENAS no array 'segmentos_video', NUNCA fale os números no roteiro!):
{digest}

REGRAS ESTRITAS DE ESTRUTURA NARRATIVA (OBRIGATÓRIAS):
1. Idioma do roteiro, gancho e descrição: {lp['lang_name']}.
2. TODO ROTEIRO PRECISA TER INÍCIO, MEIO, CLÍMAX E RESOLUÇÃO REAL (JAMAIS acabar do nada ou deixar a história no ar!):
   - **INÍCIO (~20% do roteiro):** Apresenta quem está na cena + qual é o plano, desafio, segredo ou conflito que dá início à história (gancho que prende na hora!).
   - **MEIO (~35% do roteiro):** Mostra o plano acontecendo, o conflito escalando, as reações dos envolvidos e a reviravolta! Coloque o CTA "{lp['cta']}" EXATAMENTE aqui na metade do roteiro (1 única vez — NUNCA no início e NUNCA no final!).
   - **CLÍMAX E RESOLUÇÃO OBRIGATÓRIA (~35% do roteiro):** ENTREGA O DESFECHO COMPLETO do que foi prometido no início! O plano deu certo ou deu errado? Como a cena terminou de verdade? Qual foi a consequência final / reação definitiva dos personagens? Feche a história de forma redonda e satisfatória!
   - **FECHAMENTO (~10% do roteiro):** 1 frase curta de conclusão memorável ("moral da história" / punchline final) + 1 pergunta rápida diretamente ligada ao desfecho!
3. Responda APENAS com um JSON válido com estas 6 chaves:
   - "roteiro": string (OBRIGATÓRIO 200 a 225 palavras para garantir 63 a 72 segundos de áudio a +12% de velocidade, seguindo rigorosamente INÍCIO → MEIO + CTA → CLÍMAX & RESOLUÇÃO → FECHAMENTO! Sem saudações, sem timestamps falados, sem terminar no ar!)
   - "gancho_tela": string (3 a 5 palavras EM CAIXA ALTA)
   - "segmentos_video": array de 14 a 18 números inteiros representando os SEGUNDOS EXATOS (timestamps do vídeo acima) em ordem cronológica cobrindo o INÍCIO, o MEIO e o FINAL/RESOLUÇÃO da história (todos entre 5 e {max(20, int(max_cue_t - 8))})
   - "descricao_tiktok": string (1 frase chamativa SEM hashtags)
   - "hashtags": array de 6 a 8 hashtags começando com #
   - "resumo_pauta": string (1 frase resumindo a história completa do início ao fim)

Retorne APENAS o JSON:"""

    _log(log_cb, f"[AdaptadorYT] Gerando roteiro sincronizado + timestamps exatos...")
    raw = _call_ollama(ollama_url, model, prompt, timeout=timeout, config=config)
    data = _parse_json_response(raw)

    def _strip_spoken_timestamps(txt: str) -> str:
        # Remove qualquer vazamento de timestamp da transcrição no meio da narração ("aos 48 segundos", "aos 60,", etc.)
        cleaned_t = re.sub(
            r"(?:,\s*)?\b(?:aos|nos?|em|no\s+segundo|aos\s+exatos?)\s+\d{1,3}\s*(?:segundos?|seg\.?|s\b)(?:\s*,)?",
            "",
            txt,
            flags=re.IGNORECASE,
        )
        cleaned_t = re.sub(r"(?:,\s*)?\baos\s+\d{1,3}(?=[,\s.])", "", cleaned_t, flags=re.IGNORECASE)
        return re.sub(r"\s{2,}", " ", cleaned_t).replace(" ,", ",").strip()

    roteiro = _strip_spoken_timestamps(_clean_script_text(str(data.get("roteiro") or "")))
    wc = _count_words(roteiro)
    if wc < 185:
        _log(log_cb, f"[AdaptadorYT] Expandindo roteiro ({wc} -> alvo 195-218 palavras para passar de 60s)...")
        exp_prompt = (
            f"O campo 'roteiro' tem apenas {wc} palavras (daria menos de 60 segundos). Expanda para 198-218 palavras em {lp['lang_name']} "
            f"enriquecendo a história com mais reações reais e garantindo um ARCO COMPLETO (INÍCIO → MEIO com CTA único na metade → CLÍMAX E RESOLUÇÃO REAL → FECHAMENTO redondo, SEM acabar do nada e SEM falar números de segundos/timestamps!).\n"
            f"JSON ATUAL:\n{json.dumps(data, ensure_ascii=False)}\nResponda APENAS com o JSON corrigido:"
        )
        try:
            adj_raw = _call_ollama(ollama_url, model, exp_prompt, timeout=timeout, config=config)
            adj_data = _parse_json_response(adj_raw)
            adj_rot = _strip_spoken_timestamps(_clean_script_text(str(adj_data.get("roteiro") or "")))
            if _count_words(adj_rot) > wc:
                data = adj_data
                roteiro = adj_rot
                wc = _count_words(roteiro)
        except Exception:
            pass

    raw_segs = data.get("segmentos_video") or []
    timestamps: list[float] = []
    for item in raw_segs:
        if isinstance(item, (int, float)):
            t_val = float(item)
        elif isinstance(item, dict) and "start" in item:
            t_val = float(item["start"])
        else:
            continue
        if 2.0 <= t_val <= max(10.0, max_cue_t - 4.0):
            timestamps.append(round(t_val, 2))

    if len(timestamps) < 10:
        span_end = min(max_cue_t - 6.0, 240.0)
        step = max(4.0, (span_end - 8.0) / 16.0)
        timestamps = [round(8.0 + i * step, 2) for i in range(16)]

    # Agrupa os timestamps em até 3 janelas compactas (máx ~95s cada) para NUNCA jogar fora metade da história
    # (ex.: baixa TANTO a janela do leão esmagando o coco QUANTO a janela do crocodilo mordendo o sensor!).
    timestamps = sorted(set(timestamps))
    windows: list[list[float]] = []
    for t_val in timestamps:
        if not windows or (t_val - windows[-1][0]) > 85.0:
            windows.append([t_val])
        else:
            windows[-1].append(t_val)

    # Se vieram mais de 3 janelas distantes, mantém as 3 mais ricas em ordem cronológica
    if len(windows) > 3:
        top3 = sorted(windows, key=lambda w: len(w), reverse=True)[:3]
        windows = sorted(top3, key=lambda w: w[0])

    downloaded_parts: list[tuple[Path, float, float, float, int, int]] = []  # (path, win_start, lead_in, actual_dur, w, h)
    matched_scenes: list[tuple[Path, float, int, int]] = []

    if pre_downloaded_video is not None and pre_downloaded_video.exists():
        actual_dur, vw, vh = probe_video_info(pre_downloaded_video)
        downloaded_parts.append((pre_downloaded_video, 0.0, 0.0, actual_dur, vw, vh))
        for ts in timestamps:
            rel_t = max(0.2, min(max(1.0, actual_dur - 4.2), ts))
            matched_scenes.append((pre_downloaded_video, round(rel_t, 2), vw, vh))
    else:
        for w_idx, win_ts in enumerate(windows):
            # Adiciona margem extra (+10s antes e +16s depois) porque a ação visual às vezes acontece segundos após a fala!
            win_start = max(0.0, min(win_ts) - 8.0)
            win_end = min(max_cue_t, max(win_ts) + 16.0)
            if (win_end - win_start) > 125.0:
                win_end = win_start + 125.0
            part_path = work_dir / f"adapted_part_{w_idx:02d}.mp4"
            sec_str = f"*{win_start:.1f}-{win_end:.1f}" if (video.duration > 180 or win_start > 10.0) else None
            dl = download_video_hd(video.url, part_path, section=sec_str, log_cb=log_cb)
            if not dl and sec_str is not None and len(windows) == 1:
                _log(log_cb, "[AdaptadorYT] Recorte falhou; tentando baixar vídeo completo em HD...")
                dl = download_video_hd(video.url, part_path, section=None, log_cb=log_cb)
                win_start = 0.0
            if not dl:
                continue

            actual_dur, vw, vh = probe_video_info(dl)
            requested_span = max(1.0, win_end - win_start)
            lead_in = 0.0
            if sec_str is not None and win_start > 0.0 and actual_dur > requested_span + 0.6:
                lead_in = min(6.0, actual_dur - requested_span)

            downloaded_parts.append((dl, win_start, lead_in, actual_dur, vw, vh))
            for ts in win_ts:
                rel_t = max(0.2, (ts - win_start) + lead_in)
                if rel_t > max(1.0, actual_dur - 4.2):
                    rel_t = max(0.2, min(actual_dur - 4.2, rel_t))
                matched_scenes.append((dl, round(rel_t, 2), vw, vh))

    if not downloaded_parts:
        raise RuntimeError(f"Não foi possível baixar nenhum trecho HD do vídeo: {video.url}")

    # Garante pelo menos 14 cenas cobrindo todas as janelas baixadas
    if len(matched_scenes) < 14:
        extra_scenes: list[tuple[Path, float, int, int]] = list(matched_scenes)
        for dl, _, _, actual_dur, vw, vh in downloaded_parts:
            step = max(3.5, (actual_dur - 6.0) / 6.0)
            t_cur = 2.0
            while t_cur < actual_dur - 4.2 and len(extra_scenes) < 16:
                extra_scenes.append((dl, round(t_cur, 2), vw, vh))
                t_cur += step
        matched_scenes = extra_scenes[:18]
    else:
        matched_scenes = matched_scenes[:18]

    source_paths = [p[0] for p in downloaded_parts]
    dl = source_paths[0]

    gancho = str(data.get("gancho_tela") or video.title[:40]).strip().upper()[:50]
    descricao = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", str(data.get("descricao_tiktok") or video.title)).strip()
    hashtags = _normalize_hashtags(data.get("hashtags"), config)

    article = NewsArticle(
        title=video.title,
        url=video.url,
        source=f"Adaptador YT • {video.channel}",
        summary=str(data.get("resumo_pauta") or video.title),
        full_text=str(data.get("resumo_pauta") or video.title),
    )
    script = ScriptPackage(
        roteiro=roteiro,
        gancho_tela=gancho,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_count=wc,
    )
    _log(
        log_cb,
        f"[AdaptadorYT] Pronto! {wc} palavras + {len(source_paths)} janela(s) HD baixadas de '{video.channel}'!",
    )
    return AdaptedPlan(
        video=video,
        article=article,
        script=script,
        matched_scenes=matched_scenes,
        source_video_path=dl,
        source_video_paths=source_paths,
    )


def run_original_cut_stage(
    config: dict[str, Any],
    work_dir: Path,
    exclude_urls: set[str] | None = None,
    custom_url: str = "",
    log_cb: LogCallback | None = None,
) -> OriginalCutPlan:
    """
    MODO CORTADOR VIRAL (ÁUDIO ORIGINAL + LEGENDA — Alanzoka, MasterChef, Pesadelo na Cozinha):
    1. Descobre vídeo recente (ou usa link colado) e baixa transcrição com timestamps.
    2. qwen3:8b identifica o trecho contínuo de 62-74s MAIS EXPLOSIVO / ENGRAÇADO / POLÊMICO!
    3. Baixa SOMENTE esse trecho em 1080p HD COM ÁUDIO ORIGINAL e extrai legendas palavra por palavra!
    """
    ollama_url = str(config.get("ollama_url", "http://localhost:11434"))
    model = str(config.get("ollama_modelo", "qwen3:8b"))
    timeout = int(config.get("ollama_timeout", 180))
    lp = _language_pack(config)
    idioma = str(config.get("idioma", "pt-BR"))
    pref_langs = ("pt", "en", "es") if idioma.startswith("pt") else ("en", "es", "pt")

    tried_urls = set(exclude_urls or set())
    video: YouTubeCandidate | None = None
    cues: list[TranscriptCue] = []
    last_err: Exception | None = None

    for attempt in range(1, 4):
        try:
            video = discover_youtube_candidate(
                config=config,
                mode="corte_original",
                exclude_urls=tried_urls,
                custom_url=custom_url if attempt == 1 else "",
                log_cb=log_cb,
            )
            tried_urls.add(video.url)
            cues = fetch_transcript_cues(video.url, work_dir, preferred_langs=pref_langs, log_cb=log_cb)
            if cues:
                break
        except Exception as exc:
            last_err = exc
            if custom_url:
                raise
            _log(log_cb, f"[CortadorViral] Tentando próximo vídeo com transcrição ({attempt}/3)...")

    if video is None or not cues:
        raise RuntimeError(f"Não foi possível obter vídeo com transcrição: {last_err}")
    max_cue_t = cues[-1].end if cues else float(video.duration or 300)
    digest = _compress_transcript_digest(cues, block_seconds=12, max_chars=12000)

    prompt = f"""Você é um editor viral profissional de cortes de TikTok e YouTube Shorts (estilo cortes de Alanzoka, MasterChef Brasil e Pesadelo na Cozinha).
Analise a transcrição com timestamps abaixo do vídeo "{video.title}" ({video.channel}) e escolha o ÚNICO TRECHO CONTÍNUO de 62 a 74 segundos mais explosivo, engraçado, tenso, polêmico ou surpreendente!

TRANSCRIÇÃO COM TIMESTAMPS (em segundos):
{digest}

REGRAS ESTRITAS:
1. Responda APENAS com um JSON válido com estas 5 chaves:
   - "start_sec": número inteiro (segundo de início do corte, mínimo 10, máximo {max(15, int(max_cue_t - 65))})
   - "end_sec": número inteiro (segundo de fim do corte; DEVE satisfazer end_sec - start_sec entre 62 e 74 segundos!)
   - "gancho_tela": string em {lp['lang_name']} (4 a 7 palavras EM CAIXA ALTA que façam a pessoa parar o scroll na hora, ex.: "JACQUIN PERDE A PACIÊNCIA NA COZINHA!" ou "LUCIA CAI DO FAROL E SOBREVIVE!")
   - "descricao_tiktok": string curta e provocativa em {lp['lang_name']} (SEM hashtags)
   - "hashtags": array de 6 a 8 hashtags começando com #
2. O corte deve começar num gancho forte de fala/ação e terminar num fechamento/pico marcante (não corte no meio de uma frase se possível).

Retorne APENAS o JSON:"""

    _log(log_cb, f"[CortadorViral] Analisando transcrição para achar o melhor corte de 62–74s...")
    raw = _call_ollama(ollama_url, model, prompt, timeout=timeout, config=config)
    data = _parse_json_response(raw)

    start_sec = float(data.get("start_sec") or 20.0)
    end_sec = float(data.get("end_sec") or (start_sec + 66.0))

    # Valida e ajusta limites de duração (alvo 62–74s para monetizar no TikTok!)
    start_sec = max(0.0, min(start_sec, max(0.0, max_cue_t - 64.0)))
    clip_len = end_sec - start_sec
    if clip_len < 61.0:
        end_sec = min(max_cue_t, start_sec + 66.0)
    elif clip_len > 76.0:
        end_sec = start_sec + 70.0
    end_sec = min(max_cue_t, end_sec)

    _log(log_cb, f"[CortadorViral] Melhor trecho viral encontrado: {start_sec:.0f}s até {end_sec:.0f}s ({end_sec - start_sec:.0f}s)!")

    clip_path = _download_clip_with_audio(
        video_url=video.url,
        start_sec=start_sec,
        end_sec=end_sec,
        output_path=work_dir / "original_cut.mp4",
        log_cb=log_cb,
    )
    actual_dur, _, _ = probe_video_info(clip_path)
    if actual_dur <= 1.0:
        actual_dur = max(10.0, end_sec - start_sec)

    # Recorta e desloca os timestamps das palavras do YouTube para [0.0 .. actual_dur]
    requested_cut_len = max(1.0, end_sec - start_sec)
    cut_lead_in = 0.0
    if actual_dur > requested_cut_len + 0.5 and (actual_dur - requested_cut_len) <= 4.5:
        cut_lead_in = actual_dur - requested_cut_len

    shifted_words: list[WordTiming] = []
    excerpt_parts: list[str] = []
    for cue in cues:
        if cue.end < start_sec - 0.15 or cue.start > end_sec + 0.15:
            continue
        excerpt_parts.append(cue.text)
        for wt in cue.words:
            if wt.end < start_sec or wt.start > end_sec:
                continue
            w_s = max(0.0, (wt.start - start_sec) + cut_lead_in)
            w_e = min(actual_dur, max(w_s + 0.06, (wt.end - start_sec) + cut_lead_in))
            shifted_words.append(WordTiming(text=wt.text, start=w_s, end=w_e))

    gancho = str(data.get("gancho_tela") or video.title[:44]).strip().upper()[:54]
    descricao = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", str(data.get("descricao_tiktok") or video.title)).strip()
    hashtags = _normalize_hashtags(data.get("hashtags"), config)

    article = NewsArticle(
        title=f"{video.title} ({int(start_sec)}s-{int(end_sec)}s)",
        url=video.url,
        source=f"Corte Original • {video.channel}",
        summary=" ".join(excerpt_parts)[:400],
        full_text=" ".join(excerpt_parts),
    )

    return OriginalCutPlan(
        video=video,
        article=article,
        clip_path=clip_path,
        clip_duration=actual_dur,
        start_sec=start_sec,
        end_sec=end_sec,
        gancho_tela=gancho,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_timings=shifted_words,
        transcript_excerpt=" ".join(excerpt_parts),
    )
