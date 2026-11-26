"""Cliente unificado de IA (Gemini API + Ollama Local) para roteiros JSON e Visão em lote."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from src import BASE_DIR

SECRETS_PATH = BASE_DIR / "secrets.json"

GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[0]


def load_gemini_api_key() -> str:
    """Lê a chave Gemini API de secrets.json (local, no .gitignore) ou da variável GEMINI_API_KEY."""
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_key:
        return env_key
    if SECRETS_PATH.exists():
        try:
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
            return str(data.get("gemini_api_key") or "").strip()
        except Exception:
            return ""
    return ""


def save_gemini_api_key(api_key: str) -> None:
    """Salva a chave Gemini API apenas localmente em secrets.json (ignorado pelo Git)."""
    data: dict[str, Any] = {}
    if SECRETS_PATH.exists():
        try:
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["gemini_api_key"] = (api_key or "").strip()
    SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_gemini_active(config: dict[str, Any]) -> bool:
    """Retorna True se o provedor configurado for Gemini e houver chave disponível."""
    provider = str(config.get("provedor_ia", "ollama")).strip().lower()
    if provider != "gemini":
        return False
    key = str(config.get("gemini_api_key") or "").strip() or load_gemini_api_key()
    return bool(key)


def _call_gemini_rest(
    prompt: str,
    model: str,
    api_key: str,
    images_b64: list[str] | None = None,
    expect_json: bool = True,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    timeout: int = 90,
) -> str:
    """Chama a Gemini REST API (Google AI Studio) com fallback automático em caso de 404/503/429."""
    clean_key = (api_key or "").strip()
    parts: list[dict[str, Any]] = []
    for b64 in images_b64 or []:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    parts.append({"text": prompt})

    gen_cfg: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if expect_json:
        gen_cfg["responseMimeType"] = "application/json"

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": clean_key,
    }

    candidates_order: list[str] = []
    for m in [model, *GEMINI_MODELS]:
        m_clean = (m or "").strip()
        if m_clean and m_clean not in candidates_order:
            candidates_order.append(m_clean)

    last_err = ""
    for idx, candidate_model in enumerate(candidates_order):
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate_model}:generateContent"
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)

        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Chave da Gemini API inválida ou sem permissão. Gere uma grátis em aistudio.google.com/apikey."
            )
        if resp.status_code in (404, 429, 503):
            last_err = f"HTTP {resp.status_code} ({candidate_model}): {resp.text[:220]}"
            if idx + 1 < len(candidates_order):
                time.sleep(0.8)
                continue
            if resp.status_code == 429:
                raise RuntimeError(
                    "Limite temporário (429) da camada gratuita do Gemini atingido. Aguarde alguns segundos ou alterne para Ollama Local."
                )
            raise RuntimeError(f"Gemini API indisponível: {last_err}")
        if resp.status_code == 400:
            raise RuntimeError(f"Gemini API erro 400: {resp.text[:300]}")
        resp.raise_for_status()

        body = resp.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini API retornou resposta sem candidatos: {str(body)[:250]}")
        content_parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [str(p.get("text") or "") for p in content_parts if p.get("text") and not p.get("thought")]
        if not texts:
            texts = [str(p.get("text") or "") for p in content_parts if p.get("text")]
        out = "\n".join(texts).strip()
        if not out:
            raise RuntimeError("Gemini API retornou texto vazio.")
        return out

    raise RuntimeError(f"Gemini API falhou em todos os modelos: {last_err}")


def generate_text_llm(
    prompt: str,
    config: dict[str, Any],
    expect_json: bool = True,
    temperature: float = 0.72,
    max_tokens: int = 1400,
    timeout: int = 180,
) -> str:
    """
    Gera texto/JSON usando o provedor ativo:
    - Gemini API (`gemini-flash-lite-latest`, `gemini-flash-latest`, `gemini-3.8-flash`) quando selecionado e com chave;
    - Ollama local (`qwen3:8b`, `deepseek-r1:8b`, `hermes3:8b`, `qwen2.5:7b`, etc.) caso contrário.
    """
    if is_gemini_active(config):
        api_key = str(config.get("gemini_api_key") or "").strip() or load_gemini_api_key()
        model = str(config.get("gemini_modelo") or DEFAULT_GEMINI_MODEL).strip()
        return _call_gemini_rest(
            prompt=prompt,
            model=model,
            api_key=api_key,
            expect_json=expect_json,
            temperature=temperature,
            max_tokens=max(8192, max_tokens),
            timeout=min(timeout, 120),
        )

    # Ollama Local
    ollama_url = str(config.get("ollama_url", "http://localhost:11434"))
    model = str(config.get("ollama_modelo", "qwen3:8b"))
    endpoint = f"{ollama_url.rstrip('/')}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": max_tokens,
        },
    }
    if expect_json:
        payload["format"] = "json"
    if "qwen3" in model.lower():
        payload["think"] = False

    resp = requests.post(endpoint, json=payload, timeout=timeout)
    if resp.status_code == 400 and "think" in payload:
        payload.pop("think", None)
        resp = requests.post(endpoint, json=payload, timeout=timeout)

    if resp.status_code == 404:
        raise RuntimeError(
            f"Modelo Ollama '{model}' não encontrado localmente. "
            f"Execute 'ollama pull {model}' ou selecione outro modelo na lista."
        )
    resp.raise_for_status()
    text = str(resp.json().get("response") or "")
    if not text:
        raise RuntimeError("Ollama retornou resposta vazia.")
    return text


def describe_frames_batch_gemini(
    frames_b64: list[str],
    config: dict[str, Any],
    niche_hint: str,
) -> list[str]:
    """
    Descreve até 10 quadros de vídeo em UMA ÚNICA chamada ultrarrápida ao Gemini Vision!
    Retorna lista de descrições na mesma ordem dos quadros.
    """
    if not frames_b64:
        return []
    api_key = str(config.get("gemini_api_key") or "").strip() or load_gemini_api_key()
    model = str(config.get("gemini_modelo") or DEFAULT_GEMINI_MODEL).strip()
    n = len(frames_b64)

    prompt = (
        f"You are analyzing {n} video frames from a {niche_hint} video, in order from 0 to {n - 1}.\n"
        f"Return ONLY a JSON object: {{\"descriptions\": [\"desc0\", \"desc1\", ...]}} with EXACTLY {n} short, "
        f"concrete descriptions (10-18 words each) naming characters, actions, objects, and locations."
    )
    raw = _call_gemini_rest(
        prompt=prompt,
        model=model,
        api_key=api_key,
        images_b64=frames_b64,
        expect_json=True,
        temperature=0.15,
        max_tokens=4096,
        timeout=60,
    )
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    parsed = json.loads(cleaned[start : end + 1])
    descs = parsed.get("descriptions") or []
    out: list[str] = []
    for i in range(n):
        d = str(descs[i]).strip() if i < len(descs) else f"{niche_hint} scene"
        out.append(d[:160])
    return out
