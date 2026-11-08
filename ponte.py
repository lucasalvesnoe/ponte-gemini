#!/usr/bin/env python3
"""Ponte local entre o Claude Code e os modelos.

- Modelo com prefixo de provedor do 9router (ag/, gh/, gc/, nvidia/, ollama/...) -> 9router.
- Qualquer outro modelo -> api.anthropic.com, repassando o login do Claude Code sem tocar.
- Se a Anthropic responder limite (429, ou 400/403 de credito), a mesma requisicao vai
  para a cadeia de reserva no 9router, com notificacao no macOS e estado gravado para a statusline.
- Quando a Anthropic volta a responder 2xx, avisa de novo e sai do modo reserva.

Escuta so em 127.0.0.1. Nunca loga cabecalho nem corpo.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM = os.environ.get("PONTE_UPSTREAM", "https://api.anthropic.com")
ROUTER = os.environ.get("PONTE_ROUTER", "http://127.0.0.1:20128")
PORT = int(os.environ.get("PONTE_PORT", "8788"))
FALLBACK_CHAIN = [
    m.strip()
    for m in os.environ.get(
        "PONTE_FALLBACK",
        "ag/gemini-3.8-flash-high,ag/gemini-3.7-flash-high,gc/gemini-3-flash-preview",
    ).split(",")
    if m.strip()
]
ROUTER_PREFIXES = ("ag/", "gh/", "gc/", "qd/", "nvidia/", "ollama/", "openrouter/", "mmf/", "oc/")
STATE_FILE = Path(os.environ.get("PONTE_STATE_FILE", Path.home() / ".claude" / "ponte-gemini" / "state.json"))
KEYCHAIN_SERVICE = os.environ.get("PONTE_KEYCHAIN_SERVICE", "9router-local")
HOP_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "accept-encoding", "content-encoding", "proxy-connection", "upgrade",
}
KEEP_FIELDS = {
    "model", "messages", "system", "max_tokens", "tools", "tool_choice", "stream",
    "temperature", "top_p", "top_k", "stop_sequences", "metadata",
}
WARNING_LINE = "⚠️ [Ponte] Limite do Claude atingido: respondendo pelo {model} (gratis) ate o Claude voltar."

log = logging.getLogger("ponte")


def router_key() -> str:
    """Chave do 9router: variavel PONTE_ROUTER_KEY ou, no macOS, o Keychain (servico PONTE_KEYCHAIN_SERVICE)."""
    key = os.environ.get("PONTE_ROUTER_KEY", "").strip()
    if key or not shutil.which("security"):
        return key
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip()


def is_router_model(model: str) -> bool:
    return model.startswith(ROUTER_PREFIXES)


class State:
    def __init__(self) -> None:
        self.mode = "claude"
        self.model = ""
        self.since = time.time()
        self.write()

    def write(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mode": self.mode, "model": self.model, "since": self.since}))
        tmp.replace(STATE_FILE)

    async def enter_fallback(self, model: str, reason: str) -> None:
        if self.mode == "reserva" and self.model == model:
            return
        self.mode, self.model, self.since = "reserva", model, time.time()
        self.write()
        log.warning("RESERVA ligada (%s) -> %s", reason, model)
        await notify("Claude sem token", f"Trocou sozinho para {model}. Motivo: {reason}")

    async def back_to_claude(self) -> None:
        if self.mode == "claude":
            return
        self.mode, self.model, self.since = "claude", "", time.time()
        self.write()
        log.warning("RESERVA desligada: Claude respondeu de novo")
        await notify("Claude voltou", "A ponte voltou a usar o Claude.")


async def notify(title: str, text: str) -> None:
    if sys.platform != "darwin":
        log.warning("%s: %s", title, text)
        return
    script = f'display notification {json.dumps(text)} with title {json.dumps("Ponte: " + title)} sound name "Glass"'
    try:
        proc = await asyncio.create_subprocess_exec("osascript", "-e", script)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception as exc:  # notificacao nunca derruba a requisicao
        log.error("notificacao falhou: %s", exc)


def strip_cache_control(node):
    if isinstance(node, dict):
        return {k: strip_cache_control(v) for k, v in node.items() if k != "cache_control"}
    if isinstance(node, list):
        return [strip_cache_control(v) for v in node]
    return node


def adapt_for_router(body: dict, model: str) -> dict:
    """Copia o corpo para o 9router: sem campos so-Anthropic, sem blocos de thinking assinados."""
    out = {k: v for k, v in body.items() if k in KEEP_FIELDS}
    out["model"] = model
    msgs = []
    for msg in out.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            content = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
            if not content:
                continue
        msgs.append({**msg, "content": content})
    out["messages"] = msgs
    if out.get("tools"):
        out["tools"] = [t for t in out["tools"] if "input_schema" in t]
        if not out["tools"]:
            out.pop("tools")
            out.pop("tool_choice", None)
    out = strip_cache_control(out)
    return out


def sse_event(etype: str, data: dict) -> bytes:
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def sse_with_note(stream, note: str):
    """Repassa o SSE inserindo um bloco de texto com o aviso como bloco 0 (demais indices +1)."""
    buf = b""
    async for chunk in stream.iter_any():
        buf += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            yield shift_event(raw)
            if b'"message_start"' in raw:
                yield sse_event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                yield sse_event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": note + "\n\n"}})
                yield sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
    if buf:
        yield buf


def shift_event(raw: bytes) -> bytes:
    lines = raw.split(b"\n")
    for i, line in enumerate(lines):
        if line.startswith(b"data:"):
            try:
                data = json.loads(line[5:])
            except ValueError:
                return raw + b"\n\n"
            if isinstance(data, dict) and data.get("type", "").startswith("content_block") and "index" in data:
                data["index"] += 1
                lines[i] = b"data: " + json.dumps(data, ensure_ascii=False).encode()
    return b"\n".join(lines) + b"\n\n"


def is_new_user_turn(body: dict) -> bool:
    """Ultima mensagem de usuario/assistente e texto do usuario, nao resultado de ferramenta.

    O Claude Code 2.1.x pode fechar a lista com mensagens role=system (tool_addition etc.); elas sao puladas.
    """
    for msg in reversed(body.get("messages") or []):
        role = msg.get("role")
        if role == "system":
            continue
        if role != "user":
            return False
        content = msg.get("content")
        if isinstance(content, str):
            return True
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content or [])
    return False


def estimate_tokens(body: dict) -> int:
    return max(1, len(json.dumps(body, ensure_ascii=False)) // 4)


class Ponte:
    def __init__(self) -> None:
        self.state = State()
        self.key = router_key()
        self.session: ClientSession | None = None

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(
            timeout=ClientTimeout(total=None, sock_connect=15, sock_read=900),
            connector=TCPConnector(limit=64),
            auto_decompress=True,
        )

    async def stop(self, app: web.Application) -> None:
        if self.session:
            await self.session.close()

    async def relay(self, request: web.Request, upstream, note: str | None = None) -> web.StreamResponse:
        if note and "json" in upstream.headers.get("content-type", ""):
            data = json.loads(await upstream.read())
            data["content"] = [{"type": "text", "text": note + "\n\n"}] + data.get("content", [])
            return web.json_response(data, status=upstream.status)
        resp = web.StreamResponse(status=upstream.status)
        for k, v in upstream.headers.items():
            if k.lower() not in HOP_HEADERS:
                resp.headers[k] = v
        await resp.prepare(request)
        if note and "event-stream" in upstream.headers.get("content-type", ""):
            async for event in sse_with_note(upstream.content, note):
                await resp.write(event)
        else:
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
        await resp.write_eof()
        return resp

    async def to_router(self, request: web.Request, body: dict, model: str):
        payload = adapt_for_router(body, model)
        headers = {
            "content-type": "application/json",
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            "x-api-key": self.key,
            "authorization": f"Bearer {self.key}",
        }
        return await self.session.post(f"{ROUTER}/v1/messages", json=payload, headers=headers)

    async def router_chain(self, request: web.Request, body: dict, models: list[str], reason: str):
        last_status, last_body = 502, b'{"type":"error","error":{"type":"api_error","message":"ponte: nenhum modelo de reserva respondeu"}}'
        for model in models:
            warn = reason != "escolhido" and bool(body.get("tools")) and is_new_user_turn(body)
            try:
                up = await self.to_router(request, body, model)
            except Exception as exc:
                log.error("9router %s falhou: %s", model, exc)
                continue
            if up.status < 400:
                if reason != "escolhido":
                    await self.state.enter_fallback(model, reason)
                log.info("-> 9router %s %s", model, up.status)
                note = WARNING_LINE.format(model=model) if warn else None
                return await self.relay(request, up, note)
            last_status, last_body = up.status, await up.read()
            log.warning("9router %s respondeu %s", model, up.status)
            up.release()
        if reason != "escolhido":
            await notify("reserva falhou", "Claude e reserva sem resposta. Veja o log da ponte.")
        return web.Response(status=last_status, body=last_body, content_type="application/json")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        path = request.path
        is_messages = request.method == "POST" and path.startswith("/v1/messages")
        body = None
        if is_messages:
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        model = (body or {}).get("model", "")

        if body is not None and is_router_model(model):
            if path.endswith("/count_tokens"):
                return web.json_response({"input_tokens": estimate_tokens(body)})
            return await self.router_chain(request, body, [model] + FALLBACK_CHAIN, "escolhido")

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        try:
            up = await self.session.request(
                request.method, UPSTREAM + request.path_qs, data=raw, headers=headers
            )
        except Exception as exc:
            log.error("Anthropic inacessivel: %s", exc)
            if body is not None and not path.endswith("/count_tokens"):
                return await self.router_chain(request, body, FALLBACK_CHAIN, "Anthropic inacessivel")
            raise web.HTTPBadGateway(text="ponte: Anthropic inacessivel")

        if is_messages and body is not None and not path.endswith("/count_tokens"):
            reason, cached = await limit_reason(up)
            if reason:
                log.warning("Anthropic %s (%s) model=%s", up.status, reason, model)
                up.release()
                return await self.router_chain(request, body, FALLBACK_CHAIN, reason)
            if up.status < 300:
                await self.state.back_to_claude()
            if cached is not None:
                log.info("-> anthropic %s %s %s", request.method, path, up.status)
                return web.Response(status=up.status, body=cached, headers={
                    k: v for k, v in up.headers.items() if k.lower() not in HOP_HEADERS})
        log.info("-> anthropic %s %s %s", request.method, path, up.status)
        return await self.relay(request, up)


async def limit_reason(up) -> tuple[str, bytes | None]:
    """Motivo de troca para a reserva (vazio = nao trocar) e o corpo ja lido, se foi preciso ler."""
    if up.status == 429:
        status = up.headers.get("anthropic-ratelimit-unified-status", "")
        return ("limite do plano" if status == "rejected" else "limite de uso (429)"), None
    if up.status in (400, 402, 403):
        data = await up.read()
        text = data.decode("utf-8", "ignore").lower()
        if "credit balance" in text or "usage limit" in text or "quota" in text:
            return f"sem credito ({up.status})", data
        return "", data
    return "", None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ponte = Ponte()
    if not ponte.key:
        log.error("chave do 9router nao encontrada (PONTE_ROUTER_KEY ou Keychain %s)", KEYCHAIN_SERVICE)
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.on_startup.append(ponte.start)
    app.on_cleanup.append(ponte.stop)
    app.router.add_route("*", "/{tail:.*}", ponte.handle)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
