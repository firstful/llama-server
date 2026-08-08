"""llama-ctrl — wake/sleep controller + OpenAI-compatible proxy for the local
GPU llama-server (ns llama, Deployment 'llama-server').

Idle-cooldown design:
  - sleeping: Deployment at 0 replicas — no VRAM used (GPU free for transcode)
  - the first /v1/* request (or POST /wake) scales the Deployment to 1 and
    waits until readyReplicas >= 1 (llama-server /health 200 = model loaded)
  - every proxied request touches the idle clock; when in-flight is 0 AND no
    request has arrived for IDLE_TIMEOUT seconds, scales back to 0

Flux is configured (apps Kustomization `ignore` rule) to NOT manage
spec.replicas on the llama-server Deployment, so scale calls stick.

LAN-only (llama.homelab.internal); never tunneled. No auth — same trust
domain as the llm gateway.
"""

import asyncio
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

APP_VERSION = "1.0.0"

NS = os.environ.get("LLAMA_NAMESPACE", "llama")
DEPLOYMENT = os.environ.get("LLAMA_DEPLOYMENT", "llama-server")
UPSTREAM = os.environ.get(
    "LLAMA_UPSTREAM", "http://llama-server.llama.svc.cluster.local:8080"
).rstrip("/")
MODEL_ID = os.environ.get("LLAMA_MODEL_ID", "lfm2.5-2.6b")
IDLE_TIMEOUT = float(os.environ.get("LLAMA_IDLE_TIMEOUT", "600"))
WAKE_TIMEOUT = float(os.environ.get("LLAMA_WAKE_TIMEOUT", "180"))
SCALE_POLL = float(os.environ.get("LLAMA_SCALE_POLL", "1.0"))
PORT = int(os.environ.get("PORT", "8080"))
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_DEP_URL = f"https://{K8S_HOST}/apis/apps/v1/namespaces/{NS}/deployments/{DEPLOYMENT}"

# passthrough request headers for the proxy hop
_PROXY_HEADERS = ("content-type", "accept", "authorization")

state = {"value": "sleeping", "since": time.time()}  # sleeping | warming | ready
_wake_lock = asyncio.Lock()
_inflight = 0
_last_request = 0.0
_k8s: httpx.AsyncClient | None = None


async def _api() -> httpx.AsyncClient:
    global _k8s
    if _k8s is None:
        with open(f"{_SA_DIR}/token") as f:
            token = f.read().strip()
        with open(f"{_SA_DIR}/ca.crt", "rb") as f:
            ca = f.read()
        # httpx verify= wants a FILE PATH, not raw CA bytes (bytes breaks the
        # SSL context setup with "'bytes' object has no attribute
        # 'set_alpn_protocols'") — write the SA CA to a temp file once.
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as tf:
            tf.write(ca)
            ca_path = tf.name
        _k8s = httpx.AsyncClient(
            verify=ca_path, timeout=10.0, headers={"Authorization": f"Bearer {token}"}
        )
    return _k8s


async def _scale(replicas: int) -> None:
    c = await _api()
    r = await c.patch(
        f"{_DEP_URL}/scale",
        json={"spec": {"replicas": replicas}},
        headers={"Content-Type": "application/merge-patch+json"},
    )
    r.raise_for_status()


async def _deployment_state() -> tuple[int, int]:
    """(desired, ready) replica counts from the live Deployment."""
    c = await _api()
    r = await c.get(f"{_DEP_URL}/status")
    r.raise_for_status()
    d = r.json()
    return (
        d.get("spec", {}).get("replicas", 0),
        d.get("status", {}).get("readyReplicas", 0),
    )


async def _wake() -> None:
    """Scale up and wait until ready. Idempotent; concurrent callers share one wake."""
    if state["value"] == "ready":
        return
    async with _wake_lock:
        if state["value"] == "ready":
            return
        await _scale(1)
        state["value"] = "warming"
        state["since"] = time.time()
        deadline = time.time() + WAKE_TIMEOUT
        while time.time() < deadline:
            _, ready = await _deployment_state()
            if ready >= 1:
                state["value"] = "ready"
                state["since"] = time.time()
                return
            await asyncio.sleep(SCALE_POLL)
        raise RuntimeError(f"{DEPLOYMENT} not ready within {WAKE_TIMEOUT:.0f}s")


async def _cooldown_loop() -> None:
    while True:
        await asyncio.sleep(15)
        if state["value"] != "ready":
            continue
        if _inflight > 0:
            continue
        if time.time() - _last_request >= IDLE_TIMEOUT:
            try:
                await _scale(0)
            except Exception:
                pass  # scale-down is best-effort; next idle tick retries
            state["value"] = "sleeping"
            state["since"] = time.time()


app = FastAPI(title="llama-ctrl", version=APP_VERSION)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_cooldown_loop())


@app.get("/health")
async def health():
    return {"ok": True, "state": state["value"]}


@app.get("/status")
async def status():
    desired, ready = await _deployment_state()
    return {
        "service": "llama",
        "state": state["value"],
        "state_since": state["since"],
        "model": MODEL_ID,
        "desired_replicas": desired,
        "ready_replicas": ready,
        "in_flight": _inflight,
        "idle_for": round(time.time() - _last_request, 1) if _last_request else None,
        "idle_timeout": IDLE_TIMEOUT,
        "upstream": UPSTREAM,
    }


@app.post("/wake")
async def wake():
    try:
        await _wake()
        return {"ok": True, "state": state["value"]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


@app.post("/sleep")
async def sleep():
    try:
        await _scale(0)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})
    state["value"] = "sleeping"
    state["since"] = time.time()
    return {"ok": True, "state": "sleeping"}


@app.get("/v1/models")
@app.get("/models")  # alias: the llm-gateway probes {base_url}/models
async def v1_models():
    """Static model list — NEVER wakes the server for a listing."""
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "llama-local",
            }
        ],
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    if path == "models":
        return await v1_models()
    try:
        await _wake()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"error": {"message": f"llama-server wake failed: {e}", "type": "server_error", "code": None}},
        )

    global _inflight, _last_request
    _inflight += 1
    _last_request = time.time()
    try:
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() in _PROXY_HEADERS}
        url = f"{UPSTREAM}/v1/{path}"
        # small retry loop: right after readiness the Service endpoints can lag
        # a beat -> connection refused / 502. Only 3 attempts, 1.5s apart.
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                # NOTE: do NOT use `async with httpx.AsyncClient(...)` here —
                # the context manager closes the client as soon as `return`
                # runs, but StreamingResponse consumes the body LATER → the
                # stream is already closed → 200 with 0 bytes. Keep the client
                # alive and close it from the generator's finally instead.
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None)
                )
                req = client.build_request(request.method, url, headers=headers, content=body)
                r = await client.send(req, stream=True)
                if r.status_code in (502, 503, 504) and attempt < 2:
                    await r.aclose()
                    await client.aclose()
                    await asyncio.sleep(1.5)
                    continue
                ctype = r.headers.get("content-type")

                async def _stream():
                    try:
                        async for chunk in r.aiter_raw():
                            yield chunk
                    finally:
                        await r.aclose()
                        await client.aclose()

                return StreamingResponse(
                    _stream(),
                    status_code=r.status_code,
                    media_type=ctype,
                )
            except httpx.TransportError as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"llama-server unreachable: {last_err}", "type": "server_error", "code": None}},
        )
    finally:
        _inflight -= 1
        _last_request = time.time()
