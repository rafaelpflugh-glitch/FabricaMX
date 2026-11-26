"""Módulo Nível 2: Indexador Semântico de Cenas com IA Visão Local (Ollama) + Sincronia por Narração."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from src import normalize_text
from src.scriptwriter import list_ollama_models
from src.tts_subtitles import WordTiming
from src.video_fetcher import probe_video_info

LogCallback = Callable[[str], None]

PREFERRED_VISION_MODELS = (
    "moondream",
    "qwen2.5vl",
    "llava",
    "llama3.2-vision",
    "minicpm-v",
    "gemma3",
)


@dataclass
class IndexedScene:
    """Uma cena de vídeo indexada semanticamente com descrição visual."""

    video_path: Path
    start: float
    width: int
    height: int
    description: str
    is_contextual: bool = False


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def find_vision_model(ollama_url: str = "http://localhost:11434") -> str | None:
    """Detecta automaticamente o melhor modelo de visão instalado no Ollama local."""
    try:
        models = list_ollama_models(ollama_url, timeout=6)
    except Exception:
        return None

    for pref in PREFERRED_VISION_MODELS:
        for m in models:
            if pref in m.lower():
                return m
    return None


def _extract_frame_b64(video_path: Path, timestamp_s: float, temp_jpg: Path) -> str | None:
    """Extrai um frame leve (384px) em timestamp exato via FFmpeg e retorna base64."""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp_s:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=384:-2",
        "-q:v",
        "5",
        str(temp_jpg),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0 and temp_jpg.exists() and temp_jpg.stat().st_size > 1000:
            raw = temp_jpg.read_bytes()
            temp_jpg.unlink(missing_ok=True)
            return base64.b64encode(raw).decode("ascii")
    except Exception:
        pass
    temp_jpg.unlink(missing_ok=True)
    return None


def _describe_frame_ollama(
    b64_img: str,
    vision_model: str,
    ollama_url: str,
    niche_hint: str,
) -> str:
    """Pede ao modelo de visão local uma descrição curta e direta da cena."""
    # Moondream funciona muito melhor com prompt simples e direto ("Describe this image briefly.")
    if "moondream" in vision_model.lower():
        prompt = "Describe this image briefly."
    else:
        prompt = f"Describe what is happening in this {niche_hint} frame in one short sentence."

    payload = {
        "model": vision_model,
        "prompt": prompt,
        "images": [b64_img],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 65},
    }
    try:
        resp = requests.post(f"{ollama_url.rstrip('/')}/api/generate", json=payload, timeout=35)
        resp.raise_for_status()
        text = str(resp.json().get("response") or "").strip()
        text = re.sub(r"(!+\s*IMPORTANT\s*!+|This is the wrong answer\.?)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:160] if len(text) >= 8 else ""
    except Exception:
        return ""


def _load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache_path: Path, data: dict[str, Any]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def index_video_pool(
    contextual_videos: list[Path],
    library_videos: list[Path],
    scene_dur: float,
    work_dir: Path,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> list[IndexedScene]:
    """
    Indexa semanticamente os vídeos do pool:
    - Vídeos permanentes da pasta `assets/broll/` são indexados 1 ÚNICA VEZ e salvos em `scene_index.json`!
    - Vídeos contextuais baixados para esta pauta são indexados rapidamente sob demanda (~10-14 amostras).
    """
    ollama_url = str(config.get("ollama_url", "http://localhost:11434"))
    niche_id = str(config.get("nicho_ativo", "gta_vi"))
    niche_hint = "GTA 6 video game" if niche_id == "gta_vi" else "Mexican football soccer match"
    paths_dict = config.get("paths") or {}
    broll_dir = Path(paths_dict["broll_dir"]) if paths_dict.get("broll_dir") else work_dir
    cache_path = broll_dir / "scene_index.json"

    vision_model = find_vision_model(ollama_url)
    if not vision_model:
        _log(
            log_cb,
            "[SceneMatcher] Nenhum modelo de visão instalado no Ollama (ex.: 'ollama pull moondream'). "
            "Usando seleção inteligente padrão.",
        )
        return []

    cache = _load_cache(cache_path)
    cache_dirty = False
    indexed: list[IndexedScene] = []
    tmp_frame = work_dir / "_vision_probe.jpg"

    def _index_one(vf: Path, is_ctx: bool, max_samples: int) -> None:
        nonlocal cache_dirty
        dur, w, h = probe_video_info(vf)
        if dur < scene_dur + 1.0:
            return

        cache_key = f"{vf.name}:{vf.stat().st_size}"
        if not is_ctx and cache_key in cache:
            cached_entries = cache[cache_key]
            for item in cached_entries:
                t_val = float(item.get("start", 0.0))
                if t_val + scene_dur <= dur - 0.5:
                    indexed.append(
                        IndexedScene(
                            video_path=vf,
                            start=t_val,
                            width=w,
                            height=h,
                            description=str(item.get("desc", "")),
                            is_contextual=False,
                        )
                    )
            _log(log_cb, f"[SceneMatcher] Cache carregado para {vf.name} ({len(cached_entries)} cenas indexadas).")
            return

        start_margin = 3.5 if dur > 25.0 else 0.5
        end_limit = dur - scene_dur - (5.0 if dur > 30.0 else 0.5)
        if end_limit <= start_margin:
            timestamps = [0.0]
        else:
            step = max(scene_dur + 0.8, (end_limit - start_margin) / max(1, max_samples - 1))
            timestamps = []
            t = start_margin
            while t <= end_limit and len(timestamps) < max_samples:
                timestamps.append(round(t, 2))
                t += step

        tag = "vídeo contextual" if is_ctx else "biblioteca (cache permanente)"
        _log(
            log_cb,
            f"[SceneMatcher] Indexando {len(timestamps)} cenas de {vf.name} ({tag}) com {vision_model}...",
        )

        entries_for_cache: list[dict[str, Any]] = []
        for ts in timestamps:
            # Pega o frame do meio da cena (ts + scene_dur / 2) para refletir a ação principal
            b64 = _extract_frame_b64(vf, ts + scene_dur * 0.45, tmp_frame)
            if not b64:
                continue
            desc = _describe_frame_ollama(b64, vision_model, ollama_url, niche_hint)
            if not desc:
                desc = f"{niche_hint} action scene"
            indexed.append(
                IndexedScene(
                    video_path=vf,
                    start=ts,
                    width=w,
                    height=h,
                    description=desc,
                    is_contextual=is_ctx,
                )
            )
            entries_for_cache.append({"start": ts, "desc": desc})

        if not is_ctx and entries_for_cache:
            cache[cache_key] = entries_for_cache
            cache_dirty = True

    # Indexa vídeos contextuais primeiro (12 amostras por vídeo contextual)
    for vf in contextual_videos:
        _index_one(vf, is_ctx=True, max_samples=12)

    # Indexa biblioteca permanente (até 26 amostras por vídeo -> salvo em cache para sempre!)
    for vf in library_videos:
        _index_one(vf, is_ctx=False, max_samples=26)

    if cache_dirty:
        _save_cache(cache_path, cache)
        _log(log_cb, f"[SceneMatcher] Índice visual salvo em {cache_path.name} (não precisará reindexar!).")

    return indexed


def _build_narration_slots(
    word_timings: list[WordTiming],
    total_duration: float,
    num_slots: int,
) -> list[str]:
    """Agrupa as palavras narradas em cada janela temporal correspondente às N cenas do vídeo."""
    if num_slots <= 0:
        return []
    slot_len = max(0.5, total_duration / num_slots)
    buckets: list[list[str]] = [[] for _ in range(num_slots)]

    for wt in word_timings:
        mid = (wt.start + wt.end) * 0.5
        idx = min(num_slots - 1, max(0, int(mid / slot_len)))
        buckets[idx].append(wt.text)

    return [" ".join(b).strip() or "(transição visual)" for b in buckets]


def _fallback_keyword_match(
    narration_slots: list[str],
    scenes: list[IndexedScene],
) -> list[IndexedScene]:
    """Fallback local rápido caso o LLM de texto falhe ao retornar o JSON de mapeamento."""
    chosen: list[IndexedScene] = []
    used_indices: set[int] = set()

    for slot_text in narration_slots:
        slot_tokens = set(re.findall(r"[a-z0-9]{3,}", normalize_text(slot_text)))
        best_idx = -1
        best_score = -1.0

        for idx, sc in enumerate(scenes):
            sc_tokens = set(re.findall(r"[a-z0-9]{3,}", normalize_text(sc.description)))
            overlap = len(slot_tokens & sc_tokens)
            score = float(overlap * 3)
            if sc.is_contextual:
                score += 1.5
            if idx in used_indices:
                score -= 10.0
            if chosen and chosen[-1].video_path == sc.video_path and abs(chosen[-1].start - sc.start) < 1.0:
                score -= 20.0
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx < 0:
            best_idx = len(chosen) % len(scenes)
        used_indices.add(best_idx)
        chosen.append(scenes[best_idx])

    return chosen


def match_scenes_to_narration(
    word_timings: list[WordTiming],
    total_duration: float,
    num_scenes: int,
    indexed_scenes: list[IndexedScene],
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> list[tuple[Path, float, int, int]]:
    """
    Casa semanticamente cada janela de fala da narração com a melhor cena indexada via Ollama,
    evitando repetições e priorizando os vídeos contextuais da pauta!
    """
    if not indexed_scenes:
        return []

    narration_slots = _build_narration_slots(word_timings, total_duration, num_scenes)
    ollama_url = str(config.get("ollama_url", "http://localhost:11434"))
    text_model = str(config.get("ollama_modelo", "qwen3:8b"))

    catalog_lines = []
    for idx, sc in enumerate(indexed_scenes):
        src_tag = "[CONTEXTO_NOTICIA]" if sc.is_contextual else "[BIBLIOTECA]"
        catalog_lines.append(f"{idx}: {src_tag} {sc.description}")

    slots_lines = [f"SLOT {i}: \"{txt}\"" for i, txt in enumerate(narration_slots)]

    prompt = f"""Você é um editor profissional de vídeo para TikTok.
Sua tarefa é escolher a MELHOR cena visual do CATÁLOGO abaixo para cada SLOT da narração, garantindo máxima coerência visual entre o que está sendo falado e o que aparece na tela.

SLOTS DA NARRAÇÃO ({num_scenes} slots em ordem cronológica):
{chr(10).join(slots_lines)}

CATÁLOGO DE CENAS DISPONÍVEIS (ID: descrição):
{chr(10).join(catalog_lines)}

REGRAS ESTRITAS:
1. Retorne APENAS um JSON válido no formato: {{"scene_ids": [id0, id1, ...]}} com EXATAMENTE {num_scenes} números inteiros.
2. Casa semanticamente cada SLOT com a cena que melhor ilustra o que está sendo dito (ex.: falou 'prisão' -> cena de prisão; falou 'barco/pesca/praia' -> cena de barco/mar/praia; falou 'polícia/carro' -> cena de carro/polícia; falou 'gol/ataque' -> cena de jogadores/gol).
3. Priorize cenas marcadas com [CONTEXTO_NOTICIA] quando combinarem com o texto.
4. EVITE repetir o mesmo ID de cena (e NUNCA coloque o mesmo ID em dois slots seguidos).

Responda APENAS com o JSON:"""

    payload: dict[str, Any] = {
        "model": text_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.25, "num_predict": 400},
    }
    if "qwen3" in text_model.lower():
        payload["think"] = False

    selected_scenes: list[IndexedScene] = []
    try:
        _log(log_cb, f"[SceneMatcher] Sincronizando {num_scenes} trechos de fala com {len(indexed_scenes)} cenas indexadas via {text_model}...")
        resp = requests.post(f"{ollama_url.rstrip('/')}/api/generate", json=payload, timeout=90)
        if resp.status_code == 400 and "think" in payload:
            payload.pop("think", None)
            resp = requests.post(f"{ollama_url.rstrip('/')}/api/generate", json=payload, timeout=90)
        resp.raise_for_status()
        raw = str(resp.json().get("response") or "")
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start : end + 1])
        ids = parsed.get("scene_ids") or []

        used_prev = -1
        for slot_i in range(num_scenes):
            raw_id = int(ids[slot_i]) if slot_i < len(ids) else (slot_i % len(indexed_scenes))
            if raw_id < 0 or raw_id >= len(indexed_scenes):
                raw_id = slot_i % len(indexed_scenes)
            if raw_id == used_prev and len(indexed_scenes) > 1:
                raw_id = (raw_id + 1) % len(indexed_scenes)
            used_prev = raw_id
            selected_scenes.append(indexed_scenes[raw_id])

        # Loga amostra da sincronia para transparência no console do app
        for i in range(min(4, len(selected_scenes))):
            _log(
                log_cb,
                f"[SceneMatcher] Cena {i+1}: \"{narration_slots[i][:42]}...\" -> "
                f"{selected_scenes[i].video_path.name}@{selected_scenes[i].start:.1f}s ({selected_scenes[i].description[:50]})",
            )
    except Exception as exc:
        _log(log_cb, f"[SceneMatcher] Aviso no mapeamento LLM ({exc}). Usando casamento local por palavras-chave.")
        selected_scenes = _fallback_keyword_match(narration_slots, indexed_scenes)

    return [(sc.video_path, sc.start, sc.width, sc.height) for sc in selected_scenes]
