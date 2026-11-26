"""Módulo Nível 1: Caçador automático e contextual de vídeos HD (embeds da notícia + YouTube + B-Roll)."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from src import BASE_DIR, normalize_text

LogCallback = Callable[[str], None]

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
    """Baixa vídeo em 1080p/720p HD via yt-dlp (vídeo puro H.264, super rápido)."""
    ytdlp = _find_ytdlp()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.stat().st_size > 200_000:
        return output_path

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
        # Sem --force-keyframes-at-cuts para fazer stream-copy ultrarrápido (em vez de re-encodar 1080p)
        cmd.extend(["--download-sections", section])
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

    if output_path.exists() and output_path.stat().st_size < 150_000:
        output_path.unlink(missing_ok=True)
    return None


def _search_best_youtube_video(
    search_query: str,
    min_dur: int = 35,
    max_dur: int = 2400,
    must_contain_any: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
    skip_ids: set[str] | None = None,
) -> tuple[str, str, int, str] | None:
    """Busca o melhor vídeo HD no YouTube via yt-dlp --flat-playlist."""
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
        vid_id = str(entry.get("id") or "")
        title = str(entry.get("title") or "")
        dur = int(entry.get("duration") or 0)
        if not vid_id or (skip_ids and vid_id in skip_ids):
            continue
        if dur < min_dur or dur > max_dur:
            continue

        norm_title = normalize_text(title)
        if exclude_terms and any(bad in norm_title for bad in exclude_terms):
            continue
        if must_contain_any and not any(ok in norm_title for ok in must_contain_any):
            continue

        return f"https://www.youtube.com/watch?v={vid_id}", title, dur, vid_id

    return None


def ensure_broll_videos(
    config: dict[str, Any],
    article_title: str,
    work_dir: Path,
    custom_video_url: str = "",
    embedded_videos: list[str] | None = None,
    contextual_queries: list[str] | None = None,
    log_cb: LogCallback | None = None,
) -> tuple[list[Path], list[Path]]:
    """
    Caçador Contextual Nível 1 (100% automático!):
    Retorna `(contextual_videos, library_videos)`:
    - `contextual_videos`: vídeos específicos desta pauta (link manual, embed da própria matéria
      IGN/Eurogamer/Gigaton ou busca contextual no YouTube gerada pelo Ollama).
    - `library_videos`: biblioteca permanente do nicho em `nichos/<nicho>/assets/broll/`.
    """
    niche_id = str(config.get("nicho_ativo", "gta_vi"))
    paths_dict = config.get("paths") or {}
    broll_dir = Path(paths_dict["broll_dir"]) if paths_dict.get("broll_dir") else (work_dir / "broll")
    broll_dir.mkdir(parents=True, exist_ok=True)
    video_exts = (".mp4", ".mov", ".mkv", ".webm")

    contextual_videos: list[Path] = []
    used_yt_ids: set[str] = set()

    # 1) Link manual do usuário (se preenchido no painel) -> prioridade absoluta
    if custom_video_url and custom_video_url.strip().startswith("http"):
        _log(log_cb, "[VideoFetcher] Baixando vídeo HD do link informado...")
        custom_out = work_dir / "custom_source.mp4"
        dl = download_video_hd(custom_video_url.strip(), custom_out, section="*0:05-3:30", log_cb=log_cb)
        if not dl:
            dl = download_video_hd(custom_video_url.strip(), custom_out, section=None, log_cb=log_cb)
        if dl:
            return [dl], []

    # 2) Vídeos YouTube embutidos diretamente na matéria (ex.: clipe do IGN/Eurogamer/Gigaton)
    for emb_idx, emb_url in enumerate((embedded_videos or [])[:2], start=1):
        _log(log_cb, f"[VideoFetcher] Vídeo embutido encontrado na matéria! Baixando HD ({emb_idx})...")
        out_emb = work_dir / f"embedded_{emb_idx:02d}.mp4"
        dl = download_video_hd(emb_url, out_emb, section="*0:10-2:15", log_cb=log_cb)
        if not dl:
            dl = download_video_hd(emb_url, out_emb, section=None, log_cb=log_cb)
        if dl:
            contextual_videos.append(dl)

    # 3) Busca contextual no YouTube usando as queries específicas da notícia geradas pelo Ollama!
    queries = [q.strip() for q in (contextual_queries or []) if q and q.strip()]
    if niche_id == "gta_vi":
        must_any = ("gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "leonida", "vice city")
        exclude = ("gta 5", "gta v", "mod", "fan made", "concept", "unreal engine", "reaction", "podcast", "podcasting")
    elif niche_id == "futebol_mexicano":
        must_any = ("gol", "resumen", "highlight", "america", "chivas", "tigres", "cruz azul", "pumas", "monterrey", "liga mx")
        exclude = ("fifa", "ea fc", "fc 25", "fc 26", "efootball", "pes", "en vivo", "live", "reaccion")
    else:
        must_any = ()
        exclude = ("reaction", "live")

    for q_idx, sq in enumerate(queries[:2], start=1):
        if len(contextual_videos) >= 2:
            break
        _log(log_cb, f"[VideoFetcher] Caçando vídeo contextual no YouTube ({q_idx}/2): '{sq}'...")
        found = _search_best_youtube_video(
            sq,
            min_dur=35,
            max_dur=2400,
            must_contain_any=must_any,
            exclude_terms=exclude,
            skip_ids=used_yt_ids,
        )
        if found:
            v_url, v_title, v_dur, v_id = found
            used_yt_ids.add(v_id)
            _log(log_cb, f"[VideoFetcher] Vídeo contextual encontrado: '{v_title[:60]}' ({v_dur}s)")
            # Baixa ~105s de miolo (evita intro/outro falando) para rapidez e variedade
            if v_dur > 180:
                sec = "*0:30-2:15"
            elif v_dur > 110:
                sec = "*0:12-1:45"
            else:
                sec = None
            dl = download_video_hd(v_url, work_dir / f"ctx_{q_idx:02d}.mp4", section=sec, log_cb=log_cb)
            if dl:
                contextual_videos.append(dl)

    # 4) Garante biblioteca permanente 1080p do nicho (ex.: trailers oficiais de GTA VI baixados 1 vez)
    library_videos = sorted(
        [p for p in broll_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts and p.stat().st_size > 200_000]
    )

    if niche_id == "gta_vi" and len(library_videos) < 2:
        _log(log_cb, "[VideoFetcher] Completando biblioteca permanente 1080p oficial de GTA VI...")
        for fname, url, sec in GTA6_OFFICIAL_VIDEOS:
            target = broll_dir / fname
            if not target.exists():
                download_video_hd(url, target, section=sec, log_cb=log_cb)
        library_videos = sorted(
            [p for p in broll_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts and p.stat().st_size > 200_000]
        )

    _log(
        log_cb,
        f"[VideoFetcher] Pool de vídeo pronto: {len(contextual_videos)} vídeo(s) contextual(is) da pauta + "
        f"{len(library_videos)} vídeo(s) na biblioteca do nicho.",
    )
    return contextual_videos, library_videos


def build_video_scene_slices(
    video_files: list[Path],
    num_scenes: int,
    scene_dur: float,
) -> list[tuple[Path, float, int, int]]:
    """Fallback de fatiamento distribuído quando o Scene Matcher semântico não está disponível."""
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

        step = max(scene_dur + 1.0, (end_limit - start_margin) / max(1, min(22, num_scenes + 4)))
        t = start_margin
        while t <= end_limit:
            pool.append((vf, round(t, 2), w, h))
            t += step

    if not pool:
        return []

    random.shuffle(pool)
    selected: list[tuple[Path, float, int, int]] = []
    for idx in range(num_scenes):
        selected.append(pool[idx % len(pool)])
    return selected
