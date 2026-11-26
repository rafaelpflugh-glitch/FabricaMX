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
from src.video_fetcher import _find_ytdlp, download_video_hd, probe_video_info

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

        for entry in (parsed.entries or [])[:12]:
            link = str(entry.get("link") or "").strip()
            title = str(entry.get("title") or "").strip()
            if "/shorts/" in link.lower():
                continue  # Queremos vídeos horizontais com conteúdo rico para recortar
            vid_id = _extract_yt_id(link)
            if not vid_id or vid_id in seen_ids:
                continue

            norm_t = normalize_text(title)
            score = 1
            for kw in keywords:
                if kw and kw in norm_t:
                    score += 4
            if foco_norm and foco_norm in norm_t:
                score += 4
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

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        # Pega o melhor candidato que tenha duração adequada (>= 90s)
        for _, cand in candidates[:6]:
            _, _, dur = _probe_yt_metadata(cand.url)
            if dur >= 85 or dur == 0:
                cand.duration = dur
                _log(log_cb, f"[YouTube] Vídeo selecionado ({cand.channel}): '{cand.title}' ({dur}s)")
                return cand

    # Fallback ytsearch se nenhum canal RSS retornou vídeo elegível
    search_q = str(
        config.get("yt_busca_corte") if mode == "corte_original" else config.get("yt_busca_adaptador")
    ).strip() or str(config.get("nicho_nome", "GTA 6"))
    _log(log_cb, f"[YouTube] Buscando vídeo via yt-dlp: '{search_q}'...")
    ytdlp = _find_ytdlp()
    out = subprocess.check_output(
        [ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", f"ytsearch8:{search_q}"],
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    entries = json.loads(out).get("entries") or []
    for e in entries:
        vid_id = str(e.get("id") or "")
        dur = int(e.get("duration") or 0)
        if not vid_id or vid_id in seen_ids or dur < 90 or dur > 5400:
            continue
        cand = YouTubeCandidate(
            video_id=vid_id,
            url=f"https://www.youtube.com/watch?v={vid_id}",
            title=str(e.get("title") or "YouTube Video"),
            channel=str(e.get("channel") or "YouTube"),
            duration=dur,
        )
        _log(log_cb, f"[YouTube] Vídeo encontrado ({cand.channel}): '{cand.title}' ({dur}s)")
        return cand

    raise RuntimeError("Nenhum vídeo novo do YouTube encontrado para este nicho.")


def _parse_json3_subtitles(json3_path: Path) -> list[TranscriptCue]:
    """Faz parse do formato json3 do YouTube preservando timestamps exatos por palavra!"""
    data = json.loads(json3_path.read_text(encoding="utf-8", errors="replace"))
    cues: list[TranscriptCue] = []

    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        base_s = float(ev["tStartMs"]) / 1000.0
        dur_s = float(ev.get("dDurationMs") or 2000.0) / 1000.0
        end_s = base_s + max(0.3, dur_s)

        full_parts: list[str] = []
        raw_words: list[tuple[str, float]] = []
        for seg in segs:
            txt = str(seg.get("utf8") or "").replace("\n", " ").strip()
            if not txt:
                continue
            full_parts.append(txt)
            off_s = float(seg.get("tOffsetMs") or 0.0) / 1000.0
            for tok in txt.split():
                clean_tok = tok.replace(">>", "").strip()
                if clean_tok and clean_tok not in (">", ">>", ">>>", "-"):
                    raw_words.append((clean_tok, base_s + off_s))

        joined = " ".join(full_parts).strip()
        if not joined or joined in ("[Music]", "[Música]", "[Applause]", "[Aplausos]"):
            continue

        words: list[WordTiming] = []
        for idx, (tok, w_start) in enumerate(raw_words):
            if idx + 1 < len(raw_words):
                w_end = max(w_start + 0.08, raw_words[idx + 1][1])
            else:
                w_end = max(w_start + 0.15, end_s)
            words.append(WordTiming(text=tok, start=w_start, end=w_end))

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
        "bestvideo[height<=1080][height>=720][ext=mp4]+bestaudio[ext=m4a]/"
        "best[height<=1080][ext=mp4]/best[height<=1080]"
    )

    _log(log_cb, f"[YouTube] Baixando recorte HD com áudio original ({start_sec:.0f}s -> {end_sec:.0f}s)...")
    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
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
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
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
    # Para o Adaptador (gameranx, IGN, Premier Guitar, Rick Beato), prioriza faixa original (en -> pt -> es) para evitar 429 de tradução
    pref_langs = ("en", "pt", "es") if str(config.get("nicho_ativo", "")) in ("musica_gear", "gta_vi") else (
        ("pt", "en", "es") if idioma.startswith("pt") else ("es", "en", "pt")
    )

    tried_urls = set(exclude_urls or set())
    video: YouTubeCandidate | None = None
    cues: list[TranscriptCue] = []
    last_err: Exception | None = None

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
            cues = fetch_transcript_cues(video.url, work_dir, preferred_langs=pref_langs, log_cb=log_cb)
            if cues:
                break
        except Exception as exc:
            last_err = exc
            if custom_url:
                raise
            _log(log_cb, f"[AdaptadorYT] Tentando próximo vídeo com transcrição ({attempt}/3)...")

    if video is None or not cues:
        raise RuntimeError(f"Não foi possível obter vídeo com transcrição: {last_err}")
    max_cue_t = cues[-1].end if cues else float(video.duration or 300)
    digest = _compress_transcript_digest(cues, block_seconds=12)

    prompt = f"""{lp['persona']}
Você recebeu a TRANSCRIÇÃO COM TIMESTAMPS de um vídeo real do YouTube ({video.channel}: "{video.title}").
Escolha o assunto/segredo/detalhe MAIS VIRAL deste vídeo e transforme em um roteiro de 62 a 72 segundos com SINCRONIA VISUAL PERFEITA usando os timestamps exatos de onde cada coisa aparece no vídeo!

TRANSCRIÇÃO COM TIMESTAMPS (em segundos):
{digest}

REGRAS ESTRITAS (OBRIGATÓRIAS):
1. Idioma do roteiro, gancho e descrição: {lp['lang_name']}.
2. Responda APENAS com um JSON válido com estas 6 chaves:
   - "roteiro": string (OBRIGATÓRIO 175 a 200 palavras, frases curtas de impacto, sem saudações, com loop aberto no início, CTA no meio "{lp['cta']}" e pergunta provocativa no final)
   - "gancho_tela": string (3 a 5 palavras EM CAIXA ALTA)
   - "segmentos_video": array de 14 a 18 números inteiros representando os SEGUNDOS EXATOS (timestamps do vídeo acima) em ordem cronológica que mostram exatamente o que cada parte do seu roteiro está explicando! (ex.: [18, 24, 31, 45, 52, ...], todos entre 5 e {max(20, int(max_cue_t - 8))})
   - "descricao_tiktok": string (1 frase chamativa SEM hashtags)
   - "hashtags": array de 6 a 8 hashtags começando com #
   - "resumo_pauta": string (1 frase resumindo o trecho escolhido)

Retorne APENAS o JSON:"""

    _log(log_cb, f"[AdaptadorYT] Gerando roteiro sincronizado + timestamps exatos...")
    raw = _call_ollama(ollama_url, model, prompt, timeout=timeout, config=config)
    data = _parse_json_response(raw)

    roteiro = _clean_script_text(str(data.get("roteiro") or ""))
    wc = _count_words(roteiro)
    if wc < 155:
        _log(log_cb, f"[AdaptadorYT] Expandindo roteiro ({wc} -> alvo 175-198 palavras)...")
        exp_prompt = (
            f"O campo 'roteiro' tem apenas {wc} palavras. Expanda para 178-198 palavras em {lp['lang_name']} "
            f"usando mais detalhes reais da transcrição acima, mantendo frases curtas, SEM repetir o pedido de seguir mais de uma vez.\n"
            f"JSON ATUAL:\n{json.dumps(data, ensure_ascii=False)}\nResponda APENAS com o JSON corrigido:"
        )
        try:
            adj_raw = _call_ollama(ollama_url, model, exp_prompt, timeout=timeout, config=config)
            adj_data = _parse_json_response(adj_raw)
            adj_rot = _clean_script_text(str(adj_data.get("roteiro") or ""))
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

    # Baixa apenas o intervalo necessário do vídeo original em 1080p HD (muito mais rápido!)
    min_t = max(0.0, min(timestamps) - 2.0)
    max_t = min(max_cue_t, max(timestamps) + 8.0)
    if (max_t - min_t) > 240.0:
        # Limita span máximo de download para 4 minutos mantendo o foco principal
        max_t = min_t + 240.0
        timestamps = [t for t in timestamps if t <= max_t - 5.0] or [min_t + i * 5.0 for i in range(16)]

    src_video = work_dir / "adapted_source.mp4"
    sec_str = f"*{min_t:.1f}-{max_t:.1f}" if (video.duration > 260 or min_t > 10.0) else None
    dl = download_video_hd(video.url, src_video, section=sec_str, log_cb=log_cb)
    if not dl:
        dl = download_video_hd(video.url, src_video, section=None, log_cb=log_cb)
        min_t = 0.0
    if not dl:
        raise RuntimeError(f"Não foi possível baixar o vídeo base em HD: {video.url}")

    actual_dur, vw, vh = probe_video_info(dl)
    matched_scenes: list[tuple[Path, float, int, int]] = []
    for ts in timestamps[:18]:
        rel_t = max(0.2, ts - min_t)
        if rel_t > max(1.0, actual_dur - 4.5):
            rel_t = max(0.2, (rel_t % max(4.0, actual_dur - 5.0)))
        matched_scenes.append((dl, round(rel_t, 2), vw, vh))

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
        f"[AdaptadorYT] Pronto! {wc} palavras + {len(matched_scenes)} timestamps exatos sincronizados de '{video.channel}'!",
    )
    return AdaptedPlan(
        video=video,
        article=article,
        script=script,
        matched_scenes=matched_scenes,
        source_video_path=dl,
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
    shifted_words: list[WordTiming] = []
    excerpt_parts: list[str] = []
    for cue in cues:
        if cue.end < start_sec - 0.2 or cue.start > end_sec + 0.2:
            continue
        excerpt_parts.append(cue.text)
        for wt in cue.words:
            if wt.end < start_sec or wt.start > end_sec:
                continue
            w_s = max(0.0, wt.start - start_sec)
            w_e = min(actual_dur, max(w_s + 0.08, wt.end - start_sec))
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
