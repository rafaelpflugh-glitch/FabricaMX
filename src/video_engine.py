"""Motor de renderização multi-nicho 1080x1920@30fps com 100% vídeo real HD e enquadramento limpo."""

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
from src.video_fetcher import build_video_scene_slices

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

VIDEO_FILTER_PRESETS: dict[str, dict[str, str]] = {
    "vivido2": {
        "label": "Vívido 2 (Viral TikTok • Recomendado)",
        "vf": (
            "eq=contrast=1.14:brightness=0.02:saturation=1.34:gamma=1.03,"
            "colorbalance=rs=0.02:gs=0.01:bs=0.03:rm=0.03:gm=0.01:bm=-0.02:rh=0.04:gh=0.02:bh=-0.02,"
            "unsharp=5:5:0.75:3:3:0.35"
        ),
    },
    "vivido_hdr": {
        "label": "HDR Ultra Nítido (Futebol / Gameplay)",
        "vf": (
            "eq=contrast=1.20:brightness=0.01:saturation=1.38:gamma=1.02,"
            "colorbalance=rs=-0.02:bs=0.04:rh=0.05:gh=0.02:bh=-0.02,"
            "unsharp=5:5:1.05:3:3:0.45"
        ),
    },
    "cinema_teal_orange": {
        "label": "Cinema Teal & Orange (Dramático)",
        "vf": (
            "eq=contrast=1.16:brightness=-0.01:saturation=1.18,"
            "colorbalance=rs=-0.06:gs=0.01:bs=0.08:rm=0.04:gm=0.00:bm=-0.03:rh=0.08:gh=0.02:bh=-0.06,"
            "unsharp=5:5:0.55:3:3:0.20"
        ),
    },
    "neon_vice": {
        "label": "Neon Vice (GTA VI / Cyber)",
        "vf": (
            "eq=contrast=1.18:brightness=0.01:saturation=1.42:gamma=1.02,"
            "colorbalance=rs=0.04:gs=-0.03:bs=0.08:rh=0.06:gh=-0.02:bh=0.06,"
            "unsharp=5:5:0.70:3:3:0.30"
        ),
    },
    "quente_tv": {
        "label": "Quente & Limpo (TV / Cortes)",
        "vf": (
            "eq=contrast=1.09:brightness=0.02:saturation=1.18:gamma=1.03,"
            "colorbalance=rm=0.03:gm=0.01:bm=-0.02:rh=0.04:gh=0.02:bh=-0.03,"
            "unsharp=5:5:0.55:3:3:0.25"
        ),
    },
    "nenhum": {
        "label": "Original (Sem Filtro)",
        "vf": "eq=contrast=1.04:saturation=1.05",
    },
}


def get_video_filter_chain(filter_id: str | None) -> tuple[str, str]:
    """Retorna (label_legivel, cadeia_vf_ffmpeg) para o filtro de cor escolhido."""
    key = str(filter_id or "vivido2").strip().lower()
    preset = VIDEO_FILTER_PRESETS.get(key) or VIDEO_FILTER_PRESETS["vivido2"]
    return preset["label"], preset["vf"]


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
    return lines[:2]


def create_hook_banner_png(
    hook_text: str,
    output_path: Path,
    club_label: str = "FÁBRICA TK",
    accent_rgb: tuple[int, int, int] = (250, 204, 21),
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """
    Cria banner limpo posicionado na zona superior (y=120..315), logo acima do viewport 1080x1080
    para nunca tampar jogadores, bola ou personagens de GTA VI.
    """
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    title_font = _load_windows_font(size=54, bold=True)
    badge_font = _load_windows_font(size=26, bold=True)

    clean_hook = (hook_text or "NOVIDADE!").strip().upper()
    card_margin_x = 76
    card_w = width - (card_margin_x * 2)
    text_max_w = card_w - 72

    lines = _wrap_text_pixels(draw, clean_hook, title_font, text_max_w) or ["NOVIDADE!"]
    line_heights = [
        draw.textbbox((0, 0), line, font=title_font)[3] - draw.textbbox((0, 0), line, font=title_font)[1]
        for line in lines
    ]

    line_spacing = 12
    total_text_h = sum(line_heights) + line_spacing * (len(lines) - 1)
    pad_top, pad_bottom = 38, 28
    card_h = min(195, total_text_h + pad_top + pad_bottom)

    # Zona segura superior do TikTok (abaixo da barra 'Seguindo | Para você' em y~165)
    card_x0, card_y0 = card_margin_x, 188
    card_x1, card_y1 = card_x0 + card_w, card_y0 + card_h

    draw.rounded_rectangle((card_x0 + 6, card_y0 + 8, card_x1 + 6, card_y1 + 8), radius=24, fill=(0, 0, 0, 150))
    draw.rounded_rectangle(
        (card_x0, card_y0, card_x1, card_y1),
        radius=24,
        fill=(10, 14, 24, 232),
        outline=(*accent_rgb, 255),
        width=5,
    )

    badge_text = f"URGENTE  •  {club_label.upper()}"
    b_box = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (b_box[2] - b_box[0]) + 46
    badge_h = (b_box[3] - b_box[1]) + 18
    badge_x0 = (width - badge_w) // 2
    badge_y0 = card_y0 - (badge_h // 2) - 2

    draw.rounded_rectangle(
        (badge_x0, badge_y0, badge_x0 + badge_w, badge_y0 + badge_h),
        radius=14,
        fill=(220, 38, 38, 255),
        outline=(255, 255, 255, 220),
        width=2,
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
            fill=(*accent_rgb, 255),
            anchor="mm",
            stroke_width=4,
            stroke_fill=(0, 0, 0, 255),
        )
        cur_y += lh + line_spacing

    canvas.save(output_path, format="PNG")
    return output_path


def create_scoreboard_png(
    placar_texto: str,
    output_path: Path,
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """Cria overlay de placar esportivo limpo no rodapé (y=1565) estilo crônica viral."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = _load_windows_font(size=54, bold=True)

    label = placar_texto.strip().upper()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 52, 24
    box_w = min(width - 80, tw + pad_x * 2)
    box_h = th + pad_y * 2
    x0 = (width - box_w) // 2
    y0 = 1565

    draw.rounded_rectangle((x0 + 5, y0 + 7, x0 + box_w + 5, y0 + box_h + 7), radius=22, fill=(0, 0, 0, 160))
    draw.rounded_rectangle(
        (x0, y0, x0 + box_w, y0 + box_h),
        radius=22,
        fill=(12, 18, 30, 232),
        outline=(250, 204, 21, 255),
        width=5,
    )
    draw.text(
        (width // 2, y0 + box_h // 2),
        label,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        stroke_width=4,
        stroke_fill=(0, 0, 0, 255),
    )
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


def _generate_cinematic_bed(work_dir: Path, niche_id: str = "gta_vi") -> Path:
    """Gera trilha instrumental pulsante livre de direitos autorais (fallback se assets/music vazio)."""
    wav_path = work_dir / "auto_tension_bed.wav"
    sr = 22050
    bpm = 118.0 if niche_id == "gta_vi" else 114.0
    beat_dur = 60.0 / bpm
    total_beats = 16
    duration = total_beats * beat_dur
    num_samples = int(sr * duration)

    if niche_id == "gta_vi":
        chords = [
            (55.00, 220.00, 261.63, 329.63),
            (43.65, 174.61, 220.00, 261.63),
            (65.41, 130.81, 164.81, 196.00),
            (49.00, 196.00, 246.94, 293.66),
        ]
    else:
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

        bass = 0.36 * math.sin(two_pi * bass_f * t) * (0.42 + 0.58 * math.exp(-half_frac * 4.8))

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
    broll_videos: list[Path] | None = None,
    matched_scenes: list[tuple[Path, float, int, int]] | None = None,
    timed_scenes: list[dict[str, Any]] | None = None,
    placar_texto: str = "",
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, Path]:
    """
    Renderiza vídeo vertical 1080x1920@30fps:
    - Quando `broll_videos` está disponível: monta 100% EM VÍDEO REAL HD EM MOVIMENTO
      com enquadramento limpo 1080x1080 nítido (1:1 nativo, ZERO zoom excessivo, ZERO cortes
      bruscos em jogadores/personagens) sobre fundo atmosférico borrado em movimento!
    - Sem vídeos B-Roll: usa fotos HD com movimento super suave (1.00 -> 1.04) no mesmo enquadramento limpo.
    - Queima banner superior opcional (acima do viewport), placar opcional (abaixo) e legendas .ass.
    """
    video_cfg = config.get("video", {})
    fmt_choice = str(video_cfg.get("formato_tela", "auto")).strip().lower()
    if fmt_choice == "1x1":
        width, height, aspect_str, dar_expr = 1080, 1080, "1:1", "1/1"
    else:
        width = int(video_cfg.get("largura", 1080))
        height = int(video_cfg.get("altura", 1920))
        aspect_str = "9:16"
        dar_expr = "9/16"
    fps = int(video_cfg.get("fps", 30))
    trans_dur = min(float(video_cfg.get("duracao_transicao", 0.18)), 0.30)
    usar_banner_topo = bool(video_cfg.get("usar_banner_topo", True))
    banner_dur = float(video_cfg.get("duracao_banner_segundos", 3.0))
    intro_max_dur = float(video_cfg.get("duracao_intro_segundos", 4.5))
    music_vol = float(video_cfg.get("volume_musica", 0.20))
    usar_musica = bool(video_cfg.get("usar_musica", True))
    usar_intro = bool(video_cfg.get("usar_intro", True))
    preferir_fotos = bool(video_cfg.get("preferir_fotos", False))
    target_scenes = max(6, min(22, int(video_cfg.get("num_cenas_alvo", 18))))

    niche_id = str(config.get("nicho_ativo", "gta_vi"))
    niche_name = str(config.get("nicho_nome", "Fábrica TK"))
    paths_dict = config.get("paths") or {}

    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    music_dirs = [Path(paths_dict["music_dir"]), MUSIC_DIR] if paths_dict.get("music_dir") else [MUSIC_DIR]
    intro_dirs = [Path(paths_dict["intros_dir"]), INTROS_DIR] if paths_dict.get("intros_dir") else [INTROS_DIR]
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]

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

    # 2) Trilha musical
    music_path: Path | None = None
    if usar_musica:
        music_path = _pick_random_asset(music_dirs, (".mp3", ".wav", ".m4a", ".ogg"))
        if music_path:
            _log(log_cb, f"[VideoEngine] Trilha de fundo: {music_path.name} (vol {int(music_vol * 100)}%)")
        else:
            music_path = _generate_cinematic_bed(work_dir, niche_id=niche_id)
            _log(
                log_cb,
                f"[VideoEngine] Trilha pulsante do nicho '{niche_id}' ativada (vol {int(music_vol * 100)}%).",
            )

    # 3) Marca d'água / Logo do canal opcional (aceita PNG transparente ou JPG/WEBP em assets/branding/)
    branding_path = _pick_random_asset(brand_dirs, (".png", ".jpg", ".jpeg", ".webp"))
    if branding_path:
        _log(log_cb, f"[VideoEngine] Logo do canal aplicado: {branding_path.name}")

    # 4) Banner superior limpo (posicionado na zona segura superior do TikTok/Reels)
    banner_png: Path | None = None
    if usar_banner_topo and gancho_tela:
        banner_png = work_dir / "hook_banner.png"
        niche_accent_map = {
            "gta_vi": (255, 46, 209),
            "virais_mundo": (249, 115, 22),
            "fofoca_famosos": (236, 72, 153),
            "facebook_fofoca": (59, 130, 246),
            "recap_comedia": (168, 85, 247),
            "futebol_rivalidade": (34, 197, 94),
            "musica_gear": (245, 158, 11),
        }
        accent = niche_accent_map.get(niche_id, (250, 204, 21))
        create_hook_banner_png(
            hook_text=gancho_tela,
            output_path=banner_png,
            club_label=niche_name,
            accent_rgb=accent,
            width=width,
            height=height,
        )

    # 5) Placar opcional no rodapé (para crônicas de jogo)
    scoreboard_png: Path | None = None
    if placar_texto and placar_texto.strip():
        scoreboard_png = work_dir / "scoreboard.png"
        create_scoreboard_png(placar_texto.strip(), scoreboard_png, width=width, height=height)
        _log(log_cb, f"[VideoEngine] Placar em tela: '{placar_texto.strip()}'")

    # 6) Monta lista de cenas (sincronia exata Fala<->Cena / híbrido / sincronia semântica / B-Roll / fallback fotos)
    scenes: list[dict[str, Any]] = []
    video_list = list(broll_videos or [])
    pre_matched = list(matched_scenes or [])
    pre_timed = list(timed_scenes or [])
    if pre_timed:
        num_slots = len(pre_timed)
    elif preferir_fotos and image_paths:
        num_slots = min(max(6, target_scenes), len(image_paths) + (3 if video_list else 0))
    else:
        num_slots = len(pre_matched) if pre_matched else (target_scenes if video_list else len(image_paths))
    if num_slots <= 0:
        raise ValueError("Nenhuma cena (vídeo ou imagem) disponível para renderização.")

    num_segments = num_slots + (1 if intro_path else 0)
    raw_seg_dur = (total_target_dur - intro_dur + (num_segments - 1) * trans_dur) / num_slots
    seg_frames = max(int(round(max(2.0, raw_seg_dur) * fps)), int(round((trans_dur + 0.9) * fps)))
    seg_dur = seg_frames / float(fps)

    if pre_timed:
        for sc_idx, ts_item in enumerate(pre_timed):
            item_dur = max(trans_dur + 0.9, float(ts_item.get("dur", seg_dur)))
            # Adiciona trans_dur (exceto na última cena) para que o xfade ocorra no timestamp exato da troca de fala!
            if sc_idx < len(pre_timed) - 1:
                item_dur += trans_dur
            sc_copy = dict(ts_item)
            sc_copy["dur"] = item_dur
            scenes.append(sc_copy)
        _log(
            log_cb,
            f"[VideoEngine] MODO SINCRONIA FALA ↔ IMAGEM: {len(scenes)} cenas cronológicas "
            f"alinhadas exatamente aos timestamps da narração!",
        )
    elif preferir_fotos and image_paths:
        # Estilo Facebook/Fofoca: alterna fotos HD dos envolvidos (foto dela, foto dele, evento) + videozinhos curtos!
        vid_slices = build_video_scene_slices(video_list, num_scenes=max(2, num_slots // 3), scene_dur=seg_dur + 0.15) if video_list else []
        img_i, vid_i = 0, 0
        for slot in range(num_slots):
            use_vid = bool(vid_slices) and (slot % 3 == 2) and vid_i < len(vid_slices)
            if use_vid:
                vf, v_start, vw, vh = vid_slices[vid_i]
                scenes.append({"type": "video", "path": vf, "start": v_start, "w": vw, "h": vh})
                vid_i += 1
            else:
                img_p = image_paths[img_i % len(image_paths)]
                scenes.append({"type": "image", "path": img_p})
                img_i += 1
        _log(
            log_cb,
            f"[VideoEngine] MODO HÍBRIDO FOTOS + CLIPES: {img_i} fotos HD + {vid_i} trechos de vídeo "
            f"({seg_dur:.2f}s cada)!",
        )
    elif pre_matched:
        for vf, v_start, vw, vh in pre_matched:
            scenes.append({"type": "video", "path": vf, "start": v_start, "w": vw, "h": vh})
        _log(
            log_cb,
            f"[VideoEngine] MODO SINCRONIA SEMÂNTICA (IA VISÃO): {len(scenes)} cenas em vídeo HD "
            f"casadas com a narração ({seg_dur:.2f}s cada)!",
        )
    elif video_list:
        slices = build_video_scene_slices(video_list, num_scenes=num_slots, scene_dur=seg_dur + 0.15)
        for vf, v_start, vw, vh in slices:
            scenes.append({"type": "video", "path": vf, "start": v_start, "w": vw, "h": vh})
        _log(
            log_cb,
            f"[VideoEngine] MODO 100% VÍDEO REAL HD: {len(scenes)} cenas em movimento "
            f"({seg_dur:.2f}s cada, tela cheia 9:16)!",
        )

    if not scenes:
        for img_p in image_paths:
            scenes.append({"type": "image", "path": img_p})
        _log(
            log_cb,
            f"[VideoEngine] Modo fotos HD: {len(scenes)} cenas ({seg_dur:.2f}s cada, zoom suave 4%).",
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
        cur_dur = float(sc.get("dur", seg_dur))
        if sc["type"] == "video":
            inputs.extend(["-ss", f"{sc['start']:.2f}", "-t", f"{cur_dur + 0.30:.2f}", "-i", str(sc["path"])])
        else:
            inputs.extend(["-i", str(sc["path"])])
        scene_entries.append((idx_counter, sc))
        idx_counter += 1

    banner_idx: int | None = None
    if banner_png is not None:
        inputs.extend(["-loop", "1", "-t", f"{banner_dur + 0.5:.2f}", "-i", str(banner_png)])
        banner_idx = idx_counter
        idx_counter += 1

    scoreboard_idx: int | None = None
    if scoreboard_png is not None:
        inputs.extend(["-i", str(scoreboard_png)])
        scoreboard_idx = idx_counter
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

    norm_tail = f"fps={fps},settb=AVTB,scale=in_range=auto:out_range=tv:out_color_matrix=bt709,format=yuv420p,setsar=1"

    if intro_idx is not None:
        filter_lines.append(
            f"[{intro_idx}:v]"
            f"trim=start=0:duration={intro_dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},"
            f"{norm_tail}[seg_intro]"
        )
        segment_labels.append(("seg_intro", intro_dur))

    # Enquadramento TELA CHEIA 9:16 nativa TikTok (1080x1920) para notícias, futebol e gameplay
    filter_label, color_vf = get_video_filter_chain(video_cfg.get("filtro_cor", "vivido2"))
    _log(log_cb, f"[VideoEngine] Filtro visual: {filter_label}")
    denom = max(1, seg_frames - 1)

    # Posição do viewport amplo 16:9 -> 9:16:
    # - Em novelas/comédia/fofoca (recap_comedia, fofoca_famosos, facebook_fofoca), ROSTOS E PESSOAS são o foco principal!
    #   Usamos viewport cinema 1080x820 (preserva ~75% da largura real da cena — mostra 2 pessoas frente a frente sem cortar rostos!)
    #   posicionado no centro limpo (y=445..1265), com fundo escuro suave (sem vídeo duplicado brilhante distraindo atrás!)
    #   e foco vertical no terço superior (0.28) onde sempre estão as cabeças/rostos.
    is_face_first = niche_id in ("recap_comedia", "fofoca_famosos", "facebook_fofoca", "tv_realities")
    vp_h_wide = 820 if is_face_first else 1040
    vp_y_wide = 445 if is_face_first else 315
    fg_y_bias = 0.28 if is_face_first else 0.40
    bg_brightness = -0.58 if is_face_first else -0.44
    bg_blur = "22:3" if is_face_first else "14:2"

    for i, (in_idx, sc) in enumerate(scene_entries):
        label = f"seg_{i:02d}"
        cur_dur = float(sc.get("dur", seg_dur))
        cur_frames = max(int(round(cur_dur * fps)), int(round((trans_dur + 0.9) * fps)))
        cur_dur = cur_frames / float(fps)
        cur_denom = max(1, cur_frames - 1)
        if sc["type"] == "video":
            vw = int(sc.get("w", 1920))
            vh = int(sc.get("h", 1080))
            if vh >= vw:
                # Clipe já é vertical ou quadrado -> tela cheia 9:16 direta + filtro visual
                filter_lines.append(
                    f"[{in_idx}:v]"
                    f"trim=start=0:duration={cur_dur:.3f},setpts=PTS-STARTPTS,"
                    f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={width}:{height},{color_vf},"
                    f"{norm_tail}[{label}]"
                )
            else:
                if height <= width + 100:
                    # Modo Quadrado 1:1 limpo (sem barras borradas, igual @highlightsmx!)
                    filter_lines.append(
                        f"[{in_idx}:v]"
                        f"trim=start=0:duration={cur_dur:.3f},setpts=PTS-STARTPTS,"
                        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                        f"crop={width}:{height}:(iw-ow)/2:(ih-oh)*0.45,{color_vf},"
                        f"{norm_tail}[{label}]"
                    )
                else:
                    # Clipe 16:9 HD -> enquadramento amplo inteligente (preserva ~65% da largura real, ZERO corte cego da ação!)
                    # Remove apenas 6% da borda inferior (tarjas) e posiciona viewport 1080x1140 nítido sobre fundo borrado 9:16
                    filter_lines.append(
                        f"[{in_idx}:v]"
                        f"trim=start=0:duration={cur_dur:.3f},setpts=PTS-STARTPTS,"
                        f"crop=iw:ih*0.94:0:0,fps={fps},settb=AVTB,format=yuv420p,setsar=1,split=2[braw_{i}][fraw_{i}]"
                    )
                    filter_lines.append(
                        f"[braw_{i}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                        f"boxblur={bg_blur},scale={width}:{height}:flags=bilinear,"
                        f"eq=brightness={bg_brightness}:saturation=0.85[bg_{i}]"
                    )
                    filter_lines.append(
                        f"[fraw_{i}]scale={width}:{vp_h_wide}:force_original_aspect_ratio=increase:flags=lanczos,"
                        f"crop={width}:{vp_h_wide}:(iw-ow)/2:(ih-oh)*{fg_y_bias},{color_vf}[fg_{i}]"
                    )
                    filter_lines.append(
                        f"[bg_{i}][fg_{i}]overlay=0:{vp_y_wide},"
                        f"drawbox=x=0:y={vp_y_wide - 2}:w={width}:h=2:color=white@0.22:t=fill,"
                        f"drawbox=x=0:y={vp_y_wide + vp_h_wide}:w={width}:h=2:color=white@0.22:t=fill,"
                        f"{norm_tail}[{label}]"
                    )
        else:
            # Foto HD sincronizada: zoom ultra-suave (3% máximo — nunca corta rostos nem bordas importantes!)
            z_expr = f"1.0+0.03*(on/{cur_denom})" if i % 2 == 0 else f"1.03-0.03*(on/{cur_denom})"
            filter_lines.append(
                f"[{in_idx}:v]"
                f"scale=1114:1980:force_original_aspect_ratio=increase:flags=lanczos,crop=1114:1980,"
                f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={cur_frames}:s={width}x{height}:fps={fps},"
                f"{color_vf},setpts=PTS-STARTPTS,{norm_tail}[{label}]"
            )
        segment_labels.append((label, cur_dur))

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

    if branding_idx is not None:
        filter_lines.append(f"[{branding_idx}:v]scale=185:-1,format=rgba,colorchannelmixer=aa=0.92[wm]")
        filter_lines.append(f"[{current_v}][wm]overlay=W-w-36:56[v_wm]")
        current_v = "v_wm"

    if scoreboard_idx is not None:
        filter_lines.append(f"[{current_v}][{scoreboard_idx}:v]overlay=0:0[v_score]")
        current_v = "v_score"

    if banner_idx is not None:
        fade_out_start = max(0.3, banner_dur - 0.30)
        filter_lines.append(
            f"[{banner_idx}:v]format=rgba,"
            f"fade=t=in:st=0:d=0.20:alpha=1,"
            f"fade=t=out:st={fade_out_start:.2f}:d=0.30:alpha=1[banner]"
        )
        filter_lines.append(
            f"[{current_v}][banner]overlay=0:0:eof_action=pass:enable='between(t,0,{banner_dur:.2f})'[v_banner]"
        )
        current_v = "v_banner"

    escaped_ass = escape_ffmpeg_filter_path(ass_path)
    # Legendas limpas com contorno forte (SEM faixa cinza atravessando o vídeo!) + aspect ratio exato (9:16 ou 1:1)
    filter_lines.append(
        f"[{current_v}]"
        f"ass=filename='{escaped_ass}',"
        f"scale={width}:{height},setsar=1,setdar={dar_expr},format=yuv420p[vout]"
    )

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
        "-aspect",
        aspect_str,
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
        f"[VideoEngine] Vídeo HD concluído! {output_mp4.name} ({size_mb:.1f} MB) + {output_txt.name}",
    )
    return output_mp4, output_txt


def render_original_cut_video(
    clip_path: Path,
    ass_path: Path,
    clip_duration: float,
    gancho_tela: str,
    article_title: str,
    article_source: str,
    article_url: str,
    descricao_tiktok: str,
    hashtags: list[str],
    transcript_excerpt: str,
    work_dir: Path,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, Path]:
    """
    Renderiza o Modo Cortador Viral (Áudio Original + Legenda — Alanzoka, MasterChef, Pesadelo):
    - Mantém 100% do áudio original do streamer/programa normalizado (loudnorm)
    - Viewport 1080x1080 nítido (zero zoom exagerado) sobre fundo atmosférico borrado
    - Card de gancho chamativo no topo (visível durante todo o corte para prender atenção!)
    - Legendas .ass sincronizadas com a fala original logo abaixo do viewport
    """
    video_cfg = config.get("video", {})
    fmt_choice = str(video_cfg.get("formato_tela", "auto")).strip().lower()
    if fmt_choice == "1x1":
        width, height, aspect_str, dar_expr = 1080, 1080, "1:1", "1/1"
    else:
        width = int(video_cfg.get("largura", 1080))
        height = int(video_cfg.get("altura", 1920))
        aspect_str = "9:16"
        dar_expr = "9/16"
    fps = int(video_cfg.get("fps", 30))

    niche_id = str(config.get("nicho_ativo", "tv_realities"))
    niche_name = str(config.get("nicho_nome", "Fábrica TK"))
    paths_dict = config.get("paths") or {}
    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]

    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Banner chamativo no topo (estilo cortes virais de MasterChef / Alanzoka)
    banner_png = work_dir / "cut_hook_banner.png"
    accent_map = {
        "gta_vi": (255, 46, 209),
        "virais_mundo": (249, 115, 22),
        "fofoca_famosos": (236, 72, 153),
        "facebook_fofoca": (59, 130, 246),
        "musica_gear": (245, 158, 11),
        "tv_realities": (250, 204, 21),
    }
    create_hook_banner_png(
        hook_text=gancho_tela,
        output_path=banner_png,
        club_label=niche_name,
        accent_rgb=accent_map.get(niche_id, (250, 204, 21)),
        width=width,
        height=height,
    )

    branding_path = _pick_random_asset(brand_dirs, (".png",))

    inputs = ["-i", str(clip_path), "-i", str(banner_png)]
    branding_idx: int | None = None
    if branding_path:
        inputs.extend(["-i", str(branding_path)])
        branding_idx = 2

    # Enquadramento limpo sem cortar rostos (y=430..1330, altura 900px) dentro das zonas seguras do TikTok:
    # - Topo seguro (y=188..385): card do gancho sem tampar cabeças
    # - Centro (y=430..1330): vídeo 16:9 amplo sem legendas originais da TV
    # - Base segura (y=1465): nossa legenda .ass travada 100% fora do vídeo
    vp_y = 430
    vp_h = 900
    escaped_ass = escape_ffmpeg_filter_path(ass_path)
    # Corta 18% do rodapé em TV & Realities (remove legendas embutidas da Band/MasterChef/Pesadelo)
    # e 12% nos demais cortes 16:9 para eliminar legendas/tarjas originais.
    filter_label, color_vf = get_video_filter_chain(video_cfg.get("filtro_cor", "vivido2"))
    _log(log_cb, f"[VideoEngine] Filtro visual (corte): {filter_label}")
    keep_ratio = 0.82 if niche_id == "tv_realities" else 0.88
    fg_filter = (
        f"[fraw]crop=iw:ih*{keep_ratio:.2f}:0:0,"
        f"scale={width}:{vp_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{vp_h},{color_vf}[fg]"
    )

    filter_lines = [
        f"[0:v]setpts=PTS-STARTPTS,fps={fps},settb=AVTB,format=yuv420p,setsar=1,split=2[braw][fraw]",
        f"[braw]crop=iw:ih*{keep_ratio:.2f}:0:0,"
        f"scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        f"boxblur=10:2,scale={width}:{height}:flags=bilinear,eq=brightness=-0.36:saturation=1.15[bg]",
        fg_filter,
        f"[bg][fg]overlay=0:{vp_y},"
        f"drawbox=x=0:y={vp_y - 3}:w={width}:h=3:color=white@0.28:t=fill,"
        f"drawbox=x=0:y={vp_y + vp_h}:w={width}:h=3:color=white@0.28:t=fill[v_base]",
        "[v_base][1:v]overlay=0:0[v_bn]",
    ]
    cur_v = "v_bn"
    if branding_idx is not None:
        filter_lines.append(f"[{branding_idx}:v]scale=150:-1,format=rgba,colorchannelmixer=aa=0.80[wm]")
        filter_lines.append(f"[{cur_v}][wm]overlay=W-w-36:48[v_wm]")
        cur_v = "v_wm"

    # Crava 1080x1920 9:16 exato (corrige display_aspect_ratio=480:853!)
    filter_lines.append(
        f"[{cur_v}]ass=filename='{escaped_ass}',scale={width}:{height},setsar=1,setdar=9/16,format=yuv420p[vout]"
    )
    filter_lines.append("[0:a]asetpts=PTS-STARTPTS,loudnorm=I=-15:TP=-1.5:LRA=11,alimiter=limit=0.95[aout]")

    filter_script_path = work_dir / "filter_cut.txt"
    filter_script_path.write_text(";\n".join(filter_lines), encoding="utf-8")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = slugify(gancho_tela or article_title, max_length=42)
    output_mp4 = output_dir / f"{timestamp}_corte_{base_slug}.mp4"
    output_txt = output_dir / f"{timestamp}_corte_{base_slug}.txt"

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
        f"{max(5.0, clip_duration):.3f}",
        "-r",
        str(fps),
        "-aspect",
        aspect_str,
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

    _log(log_cb, f"[VideoEngine] Renderizando Corte Viral HD (áudio original) -> {output_mp4.name}")

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
            if cur_time is not None and progress_cb and clip_duration > 0:
                progress_cb(max(0.0, min(1.0, cur_time / clip_duration)))
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
        raise RuntimeError(f"FFmpeg falhou no corte viral (código {ret}):\n" + "\n".join(stderr_tail[-12:]))

    save_post_metadata_txt(
        txt_path=output_txt,
        title=article_title,
        source=article_source,
        url=article_url,
        gancho_tela=gancho_tela,
        descricao_tiktok=descricao_tiktok,
        hashtags=hashtags,
        roteiro=transcript_excerpt,
    )
    if progress_cb:
        progress_cb(1.0)

    size_mb = output_mp4.stat().st_size / (1024 * 1024)
    _log(log_cb, f"[VideoEngine] Corte Viral concluído! {output_mp4.name} ({size_mb:.1f} MB) + {output_txt.name}")
    return output_mp4, output_txt
