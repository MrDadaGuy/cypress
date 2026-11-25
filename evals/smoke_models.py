"""Smoke test: one call to each provider in the model registry (Gemini, Claude, open-weights MaaS) on Vertex.

Run: services/agent/.venv/bin/python evals/smoke_models.py
"""
import subprocess

from anthropic import AnthropicVertex
from google import genai
from openai import OpenAI

PROJECT = "lhca-cypress"
PROMPT = "Say 'ready' in one word."


def gemini(model="gemini-3.8-flash"):
    c = genai.Client(vertexai=True, project=PROJECT, location="global")
    return c.models.generate_content(model=model, contents=PROMPT).text


def claude(model="claude-sonnet-5-5"):
    c = AnthropicVertex(project_id=PROJECT, region="global")
    return c.messages.create(model=model, max_tokens=20, messages=[{"role": "user", "content": PROMPT}]).content[0].text


def maas(model="openai/gpt-oss-120b-maas", region="global"):
    """Open MaaS models via the OpenAI-compatible endpoint. Region varies by model:
    gpt-oss-120b works on global; Llama 4 Maverick only on us-east5 (as of Nov 2026)."""
    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    base = f"https://{host}/v1/projects/{PROJECT}/locations/{region}/endpoints/openapi"
    r = OpenAI(base_url=base, api_key=tok).chat.completions.create(
        model=model, max_tokens=200, messages=[{"role": "user", "content": PROMPT}])
    return r.choices[0].message.content


if __name__ == "__main__":
    for name, fn in [("gemini", gemini), ("claude-sonnet", claude),
                     ("claude-haiku", lambda: claude("claude-haiku-5-5")), ("gpt-oss-120b", maas),
                     ("llama-4-maverick", lambda: maas("meta/llama-4-maverick-17b-128e-instruct-maas", "us-east5"))]:
        try:
            print(f"OK   {name}: {(fn() or '').strip()[:40]}")
        except Exception as e:
            print(f"FAIL {name}: {str(e)[:200]}")
