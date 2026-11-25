"""Motor de renderização de vídeo vertical 1080x1920@30fps via FFmpeg."""

from __future__ import annotations

import json
import random
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from src import BRANDING_DIR, INTROS_DIR, MUSIC_DIR, OUTPUT_DIR, slugify

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]

XFADE_TRANSITIONS = [
    "fade",
    "wipeleft",
    "hblur",
    "wiperight",
    "smoothleft",
    "dissolve",
    "smoothright",
    "slideleft",
    "slideright",
]


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def escape_ffmpeg_filter_path(path: Path) -> str:
    """
    Escapa corretamente caminhos do Windows para uso em filtros libavfilter (ex.: ass=...).
    Converte '\\' em '/' e escapa ':', '\\'', '[', ']', ';', ','.
    """
    resolved = str(path.resolve()).replace("\\", "/")
    resolved = resolved.replace(":", r"\:")
    resolved = resolved.replace("'", r"\'")
    resolved = resolved.replace("[", r"\[")
    resolved = resolved.replace("]", r"\]")
    resolved = resolved.replace(";", r"\;")
    resolved = resolved.replace(",", r"\,")
    return resolved


def probe_media_duration(media_path: Path) -> float:
    """Retorna a duração em segundos de um arquivo de vídeo/áudio via ffprobe."""
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
    """Carrega fonte bold confiável do Windows com fallback seguro."""
    font_candidates = (
        [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\impact.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\calibrib.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
        if bold
        else [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
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
    """Quebra texto em linhas respeitando largura máxima em pixels."""
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
    club_label: str = "LIGA MX",
    width: int = 1080,
    height: int = 1920,
) -> Path:
    """
    Cria um banner PNG transparente de alto impacto para os 3 primeiros segundos (gancho_tela).
    Usar PNG com Pillow garante acentuação perfeita em espanhol e zero erro de fonte no Windows.
    """
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    title_font = _load_windows_font(size=60, bold=True)
    badge_font = _load_windows_font(size=30, bold=True)

    clean_hook = (hook_text or "¡ÚLTIMA HORA EN LA LIGA MX!").strip().upper()
    card_margin_x = 68
    card_w = width - (card_margin_x * 2)
    text_max_w = card_w - 84

    lines = _wrap_text_pixels(draw, clean_hook, title_font, text_max_w)
    if not lines:
        lines = ["¡ÚLTIMA HORA!"]

    line_heights: list[int] = []
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=title_font)
        line_heights.append(bb[3] - bb[1])

    line_spacing = 16
    total_text_h = sum(line_heights) + line_spacing * (len(lines) - 1)
    pad_top = 48
    pad_bottom = 42
    card_h = total_text_h + pad_top + pad_bottom

    card_x0 = card_margin_x
    card_y0 = 235
    card_x1 = card_x0 + card_w
    card_y1 = card_y0 + card_h

    # Sombra suave do cartão
    draw.rounded_rectangle(
        (card_x0 + 8, card_y0 + 12, card_x1 + 8, card_y1 + 12),
        radius=28,
        fill=(0, 0, 0, 150),
    )

    # Cartão principal escuro translúcido com borda amarela vibrante
    draw.rounded_rectangle(
        (card_x0, card_y0, card_x1, card_y1),
        radius=28,
        fill=(12, 17, 28, 226),
        outline=(250, 204, 21, 255),
        width=6,
    )

    # Faixa superior "ÚLTIMA HORA • CLUBE"
    badge_text = f"ÚLTIMA HORA  •  {club_label.upper()}"
    b_box = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (b_box[2] - b_box[0]) + 54
    badge_h = (b_box[3] - b_box[1]) + 22
    badge_x0 = (width - badge_w) // 2
    badge_y0 = card_y0 - (badge_h // 2) - 4
    badge_x1 = badge_x0 + badge_w
    badge_y1 = badge_y0 + badge_h

    draw.rounded_rectangle(
        (badge_x0, badge_y0, badge_x1, badge_y1),
        radius=16,
        fill=(220, 38, 38, 255),
        outline=(255, 255, 255, 220),
        width=3,
    )
    draw.text(
        ((badge_x0 + badge_x1) // 2, (badge_y0 + badge_y1) // 2),
        badge_text,
        font=badge_font,
        fill=(255, 255, 255, 255),
        anchor="mm",
    )

    # Texto principal do gancho centralizado
    cur_y = card_y0 + pad_top
    for line, lh in zip(lines, line_heights):
        cx = width // 2
        cy = cur_y + lh // 2
        # Contorno escuro para máxima legibilidade
        draw.text(
            (cx, cy),
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


def _pick_random_asset(folder: Path, extensions: tuple[str, ...]) -> Path | None:
    if not folder.exists():
        return None
    candidates = [
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def _parse_ffmpeg_time(line: str) -> float | None:
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not match:
        return None
    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return h * 3600 + m * 60 + s


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
    """Salva o arquivo .txt pronto para copiar e colar na postagem do TikTok."""
    hashtags_line = " ".join(hashtags)
    content = (
        f"=== LEGENDA PRONTA PARA TIKTOK ===\n"
        f"{descricao_tiktok}\n\n"
        f"{hashtags_line}\n\n"
        f"=== METADADOS ===\n"
        f"Gancho em tela: {gancho_tela}\n"
        f"Notícia original: {title}\n"
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
    Renderiza o vídeo final 1080x1920@30fps via FFmpeg com:
    - Intro opcional do Google Flow (primeiros 5s de assets/intros/)
    - Cortes dinâmicos (4–5s por imagem) com zoompan alternado (zoom-in / zoom-out)
    - Transições rápidas xfade (fade/wipe/blur/dissolve/slide)
    - Vinheta e escurecimento leve para leitura
    - Marca d'água opcional (assets/branding/*.png)
    - Banner de gancho_tela nos primeiros 3 segundos
    - Legendas .ass queimadas
    - Música de fundo aleatória de assets/music/ a ~10% volume (opcional)
    """
    if not image_paths:
        raise ValueError("Lista de imagens vazia para renderização.")

    video_cfg = config.get("video", {})
    width = int(video_cfg.get("largura", 1080))
    height = int(video_cfg.get("altura", 1920))
    fps = int(video_cfg.get("fps", 30))
    trans_dur = float(video_cfg.get("duracao_transicao", 0.35))
    banner_dur = float(video_cfg.get("duracao_banner_segundos", 3.0))
    intro_max_dur = float(video_cfg.get("duracao_intro_segundos", 5.0))
    music_vol = float(video_cfg.get("volume_musica", 0.10))
    usar_musica = bool(video_cfg.get("usar_musica", True))
    usar_intro = bool(video_cfg.get("usar_intro", True))
    club_focus = str(config.get("clube_foco", "LIGA MX"))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    total_target_dur = max(5.0, float(audio_duration) + 0.35)

    # Verifica assets opcionais
    intro_path: Path | None = None
    intro_dur = 0.0
    if usar_intro:
        cand_intro = _pick_random_asset(INTROS_DIR, (".mp4", ".mov", ".mkv", ".webm"))
        if cand_intro:
            probed = probe_media_duration(cand_intro)
            if probed >= 1.5:
                intro_path = cand_intro
                intro_dur = min(intro_max_dur, probed, max(2.0, total_target_dur * 0.25))
                _log(log_cb, f"[VideoEngine] Intro selecionada: {intro_path.name} ({intro_dur:.1f}s)")

    music_path: Path | None = None
    if usar_musica:
        music_path = _pick_random_asset(MUSIC_DIR, (".mp3", ".wav", ".m4a", ".ogg"))
        if music_path:
            _log(log_cb, f"[VideoEngine] Trilha de fundo: {music_path.name} (vol {int(music_vol * 100)}%)")
        else:
            _log(log_cb, "[VideoEngine] Nenhuma trilha em assets/music/. Renderizando só com narração.")

    branding_path = _pick_random_asset(BRANDING_DIR, (".png",))
    if branding_path:
        _log(log_cb, f"[VideoEngine] Marca d'água detectada: {branding_path.name}")

    # Gera PNG do banner gancho_tela
    banner_png = work_dir / "hook_banner.png"
    create_hook_banner_png(
        hook_text=gancho_tela,
        output_path=banner_png,
        club_label=club_focus,
        width=width,
        height=height,
    )

    # Calcula duração de cada imagem para sincronizar exatamente com a duração do áudio
    num_images = len(image_paths)
    num_segments = num_images + (1 if intro_path else 0)
    trans_dur = min(trans_dur, 0.5)

    raw_img_dur = (total_target_dur - intro_dur + (num_segments - 1) * trans_dur) / num_images
    img_frames = max(int(round(max(2.0, raw_img_dur) * fps)), int(round((trans_dur + 1.0) * fps)))
    img_dur = img_frames / float(fps)

    _log(
        log_cb,
        f"[VideoEngine] Compondo {num_segments} cenas "
        f"({img_dur:.2f}s por foto, transição {trans_dur:.2f}s, duração alvo {total_target_dur:.1f}s)...",
    )

    # Monta entradas do FFmpeg e índices
    inputs: list[str] = []
    idx_counter = 0

    intro_idx: int | None = None
    if intro_path:
        inputs.extend(["-i", str(intro_path)])
        intro_idx = idx_counter
        idx_counter += 1

    img_indices: list[int] = []
    for img_p in image_paths:
        inputs.extend(["-i", str(img_p)])
        img_indices.append(idx_counter)
        idx_counter += 1

    # Banner PNG em loop curto para permitir fade in/out suave
    inputs.extend(["-loop", "1", "-t", f"{banner_dur + 0.5:.2f}", "-i", str(banner_png)])
    banner_idx = idx_counter
    idx_counter += 1

    branding_idx: int | None = None
    if branding_path:
        inputs.extend(["-i", str(branding_path)])
        branding_idx = idx_counter
        idx_counter += 1

    # Narração TTS
    inputs.extend(["-i", str(audio_path)])
    voice_idx = idx_counter
    idx_counter += 1

    # Trilha musical opcional em loop
    music_idx: int | None = None
    if music_path:
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
        music_idx = idx_counter
        idx_counter += 1

    # Constrói filter_complex em arquivo separado (evita limite de linha de comando no Windows)
    filter_lines: list[str] = []
    segment_labels: list[tuple[str, float]] = []

    # 1) Intro opcional normalizada
    if intro_idx is not None:
        filter_lines.append(
            f"[{intro_idx}:v]"
            f"trim=start=0:duration={intro_dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"fps={fps},settb=AVTB,format=yuv420p,setsar=1[seg_intro]"
        )
        segment_labels.append(("seg_intro", intro_dur))

    # 2) Cenas de imagem com zoompan suave alternado (zoom-in / zoom-out)
    # Pré-escala leve (1.2x) garante movimento suave sem tremulação de arredondamento
    up_w = int(round(width * 1.2))
    up_h = int(round(height * 1.2))
    denom = max(1, img_frames - 1)

    for i, in_idx in enumerate(img_indices):
        if i % 2 == 0:
            # Zoom IN suave: 1.00 -> 1.12
            z_expr = f"1.0+0.12*(on/{denom})"
        else:
            # Zoom OUT suave: 1.12 -> 1.00
            z_expr = f"1.12-0.12*(on/{denom})"

        label = f"seg_{i:02d}"
        filter_lines.append(
            f"[{in_idx}:v]"
            f"scale={up_w}:{up_h}:flags=bicubic,"
            f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={img_frames}:s={width}x{height}:fps={fps},"
            f"setpts=PTS-STARTPTS,settb=AVTB,fps={fps},format=yuv420p,setsar=1[{label}]"
        )
        segment_labels.append((label, img_dur))

    # 3) Encadeia transições xfade rápidas
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

    # 4) Vinheta e leve escurecimento para melhorar contraste das legendas
    filter_lines.append(
        f"[{current_v}]vignette=PI/4.8,eq=brightness=-0.04:contrast=1.06:saturation=1.08[v_graded]"
    )
    current_v = "v_graded"

    # 5) Marca d'água opcional (assets/branding/*.png)
    if branding_idx is not None:
        filter_lines.append(
            f"[{branding_idx}:v]scale=170:-1,format=rgba,colorchannelmixer=aa=0.78[wm]"
        )
        filter_lines.append(
            f"[{current_v}][wm]overlay=W-w-36:96[v_wm]"
        )
        current_v = "v_wm"

    # 6) Banner do gancho_tela nos primeiros 3 segundos com fade suave
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

    # 7) Legendas .ass queimadas (com escape seguro para Windows)
    escaped_ass = escape_ffmpeg_filter_path(ass_path)
    filter_lines.append(
        f"[{current_v}]ass=filename='{escaped_ass}',format=yuv420p[vout]"
    )

    # 8) Mixagem de áudio (narração + música de fundo opcional em ~10%)
    if music_idx is not None:
        fade_out_audio = max(0.5, total_target_dur - 2.2)
        filter_lines.append(f"[{voice_idx}:a]volume=1.06,apad=pad_dur=0.35[voice]")
        filter_lines.append(
            f"[{music_idx}:a]atrim=0:{total_target_dur:.3f},asetpts=PTS-STARTPTS,"
            f"volume={music_vol:.3f},"
            f"afade=t=in:st=0:d=1.0,"
            f"afade=t=out:st={fade_out_audio:.3f}:d=2.2[bgm]"
        )
        filter_lines.append(
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
            "alimiter=limit=0.95[aout]"
        )
    else:
        filter_lines.append(
            f"[{voice_idx}:a]volume=1.06,apad=pad_dur=0.35,alimiter=limit=0.95[aout]"
        )

    filter_script_path = work_dir / "filter_complex.txt"
    with open(filter_script_path, "w", encoding="utf-8") as f:
        f.write(";\n".join(filter_lines))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = slugify(article_title, max_length=42)
    output_mp4 = OUTPUT_DIR / f"{timestamp}_{base_slug}.mp4"
    output_txt = OUTPUT_DIR / f"{timestamp}_{base_slug}.txt"

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
        "20",
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

    _log(log_cb, f"[VideoEngine] Iniciando renderização FFmpeg -> {output_mp4.name}")

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
                pct = max(0.0, min(1.0, cur_time / total_target_dur))
                progress_cb(pct)

        ret = proc.wait()
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise

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
        f"[VideoEngine] Vídeo concluído com sucesso! {output_mp4.name} ({size_mb:.1f} MB) + {output_txt.name}",
    )
    return output_mp4, output_txt
