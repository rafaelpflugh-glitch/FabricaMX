"""Motor Profissional de Highlights Esportivos — Padrão @highlightsmx (106K+ views).

Fórmula replicada com fidelidade:
1. Formato limpo TELA CHEIA sem barras borradas: 1:1 (1080x1080) ou 9:16 (1080x1920).
2. Placar dinâmico AO VIVO no topo com ESCUDOS REAIS dos 2 clubes:
   - Começa 0x0 e atualiza automaticamente a cada gol (1x0, 2x0, 2x1...)
   - Exibe abaixo do placar o minuto + autor do gol (ex.: "30' Alejandro Zendejas")
   - Sem banner vermelho gigante tampando a tela!
3. Abertura sempre no pontapé inicial com gancho de rivalidade ("El día que X humilló/acabó con Y").
4. Cortes de GOL cirúrgicos (4.0s–4.5s) com 100% ÁUDIO ORIGINAL DA TV (grito de GOOOOL + torcida) e zero narração/legenda em cima!
5. Replay & Comemoração logo após cada gol com narração rápida de rivalidade + placar já atualizado.
6. Duração cravada em 62–65 segundos, sempre cronológico para frente, fechando com o apito final!
7. Legendas minimalistas e limpas (~46px, cor do clube na palavra ativa, sumindo nos gols) + música com ducking nos gols.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image, ImageDraw, ImageFont

from src import ASSETS_DIR, BRANDING_DIR, MUSIC_DIR, OUTPUT_DIR, normalize_text, slugify
from src.llm_client import generate_text_llm
from src.scene_matcher import index_video_pool
from src.scraper import NewsArticle
from src.scriptwriter import ScriptPackage, _clean_script_text, _language_pack, _normalize_hashtags
from src.tts_subtitles import (
    WordTiming,
    _synthesize_edge_tts,
    build_tiktok_ass,
    normalize_text_for_tts,
    probe_audio_duration,
)
from src.video_engine import (
    _generate_cinematic_bed,
    _load_windows_font,
    _parse_ffmpeg_time,
    _pick_random_asset,
    escape_ffmpeg_filter_path,
    get_video_filter_chain,
    save_post_metadata_txt,
)
from src.video_fetcher import download_video_with_audio_hd, probe_video_info
from src.youtube_transcript import (
    YouTubeCandidate,
    _compress_transcript_digest,
    discover_youtube_candidate,
    fetch_transcript_cues,
)

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]

CRESTS_CACHE_DIR = ASSETS_DIR / "crests"

# IDs públicos da ESPN CDN para escudos PNG transparentes de altíssima resolução (cache automático!)
KNOWN_ESPN_TEAM_IDS: dict[str, str] = {
    # Liga MX
    "america": "227",
    "club america": "227",
    "aguilas": "227",
    "monterrey": "220",
    "rayados": "220",
    "chivas": "219",
    "guadalajara": "219",
    "cruz azul": "218",
    "pumas": "233",
    "pumas unam": "233",
    "tigres": "232",
    "tigres uanl": "232",
    "toluca": "223",
    "atlas": "216",
    "pachuca": "234",
    "leon": "228",
    "santos": "225",
    "santos laguna": "225",
    "necaxa": "229",
    "tijuana": "10125",
    "xolos": "10125",
    "puebla": "231",
    "queretaro": "222",
    "mazatlan": "20702",
    "juarez": "17851",
    "san luis": "15720",
    # Seleções
    "mexico": "203",
    "estados unidos": "660",
    "eua": "660",
    "usa": "660",
    "brasil": "205",
    "brazil": "205",
    "argentina": "202",
    # Brasil
    "flamengo": "819",
    "palmeiras": "2029",
    "corinthians": "874",
    "sao paulo": "2026",
    "gremio": "6273",
    "internacional": "1936",
    "inter": "1936",
    "vasco": "3454",
    "botafogo": "6086",
    "fluminense": "3445",
    "cruzeiro": "2022",
    "atletico mg": "7632",
    "atletico mineiro": "7632",
    "santos fc": "2674",
    # Europa
    "real madrid": "86",
    "barcelona": "83",
    "manchester city": "382",
    "liverpool": "364",
    "psg": "160",
    "bayern": "132",
}


@dataclass
class HighlightBlock:
    """Um bloco cronológico do highlight esportivo com estado de placar ao vivo."""

    kind: str  # "kickoff" | "play" | "goal_original" | "goal_replay" | "final_whistle"
    video_start: float
    duration: float
    narration: str = ""
    score_home: int = 0
    score_away: int = 0
    scorer_tag: str = ""  # ex.: "30' Alejandro Zendejas"


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def _draw_fallback_crest(team_name: str, size: int = 128, accent: tuple[int, int, int] = (250, 204, 21)) -> Image.Image:
    """Desenha um escudo minimalista nítido caso o PNG oficial não seja encontrado."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 8
    draw.ellipse((pad, pad, size - pad, size - pad), fill=(15, 23, 42, 240), outline=(*accent, 255), width=6)
    initials = "".join(w[0].upper() for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]+", team_name)[:3])[:3] or "FC"
    font = _load_windows_font(size=int(size * 0.34), bold=True)
    draw.text((size // 2, size // 2), initials, font=font, fill=(255, 255, 255, 255), anchor="mm")
    return img


def fetch_team_crest(team_name: str, size: int = 120) -> Image.Image:
    """Baixa (e faz cache local) do escudo PNG transparente real do clube/seleção."""
    CRESTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    norm = normalize_text(team_name)
    slug = slugify(team_name, max_length=32)
    cache_file = CRESTS_CACHE_DIR / f"{slug}.png"

    if cache_file.exists() and cache_file.stat().st_size > 1000:
        try:
            img = Image.open(cache_file).convert("RGBA")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            return img
        except Exception:
            pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # 1) ESPN CDN direta (PNG transparente HD oficial!)
    espn_id = KNOWN_ESPN_TEAM_IDS.get(norm)
    if not espn_id:
        for k, tid in KNOWN_ESPN_TEAM_IDS.items():
            if k in norm or norm in k:
                espn_id = tid
                break

    candidate_urls: list[str] = []
    if espn_id:
        candidate_urls.append(f"https://a.espncdn.com/i/teamlogos/soccer/500/{espn_id}.png")
        candidate_urls.append(f"https://a.espncdn.com/i/teamlogos/countries/500/{espn_id}.png")

    # 2) Fallback DDGS transparent logo PNG
    if DDGS is not None and not candidate_urls:
        try:
            with DDGS() as ddgs:
                res = ddgs.images(f"{team_name} logo escudo png", region="mx-es", max_results=6)
                for item in res or []:
                    u = str(item.get("image") or "")
                    if u.lower().endswith(".png"):
                        candidate_urls.append(u)
        except Exception:
            pass

    for url in candidate_urls:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200 and len(r.content) > 1500:
                img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                img.save(cache_file, format="PNG")
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                return img
        except Exception:
            continue

    return _draw_fallback_crest(team_name, size=size)


def create_live_scoreboard_png(
    home_team: str,
    away_team: str,
    home_crest: Image.Image,
    away_crest: Image.Image,
    score_home: int,
    score_away: int,
    scorer_tag: str,
    output_path: Path,
    width: int = 1080,
    height: int = 1080,
    accent_rgb: tuple[int, int, int] = (250, 204, 21),
) -> Path:
    """
    Cria o placar dinâmico limpo no topo no estilo EXATO do @highlightsmx:
    [Escudo Casa]  2x1  [Escudo Visitante]
           30' Alejandro Zendejas
    Sem caixote preto pesado — escudos limpos + placar branco com contorno preto forte!
    """
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    is_square = height <= width + 100
    top_y = 66 if is_square else 155
    crest_sz = 108 if is_square else 124
    score_font = _load_windows_font(size=58 if is_square else 66, bold=True)
    scorer_font = _load_windows_font(size=28 if is_square else 32, bold=True)

    score_text = f"{score_home}x{score_away}"
    cx = width // 2

    # Cola escudos com sombra suave centralizados ao lado do placar
    c_home = home_crest.copy()
    c_home.thumbnail((crest_sz, crest_sz), Image.Resampling.LANCZOS)
    c_away = away_crest.copy()
    c_away.thumbnail((crest_sz, crest_sz), Image.Resampling.LANCZOS)

    gap = 92 if is_square else 108
    hx = cx - gap - c_home.width
    hy = top_y - c_home.height // 2
    ax = cx + gap
    ay = top_y - c_away.height // 2

    canvas.alpha_composite(c_home, (max(10, hx), max(8, hy)))
    canvas.alpha_composite(c_away, (min(width - c_away.width - 10, ax), max(8, ay)))

    draw.text(
        (cx, top_y),
        score_text,
        font=score_font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        stroke_width=6,
        stroke_fill=(0, 0, 0, 255),
    )

    if scorer_tag and scorer_tag.strip():
        tag_y = top_y + (crest_sz // 2) + 22
        draw.text(
            (cx, tag_y),
            scorer_tag.strip(),
            font=scorer_font,
            fill=(*accent_rgb, 255),
            anchor="mm",
            stroke_width=4,
            stroke_fill=(0, 0, 0, 255),
        )

    canvas.save(output_path, format="PNG")
    return output_path


def _find_goal_roar_start(video_path: Path, approx_t: float, vid_dur: float, min_t: float = 2.0) -> float:
    """
    Localiza o segundo EXATO do chute/gol analisando o pico de volume do áudio original da TV
    (o grito de 'GOOOOOL!' + explosão da torcida) numa janela [-32s .. +12s] ao redor de approx_t.
    Retorna `peak_t - 2.5s` para capturar passe -> chute -> bola entrando na rede -> grito completo!
    """
    win_start = max(2.0, min_t, approx_t - 32.0)
    win_end = min(vid_dur - 5.0, approx_t + 12.0)
    win_dur = win_end - win_start
    if win_dur < 6.0:
        return max(min_t, approx_t)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{win_start:.2f}",
        "-t",
        f"{win_dur:.2f}",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "s16le",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = proc.stdout
        if proc.returncode != 0 or len(raw) < 16000:
            return approx_t

        import array

        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
        # Calcula energia RMS em fatias de 0.5s (4000 amostras cada)
        step = 4000
        energies: list[float] = []
        for pos in range(0, len(samples) - step + 1, step):
            chunk = samples[pos : pos + step]
            rms = sum(s * s for s in chunk) / float(step)
            energies.append(rms)
        if len(energies) < 6:
            return approx_t

        # Janela deslizante de 2.5s (5 fatias) para achar a explosão sustentada do "GOOOOOL!"
        best_i = 0
        best_e = -1.0
        for i in range(len(energies) - 4):
            e_sum = sum(energies[i : i + 5])
            if e_sum > best_e:
                best_e = e_sum
                best_i = i

        peak_t = win_start + (best_i * 0.5)
        # Começa 2.5s ANTES do pico do grito para pegar o passe/chute + a bola entrando na rede!
        return round(max(2.0, min_t, min(vid_dur - 8.0, peak_t - 2.5)), 2)
    except Exception:
        return approx_t


def _synth_block_tts(
    text: str,
    audio_path: Path,
    config: dict[str, Any],
) -> tuple[float, list[WordTiming]]:
    """Sintetiza um bloco de narração rápido via Edge-TTS retornando (duração, word_timings)."""
    voice = str(config.get("tts_voz", "es-MX-JorgeNeural"))
    rate = str(config.get("tts_velocidade", "+12%"))
    pitch = str(config.get("tts_pitch", "-2Hz"))
    idioma = str(config.get("idioma", "es-MX"))
    spoken = normalize_text_for_tts(text, idioma=idioma)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        words = loop.run_until_complete(
            _synthesize_edge_tts(
                text=spoken,
                voice=voice,
                rate=rate,
                pitch=pitch,
                audio_path=audio_path,
            )
        )
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    dur = probe_audio_duration(audio_path)
    if dur <= 0.1 and words:
        dur = words[-1].end + 0.20
    return max(1.2, dur), words


def _plan_match_blocks_with_llm(
    video: YouTubeCandidate,
    digest: str,
    video_duration: float,
    config: dict[str, Any],
    extra_match_context: str = "",
    log_cb: LogCallback | None = None,
) -> dict[str, Any]:
    """Planeja um highlight enxuto de EXATAMENTE 62–65 segundos no estilo @highlightsmx."""
    lp = _language_pack(config)
    idioma = str(config.get("idioma", "es-MX"))
    clube_foco = str(config.get("clube_foco") or "").strip()

    if idioma.startswith("es"):
        opening_rule = 'SEMPRE comece o bloco 1 com "El día que [Time A] humilló / acabó con / silenció a [Time B]." (ou no drama do jogo)'
        rivalry_ex = "Al minuto 30, Zendejas apareció para abrir el marcador y callar por completo a toda la afición rival."
    else:
        opening_rule = 'SEMPRE comece o bloco 1 com "O dia em que o [Time A] humilhou / acabou com / silenciou o [Time B]."'
        rivalry_ex = "Aos 30 minutos, ele apareceu para abrir o placar e calar na hora a torcida rival inteira."

    ctx_section = f"\nCONTEXTO CONFIRMADO DA PARTIDA:\n{extra_match_context}\n" if extra_match_context else ""

    prompt = f"""{lp['persona']}
Você é o editor-chefe do canal viral @highlightsmx (vídeos de futebol CRAVADOS em 62 a 64 segundos, ritmo frenético e ZERO enrolação!).
Analise a linha do tempo cronológica do resumo oficial "{video.title}" ({video.channel}, duração total {int(video_duration)}s).{ctx_section}
LINHA DO TEMPO DO VÍDEO:
{digest}

PADRÃO OBRIGATÓRIO (@highlightsmx — 62 a 64 SEGUNDOS TOTAIS, 100% CRONOLÓGICO PARA FRENTE!):
Retorne APENAS um JSON válido com estas chaves:
- "home_team": string (nome limpo do time da casa, ex.: "Club América" ou "Chivas")
- "away_team": string (nome limpo do time visitante, ex.: "Monterrey" ou "Atlas")
- "gancho_tela": string curta EM CAIXA ALTA (3 a 5 palavras)
- "descricao_tiktok": string (1 frase de rivalidade curta SEM hashtags)
- "hashtags": array de 6 a 8 hashtags começando com #
- "blocos": array de 7 a 9 objetos EM ORDEM ESTRITAMENTE CRESCENTE DE `video_start`, somando ~145 a 165 PALAVRAS NO TOTAL (para que + 3 cortes de gol de 4.2s fechem EXATAMENTE em 62–65 segundos!):
  1. BLOCO 1 (`kind`: "kickoff"):
     - `video_start`: pontapé inicial da partida (no início do jogo)
     - `score_home`: 0, `score_away`: 0, `scorer_tag`: ""
     - `narration`: 18 a 24 palavras em {lp['lang_name']}. {opening_rule} Já emende o começo do jogo!
  2. BLOCO DE LANCE (`kind`: "play", opcional 1 bloco curto de 12-16 palavras entre lances):
     - `video_start`: lance perigoso/expulsão em ordem crescente
     - `score_home` / `score_away`: placar ATUAL naquele momento, `scorer_tag`: ""
     - `narration`: 12 a 18 palavras preparando a tensão / rivalidade
  3. PARA CADA GOL DA PARTIDA (SEMPRE EM PAR `goal_original` + `goal_replay`!):
     - Bloco A (`kind`: "goal_original"):
       • `video_start`: segundo EXATO ~1.5s ANTES da bola entrar no gol (chute/cabeçada + bola na rede + grito de GOOOOL da TV!)
       • `duration`: número entre 4.0 e 4.6 segundos (corte seco e eletrizante!)
       • `score_home` / `score_away`: placar JÁ ATUALIZADO com esse gol!
       • `scorer_tag`: minuto + autor do gol (ex.: "30' Alejandro Zendejas" ou "90+2' Germán Berterame")
       • `narration`: "" (SEMPRE VAZIO — 100% áudio original da TV!)
     - Bloco B (`kind`: "goal_replay"):
       • `video_start`: 5 a 10 segundos DEPOIS do `goal_original` (comemoração + replay!)
       • `score_home` / `score_away`: mesmo placar atualizado, `scorer_tag`: ""
       • `narration`: 15 a 22 palavras em {lp['lang_name']} com RIVALIDADE E PROVOCAÇÃO sobre esse gol (ex.: "{rivalry_ex}")
  4. ÚLTIMO BLOCO (`kind`: "final_whistle" — RESOLUÇÃO E DESFECHO OBRIGATÓRIO!):
     - `video_start`: últimos segundos da partida (juiz apitando o encerramento, entre {max(30, int(video_duration - 42))} e {max(35, int(video_duration - 8))}s)
     - `score_home` / `score_away`: placar final, `scorer_tag`: ""
     - `narration`: 18 a 25 palavras em {lp['lang_name']} FECHANDO A HISTÓRIA COM RESOLUÇÃO COMPLETA: anuncia o apito final do juiz, crava o placar final + quem venceu/calou o rival e entrega uma frase conclusiva de rivalidade fechando o arco aberto no Bloco 1 (NUNCA acabar do nada!).

REGRAS CRÍTICAS:
- `video_start` SEMPRE CRESCENTE (`bloco[i].video_start < bloco[i+1].video_start`). JAMAIS volte no tempo!
- Total de palavras somando TODAS as `narration` dos blocos: entre **140 e 168 palavras** (NUNCA passe de 175 palavras, para o vídeo cravar em 62–65s!).
- Em `goal_original`, `narration` DEVE ser `""`.
- Foco de torcida: {clube_foco or 'o clube protagonista'}.
Retorne APENAS o JSON:"""

    _log(log_cb, "[HighlightsPro] Planejando timeline estilo @highlightsmx (62-65s, placar ao vivo + áudio original nos gols)...")
    raw = generate_text_llm(prompt=prompt, config=config, expect_json=True, temperature=0.35, max_tokens=2200, timeout=120)
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    s, e = cleaned.find("{"), cleaned.rfind("}")
    return json.loads(cleaned[s : e + 1])


def _enforce_forward_blocks(
    raw_blocks: list[dict[str, Any]],
    video_duration: float,
) -> list[HighlightBlock]:
    """Garante ordem 100% cronológica para frente e placar sempre consistente."""
    parsed: list[HighlightBlock] = []
    max_t = max(12.0, video_duration - 7.0)
    min_next_t = 2.0
    cur_h, cur_a = 0, 0

    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "play").strip().lower()
        if kind not in ("kickoff", "play", "goal_original", "goal_replay", "final_whistle"):
            kind = "play"

        raw_start = float(item.get("video_start") or min_next_t)
        v_start = max(min_next_t, min(max_t, raw_start))

        sh = max(cur_h, int(item.get("score_home") if item.get("score_home") is not None else cur_h))
        sa = max(cur_a, int(item.get("score_away") if item.get("score_away") is not None else cur_a))
        cur_h, cur_a = sh, sa
        scorer_tag = str(item.get("scorer_tag") or "").strip()[:36]

        if kind == "goal_original":
            dur = max(3.8, min(4.8, float(item.get("duration") or 4.3)))
            narration = ""
        else:
            dur = max(3.2, min(8.5, float(item.get("duration") or 5.2)))
            narration = _clean_script_text(str(item.get("narration") or ""))
            if not narration:
                continue

        if v_start + dur > video_duration - 0.4:
            v_start = max(min_next_t, video_duration - dur - 0.6)
            if v_start < min_next_t:
                v_start = min(video_duration - 3.5, min_next_t)
                dur = max(3.0, min(dur, video_duration - v_start - 0.3))

        parsed.append(
            HighlightBlock(
                kind=kind,
                video_start=round(v_start, 2),
                duration=round(dur, 2),
                narration=narration,
                score_home=sh,
                score_away=sa,
                scorer_tag=scorer_tag if kind in ("goal_original", "goal_replay") else "",
            )
        )
        advance = dur if kind == "goal_original" else max(3.5, dur * 0.72)
        min_next_t = min(max_t, v_start + advance)

    if parsed and parsed[-1].kind != "final_whistle":
        parsed[-1].kind = "final_whistle"
    return parsed


def run_highlights_pro_pipeline(
    config: dict[str, Any],
    work_dir: Path,
    exclude_urls: set[str] | None = None,
    custom_url: str = "",
    placar_opcional: str = "",
    extra_match_context: str = "",
    log_cb: LogCallback | None = None,
    progress_cb: ProgressCallback | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[Path, Path, NewsArticle, ScriptPackage]:
    """Executa o fluxo completo do Highlights Futebol Pro (padrão @highlightsmx)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    idioma = str(config.get("idioma", "es-MX"))
    pref_langs = ("es", "en", "pt") if idioma.startswith("es") else ("pt", "es", "en")

    # 1) Descobre vídeo de highlights (3–20 min, bloqueando jogos completos de 90min e lives!)
    video = discover_youtube_candidate(
        config=config,
        mode="highlights_pro",
        exclude_urls=exclude_urls,
        custom_url=custom_url,
        log_cb=log_cb,
    )

    # 2) Baixa highlights HD COM ÁUDIO ORIGINAL
    src_video = work_dir / "highlights_source_av.mp4"
    dl = download_video_with_audio_hd(video.url, src_video, timeout=210, log_cb=log_cb)
    if not dl:
        raise RuntimeError(f"Não foi possível baixar o vídeo de highlights com áudio: {video.url}")
    vid_dur, _, _ = probe_video_info(dl)
    if vid_dur < 30.0:
        raise RuntimeError("Vídeo de highlights muito curto para montar resumo completo.")

    # 3) Timeline cronológica: transcrição (se existir) + varredura Gemini Vision (lê placar/minuto/gols!)
    transcript_digest = ""
    try:
        cues = fetch_transcript_cues(video.url, work_dir, preferred_langs=pref_langs, log_cb=log_cb)
        if cues:
            transcript_digest = _compress_transcript_digest(cues, block_seconds=10, max_chars=9000)
    except Exception:
        _log(log_cb, "[HighlightsPro] Sem legendas no YouTube — lendo placar, minuto e gols via IA Visão!")

    vision_lines: list[str] = []
    try:
        indexed = index_video_pool(
            contextual_videos=[dl],
            library_videos=[],
            scene_dur=4.0,
            work_dir=work_dir,
            config=config,
            log_cb=log_cb,
        )
        for sc in sorted(indexed, key=lambda s: s.start):
            vision_lines.append(f"[{int(sc.start)}s] {sc.description}")
    except Exception as exc:
        _log(log_cb, f"[HighlightsPro] Aviso na leitura visual ({exc}).")

    digest_parts: list[str] = []
    if vision_lines:
        digest_parts.append("LINHA DO TEMPO VISUAL (PLACAR, MINUTO E LANCES REAIS):\n" + "\n".join(vision_lines))
    if transcript_digest:
        digest_parts.append("TRANSCRIÇÃO DA TRANSMISSÃO:\n" + transcript_digest)
    if not digest_parts:
        step = max(15, int(vid_dur // 12))
        digest_parts.append("\n".join(f"[{t}s] Lance do jogo" for t in range(8, int(vid_dur) - 8, step)))
    digest = "\n\n".join(digest_parts)

    # 4) Planeja os blocos cronológicos estilo @highlightsmx
    plan_json = _plan_match_blocks_with_llm(
        video=video,
        digest=digest,
        video_duration=vid_dur,
        config=config,
        extra_match_context=extra_match_context,
        log_cb=log_cb,
    )
    blocks = _enforce_forward_blocks(plan_json.get("blocos") or [], video_duration=vid_dur)
    if len(blocks) < 3:
        raise RuntimeError("Não foi possível identificar blocos cronológicos suficientes nos highlights.")

    # 4b) Refina o segundo EXATO de cada gol pelo pico de áudio da TV ('GOOOOOL!' + torcida)
    # e alinha o replay imediatamente após o gol para GARANTIR que o chute + bola na rede + replay apareçam!
    for b_idx, blk in enumerate(blocks):
        if blk.kind == "goal_original":
            prev_limit = (blocks[b_idx - 1].video_start + blocks[b_idx - 1].duration + 0.5) if b_idx > 0 else 2.0
            refined = _find_goal_roar_start(dl, blk.video_start, vid_dur, min_t=prev_limit)
            if refined >= prev_limit:
                _log(log_cb, f"[HighlightsPro] Gol refinado por áudio TV: @{blk.video_start:.1f}s -> @{refined:.1f}s (chute + rede + grito!)")
                blk.video_start = refined
            blk.duration = round(max(5.2, min(6.2, blk.duration + 1.0)), 2)
            # Se o próximo bloco é o replay deste gol, posiciona logo após o gol (comemoração + replay da TV!)
            if b_idx + 1 < len(blocks) and blocks[b_idx + 1].kind == "goal_replay":
                blocks[b_idx + 1].video_start = round(min(vid_dur - 8.0, blk.video_start + blk.duration + 0.4), 2)

    # 5) Sintetiza o áudio TTS de cada bloco narrado (SEM calcular timestamps finais ainda -> zero drift!)
    block_audio_paths: list[Path | None] = []
    block_word_lists: list[list[WordTiming]] = []
    trans_dur = 0.12

    for b_idx, blk in enumerate(blocks):
        if blk.kind == "goal_original" or not blk.narration:
            block_audio_paths.append(None)
            block_word_lists.append([])
            blk.duration = round(max(5.2, min(6.2, blk.duration)), 2)
            _log(
                log_cb,
                f"[HighlightsPro] Bloco {b_idx+1} [GOL {blk.score_home}x{blk.score_away} • ÁUDIO ORIGINAL TV]: "
                f"@{blk.video_start:.1f}s ({blk.duration:.1f}s) | {blk.scorer_tag}",
            )
        else:
            mp3_p = work_dir / f"blk_{b_idx:02d}.mp3"
            tts_dur, blk_words = _synth_block_tts(blk.narration, mp3_p, config)
            blk.duration = round(max(2.6, tts_dur + 0.18), 2)
            if blk.video_start + blk.duration > vid_dur - 0.4:
                blk.video_start = max(0.0, vid_dur - blk.duration - 0.4)
            block_audio_paths.append(mp3_p)
            block_word_lists.append(blk_words)
            _log(
                log_cb,
                f"[HighlightsPro] Bloco {b_idx+1} [{blk.kind.upper()} {blk.score_home}x{blk.score_away}]: "
                f"@{blk.video_start:.1f}s ({blk.duration:.1f}s) | \"{blk.narration[:50]}...\"",
            )

    # Calcula duração total exata considerando acrossfade
    def _calc_total_dur() -> float:
        t_cur = blocks[0].duration
        for k in range(1, len(blocks)):
            t_cur = max(0.05, t_cur - trans_dur) + blocks[k].duration
        return t_cur

    # Se ficou abaixo de 62.0s, alonga gols (pega mais do replay!) e bloco final ANTES de gerar legendas!
    cur_total = _calc_total_dur()
    if cur_total < 62.0:
        missing = 62.8 - cur_total
        goal_indices = [i for i, b in enumerate(blocks) if b.kind == "goal_original"]
        extra_per_goal = min(1.5, missing / max(1, len(goal_indices) + 1))
        for gi in goal_indices:
            blocks[gi].duration = round(blocks[gi].duration + extra_per_goal, 2)
        cur_total = _calc_total_dur()
        if cur_total < 62.2:
            blocks[-1].duration = round(blocks[-1].duration + (62.6 - cur_total), 2)

    # Re-garante monotonicidade estrita dos video_start
    for i in range(1, len(blocks)):
        min_start = blocks[i - 1].video_start + max(2.2, blocks[i - 1].duration * 0.72)
        if blocks[i].video_start < min_start:
            blocks[i].video_start = round(min(vid_dur - blocks[i].duration - 0.35, min_start), 2)

    # AGORA SIM (com TODAS as durações 100% congeladas!), monta os timestamps das legendas
    # usando EXATAMENTE a mesma fórmula de offset do acrossfade no FFmpeg -> 0.000s de drift!
    all_words: list[WordTiming] = []
    cum_t = 0.0
    for b_idx, (blk, blk_words) in enumerate(zip(blocks, block_word_lists)):
        blk_start_in_timeline = 0.0 if b_idx == 0 else max(0.05, cum_t - trans_dur)
        if blk_words:
            for wt in blk_words:
                w_s = blk_start_in_timeline + wt.start
                w_e = min(blk_start_in_timeline + blk.duration - 0.05, blk_start_in_timeline + wt.end)
                if w_e > w_s:
                    all_words.append(WordTiming(text=wt.text, start=w_s, end=w_e))
        cum_t = blk_start_in_timeline + blk.duration

    # 6) Resolve formato de tela (Automático -> 1:1 1080x1080 igual @highlightsmx, ou 9:16 1080x1920!)
    video_cfg = config.get("video", {})
    fmt_choice = str(video_cfg.get("formato_tela", "auto")).strip().lower()
    if fmt_choice == "9x16":
        width, height, aspect_str = 1080, 1920, "9:16"
        dar_expr = "9/16"
    else:
        # "auto" ou "1x1" -> Quadrado 1080x1080 limpo (padrão dos vídeos de 106K views do @highlightsmx!)
        width, height, aspect_str = 1080, 1080, "1:1"
        dar_expr = "1/1"

    fps = int(video_cfg.get("fps", 30))
    niche_id = str(config.get("nicho_ativo", "futebol_mexicano"))
    paths_dict = config.get("paths") or {}
    output_dir = Path(paths_dict["output_dir"]) if paths_dict.get("output_dir") else OUTPUT_DIR
    music_dirs = [Path(paths_dict["music_dir"]), MUSIC_DIR] if paths_dict.get("music_dir") else [MUSIC_DIR]
    brand_dirs = [Path(paths_dict["branding_dir"]), BRANDING_DIR] if paths_dict.get("branding_dir") else [BRANDING_DIR]
    output_dir.mkdir(parents=True, exist_ok=True)

    # 7) Escudos reais dos 2 clubes + Placar dinâmico por bloco (atualiza ao vivo gol a gol!)
    home_team = str(plan_json.get("home_team") or "Club América").strip()
    away_team = str(plan_json.get("away_team") or "Rival").strip()
    _log(log_cb, f"[HighlightsPro] Carregando escudos oficiais: {home_team} x {away_team} | Formato: {width}x{height} ({aspect_str})")
    home_crest = fetch_team_crest(home_team, size=124)
    away_crest = fetch_team_crest(away_team, size=124)

    accent_rgb = (34, 197, 94) if niche_id == "futebol_rivalidade" else (250, 204, 21)
    score_pngs: list[Path] = []
    for b_idx, blk in enumerate(blocks):
        sp = work_dir / f"score_blk_{b_idx:02d}.png"
        create_live_scoreboard_png(
            home_team=home_team,
            away_team=away_team,
            home_crest=home_crest,
            away_crest=away_crest,
            score_home=blk.score_home,
            score_away=blk.score_away,
            scorer_tag=blk.scorer_tag,
            output_path=sp,
            width=width,
            height=height,
            accent_rgb=accent_rgb,
        )
        score_pngs.append(sp)

    # 8) Legendas minimalistas e limpas estilo @highlightsmx (~46px, cor do clube, sem poluir!)
    sub_cfg = dict(config.get("legenda") or {})
    sub_cfg["tamanho_fonte"] = 44 if height <= 1080 else 52
    sub_cfg["espessura_contorno"] = 5
    sub_cfg["sombra"] = 2
    sub_cfg["palavras_por_bloco"] = 3
    sub_cfg_runtime = dict(config)
    sub_cfg_runtime["video"] = {**video_cfg, "largura": width, "altura": height}
    sub_cfg_runtime["legenda"] = sub_cfg
    ass_path = work_dir / "highlights_subs.ass"
    sub_pos_y = 955 if height <= 1080 else 1560
    build_tiktok_ass(all_words, ass_path, sub_cfg_runtime, pos_y=sub_pos_y)

    # 9) Trilha sonora de fundo (com ducking nos gols -> 0% nos gols, 15% na narração!)
    usar_musica = bool(video_cfg.get("usar_musica", True))
    music_path: Path | None = None
    if usar_musica:
        music_path = _pick_random_asset(music_dirs, (".mp3", ".wav", ".m4a", ".ogg"))
        if not music_path:
            music_path = _generate_cinematic_bed(work_dir, niche_id=niche_id)

    branding_path = _pick_random_asset(brand_dirs, (".png",))
    filter_label, color_vf = get_video_filter_chain(video_cfg.get("filtro_cor", "vivido2"))
    _log(log_cb, f"[HighlightsPro] Filtro visual: {filter_label} | Tela cheia limpa {width}x{height} ({aspect_str})")

    # 10) Filtergraph FFmpeg: Tela cheia limpa (ZERO barras borradas!) + Placar dinâmico ao vivo + Ducking nos gols!
    inputs: list[str] = []
    idx_c = 0
    blk_vid_indices: list[int] = []
    blk_score_indices: list[int] = []
    blk_tts_indices: list[int | None] = []

    for blk, sp, mp3_p in zip(blocks, score_pngs, block_audio_paths):
        inputs.extend(["-ss", f"{blk.video_start:.2f}", "-t", f"{blk.duration + 0.35:.2f}", "-i", str(dl)])
        blk_vid_indices.append(idx_c)
        idx_c += 1
        inputs.extend(["-i", str(sp)])
        blk_score_indices.append(idx_c)
        idx_c += 1
        if mp3_p is not None:
            inputs.extend(["-i", str(mp3_p)])
            blk_tts_indices.append(idx_c)
            idx_c += 1
        else:
            blk_tts_indices.append(None)

    music_idx: int | None = None
    if music_path is not None:
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
        music_idx = idx_c
        idx_c += 1

    branding_idx: int | None = None
    if branding_path is not None:
        inputs.extend(["-i", str(branding_path)])
        branding_idx = idx_c
        idx_c += 1

    norm_tail = f"fps={fps},settb=AVTB,scale=in_range=auto:out_range=tv:out_color_matrix=bt709,format=yuv420p,setsar=1"
    filter_lines: list[str] = []
    v_labels: list[tuple[str, float]] = []
    a_labels: list[str] = []

    for i, (blk, v_idx, s_idx, t_idx) in enumerate(zip(blocks, blk_vid_indices, blk_score_indices, blk_tts_indices)):
        dur = blk.duration
        v_lbl = f"vseg_{i:02d}"
        a_lbl = f"aseg_{i:02d}"

        # TELA CHEIA LIMPA (1080x1080 1:1 ou 1080x1920 9:16 — ZERO barras borradas!) + Placar vivo no topo!
        filter_lines.append(
            f"[{v_idx}:v]tpad=stop_mode=clone:stop_duration=0.6,trim=start=0:duration={dur:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height}:(iw-ow)/2:(ih-oh)*0.45,{color_vf}[base_{i}]"
        )
        filter_lines.append(
            f"[base_{i}][{s_idx}:v]overlay=0:0,{norm_tail}[{v_lbl}]"
        )
        v_labels.append((v_lbl, dur))

        # ÁUDIO:
        # - Nos GOLS (`t_idx is None`): 100% ÁUDIO ORIGINAL DA TV (zero voz TTS, zero música competindo!)
        # - Nos blocos narrados: Voz TTS (112%) + estádio original baixinho (14%)
        if t_idx is None:
            filter_lines.append(
                f"[{v_idx}:a]aresample=44100,aformat=channel_layouts=stereo,"
                f"apad=whole_dur={dur:.3f},atrim=start=0:duration={dur:.3f},asetpts=PTS-STARTPTS,volume=1.18,"
                f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0.15, dur - 0.14):.3f}:d=0.14[{a_lbl}]"
            )
        else:
            filter_lines.append(
                f"[{v_idx}:a]aresample=44100,aformat=channel_layouts=stereo,"
                f"apad=whole_dur={dur:.3f},atrim=start=0:duration={dur:.3f},asetpts=PTS-STARTPTS,volume=0.14[amb_{i}]"
            )
            filter_lines.append(
                f"[{t_idx}:a]aresample=44100,aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},"
                f"atrim=start=0:duration={dur:.3f},asetpts=PTS-STARTPTS,volume=1.12[tts_{i}]"
            )
            filter_lines.append(
                f"[tts_{i}][amb_{i}]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[{a_lbl}]"
            )
        a_labels.append(a_lbl)

    cur_v, cum_dur = v_labels[0]
    cur_a = a_labels[0]
    goal_windows: list[tuple[float, float]] = []
    if blocks[0].kind == "goal_original":
        goal_windows.append((0.0, blocks[0].duration))

    for step_i in range(1, len(v_labels)):
        next_v, next_dur = v_labels[step_i]
        next_a = a_labels[step_i]
        offset = max(0.05, cum_dur - trans_dur)
        if blocks[step_i].kind == "goal_original":
            goal_windows.append((offset, offset + next_dur))
        out_v = f"xfv_{step_i:02d}"
        out_a = f"xfa_{step_i:02d}"
        filter_lines.append(
            f"[{cur_v}][{next_v}]xfade=transition=fadefast:duration={trans_dur:.3f}:offset={offset:.3f}[{out_v}]"
        )
        filter_lines.append(
            f"[{cur_a}][{next_a}]acrossfade=d={trans_dur:.3f}:c1=tri:c2=tri[{out_a}]"
        )
        cur_v = out_v
        cur_a = out_a
        cum_dur = offset + next_dur

    if branding_idx is not None:
        filter_lines.append(f"[{branding_idx}:v]scale=130:-1,format=rgba,colorchannelmixer=aa=0.75[wm]")
        filter_lines.append(f"[{cur_v}][wm]overlay=W-w-28:H-h-28[v_wm]")
        cur_v = "v_wm"

    escaped_ass = escape_ffmpeg_filter_path(ass_path)
    filter_lines.append(
        f"[{cur_v}]ass=filename='{escaped_ass}',scale={width}:{height},setsar=1,setdar={dar_expr},format=yuv420p[vout]"
    )

    # Música de fundo com ducking automático nos gols (volume 0.02 durante o grito de gol, 0.14 na narração!)
    if music_idx is not None:
        if goal_windows:
            cond = "+".join(f"between(t,{gs:.2f},{ge:.2f})" for gs, ge in goal_windows)
            vol_expr = f"if({cond},0.02,0.14)"
        else:
            vol_expr = "0.14"
        fade_out_m = max(0.5, cum_dur - 1.5)
        filter_lines.append(
            f"[{music_idx}:a]atrim=0:{cum_dur:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=44100,aformat=channel_layouts=stereo,"
            f"volume='{vol_expr}':eval=frame,"
            f"afade=t=in:st=0:d=0.6,afade=t=out:st={fade_out_m:.3f}:d=1.5[bgm]"
        )
        filter_lines.append(
            f"[{cur_a}][bgm]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]"
        )
    else:
        filter_lines.append(f"[{cur_a}]alimiter=limit=0.95[aout]")

    filter_script_path = work_dir / "filter_highlights_pro.txt"
    filter_script_path.write_text(";\n".join(filter_lines), encoding="utf-8")

    gancho = str(plan_json.get("gancho_tela") or f"{home_team} vs {away_team}").strip().upper()[:54]
    descricao = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", str(plan_json.get("descricao_tiktok") or video.title)).strip()
    hashtags = _normalize_hashtags(plan_json.get("hashtags"), config)
    full_roteiro = " ".join(b.narration for b in blocks if b.narration).strip()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_slug = slugify(f"{home_team}_vs_{away_team}", max_length=42)
    output_mp4 = output_dir / f"{ts}_hlmx_{aspect_str.replace(':', 'x')}_{base_slug}.mp4"
    output_txt = output_dir / f"{ts}_hlmx_{aspect_str.replace(':', 'x')}_{base_slug}.txt"

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
        f"{cum_dur:.3f}",
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

    _log(log_cb, f"[HighlightsPro] Renderizando FFmpeg -> nichos/{niche_id}/output/{output_mp4.name} (~{cum_dur:.1f}s)")
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
            cur_t = _parse_ffmpeg_time(stripped)
            if cur_t is not None and progress_cb and cum_dur > 0:
                progress_cb(max(0.0, min(1.0, cur_t / cum_dur)))
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
        raise RuntimeError(f"FFmpeg falhou no Highlights Pro (código {ret}):\n" + "\n".join(stderr_tail[-12:]))

    save_post_metadata_txt(
        txt_path=output_txt,
        title=video.title,
        source=f"Highlights Pro ({aspect_str}) • {video.channel}",
        url=video.url,
        gancho_tela=gancho,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        roteiro=full_roteiro,
    )
    if progress_cb:
        progress_cb(1.0)

    size_mb = output_mp4.stat().st_size / (1024 * 1024)
    _log(log_cb, f"[HighlightsPro] Vídeo concluído! {output_mp4.name} ({size_mb:.1f} MB, {cum_dur:.1f}s) + {output_txt.name}")

    article = NewsArticle(title=video.title, url=video.url, source=f"Highlights Pro • {video.channel}")
    script = ScriptPackage(
        roteiro=full_roteiro,
        gancho_tela=gancho,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_count=len(full_roteiro.split()),
    )
    return output_mp4, output_txt, article, script
