"""Módulo de busca e download automático de B-Roll em vídeo 1080p HD via yt-dlp."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from src import BASE_DIR, normalize_text

LogCallback = Callable[[str], None]

# Trailers oficiais 100% GTA VI (Rockstar Games) para montar a biblioteca base de B-Roll uma única vez
GTA6_OFFICIAL_VIDEOS = [
    ("gta6_trailer_2_rockstar.mp4", "https://www.youtube.com/watch?v=VQRLujxTm3c", None),
    ("gta6_trailer_1_rockstar.mp4", "https://www.youtube.com/watch?v=QdBZY2fkU-0", None),
]


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _find_ytdlp() -> str:
    venv_bin = BASE_DIR / ".venv" / "Scripts" / "yt-dlp.exe"
    if venv_bin.exists():
        return str(venv_bin)
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise RuntimeError("yt-dlp não encontrado. Execute '.venv\\Scripts\\pip install yt-dlp'.")


def probe_video_info(video_path: Path) -> tuple[float, int, int]:
    """Retorna (duracao_segundos, largura, altura) via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(proc.stdout)
        dur = float(data.get("format", {}).get("duration") or 0.0)
        streams = data.get("streams") or [{}]
        w = int(streams[0].get("width") or 1920)
        h = int(streams[0].get("height") or 1080)
        return dur, w, h
    except Exception:
        return 0.0, 1920, 1080


def download_video_hd(
    url_or_query: str,
    output_path: Path,
    section: str | None = None,
    timeout: int = 180,
    log_cb: LogCallback | None = None,
) -> Path | None:
    """
    Baixa vídeo em 1080p/720p HD (sem precisar baixar áudio, muito mais rápido!) via yt-dlp.
    Suporta `section` (ex.: '*0:20-2:40') para baixar apenas um trecho limpo de vídeos longos.
    """
    ytdlp = _find_ytdlp()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.stat().st_size > 200_000:
        return output_path

    # Vídeo puro H.264/MP4 até 1080p (super rápido e compatível com FFmpeg sem re-mux pesado)
    fmt = (
        "bestvideo[height<=1080][height>=720][vcodec^=avc1][ext=mp4]/"
        "bestvideo[height<=1080][height>=720][ext=mp4]/"
        "best[height<=1080][height>=720][ext=mp4]/"
        "bestvideo[height<=1080][ext=mp4]/"
        "best[height<=1080]"
    )

    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_path),
    ]
    if section:
        cmd.extend(["--download-sections", section, "--force-keyframes-at-cuts"])
    cmd.append(url_or_query)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 150_000:
            dur, w, h = probe_video_info(output_path)
            if dur >= 8.0 and h >= 480:
                _log(log_cb, f"[VideoFetcher] Clipe HD pronto: {output_path.name} ({w}x{h}, {dur:.0f}s)")
                return output_path
    except Exception as exc:
        _log(log_cb, f"[VideoFetcher] Aviso no download ({exc}).")

    # Remove arquivo parcial se falhou
    if output_path.exists() and output_path.stat().st_size < 150_000:
        output_path.unlink(missing_ok=True)
    return None


def _search_best_youtube_video(
    search_query: str,
    min_dur: int = 60,
    max_dur: int = 1500,
    must_contain_any: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
) -> tuple[str, str, int] | None:
    """Busca o melhor vídeo HD no YouTube via yt-dlp --flat-playlist sem baixar ainda."""
    ytdlp = _find_ytdlp()
    cmd = [
        ytdlp,
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        f"ytsearch6:{search_query}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        entries = data.get("entries") or []
    except Exception:
        return None

    for entry in entries:
        vid_id = entry.get("id")
        title = str(entry.get("title") or "")
        dur = int(entry.get("duration") or 0)
        if not vid_id or dur < min_dur or dur > max_dur:
            continue

        norm_title = normalize_text(title)
        if exclude_terms and any(bad in norm_title for bad in exclude_terms):
            continue
        if must_contain_any and not any(ok in norm_title for ok in must_contain_any):
            continue

        return f"https://www.youtube.com/watch?v={vid_id}", title, dur

    return None


def ensure_broll_videos(
    config: dict[str, Any],
    article_title: str,
    work_dir: Path,
    custom_video_url: str = "",
    log_cb: LogCallback | None = None,
) -> list[Path]:
    """
    Garante automaticamente clipes de vídeo HD reais para o vídeo atual (ZERO busca manual!):
    - Se o usuário colou um link YouTube customizado -> baixa e usa 100% dele.
    - Nicho GTA VI -> baixa 1 única vez os Trailers Oficiais 1 e 2 da Rockstar + 1 gameplay PS5
      em nichos/gta_vi/assets/broll/ e reutiliza em todos os vídeos (zero GTA V!).
    - Nicho Futebol Mexicano -> busca automaticamente um vídeo HD de gols/resumen do clube ou
      partida atual no YouTube + combina com clipes de nichos/futebol_mexicano/assets/broll/.
    """
    niche_id = str(config.get("nicho_ativo", "gta_vi"))
    paths_dict = config.get("paths") or {}
    broll_dir = Path(paths_dict["broll_dir"]) if paths_dict.get("broll_dir") else (work_dir / "broll")
    broll_dir.mkdir(parents=True, exist_ok=True)
    video_exts = (".mp4", ".mov", ".mkv", ".webm")

    # 1) Link YouTube colado opcionalmente pelo usuário -> prioridade total
    if custom_video_url and custom_video_url.strip().startswith("http"):
        _log(log_cb, f"[VideoFetcher] Baixando vídeo HD do link informado...")
        custom_out = work_dir / "custom_source.mp4"
        dl = download_video_hd(custom_video_url.strip(), custom_out, section="*0:05-3:30", log_cb=log_cb)
        if dl:
            return [dl]
        dl_full = download_video_hd(custom_video_url.strip(), custom_out, section=None, log_cb=log_cb)
        if dl_full:
            return [dl_full]

    existing_broll = sorted(
        [p for p in broll_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts and p.stat().st_size > 200_000]
    )

    # 2) Nicho GTA VI: monta automaticamente a biblioteca permanente 1080p oficial (só baixa 1 vez!)
    if niche_id == "gta_vi":
        if len(existing_broll) < 2:
            _log(log_cb, "[VideoFetcher] Preparando biblioteca 1080p oficial de GTA VI (apenas na 1ª vez)...")
            for fname, url, sec in GTA6_OFFICIAL_VIDEOS:
                target = broll_dir / fname
                if not target.exists():
                    _log(log_cb, f"[VideoFetcher] Baixando clipe oficial Rockstar: {fname}...")
                    download_video_hd(url, target, section=sec, log_cb=log_cb)

            # Tenta baixar também 1 trecho real de gameplay PS5 de GTA 6 (sem GTA 5!)
            gp_target = broll_dir / "gta6_gameplay_hd.mp4"
            if not gp_target.exists():
                found_gp = _search_best_youtube_video(
                    "GTA 6 gameplay PS5 4K no commentary",
                    min_dur=120,
                    max_dur=3600,
                    must_contain_any=("gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6"),
                    exclude_terms=("gta 5", "gta v", "mod", "fan made", "concept", "unreal engine", "reaction"),
                )
                if found_gp:
                    gp_url, gp_title, _ = found_gp
                    _log(log_cb, f"[VideoFetcher] Baixando trecho de gameplay real GTA 6: '{gp_title[:55]}'...")
                    download_video_hd(gp_url, gp_target, section="*1:00-3:15", log_cb=log_cb)

        existing_broll = sorted(
            [p for p in broll_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts and p.stat().st_size > 200_000]
        )
        if existing_broll:
            _log(log_cb, f"[VideoFetcher] {len(existing_broll)} vídeo(s) 1080p oficiais de GTA VI prontos para corte!")
        return existing_broll

    # 3) Nicho Futebol Mexicano: busca automaticamente melhores momentos / gols reais em HD!
    if niche_id == "futebol_mexicano":
        foco = str(config.get("clube_foco") or "Club América")
        # Tenta buscar um resumo específico da matéria ou do clube foco
        clean_title_words = [w for w in article_title.split() if len(w) > 3][:5]
        search_queries = [
            f"{foco} goles resumen highlights Liga MX",
            f"{' '.join(clean_title_words)} resumen goles Liga MX",
        ]
        auto_clip: Path | None = None
        for sq in search_queries:
            found = _search_best_youtube_video(
                sq,
                min_dur=90,
                max_dur=1800,
                must_contain_any=("gol", "resumen", "highlight", "america", "chivas", "tigres", "cruz azul", "liga mx"),
                exclude_terms=("fifa", "ea fc", "fc 25", "fc 26", "efootball", "pes", "en vivo", "live"),
            )
            if found:
                v_url, v_title, v_dur = found
                _log(log_cb, f"[VideoFetcher] Lances HD encontrados: '{v_title[:60]}' ({v_dur}s)")
                sec = "*0:18-2:55" if v_dur > 190 else None
                auto_clip = download_video_hd(v_url, work_dir / "liga_mx_highlights.mp4", section=sec, log_cb=log_cb)
                if auto_clip:
                    break

        clips = ([auto_clip] if auto_clip else []) + existing_broll
        return clips

    return existing_broll


def build_video_scene_slices(
    video_files: list[Path],
    num_scenes: int,
    scene_dur: float,
) -> list[tuple[Path, float, int, int]]:
    """
    Gera lista de `num_scenes` trechos não repetidos (arquivo, start_s, width, height)
    distribuídos ao longo dos vídeos HD de B-Roll, pulando intros/outros pretos.
    """
    if not video_files or num_scenes <= 0:
        return []

    pool: list[tuple[Path, float, int, int]] = []
    for vf in video_files:
        dur, w, h = probe_video_info(vf)
        if dur < scene_dur + 1.0:
            continue
        start_margin = 4.0 if dur > 25.0 else 0.5
        end_limit = dur - scene_dur - (6.0 if dur > 30.0 else 0.5)
        if end_limit <= start_margin:
            pool.append((vf, 0.0, w, h))
            continue

        # Divide o vídeo em fatias limpas bem espaçadas para máxima variedade visual
        step = max(scene_dur + 1.0, (end_limit - start_margin) / max(1, min(22, num_scenes + 4)))
        t = start_margin
        while t <= end_limit:
            pool.append((vf, round(t, 2), w, h))
            t += step

    if not pool:
        return []

    random.shuffle(pool)
    selected: list[tuple[Path, float, int, int]] = []
    idx = 0
    while len(selected) < num_scenes:
        selected.append(pool[idx % len(pool)])
        idx += 1
    return selected
