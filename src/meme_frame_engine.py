"""Motor Moldura Página Viral (Estilo Tweet / NU GRAU 244 ✓ — Logo Redondo + Selo Azul + Frase + Vídeo).

Fórmula visual fiel:
1. Fundo branco limpo 9:16 (1080x1920) estilo post/página viral (ou dark opcional).
2. Topo: Avatar circular do canal (usa assets/branding/*.png/.jpg ou gera logo estilizado) +
   Nome do canal em negrito + Selo azul verificado (✓).
3. Frase/manchete viral curta e centralizada logo acima do vídeo (gerada pela IA Visão ou informada).
4. Vídeo HD centralizado com cantos arredondados suaves (944x1240), foco nos rostos/ação +
   filtro Vívido 2 + discreto "clica em seguir" na base do vídeo.
5. Aceita 3 fontes automaticamente:
   - Link YouTube / YouTube Shorts colado na interface;
   - Clipes .mp4 colocados na pasta nichos/<nicho>/assets/broll/;
   - Busca automática de Shorts/vídeos HD limpos para o sub-foco escolhido (ex.: Garotas Góticas / Goth Aesthetic).
"""

from __future__ import annotations

import base64
import json
import math
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src import (
    BRANDING_DIR,
    HISTORY_PATH,
    MUSIC_DIR,
    OUTPUT_DIR,
    load_history,
    normalize_text,
    slugify,
)
from src.llm_client import describe_frames_batch_gemini, generate_text_llm, is_gemini_active
from src.scraper import NewsArticle
from src.scriptwriter import ScriptPackage, _normalize_hashtags
from src.video_engine import (
    _generate_cinematic_bed,
    _load_windows_font,
    _parse_ffmpeg_time,
    _pick_random_asset,
    get_video_filter_chain,
    save_post_metadata_txt,
)
from src.video_fetcher import (
    _find_ytdlp,
    download_video_with_audio_hd,
    probe_video_info,
    ytdlp_base_args,
)
from src.youtube_transcript import _extract_yt_id, _probe_yt_metadata

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _has_audio_stream(video_path: Path) -> bool:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        return "audio" in out.lower()
    except Exception:
        return False


def _make_circular_avatar(logo_path: Path | None, channel_name: str, size: int = 138) -> Image.Image:
    """Cria avatar circular perfeito (usa logo do usuário se existir, ou gera emblema estilizado neon/dark)."""
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((2, 2, size - 3, size - 3), fill=255)

    if logo_path and logo_path.exists():
        try:
            raw = Image.open(logo_path).convert("RGBA")
            fitted = ImageOps.fit(raw, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            avatar.paste(fitted, (0, 0), mask)
            # Borda fina elegante
            draw = ImageDraw.Draw(avatar)
            draw.ellipse((2, 2, size - 3, size - 3), outline=(148, 163, 184, 180), width=3)
            return avatar
        except Exception:
            pass

    # Fallback: avatar estilizado estilo página viral (roxo escuro + neon violeta + iniciais)
    avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg = Image.new("RGBA", (size, size), (15, 10, 28, 255))
    bdraw = ImageDraw.Draw(bg)
    for r in range(size // 2, 0, -2):
        ratio = r / (size / 2)
        cr = int(124 * (1 - ratio) + 18 * ratio)
        cg = int(58 * (1 - ratio) + 10 * ratio)
        cb = int(237 * (1 - ratio) + 36 * ratio)
        bdraw.ellipse((size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r), fill=(cr, cg, cb, 255))

    initials = "".join(w[0].upper() for w in re.findall(r"[A-Za-zÁÉÍÓÚÃÕÇ0-9]+", channel_name)[:2]) or "TK"
    font = _load_windows_font(size=int(size * 0.36), bold=True)
    bdraw.text((size // 2, size // 2), initials, font=font, fill=(255, 255, 255, 255), anchor="mm")

    avatar.paste(bg, (0, 0), mask)
    odraw = ImageDraw.Draw(avatar)
    odraw.ellipse((3, 3, size - 4, size - 4), outline=(168, 85, 247, 255), width=4)
    return avatar


def _draw_verified_badge(canvas: Image.Image, cx: int, cy: int, radius: int = 19) -> None:
    """Desenha selo azul verificado estilo X/Instagram (círculo #1D9BF0 + check branco nítido)."""
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(29, 155, 240, 255))
    # Checkmark proporcional
    p1 = (cx - int(radius * 0.44), cy + int(radius * 0.02))
    p2 = (cx - int(radius * 0.12), cy + int(radius * 0.36))
    p3 = (cx + int(radius * 0.46), cy - int(radius * 0.34))
    draw.line([p1, p2, p3], fill=(255, 255, 255, 255), width=max(4, radius // 4), joint="curve")


def _wrap_headline(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.strip().split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        test = f"{cur} {w}"
        box = draw.textbbox((0, 0), test, font=font)
        if (box[2] - box[0]) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines[:2]


def create_meme_frame_overlay_png(
    channel_name: str,
    headline: str,
    logo_path: Path | None,
    output_path: Path,
    cta_footer: str = "clica em seguir",
    dark_mode: bool = False,
    width: int = 1080,
    height: int = 1920,
    vid_box: tuple[int, int, int, int] = (68, 485, 944, 1260),
    corner_radius: int = 24,
) -> Path:
    """
    Gera o overlay PNG transparente no centro (onde o vídeo aparece com cantos arredondados!)
    e moldura branca (ou escura) ao redor com:
    - Avatar circular do canal + Nome do canal + Selo verificado azul (✓)
    - Frase viral centralizada logo acima do vídeo
    - Texto discreto "clica em seguir" na base do vídeo
    """
    bg_rgba = (14, 17, 23, 255) if dark_mode else (255, 255, 255, 255)
    fg_rgba = (248, 250, 252, 255) if dark_mode else (15, 23, 42, 255)

    # 1) Fundo sólido da moldura + recorte transparente com cantos arredondados para a janela do vídeo!
    canvas = Image.new("RGBA", (width, height), bg_rgba)
    vx, vy, vw, vh = vid_box

    # Recorta a janela do vídeo deixando alpha=0 no centro com cantos arredondados suaves
    alpha_mask = Image.new("L", (width, height), 255)
    am_draw = ImageDraw.Draw(alpha_mask)
    am_draw.rounded_rectangle((vx, vy, vx + vw, vy + vh), radius=corner_radius, fill=0)
    canvas.putalpha(alpha_mask)

    draw = ImageDraw.Draw(canvas)
    # Borda ultrafina elegante ao redor do vídeo
    border_col = (51, 65, 85, 190) if dark_mode else (226, 232, 240, 255)
    draw.rounded_rectangle((vx - 2, vy - 2, vx + vw + 2, vy + vh + 2), radius=corner_radius + 2, outline=border_col, width=3)

    # 2) Cabeçalho no topo: [Avatar Redondo]  NOME DO CANAL [✓]
    avatar_sz = 136
    avatar = _make_circular_avatar(logo_path, channel_name, size=avatar_sz)
    name_font = _load_windows_font(size=40, bold=True)
    clean_name = (channel_name or "GÓTICAS BR").strip().upper()[:24]
    nb = draw.textbbox((0, 0), clean_name, font=name_font)
    name_w = nb[2] - nb[0]
    badge_r = 19
    gap_av = 22
    gap_badge = 16
    total_header_w = avatar_sz + gap_av + name_w + gap_badge + (badge_r * 2)

    hx = max(60, (width - total_header_w) // 2)
    header_cy = 235
    av_y = header_cy - avatar_sz // 2
    canvas.alpha_composite(avatar, (hx, av_y))

    name_x = hx + avatar_sz + gap_av
    draw.text((name_x, header_cy), clean_name, font=name_font, fill=fg_rgba, anchor="lm")
    _draw_verified_badge(canvas, name_x + name_w + gap_badge + badge_r, header_cy, radius=badge_r)

    # 3) Manchete / Frase viral centralizada acima do vídeo (y=340..465)
    clean_head = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]+", "", (headline or "")).strip()
    head_font = _load_windows_font(size=52, bold=True)
    lines = _wrap_headline(clean_head or "NINGUÉM RESISTE A ESSE ESTILO", head_font, max_w=width - 140, draw=draw)
    if len(lines) == 1:
        draw.text((width // 2, 410), lines[0], font=head_font, fill=fg_rgba, anchor="mm")
    else:
        draw.text((width // 2, 378), lines[0], font=head_font, fill=fg_rgba, anchor="mm")
        draw.text((width // 2, 438), lines[1], font=head_font, fill=fg_rgba, anchor="mm")

    # 4) Discreto "clica em seguir" na base interna do vídeo (igual ao print!)
    if cta_footer and cta_footer.strip():
        cta_font = _load_windows_font(size=34, bold=True)
        draw.text(
            (width // 2, vy + vh - 46),
            cta_footer.strip(),
            font=cta_font,
            fill=(255, 255, 255, 245),
            anchor="mm",
            stroke_width=4,
            stroke_fill=(0, 0, 0, 220),
        )

    canvas.save(output_path, format="PNG")
    return output_path


def _extract_quick_frames_b64(video_path: Path, work_dir: Path, dur: float, count: int = 4) -> list[str]:
    out: list[str] = []
    if dur <= 1.0:
        return out
    times = [max(0.5, dur * ((i + 0.5) / count)) for i in range(count)]
    for idx, t in enumerate(times):
        jpg = work_dir / f"mf_frame_{idx}.jpg"
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{t:.2f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=384:-2",
                    "-q:v",
                    "5",
                    str(jpg),
                ],
                capture_output=True,
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and jpg.exists() and jpg.stat().st_size > 1000:
                out.append(base64.b64encode(jpg.read_bytes()).decode("ascii"))
                jpg.unlink(missing_ok=True)
        except Exception:
            jpg.unlink(missing_ok=True)
    return out


def _generate_viral_headline(
    video_title: str,
    visual_desc: str,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> dict[str, Any]:
    """Gera frase viral curta pro topo da moldura + legenda + hashtags usando Gemini/Ollama."""
    foco = str(config.get("clube_foco") or "Garotas Góticas / Estilo Alt").strip()
    prompt = f"""Você é o social media de uma página viral brasileira de TikTok/Reels/Shorts no estilo 'moldura branca com logo + frase de impacto no topo' (tipo páginas de estética gótica, estilo, beleza e humor viral).
FOCO DA PÁGINA: {foco}
TÍTULO ORIGINAL DO CLIPE: "{video_title}"
O QUE APARECE VISUALMENTE NO VÍDEO: {visual_desc or video_title}

Crie uma frase CURTA, VICIANTEMENTE CHAMATIVA e 100% COERENTE com o que aparece no vídeo para colocar no topo da moldura (sem palavras explícitas/proibidas, 100% monetizável no TikTok/Reels/YouTube).
Retorne APENAS um JSON válido com estas 3 chaves:
- "frase_topo": string de 3 a 7 palavras em Português do Brasil EM CAIXA ALTA, estilo meme/página viral (ex.: "QUANDO A GÓTICA OLHA PRA VOCÊ", "ESSE ESTILO GÓTICO NUNCA ERRA", "BUGOU TODO MUNDO COM ESSE LOOK", "QUEM RESISTE A UMA GÓTICA ASSIM?")
- "descricao_tiktok": string (1 frase curta e envolvente SEM hashtags pedindo comentário ou curtida)
- "hashtags": array de 6 a 8 hashtags começando com #

Retorne APENAS o JSON:"""

    try:
        _log(log_cb, "[MolduraViral] Criando frase viral coerente com o clipe via IA...")
        raw = generate_text_llm(prompt=prompt, config=config, expect_json=True, temperature=0.72, max_tokens=600, timeout=45)
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
        s, e = cleaned.find("{"), cleaned.rfind("}")
        return json.loads(cleaned[s : e + 1])
    except Exception:
        return {
            "frase_topo": "ESSE ESTILO NUNCA ERRA",
            "descricao_tiktok": "Comenta de 0 a 10 o que achou desse visual!",
            "hashtags": _normalize_hashtags([], config),
        }


def _discover_short_clip_url(
    config: dict[str, Any],
    exclude_urls: set[str] | None = None,
    log_cb: LogCallback | None = None,
) -> tuple[str, str, str, int]:
    """Busca automaticamente um YouTube Short / vídeo HD limpo (10–55s) para o sub-foco ativo."""
    paths_dict = config.get("paths") or {}
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    seen_ids: set[str] = set()
    for item in load_history(history_path):
        vid = _extract_yt_id(item.get("url", "") if isinstance(item, dict) else str(item))
        if vid:
            seen_ids.add(vid)
    for u in exclude_urls or set():
        vid = _extract_yt_id(u)
        if vid:
            seen_ids.add(vid)

    foco_norm = normalize_text(str(config.get("clube_foco") or ""))
    if "gotica" in foco_norm or "goth" in foco_norm or "alt" in foco_norm:
        queries = [
            "goth girl aesthetic outfit transition #shorts",
            "garota gotica estilo look maquiagem #shorts",
            "goth fashion makeup transformation #shorts",
        ]
    elif "grau" in foco_norm or "moto" in foco_norm or "carro" in foco_norm:
        queries = [
            "moto grau humor reacao #shorts",
            "supercar exhaust reaction #shorts",
        ]
    elif "humor" in foco_norm or "reac" in foco_norm:
        queries = [
            "humor brasileiro reacao engraçada #shorts",
            "pegadinha engraçada reacao #shorts",
        ]
    else:
        queries = [
            "fashion beauty aesthetic outfit transition #shorts",
            "estilo moda beleza look viral #shorts",
        ]

    ytdlp = _find_ytdlp()
    bad_tokens = ("compilation", "1 hour", "playlist", "podcast", "ao vivo", "live", "gameplay", "minecraft", "roblox", "anime edit")

    for q in queries:
        _log(log_cb, f"[MolduraViral] Buscando clipe curto HD: '{q}'...")
        try:
            out = subprocess.check_output(
                [ytdlp, "--flat-playlist", "--dump-single-json", "--no-warnings", f"ytsearch15:{q}"],
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            entries = json.loads(out).get("entries") or []
        except Exception:
            continue

        scored: list[tuple[int, str, str, str, int]] = []
        for pos, e in enumerate(entries):
            vid_id = str(e.get("id") or "")
            dur = int(e.get("duration") or 0)
            title = str(e.get("title") or "Short Viral")
            ch = str(e.get("channel") or "YouTube")
            norm_t = normalize_text(title)
            if not vid_id or vid_id in seen_ids:
                continue
            if dur < 8 or dur > 65:
                continue
            if any(b in norm_t for b in bad_tokens):
                continue
            sc = max(1, 15 - pos)
            if 10 <= dur <= 35:
                sc += 6
            if (e.get("view_count") or 0) >= 50_000:
                sc += 5
            scored.append((sc, f"https://www.youtube.com/watch?v={vid_id}", title, ch, dur))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            _, url, title, ch, dur = scored[0]
            _log(log_cb, f"[MolduraViral] Clipe selecionado ({ch}): '{title}' ({dur}s)")
            return url, title, ch, dur

    raise RuntimeError("Nenhum clipe curto HD encontrado automaticamente. Cole um link do YouTube/Shorts ou jogue clipes em assets/broll/!")


def run_moldura_viral_pipeline(
    config: dict[str, Any],
    work_dir: Path,
    exclude_urls: set[str] | None = None,
    custom_url: str = "",
    headline_override: str = "",
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, Path, NewsArticle, ScriptPackage]:
    """Executa o fluxo completo da Moldura Página Viral (Logo + Selo Azul ✓ + Frase + Vídeo)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    paths_dict = config.get("paths") or {}
    niche_id = str(config.get("nicho_ativo", "moldura_viral"))
    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]
    broll_dir = Path(paths_dict["broll_dir"]) if paths_dict.get("broll_dir") else None
    music_dirs = [Path(paths_dict["music_dir"]), MUSIC_DIR] if paths_dict.get("music_dir") else [MUSIC_DIR]
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Fonte do clipe: (A) Link colado -> (B) Clipe local em assets/broll/ -> (C) Busca automática HD
    src_video: Path | None = None
    src_url = ""
    src_title = ""
    src_channel = ""

    if custom_url and custom_url.strip().startswith("http"):
        src_url = custom_url.strip()
        src_title, src_channel, _ = _probe_yt_metadata(src_url)
        _log(log_cb, f"[MolduraViral] Baixando link informado: '{src_title}' ({src_channel})...")
        src_video = download_video_with_audio_hd(src_url, work_dir / "source_clip.mp4", timeout=150, log_cb=log_cb)
        if not src_video:
            raise RuntimeError(f"Não foi possível baixar o vídeo informado: {src_url}")
    else:
        # Checa se o usuário jogou clipes locais em assets/broll/ ainda não postados!
        if broll_dir and broll_dir.exists():
            seen_urls = {str(item.get("url", "")) for item in load_history(history_path) if isinstance(item, dict)}
            if exclude_urls:
                seen_urls |= set(exclude_urls)
            local_clips = sorted(
                p for p in broll_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and f"local://{p.name}" not in seen_urls
            )
            if local_clips:
                chosen = local_clips[0]
                src_video = chosen
                src_url = f"local://{chosen.name}"
                src_title = chosen.stem.replace("_", " ").replace("-", " ")
                src_channel = "Pasta Local (broll)"
                _log(log_cb, f"[MolduraViral] Usando clipe local da pasta broll: {chosen.name}")

        if src_video is None:
            src_url, src_title, src_channel, _ = _discover_short_clip_url(config, exclude_urls=exclude_urls, log_cb=log_cb)
            src_video = download_video_with_audio_hd(src_url, work_dir / "source_clip.mp4", timeout=150, log_cb=log_cb)
            if not src_video:
                raise RuntimeError(f"Falha ao baixar clipe HD: {src_url}")

    vid_dur, vw, vh = probe_video_info(src_video)
    if vid_dur < 3.0:
        raise RuntimeError("Clipe muito curto para montar a moldura viral.")
    # Duração ideal de Short/Reel viral: até 45s (mantém o clipe inteiro se já for curto!)
    target_dur = min(vid_dur, 45.0)
    if progress_cb:
        progress_cb(0.25)

    # 2) IA Visão observa o clipe para criar uma frase viral 100% coerente (se não foi digitada manualmente!)
    headline = (headline_override or "").strip()
    visual_desc = ""
    if not headline and is_gemini_active(config):
        frames_b64 = _extract_quick_frames_b64(src_video, work_dir, target_dur, count=4)
        if frames_b64:
            try:
                descs = describe_frames_batch_gemini(frames_b64, config, niche_hint=str(config.get("clube_foco") or "estética viral"))
                visual_desc = " | ".join(descs)
                _log(log_cb, f"[MolduraViral] IA Visão leu o vídeo: {visual_desc[:120]}...")
            except Exception:
                pass

    meta = _generate_viral_headline(src_title, visual_desc, config, log_cb=log_cb)
    if not headline:
        headline = str(meta.get("frase_topo") or "ESSE ESTILO NUNCA ERRA").strip()
    descricao = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", str(meta.get("descricao_tiktok") or src_title)).strip()
    hashtags = _normalize_hashtags(meta.get("hashtags"), config)

    # 3) Gera PNG da Moldura (Fundo Branco + Logo Redondo + Nome do Canal + Selo Azul ✓ + Frase + "clica em seguir")
    channel_name = str(config.get("canal_nome") or config.get("nicho_etiqueta") or "GÓTICAS BR").split("•")[0].strip()
    dark_mode = bool(config.get("moldura_escura", False))
    logo_path = _pick_random_asset(brand_dirs, (".png", ".jpg", ".jpeg", ".webp"))
    _log(
        log_cb,
        f"[MolduraViral] Montando moldura {'escura' if dark_mode else 'branca'} | Canal: '{channel_name} ✓' | "
        f"Logo: {logo_path.name if logo_path else 'avatar automático'} | Frase: '{headline}'",
    )

    width, height = 1080, 1920
    vid_box = (68, 485, 944, 1260)  # Janela ampla central com cantos arredondados
    overlay_png = work_dir / "meme_frame_overlay.png"
    create_meme_frame_overlay_png(
        channel_name=channel_name,
        headline=headline,
        logo_path=logo_path,
        output_path=overlay_png,
        cta_footer="clica em seguir",
        dark_mode=dark_mode,
        width=width,
        height=height,
        vid_box=vid_box,
        corner_radius=24,
    )
    if progress_cb:
        progress_cb(0.45)

    # 4) Render FFmpeg: vídeo centralizado nítido (foco nos rostos!) + filtro Vívido 2 + overlay da moldura!
    video_cfg = config.get("video", {})
    fps = int(video_cfg.get("fps", 30))
    filter_label, color_vf = get_video_filter_chain(video_cfg.get("filtro_cor", "vivido2"))
    _log(log_cb, f"[MolduraViral] Filtro visual: {filter_label} | Duração: {target_dur:.1f}s")

    has_audio = _has_audio_stream(src_video)
    music_path: Path | None = None
    if not has_audio:
        music_path = _pick_random_asset(music_dirs, (".mp3", ".wav", ".m4a", ".ogg"))
        if not music_path:
            music_path = _generate_cinematic_bed(work_dir, niche_id=niche_id)

    vx, vy, vw_box, vh_box = vid_box
    inputs = ["-i", str(src_video), "-i", str(overlay_png)]
    if music_path is not None:
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])

    # Enquadramento inteligente dentro da janela 944x1260 com foco no terço superior (rostos sempre inteiros!)
    filter_lines = [
        f"color=c={'0x0E1117' if dark_mode else 'white'}:s={width}x{height}:r={fps}:d={target_dur:.3f}[bg]",
        (
            f"[0:v]trim=start=0:duration={target_dur:.3f},setpts=PTS-STARTPTS,fps={fps},"
            f"scale={vw_box}:{vh_box}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={vw_box}:{vh_box}:(iw-ow)/2:(ih-oh)*0.24,{color_vf},format=yuv420p[vid_fit]"
        ),
        f"[bg][vid_fit]overlay={vx}:{vy}:shortest=1[composed]",
        f"[composed][1:v]overlay=0:0,scale={width}:{height},setsar=1,setdar=9/16,format=yuv420p[vout]",
    ]
    if has_audio:
        filter_lines.append(
            f"[0:a]atrim=start=0:duration={target_dur:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo,alimiter=limit=0.95[aout]"
        )
    else:
        fade_out_a = max(0.3, target_dur - 1.0)
        filter_lines.append(
            f"[2:a]atrim=start=0:duration={target_dur:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo,volume=0.35,"
            f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out_a:.3f}:d=1.0[aout]"
        )

    filter_path = work_dir / "filter_moldura_viral.txt"
    filter_path.write_text(";\n".join(filter_lines), encoding="utf-8")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = slugify(headline or src_title, max_length=42)
    output_mp4 = output_dir / f"{ts}_moldura_{base_slug}.mp4"
    output_txt = output_dir / f"{ts}_moldura_{base_slug}.txt"

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        *inputs,
        "-/filter_complex",
        str(filter_path),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        f"{target_dur:.3f}",
        "-r",
        str(fps),
        "-aspect",
        "9:16",
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

    _log(log_cb, f"[MolduraViral] Renderizando FFmpeg -> nichos/{niche_id}/output/{output_mp4.name} ({target_dur:.1f}s)")
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
                proc.wait(timeout=5)
                raise RuntimeError("Renderização cancelada pelo usuário.")
            stripped = line.strip()
            if stripped:
                stderr_tail.append(stripped)
                if len(stderr_tail) > 20:
                    stderr_tail.pop(0)
            cur_t = _parse_ffmpeg_time(stripped)
            if cur_t is not None and progress_cb and target_dur > 0:
                progress_cb(0.45 + 0.55 * max(0.0, min(1.0, cur_t / target_dur)))
        ret = proc.wait()
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    if ret != 0 or not output_mp4.exists() or output_mp4.stat().st_size < 10_000:
        raise RuntimeError(f"FFmpeg falhou na Moldura Viral (código {ret}):\n" + "\n".join(stderr_tail[-10:]))

    save_post_metadata_txt(
        txt_path=output_txt,
        title=src_title,
        source=f"Moldura Viral • {channel_name}",
        url=src_url,
        gancho_tela=headline,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        roteiro=f"{channel_name} ✓ | {headline}",
    )
    if progress_cb:
        progress_cb(1.0)

    size_mb = output_mp4.stat().st_size / (1024 * 1024)
    _log(log_cb, f"[MolduraViral] Vídeo concluído! {output_mp4.name} ({size_mb:.1f} MB, {target_dur:.1f}s) + {output_txt.name}")

    article = NewsArticle(title=src_title, url=src_url, source=f"Moldura Viral • {src_channel}")
    script = ScriptPackage(
        roteiro=f"{headline} — {descricao}",
        gancho_tela=headline,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_count=len(headline.split()),
    )
    return output_mp4, output_txt, article, script
