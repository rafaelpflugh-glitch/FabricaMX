"""Motor de Histórias Virais Longas para YouTube 16:9 (Fórmula 'Hypados' / Emoções e Destinos)
+ Gerador Automático de Thumbnail High-CTR (1280x720) + Teaser Vertical 9:16 (62s para TikTok/Shorts).

Fórmula decifrada e implementada:
1. Título de 4 partes: [Humilhação entre aspas] + [Vilão] + [Salto temporal exato] + [Reviravolta em suspense].
2. Narrativa em 1ª pessoa hiper-realista (nome, idade, cidade/bairro real, diálogos humanos):
   - Ato 1 (Cold Open): abre direto na cena do título, joga a frase misteriosa e segura a revelação.
   - Atos 2–4 (Queda, Semente e Construção Silenciosa): tensão real e empatia.
   - Ato 5 (Clímax e Revelação): volta à cena da abertura e entrega a reviravolta emocionante (tratar o mal com o bem).
   - Ato 6 (Reflexão humana + CTA de Hype e Comentários): "De onde você está assistindo? Clica em Hypar!"
3. Áudio calmo e natural (velocidade 0%, pausas dramáticas entre capítulos) + trilha de piano/cordas suave (-24dB).
4. Vídeo cinematográfico 16:9 (1920x1080@30fps): cenas HD com Ken Burns contemplativo, vinheta escura,
   logo circular do canal no canto superior esquerdo e selo "HISTÓRIA DE HOJE: Nome / Cidade" no canto superior direito.
5. Thumbnail 1280x720 High-CTR (estilo 141K views): split foto dramática à direita + faixas amarela e ciano à esquerda!
6. Teaser 9:16 opcional (primeiros ~62s do Ato 1) pronto para TikTok/Reels/Shorts puxando público pro vídeo longo!
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import struct
import subprocess
import threading
import wave
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src import BRANDING_DIR, HISTORY_PATH, MUSIC_DIR, OUTPUT_DIR, load_history, normalize_text, slugify
from src.llm_client import generate_text_llm
from src.scraper import NewsArticle
from src.scriptwriter import ScriptPackage, _clean_script_text, _parse_json_response
from src.tts_subtitles import WordTiming, _synthesize_edge_tts, build_tiktok_ass, normalize_text_for_tts, probe_audio_duration
from src.video_engine import (
    _load_windows_font,
    _parse_ffmpeg_time,
    _pick_random_asset,
    escape_ffmpeg_filter_path,
    get_video_filter_chain,
)

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]


@dataclass
class StoryRenderResult:
    article: NewsArticle
    script: ScriptPackage
    mp4_path: Path
    txt_path: Path
    thumb_path: Path
    short_mp4_path: Path | None = None


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _format_ts(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _pick_story_premise(config: dict[str, Any], exclude_urls: set[str] | None = None) -> tuple[str, str, str]:
    """Seleciona uma premissa ainda não produzida do banco ou usa tema/foco customizado da interface."""
    paths_dict = config.get("paths") or {}
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    history = load_history(history_path)
    seen = {str(h.get("url", "")).strip().lower() for h in history if isinstance(h, dict)}
    if exclude_urls:
        seen.update(u.strip().lower() for u in exclude_urls)

    custom_theme = str(config.get("placar_opcional") or "").strip()
    if len(custom_theme) >= 12:
        slug = slugify(custom_theme[:40])
        return f"story://{slug}_{datetime.now().strftime('%H%M%S')}", custom_theme, "Brasil"

    bank = list(config.get("temas_virais_banco") or [])
    for item in bank:
        slug = str(item.get("slug") or slugify(str(item.get("premissa", ""))[:35]))
        url = f"story://{slug}"
        if url.lower() not in seen:
            return url, str(item.get("premissa") or ""), str(item.get("cenario") or "São Paulo/SP")

    foco = str(config.get("clube_foco") or "Humilhação → Reviravolta Silenciosa")
    slug = f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return (
        f"story://{slug}",
        f"Crie uma história inédita e ultra emocionante no subtema '{foco}', com um trabalhador brasileiro humilde sendo humilhado e dando a volta por cima com dignidade e bondade.",
        "São Paulo/SP",
    )


def _generate_story_bible(
    premise: str,
    cenario: str,
    target_minutes: int,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> dict[str, Any]:
    """Etapa 1: Gera a bíblia completa da história (título viral, textos da thumbnail, 5 capítulos e cenas visuais)."""
    idioma = str(config.get("idioma", "pt-BR")).lower()
    lang_label = (
        "ESPAÑOL LATINOAMERICANO natural y emotivo"
        if idioma.startswith("es")
        else ("NATURAL EMOTIONAL AMERICAN ENGLISH" if idioma.startswith("en") else "PORTUGUÊS DO BRASIL coloquial, humano e emocionante")
    )
    total_words = max(750, min(4200, int(target_minutes * 155)))
    num_chapters = 5 if target_minutes >= 7 else 4
    words_per_ch = max(180, total_words // num_chapters)

    prompt = f"""Você é um roteirista campeão de retenção no YouTube (especialista no formato 'Hypados' de histórias emocionantes em 1ª pessoa com 140K+ views).
Crie o PLANO ESTRUTURAL (bíblia) para uma história inédita baseada nesta premissa:
PREMISSA: {premise}
CENÁRIO BASE: {cenario}
IDIOMA: {lang_label}
DURAÇÃO ALVO: ~{target_minutes} minutos (~{total_words} palavras divididas em {num_chapters} capítulos).

REGRAS DA FÓRMULA VIRAL (OBRIGATÓRIAS):
1. "titulo_viral": Siga EXATAMENTE o padrão de altíssimo CTR:
   [Frase curta de humilhação entre aspas ou ato de bondade] + [vilão/personagem] + [salto temporal exato: ex. '6 anos depois', '5 dias depois'] + [gancho de reviravolta em suspense].
   Máximo 95 caracteres!
2. "titulos_alternativos": 2 variações ainda mais curiosas para teste A/B.
3. "thumb_topo": 1 frase curta em CAIXA ALTA (4 a 7 palavras) contextualizando (ex.: "6 ANOS DEPOIS, NO AEROPORTO, ELA RIU:").
4. "thumb_amarelo": 1 fala curta de impacto/humilhação entre aspas EM CAIXA ALTA (5 a 9 palavras, ex.: "“VIROU UBER? SABIA QUE IA FRACASSAR!”").
5. "thumb_ponte": 1 frase curta de transição EM CAIXA ALTA (3 a 6 palavras, ex.: "ELE PAROU O CARRO E DISSE:").
6. "thumb_ciano": 1 fala curta de suspense/reviravolta entre aspas EM CAIXA ALTA (5 a 8 palavras, ex.: "“OLHA PRA FORA… VÊ QUEM TÁ ALI.”").
7. "protagonista_nome": Primeiro nome brasileiro comum e realista (ex.: "Wagner", "Rogério", "Sônia", "Antônio").
8. "personagens_fixos": string curta listando TODOS os personagens principais com nome e papel (ex.: "Protagonista: Cícero (42, faxineiro); Vilão: Dr. Maurício (diretor arrogante); Aliado: Dona Tereza") — esses nomes NUNCA mudam entre capítulos!
9. "cidade_uf": Cidade e estado real do Brasil (ex.: "São Paulo/SP", "Curitiba/PR", "Belo Horizonte/MG").
10. "thumb_foto_query": Busca curta EM INGLÊS (5-8 palavras) para uma FOTOGRAFIA REAL do cotidiano moderno (ex.: "serious doctor hospital hallway real photography", "bearded man driving car everyday portrait photography") — NUNCA use palavras de terror, fantasia ou cinema medieval!
11. "capitulos": Exatamente {num_chapters} capítulos na ordem:
    - Capítulo 1: COLD OPEN (começa DIRETO na cena do título, entrega a humilhação + frase misteriosa e segura a revelação: 'Mas para entender o que aconteceu, você precisa saber quem eu era antes...')
    - Capítulos do meio: Origem simples/realista, a queda/injustiça, o recomeço duro e a virada silenciosa com personagens secundários humanos.
    - Último capítulo: Volta exatamente à cena do título, ENTREGA A REVELAÇÃO EMOCIONANTE (tratando o mal com o bem / dignidade) e termina com reflexão calorosa.
    Cada capítulo deve ter:
    - "numero": inteiro (1..{num_chapters})
    - "titulo": título curto do capítulo (ex.: "O Reencontro no Aeroporto")
    - "resumo": 2 frases explicando exatamente o que acontece nesse capítulo
    - "palavras_alvo": inteiro (em torno de {words_per_ch})
    - "visual_queries": array de 3 buscas curtas EM INGLÊS (4-7 palavras cada, estilo fotografia realista cotidiana moderna horizontal, SEM texto) representando cenas concretas desse capítulo.
12. "descricao_youtube": 3 parágrafos curtos emocionantes começando com "De onde você está assistindo? Me conta nos comentários! 👇 Assista até o final." (SEM spoilers do final!).
13. "hashtags": array de 6 hashtags começando com #.

Responda APENAS com um JSON válido contendo essas chaves:"""

    _log(log_cb, f"[StoryEngine] Criando bíblia da história (~{target_minutes} min, {num_chapters} capítulos) via Gemini...")
    raw = generate_text_llm(prompt=prompt, config=config, expect_json=True, temperature=0.82, max_tokens=4096, timeout=90)
    return _parse_json_response(raw)


def _write_story_chapter(
    bible: dict[str, Any],
    chapter: dict[str, Any],
    chapter_idx: int,
    total_chapters: int,
    previous_tail: str,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> str:
    """Etapa 2: Escreve cada capítulo em prosa fluida de 1ª pessoa mantendo continuidade perfeita."""
    idioma = str(config.get("idioma", "pt-BR")).lower()
    lang_label = (
        "ESPAÑOL LATINOAMERICANO natural"
        if idioma.startswith("es")
        else ("NATURAL AMERICAN ENGLISH" if idioma.startswith("en") else "PORTUGUÊS DO BRASIL coloquial e humano")
    )
    canal_nome = str(config.get("canal_nome") or "Destino & Virada")
    palavras_alvo = int(chapter.get("palavras_alvo") or 260)
    is_first = chapter_idx == 0
    is_last = chapter_idx == (total_chapters - 1)

    extra_rule = ""
    if is_first:
        extra_rule = (
            "ESTE É O CAPÍTULO 1 (COLD OPEN): comece IMEDIATAMENTE na fala de impacto do título (SEM 'olá pessoal', SEM introdução de canal!). "
            "Depois de jogar a frase misteriosa, faça a ponte: 'Mas para você entender o que ela/ele viu, você precisa saber quem eu era antes...' e apresente nome, idade e cidade do protagonista."
        )
    elif is_last:
        extra_rule = (
            "ESTE É O CAPÍTULO FINAL: entregue a REVELAÇÃO COMPLETA e emocionante do segredo guardado desde o início, mostrando a reação de choque e vergonha do vilão diante da dignidade e bondade do protagonista. "
            f"Nos últimos 2 parágrafos, saia da 1ª pessoa como narrador caloroso do canal '{canal_nome}', faça uma reflexão humana sincera sobre caráter e bondade, pergunte 'Qual é a sua opinião? De onde você está assistindo?' e peça com carinho para clicar em HYPAR o vídeo, curtir e se inscrever no canal!"
        )
    else:
        extra_rule = (
            "Continue de forma 100% fluida a partir da última frase do capítulo anterior, sem repetir o que já foi contado, "
            "construindo diálogos naturais, detalhes cotidianos reais (nomes de lugares, valores, objetos, sentimentos) e crescendo a tensão para o próximo capítulo."
        )

    prompt = f"""Você está escrevendo o CAPÍTULO {chapter_idx + 1} de {total_chapters} da história viral "{bible.get('titulo_viral', '')}".
Protagonista: {bible.get('protagonista_nome', 'Wagner')} ({bible.get('cidade_uf', 'São Paulo/SP')}).
PERSONAGENS FIXOS (MANTENHA OS NOMES EXATAMENTE IGUAIS, NUNCA TROQUE O NOME DE NINGUÉM!): {bible.get('personagens_fixos', '')}
Título do capítulo: {chapter.get('titulo', '')}
O que DEVE acontecer neste capítulo: {chapter.get('resumo', '')}
{f'ÚLTIMAS FRASES DO CAPÍTULO ANTERIOR (continue daqui sem repetir): "{previous_tail}"' if previous_tail else ''}

REGRAS ESTRITAS:
1. Idioma: {lang_label}.
2. Extensão OBRIGATÓRIA: escreva entre {max(170, int(palavras_alvo * 0.9))} e {int(palavras_alvo * 1.15)} palavras corridas prontas para narração em voz alta!
3. Estilo: 1ª pessoa íntima, frases naturais de conversa humana (misture frases curtas de impacto com momentos descritivos envolventes). ZERO marcações de cena, ZERO colchetes, ZERO asteriscos, ZERO emojis, ZERO títulos escritos no texto.
4. {extra_rule}

Responda APENAS com um JSON válido: {{"texto": "prosa corrida do capítulo aqui..."}}"""

    _log(log_cb, f"[StoryEngine] Escrevendo Capítulo {chapter_idx + 1}/{total_chapters}: '{chapter.get('titulo', '')}' (~{palavras_alvo} palavras)...")
    raw = generate_text_llm(prompt=prompt, config=config, expect_json=True, temperature=0.80, max_tokens=4096, timeout=90)
    data = _parse_json_response(raw)
    txt = _clean_script_text(str(data.get("texto") or ""))
    if not txt:
        raise RuntimeError(f"Capítulo {chapter_idx + 1} retornou texto vazio.")
    return txt


def _fetch_widescreen_photo(query: str, out_path: Path, used_urls: set[str]) -> Path | None:
    """Busca 1 foto horizontal HD limpa (sem marca d'água e sem artes de terror/fantasia) via DDGS."""
    if DDGS is None:
        return None
    bad_tokens = (
        "shutterstock", "gettyimages", "istockphoto", "alamy", "dreamstime", "123rf",
        "depositphotos", "vecteezy", "pinterest", "vampire", "nosferatu", "horror",
        "halloween", "zombie", "anime", "cartoon", "cosplay", "medieval", "gothic",
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query=f"{query} realistic modern everyday photography", max_results=14, safesearch="moderate"))
    except Exception:
        return None

    # Prioriza imagens horizontais (w >= h) em alta resolução
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in results:
        url = str(r.get("image") or "").strip()
        title_low = str(r.get("title") or "").lower()
        check_blob = f"{url.lower()} {title_low}"
        if not url or url in used_urls or any(bd in check_blob for bd in bad_tokens):
            continue
        w = int(r.get("width") or 0)
        h = int(r.get("height") or 0)
        if w < 700 or h < 420:
            continue
        ratio = w / max(1, h)
        score = w + (600 if 1.25 <= ratio <= 2.1 else 0)
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    for _, r in scored[:6]:
        url = str(r.get("image") or "").strip()
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code != 200 or len(resp.content) < 25_000:
                continue
            im = Image.open(BytesIO(resp.content)).convert("RGB")
            iw, ih = im.size
            if iw < 640 or ih < 380:
                continue
            # Enquadra limpo 1920x1080 preservando foco superior-central (rostos)
            target_ratio = 16.0 / 9.0
            cur_ratio = iw / float(ih)
            if cur_ratio > target_ratio:
                new_w = int(ih * target_ratio)
                x0 = (iw - new_w) // 2
                im = im.crop((x0, 0, x0 + new_w, ih))
            else:
                new_h = int(iw / target_ratio)
                y0 = max(0, int((ih - new_h) * 0.25))
                im = im.crop((0, y0, iw, min(ih, y0 + new_h)))
            im = im.resize((1920, 1080), Image.Resampling.LANCZOS)
            im = ImageEnhance.Contrast(im).enhance(1.06)
            im = ImageEnhance.Color(im).enhance(1.05)
            im.save(out_path, format="JPEG", quality=93, subsampling=0)
            used_urls.add(url)
            return out_path
        except Exception:
            continue
    return None


def _generate_fallback_cinematic_scene(out_path: Path, seed: int) -> Path:
    """Gera fundo cinematográfico quente caso alguma busca de foto falhe."""
    palettes = [
        ((14, 22, 38), (48, 32, 24)),
        ((24, 18, 34), (56, 38, 28)),
        ((16, 28, 36), (34, 48, 56)),
        ((28, 20, 22), (62, 42, 30)),
    ]
    c1, c2 = palettes[seed % len(palettes)]
    im = Image.new("RGB", (1920, 1080), c1)
    draw = ImageDraw.Draw(im)
    for y in range(1080):
        t = y / 1079.0
        col = tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
        draw.line([(0, y), (1920, y)], fill=col)
    im.save(out_path, format="JPEG", quality=90)
    return out_path


def _generate_emotional_piano_bed(work_dir: Path) -> Path:
    """Sintetiza trilha emocional lenta e calorosa (piano/pad em Lá menor) livre de direitos autorais."""
    wav_path = work_dir / "emotional_story_bed.wav"
    sr = 22050
    bar_dur = 4.0
    chords = [
        (110.00, 220.00, 261.63, 329.63),  # Am
        (87.31, 174.61, 220.00, 261.63),   # F
        (130.81, 261.63, 329.63, 392.00),  # C
        (98.00, 196.00, 246.94, 293.66),   # G
    ]
    duration = len(chords) * bar_dur
    num_samples = int(sr * duration)
    two_pi = 2.0 * math.pi
    buf = bytearray(num_samples * 2)

    for n in range(num_samples):
        t = n / sr
        bar_idx = int(t // bar_dur) % len(chords)
        bar_t = t % bar_dur
        bass, n1, n2, n3 = chords[bar_idx]

        # Pad suave e respirando
        env_pad = math.sin(math.pi * min(1.0, bar_t / bar_dur)) ** 0.65
        pad = (
            0.22 * math.sin(two_pi * bass * t)
            + 0.16 * math.sin(two_pi * n1 * t)
            + 0.13 * math.sin(two_pi * n2 * t)
            + 0.10 * math.sin(two_pi * n3 * t)
        ) * env_pad

        # Notas lentas estilo piano (arpejo delicado a cada 1 segundo)
        arp_notes = (n1 * 2.0, n2 * 2.0, n3 * 2.0, n2 * 2.0)
        step_i = int(bar_t) % 4
        step_t = bar_t - int(bar_t)
        p_freq = arp_notes[step_i]
        piano_env = math.exp(-step_t * 3.4) * min(1.0, step_t * 45.0)
        piano = (0.20 * math.sin(two_pi * p_freq * t) + 0.07 * math.sin(two_pi * p_freq * 2.0 * t)) * piano_env

        edge = 1.0
        if n < 800:
            edge = n / 800.0
        elif n > num_samples - 800:
            edge = (num_samples - n) / 800.0

        val = max(-0.85, min(0.85, (pad + piano) * 0.72 * edge))
        struct.pack_into("<h", buf, n * 2, int(val * 32767))

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(buf)
    return wav_path


def _wrap_text_px(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        bbox = draw.textbbox((0, 0), cand, font=font)
        if (bbox[2] - bbox[0]) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def create_viral_story_thumbnail(
    photo_path: Path,
    thumb_topo: str,
    thumb_amarelo: str,
    thumb_ponte: str,
    thumb_ciano: str,
    out_path: Path,
) -> Path:
    """
    Gera a thumbnail 1280x720 High-CTR seguindo a fórmula exata do vídeo de 141K views:
    - Direita: foto dramática realista com degradê escuro suave para a esquerda
    - Esquerda: contexto branco/amarelo -> faixa AMARELA vibrante (humilhação) -> ponte branca -> faixa CIANO vibrante (reviravolta)!
    """
    W, H = 1280, 720
    canvas = Image.new("RGBA", (W, H), (6, 8, 12, 255))

    # Foto à direita (ocupando x=460..1280 com leve fade suave à esquerda)
    photo = Image.open(photo_path).convert("RGBA")
    pw, ph = photo.size
    target_w = 860
    scale = max(target_w / pw, H / ph)
    photo = photo.resize((int(pw * scale), int(ph * scale)), Image.Resampling.LANCZOS)
    px0 = max(0, (photo.width - target_w) // 2)
    py0 = max(0, (photo.height - H) // 3)
    photo = photo.crop((px0, py0, px0 + target_w, py0 + H))
    photo = ImageEnhance.Contrast(photo).enhance(1.12)
    photo = ImageEnhance.Color(photo).enhance(1.10)

    # Gradiente horizontal na borda esquerda da foto para fundir com o preto da esquerda
    alpha_mask = Image.new("L", (target_w, H), 255)
    am_draw = ImageDraw.Draw(alpha_mask)
    fade_w = 290
    for x in range(fade_w):
        a = int(255 * ((x / float(fade_w)) ** 1.45))
        am_draw.line([(x, 0), (x, H)], fill=a)
    photo.putalpha(alpha_mask)
    canvas.paste(photo, (W - target_w, 0), photo)

    draw = ImageDraw.Draw(canvas)
    f_top = _load_windows_font(44)
    f_box = _load_windows_font(50)
    f_bridge = _load_windows_font(36)

    max_txt_w = 650
    x_left = 34
    y = 26

    # 1) Topo (branco + destaque amarelo na última linha)
    top_lines = _wrap_text_px(draw, thumb_topo.upper(), f_top, max_txt_w)[:3]
    for li, ln in enumerate(top_lines):
        col = (255, 240, 60, 255) if (li == 1 and len(top_lines) >= 2) else (255, 255, 255, 255)
        draw.text((x_left, y), ln, font=f_top, fill=col, stroke_width=4, stroke_fill=(0, 0, 0, 255))
        y += 50

    y += 12

    # Helper para faixa estilo pincelada/rasgado de altíssimo contraste
    def _draw_highlight_banner(text: str, bg_rgb: tuple[int, int, int], txt_rgb: tuple[int, int, int], stroke_txt: tuple[int, int, int] | None, start_y: int) -> int:
        lines = _wrap_text_px(draw, text.upper(), f_box, max_txt_w - 40)[:3]
        lh = 56
        box_h = len(lines) * lh + 26
        box_w = max_txt_w + 18
        # Retângulo principal com cantos bem arredondados + bordas irregulares sutis estilo pincel
        draw.rounded_rectangle((x_left - 12, start_y, x_left + box_w, start_y + box_h), radius=26, fill=(*bg_rgb, 255))
        for j in range(start_y + 10, start_y + box_h - 10, 14):
            jitter_l = ((j * 17) % 15) - 7
            jitter_r = ((j * 23) % 19) - 9
            draw.ellipse((x_left - 20 + jitter_l, j - 8, x_left + 14, j + 8), fill=(*bg_rgb, 255))
            draw.ellipse((x_left + box_w - 14, j - 8, x_left + box_w + 18 + jitter_r, j + 8), fill=(*bg_rgb, 255))

        cy = start_y + 12
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=f_box)
            lw = bbox[2] - bbox[0]
            tx = x_left + max(10, (box_w - lw) // 2 - 6)
            if stroke_txt:
                draw.text((tx, cy), ln, font=f_box, fill=(*txt_rgb, 255), stroke_width=5, stroke_fill=(*stroke_txt, 255))
            else:
                draw.text((tx, cy), ln, font=f_box, fill=(*txt_rgb, 255))
            cy += lh
        return start_y + box_h + 14

    # 2) Faixa AMARELA (humilhação — texto preto pesado)
    y = _draw_highlight_banner(thumb_amarelo, (255, 232, 16), (8, 8, 12), None, y)

    # 3) Ponte branca
    bridge_lines = _wrap_text_px(draw, thumb_ponte.upper(), f_bridge, max_txt_w)[:2]
    for ln in bridge_lines:
        draw.text((x_left, y), ln, font=f_bridge, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))
        y += 42
    y += 8

    # 4) Faixa CIANO (reviravolta — texto amarelo com contorno preto forte)
    _draw_highlight_banner(thumb_ciano, (20, 235, 245), (255, 242, 40), (0, 0, 0), min(y, H - 185))

    canvas.convert("RGB").save(out_path, format="JPEG", quality=95, subsampling=0)
    return out_path


def _create_story_overlay_png(
    protagonista: str,
    cidade_uf: str,
    branding_path: Path | None,
    out_path: Path,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Cria overlay transparente 1920x1080 com logo circular à esquerda + 'HISTÓRIA DE HOJE: Nome / Cidade' à direita."""
    ov = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ov)

    # Vinheta sutil no topo e base para legibilidade perfeita
    for y in range(210):
        a = int(135 * ((1.0 - y / 210.0) ** 1.6))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, a))
    for y in range(height - 220, height):
        t = (y - (height - 220)) / 220.0
        a = int(155 * (t ** 1.5))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, a))

    # Logo circular no canto superior esquerdo
    if branding_path and branding_path.exists():
        logo = Image.open(branding_path).convert("RGBA").resize((156, 156), Image.Resampling.LANCZOS)
        ov.paste(logo, (42, 34), logo)

    # Selo superior direito "HISTÓRIA DE HOJE: Nome / Cidade"
    f_hdr = _load_windows_font(34)
    f_sub = _load_windows_font(38)
    lines = [
        ("HISTÓRIA DE HOJE:", f_hdr, (250, 204, 21, 255)),
        (protagonista.strip(), f_sub, (255, 255, 255, 255)),
        (cidade_uf.strip(), f_hdr, (226, 232, 240, 255)),
    ]
    ry = 40
    for txt, font, col in lines:
        if not txt:
            continue
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((width - 48 - tw, ry), txt, font=font, fill=col, stroke_width=4, stroke_fill=(0, 0, 0, 235))
        ry += 44

    ov.save(out_path, format="PNG")
    return out_path


def _concat_chapter_audios(
    chapter_texts: list[str],
    work_dir: Path,
    voice: str,
    rate: str,
    pitch: str,
    log_cb: LogCallback | None = None,
) -> tuple[Path, list[WordTiming], list[tuple[float, float]], float]:
    """Sintetiza cada capítulo separadamente (evita timeout em textos longos), junta com pausa de 0.65s e desloca word timings."""
    pause_sec = 0.65
    pause_wav = work_dir / "pause_ch.wav"
    sr = 24000
    with wave.open(str(pause_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(b"\x00\x00" * int(sr * pause_sec))

    chapter_wavs: list[Path] = []
    all_words: list[WordTiming] = []
    chapter_spans: list[tuple[float, float]] = []
    cursor_t = 0.0

    for idx, ch_txt in enumerate(chapter_texts):
        _log(log_cb, f"[StoryTTS] Narrando Capítulo {idx + 1}/{len(chapter_texts)} ({len(ch_txt.split())} palavras) com {voice}...")
        mp3_p = work_dir / f"ch_{idx:02d}.mp3"
        wav_p = work_dir / f"ch_{idx:02d}.wav"
        spoken = normalize_text_for_tts(ch_txt)
        wts = asyncio.run(
            _synthesize_edge_tts(
                text=spoken,
                voice=voice,
                rate=rate,
                pitch=pitch,
                audio_path=mp3_p,
            )
        )

        # Converte para WAV uniforme 24kHz mono para concatenação sem falhas
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_p), "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(wav_p)],
            capture_output=True, check=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        dur = probe_audio_duration(wav_p)
        ch_start = cursor_t
        for w in wts:
            all_words.append(WordTiming(text=w.text, start=w.start + cursor_t, end=w.end + cursor_t))
        cursor_t += dur
        chapter_spans.append((ch_start, cursor_t))
        chapter_wavs.append(wav_p)
        if idx + 1 < len(chapter_texts):
            chapter_wavs.append(pause_wav)
            cursor_t += pause_sec

    concat_list = work_dir / "audio_concat.txt"
    concat_list.write_text("\n".join(f"file '{p.as_posix()}'" for p in chapter_wavs), encoding="utf-8")
    merged_audio = work_dir / "story_narration.m4a"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:a", "aac", "-b:a", "192k", str(merged_audio),
        ],
        capture_output=True, check=True, timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    total_dur = probe_audio_duration(merged_audio) or cursor_t
    return merged_audio, all_words, chapter_spans, total_dur


def run_long_story_pipeline(
    config: dict[str, Any],
    work_dir: Path,
    exclude_urls: set[str] | None = None,
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> StoryRenderResult:
    """Pipeline completo do Modo História Longa YouTube 16:9 + Thumbnail High-CTR + Teaser 9:16."""
    work_dir.mkdir(parents=True, exist_ok=True)
    paths_dict = config.get("paths") or {}
    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    music_dirs = [Path(paths_dict["music_dir"]), MUSIC_DIR] if paths_dict.get("music_dir") else [MUSIC_DIR]
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]
    output_dir.mkdir(parents=True, exist_ok=True)

    target_min = max(4, min(30, int(config.get("duracao_historia_minutos") or 8)))
    voice = str(config.get("tts_voz") or config.get("voz_padrao") or "pt-BR-AntonioNeural")
    # Velocidade natural e humana para storytelling longo (sem correria!)
    story_rate = "+0%"
    story_pitch = "-1Hz"

    # 1) Premissa + Bíblia da História
    pseudo_url, premise, cenario = _pick_story_premise(config, exclude_urls=exclude_urls)
    bible = _generate_story_bible(premise, cenario, target_min, config, log_cb=log_cb)

    titulo = str(bible.get("titulo_viral") or "História Emocionante de Superação").strip()
    protagonista = str(bible.get("protagonista_nome") or "Wagner").strip()
    cidade_uf = str(bible.get("cidade_uf") or cenario).strip()
    capitulos = list(bible.get("capitulos") or [])
    if not capitulos:
        raise RuntimeError("Gemini não retornou capítulos para a história.")

    # 2) Escrita capítulo a capítulo (prosa rica e fluida)
    chapter_texts: list[str] = []
    prev_tail = ""
    for i, ch in enumerate(capitulos):
        if stop_event and stop_event.is_set():
            raise RuntimeError("Processo interrompido pelo usuário.")
        ch_txt = _write_story_chapter(bible, ch, i, len(capitulos), prev_tail, config, log_cb=log_cb)
        chapter_texts.append(ch_txt)
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", ch_txt) if s.strip()]
        prev_tail = " ".join(sents[-2:])[:320]

    full_roteiro = "\n\n".join(chapter_texts)
    total_words = len(full_roteiro.split())
    _log(log_cb, f"[StoryEngine] História completa escrita: {total_words} palavras em {len(chapter_texts)} capítulos!")

    # 3) Narração calma capítulo a capítulo + legendas .ass limpas (TV-friendly)
    audio_path, all_words, chapter_spans, total_dur = _concat_chapter_audios(
        chapter_texts, work_dir, voice=voice, rate=story_rate, pitch=story_pitch, log_cb=log_cb
    )
    ass_path = work_dir / "story_subtitles.ass"
    ass_cfg = dict(config)
    ass_cfg["video"] = {**dict(config.get("video") or {}), "largura": 1920, "altura": 1080}
    build_tiktok_ass(all_words, ass_path, ass_cfg, pos_y=975)
    _log(log_cb, f"[StoryTTS] Narração completa pronta: {total_dur / 60.0:.1f} minutos ({total_dur:.0f}s)!")

    # 4) Coleta de cenas HD 16:9 coerentes com cada capítulo + Foto da Thumbnail
    used_urls: set[str] = set()
    scene_images: list[Path] = []
    scene_durs: list[float] = []
    _log(log_cb, "[StoryVisual] Buscando fotografias cinematográficas 16:9 para cada capítulo...")

    for ci, (ch, (t0, t1)) in enumerate(zip(capitulos, chapter_spans)):
        ch_dur = max(6.0, t1 - t0)
        queries = [str(q).strip() for q in (ch.get("visual_queries") or []) if str(q).strip()][:3]
        if not queries:
            queries = [f"emotional everyday life scene {protagonista}"]
        ch_imgs: list[Path] = []
        for qi, q in enumerate(queries):
            p = work_dir / f"scene_{ci:02d}_{qi:02d}.jpg"
            got = _fetch_widescreen_photo(q, p, used_urls)
            if got:
                ch_imgs.append(got)
        if not ch_imgs:
            p = work_dir / f"scene_{ci:02d}_fb.jpg"
            ch_imgs.append(_generate_fallback_cinematic_scene(p, ci))
        per_img = ch_dur / len(ch_imgs)
        for p in ch_imgs:
            scene_images.append(p)
            scene_durs.append(per_img)

    # Foto dramática exclusiva da Thumbnail
    thumb_photo_q = str(bible.get("thumb_foto_query") or "emotional dramatic portrait two people cinematic")
    thumb_src = work_dir / "thumb_photo_raw.jpg"
    got_thumb_src = _fetch_widescreen_photo(thumb_photo_q, thumb_src, used_urls) or scene_images[0]

    # 5) Gera Thumbnail 1280x720 High-CTR + Overlay (Logo + "HISTÓRIA DE HOJE")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = f"{stamp}_{slugify(titulo)[:40]}"
    thumb_out = output_dir / f"{base_slug}_THUMBNAIL.jpg"
    create_viral_story_thumbnail(
        photo_path=got_thumb_src,
        thumb_topo=str(bible.get("thumb_topo") or "ANOS DEPOIS, O DESTINO UNIU OS DOIS:"),
        thumb_amarelo=str(bible.get("thumb_amarelo") or "“EU SABIA QUE VOCÊ IA FRACASSAR!”"),
        thumb_ponte=str(bible.get("thumb_ponte") or "ELE OLHOU CALMO E DISSE:"),
        thumb_ciano=str(bible.get("thumb_ciano") or "“OLHE BEM QUEM ESTÁ ALI FORA.”"),
        out_path=thumb_out,
    )
    _log(log_cb, f"[StoryThumb] Thumbnail viral High-CTR criada: {thumb_out.name}")

    branding_path = _pick_random_asset(brand_dirs, (".png", ".jpg", ".jpeg", ".webp"))
    overlay_png = _create_story_overlay_png(protagonista, cidade_uf, branding_path, work_dir / "story_overlay.png")

    # 6) Trilha emocional suave
    music_path = _pick_random_asset(music_dirs, (".mp3", ".wav", ".m4a", ".ogg"))
    if not music_path:
        music_path = _generate_emotional_piano_bed(work_dir)
        _log(log_cb, "[StoryEngine] Trilha emocional de piano/cordas sintetizada (vol 12%).")

    # 7) Renderização rápida 16:9 (1920x1080@24fps — leve e cinematográfico, renderiza 10+ min em ~60s!)
    # Pré-gera slides curtos em zoom sutil via concat demuxer para evitar filter_complex gigantesco com 15+ imagens
    fps = 24
    clip_paths: list[Path] = []
    _log(log_cb, f"[StoryRender] Renderizando {len(scene_images)} cenas cinematográficas 16:9...")
    for si, (img_p, sdur) in enumerate(zip(scene_images, scene_durs)):
        if stop_event and stop_event.is_set():
            raise RuntimeError("Processo interrompido pelo usuário.")
        cp = work_dir / f"seg_{si:02d}.mp4"
        frames = max(24, int(round((sdur + 0.15) * fps)))
        # Zoom lentíssimo contemplativo (1.00 -> 1.06) sem tremor
        z_expr = f"1.0+0.06*(on/{max(1, frames - 1)})" if si % 2 == 0 else f"1.06-0.06*(on/{max(1, frames - 1)})"
        vf = (
            f"scale=2048:1152:flags=lanczos,"
            f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080:fps={fps},"
            f"format=yuv420p"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(img_p), "-vf", vf, "-t", f"{sdur:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", str(cp),
            ],
            capture_output=True, check=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        clip_paths.append(cp)

    vconcat = work_dir / "video_concat.txt"
    vconcat.write_text("\n".join(f"file '{p.as_posix()}'" for p in clip_paths), encoding="utf-8")

    mp4_out = output_dir / f"{base_slug}_YOUTUBE_16x9.mp4"
    ass_escaped = escape_ffmpeg_filter_path(ass_path)
    _, color_vf = get_video_filter_chain(config.get("video", {}).get("filtro_cor", "cinema"))
    color_part = f",{color_vf}" if color_vf else ""

    filter_script = work_dir / "story_filter.txt"
    fc = (
        f"[0:v]setpts=PTS-STARTPTS{color_part}[vbase];"
        f"[vbase][1:v]overlay=0:0,ass=filename='{ass_escaped}'[vout];"
        f"[2:a]volume=1.08[voice];"
        f"[3:a]volume=0.12[mus];"
        f"[voice][mus]amix=inputs=2:duration=first:dropout_transition=3,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )
    filter_script.write_text(fc, encoding="utf-8")

    _log(log_cb, f"[StoryRender] Finalizando vídeo longo 16:9 ({total_dur / 60.0:.1f} min) -> {mp4_out.name}...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(vconcat),
        "-i", str(overlay_png),
        "-i", str(audio_path),
        "-stream_loop", "-1", "-i", str(music_path),
        "-/filter_complex", str(filter_script),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-shortest", str(mp4_out),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.strip())
        if len(tail) > 25:
            tail.pop(0)
        if progress_cb:
            t_cur = _parse_ffmpeg_time(line)
            if t_cur is not None:
                progress_cb(min(0.99, t_cur / max(1.0, total_dur)))
    rc = proc.wait()
    if rc != 0 or not mp4_out.exists():
        raise RuntimeError("Erro FFmpeg no vídeo longo 16:9:\n" + "\n".join(tail[-10:]))

    # 8) Teaser Vertical 9:16 (62s do Ato 1 Cold Open) para TikTok / Shorts / Reels!
    short_out: Path | None = None
    if bool(config.get("gerar_teaser_short", True)) and total_dur >= 65.0:
        short_out = output_dir / f"{base_slug}_TEASER_9x16.mp4"
        short_dur = min(64.0, chapter_spans[0][1] if chapter_spans else 62.0)
        short_dur = max(55.0, min(68.0, short_dur))
        _log(log_cb, f"[StoryShort] Gerando Teaser vertical 9:16 ({short_dur:.0f}s) para TikTok/Shorts...")
        # Monta vertical limpo 1080x1920 a partir do início do 16:9 com fundo borrado + quadro central nítido!
        short_fc = (
            "[0:v]split=2[b][f];"
            "[b]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,scale=1080:1920,eq=brightness=-0.30[bg];"
            "[f]scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[fg];"
            "[bg][fg]overlay=0:420,format=yuv420p[sv]"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-t", f"{short_dur:.2f}", "-i", str(mp4_out),
                "-filter_complex", short_fc, "-map", "[sv]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(short_out),
            ],
            capture_output=True, check=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    # 9) Salva pacote completo de publicação YouTube (.txt com títulos A/B, descrição, capítulos e tags)
    txt_out = output_dir / f"{base_slug}_YOUTUBE_16x9.txt"
    alt_titles = [str(t) for t in (bible.get("titulos_alternativos") or [])]
    hashtags = [str(h) for h in (bible.get("hashtags") or ["#HistoriasDeVida", "#Reviravolta", "#Superacao"])]
    chap_lines = [
        f"{_format_ts(sp[0])} - {ch.get('titulo', f'Capítulo {i+1}')}"
        for i, (ch, sp) in enumerate(zip(capitulos, chapter_spans))
    ]
    desc_yt = str(bible.get("descricao_youtube") or "De onde você está assistindo? Me conta nos comentários! 👇")
    meta_txt = (
        f"=== TÍTULO PRINCIPAL (HIGH-CTR) ===\n{titulo}\n\n"
        f"=== TÍTULOS ALTERNATIVOS (TESTE A/B) ===\n"
        + ("\n".join(f"- {t}" for t in alt_titles) if alt_titles else "-")
        + f"\n\n=== DESCRIÇÃO PRONTA PARA YOUTUBE ===\n{desc_yt}\n\n"
        f"📌 CAPÍTULOS:\n" + "\n".join(chap_lines) + "\n\n"
        + " ".join(hashtags)
        + f"\n\n=== ARQUIVOS GERADOS ===\n"
        f"- Vídeo YouTube 16:9: {mp4_out.name} ({total_dur / 60.0:.1f} min)\n"
        f"- Thumbnail High-CTR: {thumb_out.name}\n"
        + (f"- Teaser TikTok/Shorts 9:16: {short_out.name}\n" if short_out else "")
        + f"\n=== ROTEIRO COMPLETO ({total_words} palavras) ===\n{full_roteiro}\n"
    )
    txt_out.write_text(meta_txt, encoding="utf-8")

    mb = mp4_out.stat().st_size / (1024 * 1024)
    _log(log_cb, f"[StoryEngine] CONCLUÍDO! {mp4_out.name} ({mb:.1f} MB) + Thumbnail + Teaser 9:16!")

    article = NewsArticle(
        title=titulo,
        url=pseudo_url,
        source=f"História Original • {config.get('canal_nome', 'Destino & Virada')}",
        summary=desc_yt[:240],
        full_text=premise,
    )
    script = ScriptPackage(
        roteiro=full_roteiro,
        gancho_tela=str(bible.get("thumb_amarelo") or titulo[:45]),
        descricao_tiktok=desc_yt,
        hashtags=hashtags,
        word_count=total_words,
    )
    return StoryRenderResult(
        article=article,
        script=script,
        mp4_path=mp4_out,
        txt_path=txt_out,
        thumb_path=thumb_out,
        short_mp4_path=short_out,
    )
