"""Módulo de geração de narração via Edge-TTS e legendas dinâmicas .ass estilo TikTok."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import edge_tts

LogCallback = Callable[[str], None]


@dataclass
class WordTiming:
    """Marca temporal de uma palavra falada."""

    text: str
    start: float
    end: float


@dataclass
class TTSResult:
    """Resultado da síntese de voz e geração das legendas .ass."""

    audio_path: Path
    ass_path: Path
    duration: float
    word_count: int
    word_timings: list[WordTiming]


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def hex_to_ass_color(hex_color: str, alpha: str = "00") -> str:
    """
    Converte cor hexadecimal #RRGGBB para formato ASS &HAABBGGRR&.
    Ex.: '#FACC15' -> '&H0015CCFA&'
    """
    cleaned = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(cleaned) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", cleaned):
        cleaned = "FFFFFF"
    rr = cleaned[0:2].upper()
    gg = cleaned[2:4].upper()
    bb = cleaned[4:6].upper()
    return f"&H{alpha.upper()}{bb}{gg}{rr}&"


def _format_ass_time(seconds: float) -> str:
    """Converte segundos float para formato ASS H:MM:SS.cc."""
    sec = max(0.0, float(seconds))
    total_cs = int(round(sec * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Escapa caracteres especiais de override ASS."""
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
        .strip()
    )


def probe_audio_duration(audio_path: Path) -> float:
    """Obtém a duração exata do áudio usando ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
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
        info = json.loads(proc.stdout)
        return float(info["format"]["duration"])
    except Exception:
        return 0.0


def _split_sentence_to_words(text: str, start: float, end: float) -> list[WordTiming]:
    """Fallback caso a API retorne apenas SentenceBoundary: distribui tempo proporcionalmente."""
    tokens = [tok for tok in text.split() if tok.strip()]
    if not tokens:
        return []
    total_dur = max(0.15, end - start)
    weights = [max(2, len(re.sub(r"\W+", "", tok))) for tok in tokens]
    weight_sum = sum(weights)
    timings: list[WordTiming] = []
    cursor = start
    for tok, w in zip(tokens, weights):
        seg = total_dur * (w / weight_sum)
        timings.append(WordTiming(text=tok, start=cursor, end=cursor + seg))
        cursor += seg
    return timings


async def _synthesize_edge_tts(
    text: str,
    voice: str,
    rate: str,
    pitch: str,
    audio_path: Path,
) -> list[WordTiming]:
    """Executa edge-tts capturando áudio .mp3 e eventos WordBoundary."""
    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            boundary="WordBoundary",
        )
    except TypeError:
        # Compatibilidade com versões antigas do edge-tts sem parâmetro boundary
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
        )

    word_timings: list[WordTiming] = []
    sentence_timings: list[WordTiming] = []

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            chunk_type = chunk.get("type")
            if chunk_type == "audio":
                audio_file.write(chunk["data"])
            elif chunk_type == "WordBoundary":
                start_s = chunk["offset"] / 10_000_000.0
                dur_s = chunk["duration"] / 10_000_000.0
                word_text = str(chunk.get("text") or "").strip()
                if word_text:
                    word_timings.append(
                        WordTiming(text=word_text, start=start_s, end=start_s + max(0.05, dur_s))
                    )
            elif chunk_type == "SentenceBoundary":
                start_s = chunk["offset"] / 10_000_000.0
                dur_s = chunk["duration"] / 10_000_000.0
                sent_text = str(chunk.get("text") or "").strip()
                if sent_text:
                    sentence_timings.append(
                        WordTiming(text=sent_text, start=start_s, end=start_s + max(0.15, dur_s))
                    )

    if word_timings:
        return word_timings

    # Fallback se apenas SentenceBoundary chegou
    interpolated: list[WordTiming] = []
    for sent in sentence_timings:
        interpolated.extend(_split_sentence_to_words(sent.text, sent.start, sent.end))
    return interpolated


NOISE_SUBTITLE_RE = re.compile(
    r"^(?:>+|\[.*?\]|\(.*?\)|♪+|♫+|\.{2,}|-+|_+)$",
    flags=re.IGNORECASE,
)


def sanitize_word_timings(words: list[WordTiming]) -> list[WordTiming]:
    """Remove ruídos de legenda (>>, [música], [risadas]) e elimina qualquer sobreposição temporal."""
    cleaned: list[WordTiming] = []
    for wt in sorted(words, key=lambda w: (w.start, w.end)):
        txt = re.sub(r"\[[^\]]*\]", "", wt.text or "")
        txt = txt.replace(">>", "").replace("♪", "").replace("♫", "").strip()
        if not txt or NOISE_SUBTITLE_RE.match(txt):
            continue
        s = max(0.0, float(wt.start))
        e = max(s + 0.05, float(wt.end))
        cleaned.append(WordTiming(text=txt, start=s, end=e))

    # Garante ordem estritamente monotônica sem overlaps (impede libass de empilhar legendas)
    fixed: list[WordTiming] = []
    for wt in cleaned:
        s, e = wt.start, wt.end
        if fixed:
            prev = fixed[-1]
            if s < prev.end:
                # Encurta a palavra anterior para acabar exatamente quando a próxima começa
                if s > prev.start + 0.04:
                    prev.end = s
                else:
                    s = prev.end
                    e = max(s + 0.05, e)
        fixed.append(WordTiming(text=wt.text, start=s, end=e))
    return fixed


def _group_words_into_blocks(
    words: list[WordTiming],
    max_words: int = 3,
    max_chars: int = 16,
) -> list[list[WordTiming]]:
    """
    Agrupa palavras em blocos curtos (1 a 3 palavras, máx ~16 chars) que SEMPRE cabem
    em UMA ÚNICA LINHA na tela 1080px — impedindo quebra dinâmica e "dança" vertical.
    """
    if not words:
        return []

    max_words = max(2, min(3, max_words))
    blocks: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    current_chars = 0

    for idx, wt in enumerate(words):
        word_len = len(wt.text)
        gap_from_prev = (wt.start - current[-1].end) if current else 0.0

        should_break_before = bool(
            current
            and (
                len(current) >= max_words
                or (current_chars + 1 + word_len > max_chars)
                or gap_from_prev > 0.38
            )
        )

        if should_break_before:
            blocks.append(current)
            current = []
            current_chars = 0

        current.append(wt)
        current_chars += word_len + (1 if len(current) > 1 else 0)

        # Quebra natural após pontuação forte
        ends_sentence = bool(re.search(r"[.!?¿¡;:]$", wt.text))
        next_gap = (words[idx + 1].start - wt.end) if idx + 1 < len(words) else 1.0
        if ends_sentence and (len(current) >= 2 or next_gap > 0.22):
            blocks.append(current)
            current = []
            current_chars = 0

    if current:
        if len(current) == 1 and blocks and len(blocks[-1]) < max_words:
            cand_len = sum(len(w.text) for w in blocks[-1]) + len(blocks[-1]) + len(current[0].text)
            if cand_len <= max_chars:
                blocks[-1].extend(current)
            else:
                blocks.append(current)
        else:
            blocks.append(current)

    return blocks


def build_tiktok_ass(
    words: list[WordTiming],
    ass_path: Path,
    config: dict[str, Any],
    pos_y: int | None = None,
) -> None:
    """
    Gera arquivo .ass estilo TikTok com posição 100% TRAVADA (zero pulo/dança vertical),
    zero sobreposição entre eventos, linha única limpa e zona segura para UI do TikTok.
    """
    video_cfg = config.get("video", {})
    sub_cfg = config.get("legenda", {})

    play_x = int(video_cfg.get("largura", 1080))
    play_y = int(video_cfg.get("altura", 1920))

    font_name = sub_cfg.get("fonte", "Impact")
    # Limita fonte a 74px para garantir linha única estável dentro da zona segura horizontal do TikTok
    font_size = min(int(sub_cfg.get("tamanho_fonte", 74)), 74)
    active_ass = hex_to_ass_color(sub_cfg.get("cor_ativa", "#FACC15"))
    inactive_ass = hex_to_ass_color(sub_cfg.get("cor_inativa", "#FFFFFF"))
    outline_ass = hex_to_ass_color(sub_cfg.get("cor_contorno", "#000000"))
    outline_size = max(4, min(int(sub_cfg.get("espessura_contorno", 9)), 10))
    shadow_size = int(sub_cfg.get("sombra", 3))
    words_per_block = min(int(sub_cfg.get("palavras_por_bloco", 3)), 3)

    # Zona segura TikTok (1080x1920):
    # - Direita (x > 920) tem botões Like/Comentário/Share -> centralizamos em x=515 (leve respiro à direita)
    # - Rodapé (y > 1560) tem legenda/descrição do TikTok -> travamos em y=1445 (ou pos_y customizado)
    anchor_x = int(play_x * 0.485)
    if pos_y is not None:
        anchor_y = int(pos_y)
    else:
        margem_inf = int(sub_cfg.get("margem_inferior", 460))
        # Converte margem inferior em coordenada fixa segura (entre y=1360 e y=1515)
        anchor_y = max(1360, min(1515, play_y - max(405, margem_inf)))

    # WrapStyle: 2 (sem quebra automática inesperada) + Alignment=5 (centro fixo)
    header = f"""[Script Info]
Title: FabricaMX TikTok Subtitles
ScriptType: v4.00+
PlayResX: {play_x}
PlayResY: {play_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,{font_name},{font_size},{inactive_ass},{active_ass},{outline_ass},&H90000000&,-1,0,0,0,100,100,1,0,1,{outline_size},{shadow_size},5,80,140,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    clean_words = sanitize_word_timings(words)
    blocks = _group_words_into_blocks(clean_words, max_words=words_per_block, max_chars=17)
    events: list[str] = []
    last_event_end = 0.0

    for b_idx, block in enumerate(blocks):
        next_block_start = blocks[b_idx + 1][0].start if b_idx + 1 < len(blocks) else None

        # Se algum bloco ultrapassar 18 chars, reduz levemente a fonte só daquele bloco (sempre 1 linha!)
        block_plain = " ".join(item.text.upper() for item in block)
        fs_override = f"\\fs{max(58, font_size - 10)}" if len(block_plain) > 18 else ""

        for w_idx, wt in enumerate(block):
            seg_start = max(last_event_end, wt.start)
            if w_idx + 1 < len(block):
                seg_end = max(seg_start + 0.05, block[w_idx + 1].start)
            else:
                seg_end = max(seg_start + 0.08, wt.end + 0.06)
                if next_block_start is not None:
                    seg_end = min(seg_end, next_block_start)

            if seg_end <= seg_start + 0.02:
                seg_end = seg_start + 0.05
            last_event_end = seg_end

            # Posição 100% travada (\an5\pos) + destaque puro por cor (SEM \fscy110 -> zero pulo vertical!)
            prefix_tag = f"{{\\an5\\pos({anchor_x},{anchor_y}){fs_override}}}"
            parts: list[str] = []
            for j, item in enumerate(block):
                clean_word = _escape_ass_text(item.text.upper())
                if j == w_idx:
                    parts.append(f"{{\\c{active_ass}}}{clean_word}{{\\c{inactive_ass}}}")
                else:
                    parts.append(f"{{\\c{inactive_ass}}}{clean_word}")

            line_text = prefix_tag + " ".join(parts)
            events.append(
                f"Dialogue: 0,{_format_ass_time(seg_start)},{_format_ass_time(seg_end)},TikTok,,0,0,0,,{line_text}"
            )

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")


def normalize_text_for_tts(text: str, idioma: str = "pt-BR") -> str:
    """Normaliza siglas, moedas e símbolos para pronúncia 100% natural no Edge-TTS."""
    s = (text or "").strip()
    lang = idioma.lower()
    if lang.startswith("pt"):
        s = re.sub(r"\bUS\$\s*([\d.,]+)\s*(bilh[õo]es|milh[õo]es|mil)\b", r"\1 \2 de dólares", s, flags=re.IGNORECASE)
        s = re.sub(r"\bUS\$\s*([\d.,]+)\b", r"\1 dólares", s, flags=re.IGNORECASE)
        s = re.sub(r"\bR\$\s*([\d.,]+)\b", r"\1 reais", s, flags=re.IGNORECASE)
        s = re.sub(r"\bPS5\b", "PlayStation 5", s, flags=re.IGNORECASE)
        s = re.sub(r"\bGTA\s*VI\b", "GTA 6", s, flags=re.IGNORECASE)
        s = re.sub(r"\bGTA\s*V\b", "GTA 5", s, flags=re.IGNORECASE)
        s = re.sub(r"\bautocarro\b", "ônibus", s, flags=re.IGNORECASE)
        s = re.sub(r"\bconsola\b", "console", s, flags=re.IGNORECASE)
        s = re.sub(r"\becrã\b", "tela", s, flags=re.IGNORECASE)
    elif lang.startswith("es"):
        s = re.sub(r"\bUS\$\s*([\d.,]+)\b", r"\1 dólares", s, flags=re.IGNORECASE)
        s = re.sub(r"\bPS5\b", "PlayStation 5", s, flags=re.IGNORECASE)
        s = re.sub(r"\bGTA\s*VI\b", "GTA 6", s, flags=re.IGNORECASE)
    else:
        s = re.sub(r"\bPS5\b", "PlayStation 5", s, flags=re.IGNORECASE)
        s = re.sub(r"\bGTA\s*VI\b", "GTA 6", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def _parse_rate_percent(rate_str: str) -> int:
    m = re.search(r"([+-]?\d+)", str(rate_str or "+0%"))
    return int(m.group(1)) if m else 0


def generate_narration_and_subtitles(
    script_text: str,
    work_dir: Path,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
    pos_y: int | None = None,
) -> TTSResult:
    """
    Gera narração .mp3 via Edge-TTS e legendas .ass sincronizadas por palavra.
    Inclui guarda automática de duração mínima (>= 61.5s) para monetização no TikTok.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / "narration.mp3"
    ass_path = work_dir / "subtitles.ass"

    voice = config.get("tts_voz", "es-MX-JorgeNeural")
    rate = str(config.get("tts_velocidade", "+8%"))
    pitch = str(config.get("tts_pitch", "+0Hz"))
    idioma = str(config.get("idioma", "pt-BR"))
    spoken_text = normalize_text_for_tts(script_text, idioma=idioma)

    def _run_tts_once(cur_rate: str) -> tuple[list[WordTiming], float]:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            wt = loop.run_until_complete(
                _synthesize_edge_tts(
                    text=spoken_text,
                    voice=voice,
                    rate=cur_rate,
                    pitch=pitch,
                    audio_path=audio_path,
                )
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        if not audio_path.exists() or audio_path.stat().st_size < 1000:
            raise RuntimeError("Falha ao gerar arquivo de áudio via Edge-TTS.")
        dur = probe_audio_duration(audio_path)
        if dur <= 0.0 and wt:
            dur = wt[-1].end + 0.35
        return wt, dur

    _log(log_cb, f"[TTS] Gerando áudio com voz {voice} (velocidade {rate})...")
    word_timings, duration = _run_tts_once(rate)

    # Guarda automática de monetização (> 60s): ativa apenas para nichos de 60s+ (não estica vídeos curtos de Facebook/Reels!)
    video_cfg = config.get("video", {})
    cfg_min_dur = float(video_cfg.get("duracao_alvo_min", 60))
    min_target_sec = cfg_min_dur + 1.5
    if cfg_min_dur >= 55.0 and duration < min_target_sec and len(spoken_text.split()) >= 110:
        cur_pct = _parse_rate_percent(rate)
        cur_factor = max(0.75, 1.0 + cur_pct / 100.0)
        desired_sec = max(min_target_sec + 1.2, 63.0)
        new_factor = cur_factor * (duration / desired_sec)
        new_pct = max(-10, min(cur_pct - 2, int(round((new_factor - 1.0) * 100))))
        if new_pct < cur_pct:
            new_rate = f"{new_pct:+d}%"
            _log(
                log_cb,
                f"[TTS] Áudio ficou em {duration:.1f}s (< 61.5s). Recalibrando ritmo para {new_rate} para passar de 1 minuto...",
            )
            word_timings, duration = _run_tts_once(new_rate)

    if not word_timings:
        _log(log_cb, "[TTS] Aviso: sem eventos WordBoundary; interpolando legendas pela duração total.")
        word_timings = _split_sentence_to_words(script_text, 0.1, max(1.0, duration - 0.1))

    _log(
        log_cb,
        f"[TTS] Áudio gerado ({duration:.1f}s) com {len(word_timings)} marcações de palavra.",
    )

    build_tiktok_ass(word_timings, ass_path, config, pos_y=pos_y)
    _log(log_cb, f"[TTS] Legendas .ass travadas criadas em {ass_path.name}.")

    return TTSResult(
        audio_path=audio_path,
        ass_path=ass_path,
        duration=duration,
        word_count=len(word_timings),
        word_timings=word_timings,
    )
