"""Motor de renderização multi-nicho 1080x1920@30fps via FFmpeg com B-Roll automático e cortes rápidos."""

from __future__ import annotations

import json
import math
import random
import re
import struct
import subprocess
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from src import BRANDING_DIR, INTROS_DIR, MUSIC_DIR, OUTPUT_DIR, slugify

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]

XFADE_TRANSITIONS = [
    "fadefast",
    "hblur",
    "wipeleft",
    "smoothright",
    "wiperight",
    "slideleft",
    "smoothleft",
    "slideright",
]


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def escape_ffmpeg_filter_path(path: Path) -> str:
    """Escapa caminhos Windows para uso seguro em filtros libavfilter (ex.: ass=filename='...')."""
    resolved = str(path.resolve()).replace("\\", "/")
    resolved = resolved.replace(":", r"\:")
    resolved = resolved.replace("'", r"\'")
    resolved = resolved.replace("[", r"\[")
    resolved = resolved.replace("]", r"\]")
    resolved = resolved.replace(";", r"\;")
    resolved = resolved.replace(",", r"\,")
    return resolved


def probe_media_duration(media_path: Path) -> float:
    """Retorna a duração em segundos de um vídeo/áudio via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
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
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


def _load_windows_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = (
        [
            r"C:\Windows\Fonts\impact.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
        if bold
        else [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
    )
    for fp in font_candidates:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines[:3]


def create_hook_banner_png(
    hook_text: str,
    output_path: Path,
    club_label: str = "FÁBRICA TK",
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """Cria um banner PNG transparente opcional de alto impacto para os primeiros segundos."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    title_font = _load_windows_font(size=60, bold=True)
    badge_font = _load_windows_font(size=30, bold=True)

    clean_hook = (hook_text or "NOVIDADE!").strip().upper()
    card_margin_x = 68
    card_w = width - (card_margin_x * 2)
    text_max_w = card_w - 84

    lines = _wrap_text_pixels(draw, clean_hook, title_font, text_max_w) or ["NOVIDADE!"]
    line_heights = [draw.textbbox((0, 0), line, font=title_font)[3] - draw.textbbox((0, 0), line, font=title_font)[1] for line in lines]

    line_spacing = 16
    total_text_h = sum(line_heights) + line_spacing * (len(lines) - 1)
    pad_top, pad_bottom = 48, 42
    card_h = total_text_h + pad_top + pad_bottom

    card_x0, card_y0 = card_margin_x, 235
    card_x1, card_y1 = card_x0 + card_w, card_y0 + card_h

    draw.rounded_rectangle((card_x0 + 8, card_y0 + 12, card_x1 + 8, card_y1 + 12), radius=28, fill=(0, 0, 0, 150))
    draw.rounded_rectangle(
        (card_x0, card_y0, card_x1, card_y1),
        radius=28,
        fill=(12, 17, 28, 226),
        outline=(250, 204, 21, 255),
        width=6,
    )

    badge_text = f"DESTAQUE  •  {club_label.upper()}"
    b_box = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (b_box[2] - b_box[0]) + 54
    badge_h = (b_box[3] - b_box[1]) + 22
    badge_x0 = (width - badge_w) // 2
    badge_y0 = card_y0 - (badge_h // 2) - 4

    draw.rounded_rectangle(
        (badge_x0, badge_y0, badge_x0 + badge_w, badge_y0 + badge_h),
        radius=16,
        fill=(220, 38, 38, 255),
        outline=(255, 255, 255, 220),
        width=3,
    )
    draw.text(
        (width // 2, badge_y0 + badge_h // 2),
        badge_text,
        font=badge_font,
        fill=(255, 255, 255, 255),
        anchor="mm",
    )

    cur_y = card_y0 + pad_top
    for line, lh in zip(lines, line_heights):
        draw.text(
            (width // 2, cur_y + lh // 2),
            line,
            font=title_font,
            fill=(250, 204, 21, 255),
            anchor="mm",
            stroke_width=4,
            stroke_fill=(0, 0, 0, 255),
        )
        cur_y += lh + line_spacing

    canvas.save(output_path, format="PNG")
    return output_path


def _pick_random_asset(folders: list[Path], extensions: tuple[str, ...]) -> Path | None:
    for folder in folders:
        if not folder.exists():
            continue
        candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions]
        if candidates:
            return random.choice(candidates)
    return None


def _collect_broll_slices(
    broll_dir: Path,
    clip_dur: float,
    max_slices: int,
) -> list[tuple[Path, float]]:
    """
    Coleta fatias não repetidas (arquivo, timestamp_inicio) a partir de clipes .mp4 em assets/broll/.
    Permite fatiar automaticamente um trailer (ex.: GTA VI) ou vídeo de lances da Liga MX!
    """
    if not broll_dir.exists() or max_slices <= 0:
        return []

    video_exts = (".mp4", ".mov", ".mkv", ".webm")
    files = sorted([p for p in broll_dir.iterdir() if p.is_file() and p.suffix.lower() in video_exts])
    if not files:
        return []

    pool: list[tuple[Path, float]] = []
    for vf in files:
        dur = probe_media_duration(vf)
        if dur < clip_dur + 0.5:
            continue
        # Pula os 2 primeiros e 2 últimos segundos em vídeos longos para evitar telas pretas/logos
        start_margin = 2.0 if dur > 15.0 else 0.2
        end_limit = dur - clip_dur - (2.0 if dur > 15.0 else 0.2)
        if end_limit <= start_margin:
            pool.append((vf, 0.0))
            continue

        usable = end_limit - start_margin
        step = max(clip_dur + 1.2, usable / max(1, min(14, max_slices)))
        t = start_margin
        while t <= end_limit:
            pool.append((vf, round(t, 2)))
            t += step

    if not pool:
        return []

    random.shuffle(pool)
    return pool[:max_slices]


def _generate_cinematic_bed(work_dir: Path, niche_id: str = "gta_vi") -> Path:
    """
    Gera trilha instrumental pulsante livre de direitos autorais (116 BPM) como fallback
    quando a pasta assets/music/ do nicho ainda está vazia.
    """
    wav_path = work_dir / "auto_tension_bed.wav"
    sr = 22050
    bpm = 118.0 if niche_id == "gta_vi" else 112.0
    beat_dur = 60.0 / bpm
    total_beats = 16
    duration = total_beats * beat_dur
    num_samples = int(sr * duration)

    if niche_id == "gta_vi":
        # Progressão Synthwave Vice City (Am - F - C - G)
        chords = [
            (55.00, 220.00, 261.63, 329.63),
            (43.65, 174.61, 220.00, 261.63),
            (65.41, 130.81, 164.81, 196.00),
            (49.00, 196.00, 246.94, 293.66),
        ]
    else:
        # Progressão Épica Esportiva (Dm - Bb - F - C)
        chords = [
            (73.42, 146.83, 174.61, 220.00),
            (58.27, 116.54, 146.83, 174.61),
            (87.31, 174.61, 220.00, 261.63),
            (65.41, 130.81, 164.81, 196.00),
        ]

    two_pi = 2.0 * math.pi
    buf = bytearray(num_samples * 2)

    for n in range(num_samples):
        t = n / sr
        beat_pos = t / beat_dur
        bar_idx = int(beat_pos // 4) % 4
        beat_in_bar = beat_pos % 4.0
        beat_frac = beat_pos % 1.0
        half_frac = (beat_pos * 2.0) % 1.0

        bass_f, n1, n2, n3 = chords[bar_idx]

        sidechain = 0.40 + 0.60 * min(1.0, beat_frac * 3.5)
        pad = (
            0.20 * math.sin(two_pi * n1 * t)
            + 0.15 * math.sin(two_pi * n2 * t)
            + 0.14 * math.sin(two_pi * n3 * t)
            + 0.08 * math.sin(two_pi * (n3 * 2.0) * t)
        ) * sidechain

        bass_env = math.exp(-half_frac * 4.8)
        bass = 0.36 * math.sin(two_pi * bass_f * t) * (0.42 + 0.58 * bass_env)

        kick = 0.0
        for hit in (0.0, 2.0, 3.5):
            dt = beat_in_bar - hit
            if 0.0 <= dt < 0.24:
                k_t = dt * beat_dur
                k_freq = 46.0 + 98.0 * math.exp(-k_t * 34.0)
                kick += 0.44 * math.sin(two_pi * k_freq * k_t) * math.exp(-k_t * 17.0)

        arp_step = int(beat_pos * 2) % 3
        arp_freq = (n1, n2, n3)[arp_step] * 2.0
        arp = 0.10 * math.sin(two_pi * arp_freq * t) * math.exp(-half_frac * 7.5)

        edge = 1.0
        if n < 300:
            edge = n / 300.0
        elif n > num_samples - 300:
            edge = (num_samples - n) / 300.0

        sample = max(-0.92, min(0.92, (pad + bass + kick + arp) * 0.85 * edge))
        struct.pack_into("<h", buf, n * 2, int(sample * 32767))

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf)

    return wav_path


def _parse_ffmpeg_time(line: str) -> float | None:
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def save_post_metadata_txt(
    txt_path: Path,
    title: str,
    source: str,
    url: str,
    gancho_tela: str,
    descricao_tiktok: str,
    hashtags: list[str],
    roteiro: str,
) -> Path:
    hashtags_line = " ".join(hashtags)
    content = (
        f"=== LEGENDA PRONTA PARA TIKTOK ===\n"
        f"{descricao_tiktok}\n\n"
        f"{hashtags_line}\n\n"
        f"=== METADADOS ===\n"
        f"Gancho em tela: {gancho_tela}\n"
        f"Pauta original: {title}\n"
        f"Fonte: {source}\n"
        f"Link: {url}\n\n"
        f"=== ROTEIRO NARRADO ===\n"
        f"{roteiro}\n"
    )
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)
    return txt_path


def render_video(
    image_paths: list[Path],
    audio_path: Path,
    ass_path: Path,
    audio_duration: float,
    gancho_tela: str,
    article_title: str,
    article_source: str,
    article_url: str,
    descricao_tiktok: str,
    hashtags: list[str],
    roteiro: str,
    work_dir: Path,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, Path]:
    """
    Renderiza vídeo vertical 1080x1920@30fps de alta retenção:
    - Cortes rápidos (2.4–3.2s por cena)
    - Intercala automaticamente trechos em movimento de assets/broll/*.mp4 (se houver!) com fotos de ação
    - Movimento cinético (punch zoom-in/out + pan lateral) e transições rápidas xfade (0.18s)
    - Legendas .ass centralizadas estilo viral + trilha de fundo + marca d'água por nicho
    """
    if not image_paths:
        raise ValueError("Lista de imagens vazia para renderização.")

    video_cfg = config.get("video", {})
    width = int(video_cfg.get("largura", 1080))
    height = int(video_cfg.get("altura", 1920))
    fps = int(video_cfg.get("fps", 30))
    trans_dur = float(video_cfg.get("duracao_transicao", 0.18))
    usar_banner_topo = bool(video_cfg.get("usar_banner_topo", False))
    banner_dur = float(video_cfg.get("duracao_banner_segundos", 2.5))
    intro_max_dur = float(video_cfg.get("duracao_intro_segundos", 4.5))
    music_vol = float(video_cfg.get("volume_musica", 0.18))
    usar_musica = bool(video_cfg.get("usar_musica", True))
    usar_intro = bool(video_cfg.get("usar_intro", True))
    usar_broll = bool(video_cfg.get("usar_broll", True))

    niche_id = str(config.get("nicho_ativo", "futebol_mexicano"))
    niche_name = str(config.get("nicho_nome", "Fábrica TK"))
    paths_dict = config.get("paths") or {}

    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    music_dirs = [Path(paths_dict["music_dir"]), MUSIC_DIR] if paths_dict.get("music_dir") else [MUSIC_DIR]
    intro_dirs = [Path(paths_dict["intros_dir"]), INTROS_DIR] if paths_dict.get("intros_dir") else [INTROS_DIR]
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]
    broll_dir = Path(paths_dict["broll_dir"]) if paths_dict.get("broll_dir") else (work_dir / "_no_broll")

    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    total_target_dur = max(5.0, float(audio_duration) + 0.35)

    # 1) Intro opcional
    intro_path: Path | None = None
    intro_dur = 0.0
    if usar_intro:
        cand_intro = _pick_random_asset(intro_dirs, (".mp4", ".mov", ".mkv", ".webm"))
        if cand_intro:
            probed = probe_media_duration(cand_intro)
            if probed >= 1.5:
                intro_path = cand_intro
                intro_dur = min(intro_max_dur, probed, max(2.0, total_target_dur * 0.22))
                _log(log_cb, f"[VideoEngine] Intro selecionada: {intro_path.name} ({intro_dur:.1f}s)")

    # 2) Música de fundo (do nicho -> raiz -> batida sintetizada do nicho)
    music_path: Path | None = None
    if usar_musica:
        music_path = _pick_random_asset(music_dirs, (".mp3", ".wav", ".m4a", ".ogg"))
        if music_path:
            _log(log_cb, f"[VideoEngine] Trilha de fundo: {music_path.name} (vol {int(music_vol * 100)}%)")
        else:
            music_path = _generate_cinematic_bed(work_dir, niche_id=niche_id)
            _log(
                log_cb,
                f"[VideoEngine] Usando trilha pulsante embutida do nicho '{niche_id}' (vol {int(music_vol * 100)}%).",
            )

    # 3) Marca d'água opcional
    branding_path = _pick_random_asset(brand_dirs, (".png",))
    if branding_path:
        _log(log_cb, f"[VideoEngine] Marca d'água detectada: {branding_path.name}")

    # 4) Banner opcional
    banner_png: Path | None = None
    if usar_banner_topo:
        banner_png = work_dir / "hook_banner.png"
        create_hook_banner_png(
            hook_text=gancho_tela,
            output_path=banner_png,
            club_label=niche_name,
            width=width,
            height=height,
        )

    # 5) Calcula ritmo rápido de cenas (~2.5–3.2s por cena) e intercala B-Roll em vídeo se existir
    trans_dur = min(trans_dur, 0.35)
    num_slots = len(image_paths)
    num_segments = num_slots + (1 if intro_path else 0)
    raw_seg_dur = (total_target_dur - intro_dur + (num_segments - 1) * trans_dur) / num_slots
    seg_frames = max(int(round(max(1.8, raw_seg_dur) * fps)), int(round((trans_dur + 0.8) * fps)))
    seg_dur = seg_frames / float(fps)

    # Coleta fatias de B-Roll (.mp4) se houver arquivos na pasta assets/broll/ do nicho
    broll_slices: list[tuple[Path, float]] = []
    if usar_broll:
        broll_slices = _collect_broll_slices(broll_dir, clip_dur=seg_dur + 0.15, max_slices=num_slots // 2 + 2)
        if broll_slices:
            _log(
                log_cb,
                f"[VideoEngine] B-Roll detectado! Intercalando {len(broll_slices)} cenas em vídeo real com fotos de ação.",
            )

    # Monta timeline de cenas: intercala B-Roll nas posições ímpares (ou pares) mantendo fotos da pauta
    scenes: list[dict[str, Any]] = []
    broll_cursor = 0
    for idx, img_p in enumerate(image_paths):
        if broll_slices and (idx % 2 == 1) and broll_cursor < len(broll_slices):
            v_file, v_start = broll_slices[broll_cursor]
            broll_cursor += 1
            scenes.append({"type": "video", "path": v_file, "start": v_start})
        else:
            scenes.append({"type": "image", "path": img_p})

    _log(
        log_cb,
        f"[VideoEngine] Compondo {num_segments} cenas rápidas "
        f"({seg_dur:.2f}s cada, transição {trans_dur:.2f}s, duração alvo {total_target_dur:.1f}s)...",
    )

    # Entradas FFmpeg
    inputs: list[str] = []
    idx_counter = 0

    intro_idx: int | None = None
    if intro_path:
        inputs.extend(["-i", str(intro_path)])
        intro_idx = idx_counter
        idx_counter += 1

    scene_entries: list[tuple[int, dict[str, Any]]] = []
    for sc in scenes:
        if sc["type"] == "video":
            inputs.extend(["-ss", f"{sc['start']:.2f}", "-t", f"{seg_dur + 0.25:.2f}", "-i", str(sc["path"])])
        else:
            inputs.extend(["-i", str(sc["path"])])
        scene_entries.append((idx_counter, sc))
        idx_counter += 1

    banner_idx: int | None = None
    if banner_png is not None:
        inputs.extend(["-loop", "1", "-t", f"{banner_dur + 0.5:.2f}", "-i", str(banner_png)])
        banner_idx = idx_counter
        idx_counter += 1

    branding_idx: int | None = None
    if branding_path:
        inputs.extend(["-i", str(branding_path)])
        branding_idx = idx_counter
        idx_counter += 1

    inputs.extend(["-i", str(audio_path)])
    voice_idx = idx_counter
    idx_counter += 1

    music_idx: int | None = None
    if music_path:
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
        music_idx = idx_counter
        idx_counter += 1

    # Filtergraph
    filter_lines: list[str] = []
    segment_labels: list[tuple[str, float]] = []

    if intro_idx is not None:
        filter_lines.append(
            f"[{intro_idx}:v]"
            f"trim=start=0:duration={intro_dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"fps={fps},settb=AVTB,format=yuv420p,setsar=1[seg_intro]"
        )
        segment_labels.append(("seg_intro", intro_dur))

    up_w = int(round(width * 1.28))
    up_h = int(round(height * 1.28))
    denom = max(1, seg_frames - 1)

    for i, (in_idx, sc) in enumerate(scene_entries):
        label = f"seg_{i:02d}"
        if sc["type"] == "video":
            filter_lines.append(
                f"[{in_idx}:v]"
                f"trim=start=0:duration={seg_dur:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"fps={fps},settb=AVTB,format=yuv420p,setsar=1[{label}]"
            )
        else:
            motion = i % 4
            if motion == 0:
                z_expr = f"1.0+0.18*(on/{denom})"
                x_expr = "iw/2-(iw/zoom/2)"
                y_expr = "ih/2-(ih/zoom/2)"
            elif motion == 1:
                z_expr = f"1.14+0.05*(on/{denom})"
                x_expr = f"(iw-iw/zoom)*(0.20+0.60*(on/{denom}))"
                y_expr = "ih/2-(ih/zoom/2)"
            elif motion == 2:
                z_expr = f"1.18-0.18*(on/{denom})"
                x_expr = "iw/2-(iw/zoom/2)"
                y_expr = "ih/2-(ih/zoom/2)"
            else:
                z_expr = f"1.14+0.05*(on/{denom})"
                x_expr = f"(iw-iw/zoom)*(0.80-0.60*(on/{denom}))"
                y_expr = "ih/2-(ih/zoom/2)"

            filter_lines.append(
                f"[{in_idx}:v]"
                f"scale={up_w}:{up_h}:flags=lanczos,"
                f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':"
                f"d={seg_frames}:s={width}x{height}:fps={fps},"
                f"setpts=PTS-STARTPTS,settb=AVTB,fps={fps},format=yuv420p,setsar=1[{label}]"
            )
        segment_labels.append((label, seg_dur))

    # Transições rápidas xfade
    if len(segment_labels) == 1:
        current_v = segment_labels[0][0]
    else:
        current_v, cum_dur = segment_labels[0]
        for step_i in range(1, len(segment_labels)):
            next_label, next_dur = segment_labels[step_i]
            transition = XFADE_TRANSITIONS[(step_i - 1) % len(XFADE_TRANSITIONS)]
            offset = max(0.05, cum_dur - trans_dur)
            out_label = f"xf_{step_i:02d}"
            filter_lines.append(
                f"[{current_v}][{next_label}]"
                f"xfade=transition={transition}:duration={trans_dur:.3f}:offset={offset:.3f}[{out_label}]"
            )
            current_v = out_label
            cum_dur = offset + next_dur

    # Vinheta + contraste vibrante
    filter_lines.append(
        f"[{current_v}]vignette=PI/4.8,eq=brightness=-0.03:contrast=1.06:saturation=1.08[v_graded]"
    )
    current_v = "v_graded"

    if branding_idx is not None:
        filter_lines.append(f"[{branding_idx}:v]scale=170:-1,format=rgba,colorchannelmixer=aa=0.78[wm]")
        filter_lines.append(f"[{current_v}][wm]overlay=W-w-36:96[v_wm]")
        current_v = "v_wm"

    if banner_idx is not None:
        fade_out_start = max(0.3, banner_dur - 0.30)
        filter_lines.append(
            f"[{banner_idx}:v]format=rgba,"
            f"fade=t=in:st=0:d=0.22:alpha=1,"
            f"fade=t=out:st={fade_out_start:.2f}:d=0.30:alpha=1[banner]"
        )
        filter_lines.append(
            f"[{current_v}][banner]overlay=0:0:eof_action=pass:enable='between(t,0,{banner_dur:.2f})'[v_banner]"
        )
        current_v = "v_banner"

    escaped_ass = escape_ffmpeg_filter_path(ass_path)
    filter_lines.append(f"[{current_v}]ass=filename='{escaped_ass}',format=yuv420p[vout]")

    # Mixagem de áudio
    if music_idx is not None:
        fade_out_audio = max(0.5, total_target_dur - 2.0)
        filter_lines.append(f"[{voice_idx}:a]volume=1.08,apad=pad_dur=0.35[voice]")
        filter_lines.append(
            f"[{music_idx}:a]atrim=0:{total_target_dur:.3f},asetpts=PTS-STARTPTS,"
            f"volume={music_vol:.3f},"
            f"afade=t=in:st=0:d=0.8,"
            f"afade=t=out:st={fade_out_audio:.3f}:d=2.0[bgm]"
        )
        filter_lines.append(
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
            "alimiter=limit=0.95[aout]"
        )
    else:
        filter_lines.append(
            f"[{voice_idx}:a]volume=1.08,apad=pad_dur=0.35,alimiter=limit=0.95[aout]"
        )

    filter_script_path = work_dir / "filter_complex.txt"
    with open(filter_script_path, "w", encoding="utf-8") as f:
        f.write(";\n".join(filter_lines))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = slugify(article_title, max_length=42)
    output_mp4 = output_dir / f"{timestamp}_{base_slug}.mp4"
    output_txt = output_dir / f"{timestamp}_{base_slug}.txt"

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        *inputs,
        "-/filter_complex",
        str(filter_script_path),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        f"{total_target_dur:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        str(output_mp4),
    ]

    _log(log_cb, f"[VideoEngine] Renderizando FFmpeg -> nichos/{niche_id}/output/{output_mp4.name}")

    stderr_tail: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            if stop_event and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("Renderização cancelada pelo usuário.")

            stripped = line.strip()
            if stripped:
                stderr_tail.append(stripped)
                if len(stderr_tail) > 25:
                    stderr_tail.pop(0)

            cur_time = _parse_ffmpeg_time(stripped)
            if cur_time is not None and progress_cb:
                progress_cb(max(0.0, min(1.0, cur_time / total_target_dur)))

        ret = proc.wait()
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    if ret != 0 or not output_mp4.exists() or output_mp4.stat().st_size < 10_000:
        err_context = "\n".join(stderr_tail[-12:])
        raise RuntimeError(f"FFmpeg falhou (código {ret}). Detalhes:\n{err_context}")

    save_post_metadata_txt(
        txt_path=output_txt,
        title=article_title,
        source=article_source,
        url=article_url,
        gancho_tela=gancho_tela,
        descricao_tiktok=descricao_tiktok,
        hashtags=hashtags,
        roteiro=roteiro,
    )

    if progress_cb:
        progress_cb(1.0)

    size_mb = output_mp4.stat().st_size / (1024 * 1024)
    _log(
        log_cb,
        f"[VideoEngine] Vídeo concluído! {output_mp4.name} ({size_mb:.1f} MB) + {output_txt.name}",
    )
    return output_mp4, output_txt
