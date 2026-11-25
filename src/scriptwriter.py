"""Módulo de geração de roteiros virais via Ollama local (Modos Notícia Quente e Top 5 estilo @yaguilas1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from src import HISTORY_PATH, load_history, normalize_text
from src.scraper import NewsArticle

LogCallback = Callable[[str], None]

GREETING_PATTERNS = (
    r"^\s*[¡!]?\s*(hola|qué tal|que tal|bienvenidos|buenos días|buenas tardes|buenas noches|"
    r"saludos|amigos|afición|banda|mi gente)\b[^.!?]*[.!?]\s*"
)

INLINE_GREETING_PATTERNS = (
    r"\b(hoy les traigo(?: la noticia de que)?|les traigo(?: una noticia)?|"
    r"hola a todos|qué tal amigos|que tal amigos|bienvenidos a un nuevo video)\b[:,]?\s*"
)

TOP5_TOPIC_BANK: list[dict[str, Any]] = [
    {
        "slug": "top5_aficiones_mas_grandes_mexico",
        "tema": "Las 5 aficiones más grandes y apasionadas de todo México",
        "foco": (
            "Puesto 5: Tigres UANL con más de 8 millones de aficionados y un Estadio Universitario (El Volcán) siempre lleno. "
            "Puesto 4: Pumas UNAM con cerca de 10 millones de seguidores fieles y la garra universitaria en Ciudad Universitaria. "
            "Puesto 3: Cruz Azul con más de 11 millones de aficionados apasionados que nunca abandonan a La Máquina. "
            "Puesto 2: Chivas de Guadalajara con cerca de 20 millones de seguidores en todo el país y orgullo de jugar solo con mexicanos. "
            "Puesto 1: Club América, el más grande de México, con más de 26 millones de aficionados, el club más ganador de la Liga MX y el equipo que todos quieren vencer."
        ),
        "queries": [
            "Estadio Azteca aficion llena noche futbol Liga MX",
            "Tigres UANL aficion Estadio Universitario Volcan Liga MX",
            "Pumas UNAM aficion Estadio Olimpico Universitario Liga MX",
            "Cruz Azul aficion jugadores estadio futbol Liga MX",
            "Chivas Guadalajara aficion Estadio Akron futbol Liga MX",
            "Club America campeon aficion Estadio Azteca Liga MX",
        ],
    },
    {
        "slug": "top5_estadios_mas_temidos_liga_mx",
        "tema": "Los 5 estadios más temidos y difíciles de visitar en toda la Liga MX",
        "foco": (
            "Puesto 5: Estadio Nemesio Diez en Toluca, 'La Bombonera', con más de 2,600 metros de altura que ahogan a cualquier rival. "
            "Puesto 4: Estadio Akron de Chivas en Guadalajara, una caldera rojiblanca que pesa muchísimo en Liguilla. "
            "Puesto 3: Estadio BBVA de Rayados en Monterrey, el 'Gigante de Acero', moderno y con más de 50 mil aficionados presionando. "
            "Puesto 2: Estadio Universitario de Tigres, 'El Volcán', famoso por su ambiente ensordecedor de principio a fin. "
            "Puesto 1: El mítico Estadio Azteca, casa del Club América, coloso histórico de finales y títulos donde pocos rivales sobreviven."
        ),
        "queries": [
            "Estadios llenos aficion noche futbol Liga MX",
            "Estadio Nemesio Diez Toluca aficion Liga MX",
            "Estadio Akron Chivas Guadalajara aficion Liga MX",
            "Estadio BBVA Monterrey Rayados Gigante de Acero Liga MX",
            "Estadio Universitario Volcan Tigres UANL aficion Liga MX",
            "Estadio Azteca Club America aficion amarilla Liga MX",
        ],
    },
    {
        "slug": "top5_equipos_mas_ganadores_liga_mx",
        "tema": "Los 5 clubes con más títulos de Liga MX en la historia del fútbol mexicano",
        "foco": (
            "Puesto 5: Tigres UANL con 8 títulos de liga, el gigante moderno del norte liderado por la era Gignac. "
            "Puesto 4: Cruz Azul con 9 campeonatos de liga y una historia llena de gloria celeste. "
            "Puesto 3: Deportivo Toluca con 10 títulos, los Diablos Rojos que dominaron los torneos cortos con Cardozo. "
            "Puesto 2: Chivas de Guadalajara con 12 campeonatos de liga jugando únicamente con futbolistas mexicanos. "
            "Puesto 1: Club América, el rey absoluto y máximo ganador de México con 16 títulos de Liga MX tras su histórico tricampeonato."
        ),
        "queries": [
            "Trofeo campeon Liga MX futbol mexicano celebracion",
            "Tigres UANL campeon trofeo Liga MX jugadores",
            "Cruz Azul campeon trofeo Liga MX celebracion",
            "Deportivo Toluca campeon jugadores Liga MX",
            "Chivas Guadalajara campeon trofeo Liga MX",
            "Club America tricampeon trofeo Liga MX celebracion",
        ],
    },
    {
        "slug": "top5_clasicos_mas_calientes_mexico",
        "tema": "Las 5 rivalidades más calientes y apasionadas del fútbol mexicano",
        "foco": (
            "Puesto 5: Clásico Tapatío entre Chivas y Atlas, la rivalidad más antigua de Guadalajara que paraliza Jalisco. "
            "Puesto 4: Clásico Capitalino entre América y Pumas, puro orgullo y choque de identidades en la Ciudad de México. "
            "Puesto 3: Clásico Joven entre América y Cruz Azul, marcado por finales dramáticas e inolvidables. "
            "Puesto 2: Clásico Regio entre Tigres y Rayados de Monterrey, la batalla millonaria que divide a todo Nuevo León. "
            "Puesto 1: El Clásico Nacional entre Club América y Chivas, el partido que paraliza a todo México y desata pasiones sin límite."
        ),
        "queries": [
            "Clasicos futbol mexicano estadio lleno Liga MX",
            "Clasico Tapatio Chivas vs Atlas partido Liga MX",
            "Clasico Capitalino Club America vs Pumas UNAM Liga MX",
            "Clasico Joven Club America vs Cruz Azul final Liga MX",
            "Clasico Regio Tigres UANL vs Rayados Monterrey Liga MX",
            "Clasico Nacional Club America vs Chivas Guadalajara Liga MX",
        ],
    },
    {
        "slug": "top5_extranjeros_mas_letales_liga_mx",
        "tema": "Los 5 futbolistas extranjeros más letales que han brillado en la Liga MX",
        "foco": (
            "Puesto 5: Christian 'Chucho' Benítez, campeón de goleo imparable con Santos y tricampeón de goleo con Club América. "
            "Puesto 4: Humberto 'Chupete' Suazo, ídolo eterno de Rayados que destrozaba defensas en finales. "
            "Puesto 3: Evanivaldo Castro 'Cabinho', máximo goleador histórico de la liga mexicana con 312 goles. "
            "Puesto 2: André-Pierre Gignac, leyenda viva de Tigres con más de 200 goles y múltiples títulos de liga. "
            "Puesto 1: José Saturnino Cardozo, el 'Diablo Mayor' de Toluca, autor del récord casi imposible de 29 goles en un solo torneo corto."
        ),
        "queries": [
            "Goleadores historicos futbol mexicano Liga MX estadio",
            "Christian Chucho Benitez Club America goles Liga MX",
            "Humberto Chupete Suazo Rayados Monterrey Liga MX",
            "Cabinho Pumas UNAM delantero historico futbol mexicano",
            "Andre Pierre Gignac Tigres UANL celebrando gol Liga MX",
            "Jose Saturnino Cardozo Toluca goles Liga MX",
        ],
    },
    {
        "slug": "top5_porteros_mas_grandes_mexico",
        "tema": "Los 5 porteros más grandes en la historia del fútbol mexicano",
        "foco": (
            "Puesto 5: Nahuel Guzmán, figura polémica y decisiva en cinco títulos de liga con Tigres UANL. "
            "Puesto 4: Oswaldo Sánchez, capitán histórico campeón con Chivas y Santos Laguna. "
            "Puesto 3: Miguel Marín, el 'Superman' de Cruz Azul, leyenda absoluta de la década de los setenta. "
            "Puesto 2: Guillermo Ochoa, surgido del Club América, cinco veces mundialista e ícono internacional de México. "
            "Puesto 1: Jorge Campos, el 'Brody', portero revolucionario de Pumas y la Selección que cambió el fútbol mundial con su estilo único."
        ),
        "queries": [
            "Portero atajada espectacular futbol Liga MX estadio",
            "Nahuel Guzman portero Tigres UANL Liga MX",
            "Oswaldo Sanchez portero Chivas Seleccion Mexicana",
            "Miguel Marin portero Cruz Azul futbol mexicano",
            "Guillermo Ochoa portero Club America atajada Liga MX",
            "Jorge Campos portero Pumas Seleccion Mexicana",
        ],
    },
]


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


def pick_next_top5_topic(
    club_focus: str = "Club América",
    exclude_urls: set[str] | None = None,
) -> NewsArticle:
    """Seleciona o próximo tema Top 5 viral ainda não produzido em history.json."""
    history = load_history(HISTORY_PATH)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for item in history:
        if isinstance(item, dict):
            if item.get("url"):
                seen_urls.add(str(item["url"]).strip().lower())
            if item.get("title"):
                seen_titles.add(normalize_text(str(item["title"])))
    if exclude_urls:
        seen_urls.update(u.strip().lower() for u in exclude_urls)

    for topic in TOP5_TOPIC_BANK:
        pseudo_url = f"top5://{topic['slug']}"
        if pseudo_url in seen_urls or normalize_text(topic["tema"]) in seen_titles:
            continue
        return NewsArticle(
            title=topic["tema"],
            url=pseudo_url,
            source="Top 5 Viral Liga MX",
            summary=topic["foco"],
            full_text=topic["foco"],
            article_images=list(topic.get("queries", [])),  # Guarda queries verificadas para uso no scriptwriter
            matched_keywords=[club_focus, "Liga MX", "Top 5"],
        )

    # Se todos os temas do banco já foram usados, gera uma variação nova focada no clube
    custom_slug = normalize_text(club_focus).replace(" ", "_") or "ligamx"
    return NewsArticle(
        title=f"Los 5 momentos más épicos de {club_focus} en la Liga MX",
        url=f"top5://momentos_epicos_{custom_slug}_{len(history)}",
        source="Top 5 Viral Liga MX",
        summary=f"Recuento viral del 5 al 1 con los momentos más recordados de {club_focus}.",
        full_text=f"Tema Top 5: Los 5 momentos más épicos de {club_focus} en la Liga MX.",
        matched_keywords=[club_focus, "Liga MX", "Top 5"],
    )


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
    """Limpa marcações indevidas (emojis, hashtags, colchetes, asteriscos) para narração TTS limpa."""
    s = (text or "").strip()
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", s)
    s = s.replace("**", "").replace("__", "").replace("*", "").replace("#", "")
    s = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]+", "", s)
    s = re.sub(GREETING_PATTERNS, "", s, flags=re.IGNORECASE)
    s = re.sub(INLINE_GREETING_PATTERNS, "", s, flags=re.IGNORECASE)
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

    defaults = ["#LigaMX", "#FutbolMexicano", "#TikTokDeportes", "#FutbolMX"]
    club_tag = "#" + re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]", "", club_focus)
    if len(club_tag) > 2:
        defaults.insert(0, club_tag)

    for d in defaults:
        if d.lower() not in {t.lower() for t in tags}:
            tags.append(d)

    return tags[:10]


def _build_news_prompt(article: NewsArticle, club_focus: str) -> str:
    return f"""Eres el guionista principal de una cuenta viral de TikTok de fútbol mexicano (estilo @yaguilas1, enfoque: {club_focus}).
Transforma la siguiente noticia REAL en un guion magnético, con alma, ritmo rápido y tensión narrativa para un TikTok de 62 a 70 segundos.

NOTICIA REAL:
- Fuente: {article.source}
- Titular: {article.title}
- Contenido:
{article.full_text}

REGLAS DE ORO DEL ESTILO VIRAL (OBLIGATORIAS):
1. Responde ÚNICAMENTE con un JSON válido con EXACTAMENTE estas 5 claves:
   - "roteiro": string
   - "gancho_tela": string
   - "termos_busca_imagens": array de 6 strings
   - "descricao_tiktok": string
   - "hashtags": array de 6 a 8 strings
2. Reglas para "roteiro":
   - Extensión OBLIGATORIA: entre 170 y 195 palabras (para superar los 60 segundos a velocidad +10%).
   - PROHIBIDO sonar como noticiero aburrido o robot. Escribe con FRASES CORTAS Y CONTUNDENTES (8 a 15 palabras por frase), ritmo rápido, datos concretos y pasión auténtica de grada mexicana.
   - ESTRUCTURA EXACTA EN 5 GOLPES (unidos en un solo párrafo fluido, sin títulos ni números):
     • GOLPE 1 (Gancho 0-3s): Frase explosiva e imposible de ignorar sobre la noticia. PROHIBIDO saludos ("Hola", "Qué tal", "Amigos", "Hoy les traigo").
     • GOLPE 2 (El bombazo): Nombres exactos de jugadores, técnicos o clubes y qué pasó realmente.
     • GOLPE 3 (La polémica / dato clave): Por qué esto incendia a la afición o cambia el rumbo del torneo.
     • GOLPE 4 (Retención + CTA a mitad del video): Incluye de forma natural una pausa de suspenso corta como: "Pero antes de contarte lo que viene ahora, sígueme porque aquí solo sale puro fútbol mexicano."
     • GOLPE 5 (Clímax + Debate): Consecuencia final y una pregunta picante y directa que obligue a los aficionados a pelear en los comentarios.
   - PROHIBIDO inventar hechos falsos (en la Liga MX actual NO existe el descenso). Usa SOLO datos reales de la noticia.
   - Texto limpio listo para TTS: SIN emojis, SIN corchetes, SIN asteriscos, SIN guiones de lista.
3. Reglas para "gancho_tela":
   - 3 a 5 palabras EN MAYÚSCULAS (ej.: "¡SE ROMPIÓ TODO EN COAPA!").
4. Reglas para "termos_busca_imagens":
   - Exactamente 6 búsquedas MUY ESPECÍFICAS en español (mínimo 4 palabras cada una) para encontrar FOTOGRAFÍAS REALES DE FÚTBOL de los jugadores, técnicos, equipos y estadios de la noticia.
   - SIEMPRE incluye "futbol Liga MX" o "estadio partido" en cada término.
   - Ejemplo correcto: ["Club America jugadores celebrando gol Liga MX", "Andre Jardine entrenador Club America partido", "Estadio Azteca aficion amarilla futbol"]
   - PROHIBIDO palabras sueltas como "América", "Tigres" o "clubes".
5. "descricao_tiktok": 1 frase polémica corta que invite a comentar.
6. "hashtags": 6 a 8 hashtags relevantes empezando con #.

Devuelve SOLO el JSON:"""


def _build_top5_prompt(topic_article: NewsArticle, club_focus: str) -> str:
    return f"""Eres el creador viral detrás de cuentas gigantes de TikTok de Liga MX estilo @yaguilas1 (enfoque: {club_focus}).
Crea un guion de TOP 5 adictivo, emocionante, con alma de grada mexicana y CERO errores de datos para un TikTok de 62 a 70 segundos.

TEMA DEL TOP 5:
- Título: {topic_article.title}
- DATOS REALES VERIFICADOS (USA EXACTAMENTE ESTOS 5 PUESTOS EN ESTE ORDEN, NO INVENTES NI REPITAS CLUBES):
{topic_article.summary}

REGLAS DE ORO DEL FORMATO TOP 5 VIRAL (OBLIGATORIAS):
1. Responde ÚNICAMENTE con un JSON válido con EXACTAMENTE estas 5 claves:
   - "roteiro": string
   - "gancho_tela": string
   - "termos_busca_imagens": array de 6 strings
   - "descricao_tiktok": string
   - "hashtags": array de 6 a 8 strings
2. Reglas ESTRICTAS para "roteiro":
   - Extensión OBLIGATORIA: entre 175 y 198 palabras (cada puesto DEBE tener 2 frases completas de 25 a 32 palabras en total para superar los 60 segundos).
   - ESTRUCTURA EXACTA (texto continuo limpio para TTS, SIN emojis, SIN asteriscos, SIN viñetas):
     • APERTURA: Anuncia el ranking con impacto (10-14 palabras). PROHIBIDO saludos.
     • PUESTO 5: 2 frases completas (28-32 palabras) usando los datos verificados + emoción de estadio.
     • PUESTO 4: 2 frases completas (28-32 palabras) usando los datos verificados + por qué impone respeto.
     • PUESTO 3: 2 frases completas (28-32 palabras) usando los datos verificados + pasión de su gente.
     • CTA DE RETENCIÓN ANTES DEL TOP 2: "Pero antes de revelar el top dos, sígueme porque aquí solo sale contenido del fútbol mexicano."
     • PUESTO 2: 2 frases completas (28-32 palabras) usando los datos verificados + debate fuerte.
     • PUESTO 1: Clímax épico de 2 frases (30-35 palabras) revelando al número uno + pregunta provocativa final para comentarios.
   - NO repitas ningún equipo en dos puestos distintos. NO cambies los datos verificados.
3. "gancho_tela": Título ultra corto EN MAYÚSCULAS (3 a 5 palabras).
4. "termos_busca_imagens": 6 búsquedas específicas en español de los clubes/estadios mencionados + "futbol Liga MX".
5. "descricao_tiktok": 1 frase polémica corta invitando a opinar (SIN hashtags dentro de la descripción).
6. "hashtags": 6 a 8 hashtags relevantes empezando con #.

Devuelve SOLO el JSON:"""


def _build_length_adjustment_prompt(current_json: dict[str, Any], current_words: int) -> str:
    if current_words < 165:
        missing = max(25, 182 - current_words)
        instruction = (
            f"El 'roteiro' actual es DEMASIADO CORTO ({current_words} palabras). "
            f"Faltan al menos {missing} palabras para superar 1 minuto de video. "
            f"EXPANDE el 'roteiro' a 175-192 palabras agregando datos concretos, "
            f"emoción de afición y detalles reales SIN perder el ritmo ágil ni el CTA de mitad de video."
        )
    else:
        instruction = (
            f"El 'roteiro' actual es DEMASIADO LARGO ({current_words} palabras). "
            f"RECORTE el 'roteiro' a 172-188 palabras manteniendo frases cortas, el CTA intermedio y la pregunta final."
        )

    return f"""{instruction}
Reglas obligatorias:
- Español mexicano apasionado, ritmo rápido estilo TikTok viral (frases cortas)
- PROHIBIDO saludos o 'Hoy les traigo'
- PROHIBIDO inventar hechos falsos (en la Liga MX no existe descenso)
- Sin emojis, corchetes, viñetas ni asteriscos
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
            "num_predict": 1200,
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
    """Gera o roteiro estruturado em JSON via Ollama local (Notícia Viral ou Top 5 estilo @yaguilas1)."""
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_modelo", "qwen2.5:7b")
    timeout = int(config.get("ollama_timeout", 180))
    club_focus = config.get("clube_foco", "Club América")
    modo = str(config.get("modo_conteudo", "noticia")).strip().lower()

    is_top5 = modo == "top5" or article.url.startswith("top5://")
    mode_label = "Top 5 Viral" if is_top5 else "Notícia Viral"
    _log(log_cb, f"[Scriptwriter] Modo {mode_label} | Conectando ao Ollama ({model})...")

    prompt = _build_top5_prompt(article, club_focus) if is_top5 else _build_news_prompt(article, club_focus)

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

    # Ajuste automático em até 2 passadas para garantir duração entre 60s e 74s (165-202 palavras a +10%)
    for adj_try in range(1, 3):
        if 165 <= wc <= 202:
            break
        _log(
            log_cb,
            f"[Scriptwriter] Ajustando tamanho do roteiro ({wc} palavras -> alvo 170-195, tentativa {adj_try}/2)...",
        )
        try:
            data["roteiro"] = roteiro
            adj_prompt = _build_length_adjustment_prompt(data, wc)
            adj_raw = _call_ollama(ollama_url, model, adj_prompt, timeout=timeout)
            adj_data = _parse_json_response(adj_raw)
            adj_roteiro = _clean_script_text(str(adj_data.get("roteiro", "")))
            adj_wc = _count_words(adj_roteiro)
            if abs(adj_wc - 182) < abs(wc - 182) and adj_wc >= 115:
                data = adj_data
                roteiro = adj_roteiro
                wc = adj_wc
                _log(log_cb, f"[Scriptwriter] Roteiro ajustado para {wc} palavras.")
        except Exception as exc:
            _log(log_cb, f"[Scriptwriter] Aviso no ajuste de palavras ({exc}). Mantendo versão atual.")
            break

    gancho = str(data.get("gancho_tela") or "").strip().upper()
    gancho = re.sub(r"[\r\n]+", " ", gancho)
    gancho = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]+", "", gancho).strip()
    if not gancho:
        gancho = f"¡BOMBAZO EN {club_focus.upper()}!"
    if len(gancho) > 52:
        gancho = gancho[:52].rsplit(" ", 1)[0]

    # Se o tema Top 5 já traz queries pré-verificadas e ordenadas, usa-as para garantir 100% de precisão visual
    if is_top5 and article.article_images and not article.article_images[0].startswith("http"):
        terms = list(article.article_images)[:6]
        # Limpa article_images para o media_fetcher não tentar baixar strings de query como URL
        article.article_images = []
    else:
        raw_terms = data.get("termos_busca_imagens")
        terms = []
        if isinstance(raw_terms, list):
            for item in raw_terms:
                t = str(item).strip()
                if t and t not in terms:
                    terms.append(t)
        elif isinstance(raw_terms, str):
            terms = [x.strip() for x in raw_terms.split(",") if x.strip()]

    fallback_terms = [
        f"{club_focus} jugadores celebrando gol Liga MX",
        f"{club_focus} aficion estadio lleno futbol mexicano",
        f"{club_focus} partido oficial cancha Liga MX",
        "Estadio Azteca aficion futbol Liga MX",
        f"{club_focus} entrenamiento jugadores futbol mexicano",
        "Liga MX partido estadio lleno aficion futbol",
    ]
    for fb in fallback_terms:
        if len(terms) >= 6:
            break
        if fb not in terms:
            terms.append(fb)
    terms = terms[:6]

    descricao = str(data.get("descricao_tiktok") or "").strip()
    if not descricao:
        descricao = f"{article.title} ¿Tú qué opinas? ¡Te leo en los comentarios!"

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
