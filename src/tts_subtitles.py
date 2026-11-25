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


def _group_words_into_blocks(
    words: list[WordTiming],
    max_words: int = 3,
    max_chars: int = 22,
) -> list[list[WordTiming]]:
    """
    Agrupa palavras em blocos curtos (2 a 3 palavras) respeitando pausas,
    pontuação forte e largura visual para tela vertical 1080px.
    """
    if not words:
        return []

    max_words = max(2, min(4, max_words))
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
                or (current_chars + 1 + word_len > max_chars and len(current) >= 2)
                or gap_from_prev > 0.42
            )
        )

        if should_break_before:
            blocks.append(current)
            current = []
            current_chars = 0

        current.append(wt)
        current_chars += word_len + (1 if len(current) > 1 else 0)

        # Quebra natural após pontuação forte se já temos pelo menos 2 palavras (ou pausa longa depois)
        ends_sentence = bool(re.search(r"[.!?¿¡;:]$", wt.text))
        next_gap = (words[idx + 1].start - wt.end) if idx + 1 < len(words) else 1.0
        if ends_sentence and (len(current) >= 2 or next_gap > 0.25):
            blocks.append(current)
            current = []
            current_chars = 0

    if current:
        # Evita bloco órfão de 1 palavra curta no final, mesclando se couber
        if len(current) == 1 and blocks and len(blocks[-1]) < max_words:
            blocks[-1].extend(current)
        else:
            blocks.append(current)

    return blocks


def build_tiktok_ass(
    words: list[WordTiming],
    ass_path: Path,
    config: dict[str, Any],
) -> None:
    """
    Gera arquivo .ass estilo TikTok com fonte grande em negrito, contorno preto forte
    e destaque colorido dinâmico na palavra ativa dentro de blocos de 2 a 3 palavras.
    """
    video_cfg = config.get("video", {})
    sub_cfg = config.get("legenda", {})

    play_x = int(video_cfg.get("largura", 1080))
    play_y = int(video_cfg.get("altura", 1920))

    font_name = sub_cfg.get("fonte", "Impact")
    font_size = int(sub_cfg.get("tamanho_fonte", 88))
    active_ass = hex_to_ass_color(sub_cfg.get("cor_ativa", "#00F0FF"))
    inactive_ass = hex_to_ass_color(sub_cfg.get("cor_inativa", "#FFFFFF"))
    outline_ass = hex_to_ass_color(sub_cfg.get("cor_contorno", "#000000"))
    outline_size = int(sub_cfg.get("espessura_contorno", 11))
    shadow_size = int(sub_cfg.get("sombra", 3))
    words_per_block = int(sub_cfg.get("palavras_por_bloco", 3))
    alignment = int(sub_cfg.get("alinhamento", 5))
    margin_v = int(sub_cfg.get("margem_inferior", 0))

    header = f"""[Script Info]
Title: FabricaMX TikTok Subtitles
ScriptType: v4.00+
PlayResX: {play_x}
PlayResY: {play_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,{font_name},{font_size},{inactive_ass},{active_ass},{outline_ass},&H90000000&,-1,0,0,0,100,100,1,0,1,{outline_size},{shadow_size},{alignment},70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    blocks = _group_words_into_blocks(words, max_words=words_per_block, max_chars=20)
    events: list[str] = []

    for b_idx, block in enumerate(blocks):
        next_block_start = blocks[b_idx + 1][0].start if b_idx + 1 < len(blocks) else None

        for w_idx, wt in enumerate(block):
            seg_start = wt.start
            if w_idx + 1 < len(block):
                # Contíguo sem piscar até a próxima palavra do bloco
                seg_end = max(wt.end, block[w_idx + 1].start)
            else:
                seg_end = wt.end + 0.08
                if next_block_start is not None:
                    seg_end = min(seg_end, next_block_start)

            if seg_end <= seg_start:
                seg_end = seg_start + 0.08

            # Monta texto destacando a palavra ativa (ciano elétrico + leve pop) e demais em branco
            parts: list[str] = []
            for j, item in enumerate(block):
                clean_word = _escape_ass_text(item.text.upper())
                if j == w_idx:
                    parts.append(
                        f"{{\\c{active_ass}\\fscx110\\fscy110}}{clean_word}{{\\c{inactive_ass}\\fscx100\\fscy100}}"
                    )
                else:
                    parts.append(f"{{\\c{inactive_ass}}}{clean_word}")

            line_text = " ".join(parts)
            events.append(
                f"Dialogue: 0,{_format_ass_time(seg_start)},{_format_ass_time(seg_end)},TikTok,,0,0,0,,{line_text}"
            )

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")


def generate_narration_and_subtitles(
    script_text: str,
    work_dir: Path,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> TTSResult:
    """
    Gera narração .mp3 via Edge-TTS e legendas .ass sincronizadas por palavra.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / "narration.mp3"
    ass_path = work_dir / "subtitles.ass"

    voice = config.get("tts_voz", "es-MX-JorgeNeural")
    rate = config.get("tts_velocidade", "+8%")
    pitch = config.get("tts_pitch", "+0Hz")

    _log(log_cb, f"[TTS] Gerando áudio com voz {voice} (velocidade {rate})...")

    # Executa asyncio de forma segura em qualquer thread (GUI worker ou main)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        word_timings = loop.run_until_complete(
            _synthesize_edge_tts(
                text=script_text,
                voice=voice,
                rate=rate,
                pitch=pitch,
                audio_path=audio_path,
            )
        )
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    if not audio_path.exists() or audio_path.stat().st_size < 1000:
        raise RuntimeError("Falha ao gerar arquivo de áudio via Edge-TTS.")

    duration = probe_audio_duration(audio_path)
    if duration <= 0.0 and word_timings:
        duration = word_timings[-1].end + 0.35

    if not word_timings:
        _log(log_cb, "[TTS] Aviso: sem eventos WordBoundary; interpolando legendas pela duração total.")
        word_timings = _split_sentence_to_words(script_text, 0.1, max(1.0, duration - 0.1))

    _log(
        log_cb,
        f"[TTS] Áudio gerado ({duration:.1f}s) com {len(word_timings)} marcações de palavra.",
    )

    build_tiktok_ass(word_timings, ass_path, config)
    _log(log_cb, f"[TTS] Legendas .ass criadas em {ass_path.name}.")

    return TTSResult(
        audio_path=audio_path,
        ass_path=ass_path,
        duration=duration,
        word_count=len(word_timings),
    )
