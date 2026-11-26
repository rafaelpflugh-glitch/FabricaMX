"""Módulo multi-nicho de geração de roteiros virais de alta retenção via Ollama local."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from src import HISTORY_PATH, load_history, normalize_text
from src.scraper import NewsArticle

LogCallback = Callable[[str], None]

GREETING_PATTERNS = (
    r"^\s*[¡!]?\s*(hola|qué tal|que tal|bienvenidos|buenos días|buenas tardes|buenas noches|"
    r"saludos|amigos|fala galera|e aí galera|olá pessoal|oi pessoal|bem-vindos|hey guys|what's up guys|"
    r"welcome back|hello everyone)\b[^.!?]*[.!?]\s*"
)

INLINE_GREETING_PATTERNS = (
    r"\b(hoy les traigo(?: la noticia de que)?|les traigo(?: una noticia)?|"
    r"hoje eu trago|hoje trago para vocês|fala galera|hola a todos|"
    r"qué tal amigos|bienvenidos a un nuevo video|today i bring you)\b[:,]?\s*"
)


@dataclass
class ScriptPackage:
    """Pacote estruturado de roteiro e metadados gerado pelo Ollama."""

    roteiro: str
    gancho_tela: str
    termos_busca_imagens: list[str] = field(default_factory=list)
    buscas_video_youtube: list[str] = field(default_factory=list)
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
    config: dict[str, Any],
    exclude_urls: set[str] | None = None,
) -> NewsArticle:
    """Seleciona o próximo tema Top 5 / Segredos Virais do banco do nicho ativo ainda não produzido."""
    paths_dict = config.get("paths") or {}
    history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
    history = load_history(history_path)

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

    niche_name = str(config.get("nicho_nome", "Fábrica TK"))
    foco = str(config.get("clube_foco") or niche_name)
    bank = config.get("top5_banco") or []

    for topic in bank:
        slug = str(topic.get("slug") or normalize_text(topic.get("tema", "")).replace(" ", "_"))
        pseudo_url = f"top5://{slug}"
        tema = str(topic.get("tema", ""))
        if pseudo_url in seen_urls or normalize_text(tema) in seen_titles:
            continue
        return NewsArticle(
            title=tema,
            url=pseudo_url,
            source=f"Top 5 Viral • {niche_name}",
            summary=str(topic.get("foco", "")),
            full_text=str(topic.get("foco", "")),
            article_images=list(topic.get("queries", [])),
            matched_keywords=[foco, niche_name, "Top 5"],
        )

    # Fallback se todo o banco do nicho já tiver sido usado
    idioma = str(config.get("idioma", "pt-BR"))
    if idioma.startswith("es"):
        title = f"Los 5 momentos más impactantes de {foco}"
    elif idioma.startswith("en"):
        title = f"5 insane secrets about {foco} nobody noticed"
    else:
        title = f"5 segredos e momentos mais insanos de {foco}"

    return NewsArticle(
        title=title,
        url=f"top5://custom_{normalize_text(foco).replace(' ', '_')}_{len(history)}",
        source=f"Top 5 Viral • {niche_name}",
        summary=f"Ranking dinâmico do 5 ao 1 com fatos marcantes sobre {foco}.",
        full_text=f"Tema Top 5: {title}.",
        article_images=list(config.get("imagem_fallbacks", []))[:6],
        matched_keywords=[foco, niche_name, "Top 5"],
    )


def _strip_reasoning_and_fences(raw: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json_response(raw: str) -> dict[str, Any]:
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
        data = json.loads(cleaned[start : end + 1])
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


def _normalize_hashtags(raw_hashtags: Any, config: dict[str, Any]) -> list[str]:
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

    defaults = list((config.get("prompt_contexto") or {}).get("hashtags_padrao") or ["#TikTok", "#Viral"])
    for d in defaults:
        if d.lower() not in {t.lower() for t in tags}:
            tags.append(d)

    return tags[:10]


def _language_pack(config: dict[str, Any]) -> dict[str, str]:
    """Retorna instruções, persona e CTA no idioma correto (pt-BR, es-MX ou en-US)."""
    idioma = str(config.get("idioma", "pt-BR")).lower()
    ctx = config.get("prompt_contexto") or {}
    niche_name = str(config.get("nicho_nome", "TikTok"))

    if idioma.startswith("es"):
        return {
            "lang_name": "ESPAÑOL MEXICANO coloquial, vibrante y directo",
            "persona": ctx.get("persona_es")
            or f"Eres un creador viral de TikTok experto en {niche_name}, ritmo eléctrico y alta retención.",
            "cta": ctx.get("cta_meio_es")
            or f"Pero antes de revelar el punto número uno, sígueme porque aquí sale lo mejor de {niche_name}.",
            "open_loop": "Y quédate hasta el final, porque el número uno te va a volar la cabeza.",
            "regras_fatos": ctx.get("regras_fatos", ""),
        }
    if idioma.startswith("en"):
        return {
            "lang_name": "PUNCHY AMERICAN ENGLISH (casual, high-energy TikTok narration)",
            "persona": ctx.get("persona_en")
            or f"You are a viral high-retention TikTok creator covering {niche_name}.",
            "cta": ctx.get("cta_meio_en")
            or f"Before we hit number one, hit follow for daily {niche_name} breakdowns.",
            "open_loop": "And wait until you hear number one, because almost nobody saw it coming.",
            "regras_fatos": ctx.get("regras_fatos", ""),
        }
    return {
        "lang_name": "PORTUGUÊS DO BRASIL coloquial, empolgado e direto (linguagem natural de TikTok)",
        "persona": ctx.get("persona_pt")
        or f"Você é um criador viral de TikTok focado em {niche_name}, com ritmo acelerado e retenção máxima.",
        "cta": ctx.get("cta_meio_pt")
        or f"Mas antes de revelar o primeiro lugar, já me segue porque aqui sai tudo sobre {niche_name}.",
        "open_loop": "E segura até o final, porque o número um vai explodir sua cabeça.",
        "regras_fatos": ctx.get("regras_fatos", ""),
    }


def _build_news_prompt(article: NewsArticle, config: dict[str, Any]) -> str:
    lp = _language_pack(config)
    img_suffix = str(config.get("imagem_sufixo_padrao", "")).strip()

    return f"""{lp['persona']}
Transforme a notícia REAL abaixo em um roteiro VIRAL de altíssima retenção para TikTok (62 a 72 segundos).

NOTÍCIA REAL:
- Fonte: {article.source}
- Título: {article.title}
- Conteúdo:
{article.full_text}

REGRAS DE RETENÇÃO VIRAL (OBRIGATÓRIAS):
1. Idioma do roteiro, gancho e descrição: {lp['lang_name']}.
2. Responda APENAS com um JSON válido com EXATAMENTE estas 6 chaves:
   - "roteiro": string
   - "gancho_tela": string
   - "termos_busca_imagens": array de 6 strings
   - "buscas_video_youtube": array de 3 strings
   - "descricao_tiktok": string
   - "hashtags": array de 6 a 8 strings
3. Regras ESTRITAS para "roteiro":
   - Extensão OBRIGATÓRIA: entre 175 e 205 palavras (para dar 62–72 segundos a +12% de velocidade). Escreva um texto RICO e completo — NUNCA devolva um resumo curto de 100 palavras!
   - PROIBIDO parecer telejornal chato ou texto robótico! Use FRASES CURTAS E CORTANTES (6 a 13 palavras por frase), tensão real e ritmo acelerado.
   - Se o idioma for Português do Brasil e a matéria original usar termos de Portugal ("autocarro", "consola", "ecrã", "telemóvel"), adapte SEMPRE para Português do Brasil ("ônibus", "console", "tela", "celular").
   - ESTRUTURA DE RETENÇÃO (texto corrido pronto para narração, SEM títulos, SEM números de lista, SEM emojis, SEM hashtags):
     • GANCHO (0-3s): Frase chocante ou polêmica que prende no primeiro segundo. PROHIBIDO saudações ("Fala galera", "Hola amigos", "Hey guys").
     • LOOP ABERTO (3-6s): Promessa rápida de algo surpreendente que vem no final.
     • DESENVOLVIMENTO COM AÇÃO (3 blocos de ~35 palavras cada): Nomes reais, números concretos, cenas/detalhes/reações da comunidade e consequências práticas.
     • QUEBRA DE PADRÃO + CTA NO MEIO: Inclua naturalmente: "{lp['cta']}"
     • CLÍMAX + DEBATE: Revelação final + pergunta provocativa direta que faça o público discutir nos comentários.
   - Fatos: {lp['regras_fatos']}
4. "gancho_tela": 3 a 5 palavras EM CAIXA ALTA de alto impacto.
5. "termos_busca_imagens": Exatamente 6 buscas MUITO ESPECÍFICAS de AÇÃO/CENAS (mínimo 4 palavras cada). Inclua sempre "{img_suffix}".
6. "buscas_video_youtube": Exatamente 3 buscas curtas e específicas em INGLÊS ou ESPANHOL criadas ESTRITAMENTE a partir dos fatos desta notícia para achar GAMEPLAY REAL ou LANCES REAIS ligados ao tema exato (NÃO copie exemplos genéricos; use o nome do jogo/clube + o acontecimento real desta matéria).
7. "descricao_tiktok": 1 frase polêmica curta SEM hashtags dentro dela.
8. "hashtags": 6 a 8 hashtags começando com #.

Retorne APENAS o JSON:"""


def _build_top5_prompt(topic_article: NewsArticle, config: dict[str, Any]) -> str:
    lp = _language_pack(config)
    img_suffix = str(config.get("imagem_sufixo_padrao", "")).strip()

    return f"""{lp['persona']}
Crie um roteiro de TOP 5 VIRAL de altíssima retenção (62 a 72 segundos) usando os fatos reais abaixo.

TEMA DO TOP 5:
- Título: {topic_article.title}
- FATOS VERIFICADOS (USE EXATAMENTE ESTES 5 ITENS NESTA ORDEM, SEM REPETIR E SEM INVENTAR):
{topic_article.summary}

REGRAS DE RETENÇÃO VIRAL (OBRIGATÓRIAS):
1. Idioma do roteiro, gancho e descrição: {lp['lang_name']}.
2. Responda APENAS com um JSON válido com EXATAMENTE estas 5 chaves:
   - "roteiro": string
   - "gancho_tela": string
   - "termos_busca_imagens": array de 6 strings
   - "descricao_tiktok": string
   - "hashtags": array de 6 a 8 strings
3. Regras ESTRITAS para "roteiro":
   - Extensão OBRIGATÓRIA: entre 178 e 202 palavras (cada um dos 5 itens DEVE ter 2 frases curtas e fortes somando ~28 a 32 palavras para o vídeo passar de 60 segundos!).
   - RITMO VICIANTEMENTE RÁPIDO: frases curtas (6 a 13 palavras), impacto visual, verbos de ação, zero enrolação.
   - ESTRUTURA EXATA (texto limpo para narração TTS, SEM emojis, SEM asteriscos, SEM hashtags, SEM tópicos):
     • ABERTURA (0-4s): Anuncia o Top 5 + loop aberto ("{lp['open_loop']}"). PROIBIDO saudações.
     • ITEM 5: 2 frases fortes (~30 palavras) usando o fato verificado.
     • ITEM 4: 2 frases fortes (~30 palavras) usando o fato verificado.
     • ITEM 3: 2 frases fortes (~30 palavras) usando o fato verificado.
     • CTA DE RETENÇÃO NO MEIO: "{lp['cta']}"
     • ITEM 2: 2 frases fortes (~30 palavras) elevando a tensão antes do nº 1.
     • ITEM 1: Clímax explosivo (~32 palavras) + pergunta provocativa final que gera briga nos comentários.
   - Fatos: {lp['regras_fatos']}
4. "gancho_tela": 3 a 5 palavras EM CAIXA ALTA.
5. "termos_busca_imagens": 6 buscas específicas de ação/personagens/lances na ordem do vídeo, incluindo "{img_suffix}".
6. "descricao_tiktok": 1 frase curta e provocativa SEM hashtags.
7. "hashtags": 6 a 8 hashtags começando com #.

Retorne APENAS o JSON:"""


def _build_length_adjustment_prompt(
    current_json: dict[str, Any],
    current_words: int,
    config: dict[str, Any],
) -> str:
    lp = _language_pack(config)
    if current_words < 168:
        missing = max(35, 188 - current_words)
        instruction = (
            f"O campo 'roteiro' atual tem APENAS {current_words} palavras — isso dá menos de 45 segundos e NÃO monetiza no TikTok! "
            f"REESCREVA o campo 'roteiro' adicionando pelo menos +{missing} palavras novas (total entre 178 e 198 palavras). "
            f"Desenvolva 3 novos blocos curtos explicando o contexto, a reação dos fãs/jogadores e por que isso muda tudo, "
            f"mantendo frases curtas de impacto e SEM inventar fatos falsos."
        )
    else:
        instruction = (
            f"O 'roteiro' atual está LONGO DEMAIS ({current_words} palavras). "
            f"Enxugue para ficar entre 175 e 195 palavras mantendo o ritmo frenético, o CTA do meio e a pergunta final."
        )

    return f"""{instruction}
Regras obrigatórias:
- Idioma: {lp['lang_name']} (se for PT-BR, nunca use palavras de Portugal como 'autocarro' ou 'consola')
- Frases curtas de alta retenção, SEM saudações, SEM emojis, SEM hashtags no meio/fim do roteiro
- Mantenha todas as outras chaves do JSON intactas

JSON ATUAL:
{json.dumps(current_json, ensure_ascii=False, indent=2)}

Retorne APENAS o JSON corrigido:"""


def _call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout: int = 180,
) -> str:
    endpoint = f"{ollama_url.rstrip('/')}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.74,
            "top_p": 0.9,
            "num_predict": 1300,
        },
    }
    # Desativa raciocínio longo oculto em modelos Qwen3 para respostas rápidas e JSON limpo
    if "qwen3" in model.lower():
        payload["think"] = False

    resp = requests.post(endpoint, json=payload, timeout=timeout)
    if resp.status_code == 400 and "think" in payload:
        payload.pop("think", None)
        resp = requests.post(endpoint, json=payload, timeout=timeout)

    if resp.status_code == 404:
        raise RuntimeError(
            f"Modelo Ollama '{model}' não encontrado localmente. "
            f"Execute 'ollama pull {model}' ou escolha outro modelo instalado na lista."
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
    """Gera roteiro viral de alta retenção via Ollama no nicho e idioma configurados."""
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    model = config.get("ollama_modelo", "qwen3:8b")
    timeout = int(config.get("ollama_timeout", 180))
    niche_name = str(config.get("nicho_nome", "Fábrica TK"))
    idioma = str(config.get("idioma", "pt-BR"))
    modo = str(config.get("modo_conteudo", "top5")).strip().lower()

    is_top5 = modo == "top5" or article.url.startswith("top5://")
    mode_label = "Top 5 Viral" if is_top5 else "Notícia Viral"
    _log(log_cb, f"[Scriptwriter] {niche_name} | {mode_label} ({idioma}) | Modelo: {model}...")

    prompt = _build_top5_prompt(article, config) if is_top5 else _build_news_prompt(article, config)

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

    # Ajuste automático em até 2 passadas para bater 60–72s (168–208 palavras a +12%)
    for adj_try in range(1, 3):
        if 168 <= wc <= 208:
            break
        _log(
            log_cb,
            f"[Scriptwriter] Ajustando ritmo/tamanho ({wc} palavras -> alvo 175-200, tentativa {adj_try}/2)...",
        )
        try:
            data["roteiro"] = roteiro
            adj_prompt = _build_length_adjustment_prompt(data, wc, config)
            adj_raw = _call_ollama(ollama_url, model, adj_prompt, timeout=timeout)
            adj_data = _parse_json_response(adj_raw)
            adj_roteiro = _clean_script_text(str(adj_data.get("roteiro", "")))
            adj_wc = _count_words(adj_roteiro)
            if abs(adj_wc - 188) < abs(wc - 188) and adj_wc >= 95:
                data = adj_data
                roteiro = adj_roteiro
                wc = adj_wc
                _log(log_cb, f"[Scriptwriter] Roteiro ajustado para {wc} palavras.")
            else:
                _log(log_cb, f"[Scriptwriter] Ajuste retornou {adj_wc} palavras; tentando novamente...")
        except Exception as exc:
            _log(log_cb, f"[Scriptwriter] Aviso no ajuste ({exc}). Mantendo versão atual.")
            break

    gancho = str(data.get("gancho_tela") or "").strip().upper()
    gancho = re.sub(r"[\r\n]+", " ", gancho)
    gancho = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF]+", "", gancho).strip()
    if not gancho:
        gancho = f"{niche_name.upper()}!"
    if len(gancho) > 52:
        gancho = gancho[:52].rsplit(" ", 1)[0]

    # Se o Top 5 já traz queries pré-verificadas, usa-as em ordem para precisão visual de 100%
    if is_top5 and article.article_images and not article.article_images[0].startswith("http"):
        terms = list(article.article_images)[:6]
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

    for fb in config.get("imagem_fallbacks") or []:
        if len(terms) >= 6:
            break
        if fb not in terms:
            terms.append(fb)
    terms = terms[:6]

    raw_yt = data.get("buscas_video_youtube")
    yt_queries: list[str] = []
    if isinstance(raw_yt, list):
        for item in raw_yt:
            q = str(item).strip()
            if q and q not in yt_queries:
                yt_queries.append(q)
    elif isinstance(raw_yt, str) and raw_yt.strip():
        yt_queries = [x.strip() for x in raw_yt.split(",") if x.strip()]

    if not yt_queries:
        niche_id = str(config.get("nicho_ativo", ""))
        short_title = " ".join(article.title.split()[:7])
        if niche_id == "gta_vi":
            yt_queries = [f"GTA 6 {short_title} gameplay PS5", "GTA 6 PS5 gameplay 4K no commentary"]
        else:
            foco = str(config.get("clube_foco") or "Club America")
            yt_queries = [f"{short_title} resumen goles Liga MX", f"{foco} goles resumen highlights Liga MX"]
    yt_queries = yt_queries[:3]

    descricao = re.sub(r"#[\wÁÉÍÓÚÜÑáéíóúüñ]+", "", str(data.get("descricao_tiktok") or "")).strip()
    if not descricao:
        descricao = f"{article.title}"

    hashtags = _normalize_hashtags(data.get("hashtags"), config)

    _log(
        log_cb,
        f"[Scriptwriter] Pronto! {wc} palavras | Gancho: '{gancho}' | "
        f"{len(yt_queries)} buscas YouTube | {len(hashtags)} hashtags.",
    )

    return ScriptPackage(
        roteiro=roteiro,
        gancho_tela=gancho,
        termos_busca_imagens=terms,
        buscas_video_youtube=yt_queries,
        descricao_tiktok=descricao,
        hashtags=hashtags,
        word_count=wc,
    )
