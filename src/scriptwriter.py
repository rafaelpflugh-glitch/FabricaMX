"""Módulo de geração de roteiros via Ollama local em espanhol mexicano para TikTok."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from src.scraper import NewsArticle

LogCallback = Callable[[str], None]

GREETING_PATTERNS = (
    r"^\s*[¡!]?\s*(hola|qué tal|que tal|bienvenidos|buenos días|buenas tardes|buenas noches|"
    r"saludos|amigos|afición|banda|mi gente)\b[^.!?]*[.!?]\s*"
)


@dataclass
class ScriptPackage:
    """Pacote estruturado de roteiro e metadados gerado pelo Ollama."""

    roteiro: str
    gancho_tela: str
    termos_busca_imagens: list[str] = field(default_factory=list)
    descricao_tiktok: str = ""
    hashtags: list[str] = field(default_factory=list)
    word_count: int = 0


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb:
        log_cb(msg)


def list_ollama_models(ollama_url: str = "http://localhost:11434", timeout: int = 8) -> list[str]:
    """Retorna lista de modelos instalados localmente no servidor Ollama."""
    base = ollama_url.rstrip("/")
    resp = requests.get(f"{base}/api/tags", timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    models = payload.get("models") or []
    return [m["name"] for m in models if isinstance(m, dict) and m.get("name")]


def check_ollama_health(ollama_url: str = "http://localhost:11434") -> tuple[bool, list[str], str]:
    """Verifica se o servidor Ollama está ativo e retorna os modelos instalados."""
    try:
        models = list_ollama_models(ollama_url)
        return True, models, "OK"
    except Exception as exc:
        return False, [], str(exc)


def _strip_reasoning_and_fences(raw: str) -> str:
    """Remove blocos <think>...</think> e cercas markdown ```json ... ```."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Extrai e faz parse de um objeto JSON mesmo que venha texto ao redor."""
    cleaned = _strip_reasoning_and_fences(raw)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start : end + 1]
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data

    raise ValueError("A resposta do Ollama não contém um JSON válido.")


def _clean_script_text(text: str) -> str:
    """Limpa marcações indevidas (emojis, colchetes, asteriscos) para narração TTS limpa."""
    s = (text or "").strip()
    # Remove anotações de palco entre colchetes ou parênteses de direção
    s = re.sub(r"\[[^\]]*\]", " ", s)
    # Remove markdown bold/italic
    s = s.replace("**", "").replace("__", "").replace("*", "").replace("#", "")
    # Remove emojis e símbolos pictográficos para não confundir TTS/legendas
    s = re.sub(
        r"[\U00010000-\U0010FFFF\u2600-\u27BF]+",
        "",
        s,
    )
    # Remove saudações genéricas se o modelo teimar em colocar
    s = re.sub(GREETING_PATTERNS, "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _count_words(text: str) -> int:
    return len(re.findall(r"\b[\wÁÉÍÓÚÜÑáéíóúüñ'-]+\b", text))


def _normalize_hashtags(raw_hashtags: Any, club_focus: str) -> list[str]:
    tags: list[str] = []
    if isinstance(raw_hashtags, str):
        parts = re.split(r"[\s,]+", raw_hashtags)
    elif isinstance(raw_hashtags, list):
        parts = [str(item) for item in raw_hashtags]
    else:
        parts = []

    for p in parts:
        cleaned = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]", "", p.lstrip("#"))
        if cleaned:
            tag = f"#{cleaned}"
            if tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)

    defaults = ["#LigaMX", "#FutbolMexicano", "#TikTokDeportes"]
    club_tag = "#" + re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]", "", club_focus)
    if len(club_tag) > 2:
        defaults.insert(0, club_tag)

    for d in defaults:
        if d.lower() not in {t.lower() for t in tags}:
            tags.append(d)

    return tags[:10]


def _build_main_prompt(article: NewsArticle, club_focus: str) -> str:
    return f"""Eres un creador de contenido viral y periodista deportivo mexicano apasionado de la Liga MX (enfoque: {club_focus}).
Analiza la siguiente noticia real y genera un paquete completo para un video vertical de TikTok de entre 60 y 75 segundos.

NOTICIA:
- Fuente: {article.source}
- Titular: {article.title}
- Contenido:
{article.full_text}

REGLAS ESTRICTAS (OBLIGATORIAS):
1. Responde ÚNICAMENTE con un objeto JSON válido, sin explicaciones ni texto extra.
2. El JSON debe tener EXACTAMENTE estas 5 claves:
   - "roteiro": string
   - "gancho_tela": string
   - "termos_busca_imagens": array de 5 strings
   - "descricao_tiktok": string
   - "hashtags": array de 6 a 8 strings
3. Reglas para "roteiro":
   - Idioma: Español mexicano coloquial, vibrante, directo y apasionado (usa expresiones naturales del fútbol mexicano como "la afición", "bombazo", "la neta", "en la cancha", "se prendió todo", sin exagerar ni perder credibilidad periodística).
   - Extensión EXACTA: entre 155 y 180 palabras (cuenta bien las palabras para superar 1 minuto de locución).
   - PROHIBIDO empezar con saludos genéricos ("Hola", "Qué tal", "Bienvenidos", "Amigos", "Hoy les traigo").
   - Abre INMEDIATAMENTE con un gancho polémico, impactante o urgente sobre la noticia.
   - Desarrolla el contexto real, los nombres clave de jugadores/técnicos/directivos y el impacto en la Liga MX.
   - Cierra con una pregunta directa y provocativa para detonar debate en los comentarios.
   - Texto plano listo para narración (TTS): SIN emojis, SIN corchetes, SIN indicaciones de cámara, SIN asteriscos.
4. Reglas para "gancho_tela":
   - Texto MUY corto (3 a 6 palabras) EN MAYÚSCULAS para un banner de 3 segundos en pantalla (ejemplo: "¡BOMBAZO EN COAPA!" o "¿TRAICIÓN EN LA LIGA MX?").
5. Reglas para "termos_busca_imagens":
   - Lista de exactamente 5 términos de búsqueda en español muy específicos sobre los jugadores, técnicos, clubes y estadio mencionados en la noticia (para buscar fotografías reales de fútbol).
6. Reglas para "descricao_tiktok":
   - 1 o 2 frases llamativas en español mexicano que inviten a comentar.
7. Reglas para "hashtags":
   - 6 a 8 hashtags relevantes empezando con #, incluyendo #LigaMX y el club protagonista.

Devuelve SOLO el JSON:"""


def _build_length_adjustment_prompt(current_json: dict[str, Any], current_words: int) -> str:
    direction = "EXPANDE" if current_words < 155 else "RECORTE"
    return f"""El siguiente JSON tiene un "roteiro" con {current_words} palabras, pero DEBE tener ESTRICTAMENTE entre 155 y 180 palabras.
{direction} el campo "roteiro" para que quede entre 160 y 175 palabras manteniendo:
- Español mexicano coloquial y apasionado
- Sin saludos genéricos al inicio (gancho polémico directo)
- Mismos datos reales de la noticia
- Cierre con pregunta provocativa para comentarios
- Sin emojis, corchetes ni asteriscos
Mantén las otras 4 claves intactas ("gancho_tela", "termos_busca_imagens", "descricao_tiktok", "hashtags").

JSON ACTUAL:
{json.dumps(current_json, ensure_ascii=False, indent=2)}

Responde ÚNICAMENTE con el JSON corregido:"""


def _call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout: int = 180,
) -> str:
    endpoint = f"{ollama_url.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.72,
            "top_p": 0.9,
            "num_predict": 1024,
        },
    }
    resp = requests.post(endpoint, json=payload, timeout=timeout)
    if resp.status_code == 404:
        raise RuntimeError(
            f"Modelo Ollama '{model}' não encontrado localmente. "
            f"Execute 'ollama pull {model}' ou selecione um modelo instalado."
        )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("response", "")
    if not text:
        raise RuntimeError("Ollama retornou uma resposta vazia.")
    return text


def generate_script(
    article: NewsArticle,
    config: dict[str, Any],
    log_cb: LogCallback | None = None,
) -> ScriptPackage:
    """Gera o roteiro estruturado em JSON via Ollama local com validação e ajuste automático."""
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_modelo", "llama3.1:latest")
    timeout = int(config.get("ollama_timeout", 180))
    club_focus = config.get("clube_foco", "Club América")

    _log(log_cb, f"[Scriptwriter] Conectando ao Ollama ({model}) em {ollama_url}...")
    prompt = _build_main_prompt(article, club_focus)

    data: dict[str, Any] | None = None
    last_error: Exception | None = None

    for attempt in range(1, 3):
        try:
            raw = _call_ollama(ollama_url, model, prompt, timeout=timeout)
            data = _parse_json_response(raw)
            if not data.get("roteiro"):
                raise ValueError("Campo 'roteiro' ausente ou vazio no JSON.")
            break
        except Exception as exc:
            last_error = exc
            _log(log_cb, f"[Scriptwriter] Tentativa {attempt}/2 falhou ({exc}).")

    if data is None:
        raise RuntimeError(f"Falha ao gerar roteiro via Ollama: {last_error}")

    roteiro = _clean_script_text(str(data.get("roteiro", "")))
    wc = _count_words(roteiro)
    _log(log_cb, f"[Scriptwriter] Roteiro inicial gerado com {wc} palavras.")

    # Ajuste automático se fugir muito da faixa 155-180 palavras
    if wc < 145 or wc > 195:
        _log(
            log_cb,
            f"[Scriptwriter] Ajustando tamanho do roteiro ({wc} palavras -> alvo 155-180)...",
        )
        try:
            data["roteiro"] = roteiro
            adj_prompt = _build_length_adjustment_prompt(data, wc)
            adj_raw = _call_ollama(ollama_url, model, adj_prompt, timeout=timeout)
            adj_data = _parse_json_response(adj_raw)
            adj_roteiro = _clean_script_text(str(adj_data.get("roteiro", "")))
            adj_wc = _count_words(adj_roteiro)
            if abs(adj_wc - 168) < abs(wc - 168) and adj_wc >= 110:
                data = adj_data
                roteiro = adj_roteiro
                wc = adj_wc
                _log(log_cb, f"[Scriptwriter] Roteiro ajustado para {wc} palavras.")
        except Exception as exc:
            _log(log_cb, f"[Scriptwriter] Aviso no ajuste de palavras ({exc}). Mantendo versão inicial.")

    gancho = str(data.get("gancho_tela") or "").strip().upper()
    gancho = re.sub(r"[\r\n]+", " ", gancho)
    gancho = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]+", "", gancho).strip()
    if not gancho:
        gancho = f"¡ÚLTIMA HORA EN {club_focus.upper()}!"
    if len(gancho) > 52:
        gancho = gancho[:52].rsplit(" ", 1)[0]

    raw_terms = data.get("termos_busca_imagens")
    terms: list[str] = []
    if isinstance(raw_terms, list):
        for item in raw_terms:
            t = str(item).strip()
            if t and t not in terms:
                terms.append(t)
    elif isinstance(raw_terms, str):
        terms = [x.strip() for x in raw_terms.split(",") if x.strip()]

    fallback_terms = [
        f"{club_focus} jugadores Liga MX",
        f"{club_focus} partido futbol mexicano",
        f"{club_focus} aficion estadio",
        f"{club_focus} entrenamiento Liga MX",
        "Liga MX estadio partido futbol",
    ]
    for fb in fallback_terms:
        if len(terms) >= 5:
            break
        if fb not in terms:
            terms.append(fb)
    terms = terms[:5]

    descricao = str(data.get("descricao_tiktok") or "").strip()
    if not descricao:
        descricao = f"{article.title} ¿Tú qué opinas de esta noticia en la Liga MX?"

    hashtags = _normalize_hashtags(data.get("hashtags"), club_focus)

    _log(
        log_cb,
        f"[Scriptwriter] Pronto! {wc} palavras | Gancho: '{gancho}' | "
        f"{len(terms)} termos de imagem | {len(hashtags)} hashtags.",
    )

    return ScriptPackage(
        roteiro=roteiro,
        gancho_tela=gancho,
        termos_busca_imagens=terms,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_count=wc,
    )
