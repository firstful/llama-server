# llama-server — local GPU LLM for the Sutliff cluster

llama.cpp (Vulkan/RADV) serving the benchmark winner **LFM2.5-2.6B-Q4_K_M**
on the RX 470 (reed-a320ms2h), wrapped by **llama-ctrl** — a wake/sleep
controller that scales the server pod to 0 on idle cooldown so the GPU is
free for the transcode worker.

## Images

| Image | Purpose |
|---|---|
| `192.168.1.2:3000/reeds/llama-server:<tag>` | llama.cpp Vulkan + Mesa RADV (engine only — the GGUF is data, mounted from NFS) |
| `192.168.1.2:3000/reeds/llama-ctrl:<tag>` | wake/sleep control + OpenAI-compatible proxy (always on) |

## Why Arch Linux bases

llama.cpp's Vulkan build requires **glslc** (`find_package(Vulkan COMPONENTS glslc)`),
which is not packaged in Debian/Ubuntu anymore. Arch ships `shaderc` (and
matches the proven Manjaro toolchain on .50). Builder and runtime must share
the base so the binary's glibc links — both stages are `archlinux:latest`.

`LLAMACPP_COMMIT` is pinned (3653e6d — the commit Reed benchmarked 2026-08-07)
and the shallow fetch is verified against the pin before building.

## Build & push

```bash
TOKEN=<write:package token>   # mint: gitea admin user generate-access-token --scopes write:package,read:package
echo "$TOKEN" | docker login 192.168.1.2:3000 -u reeds --password-stdin
docker build -t 192.168.1.2:3000/reeds/llama-server:v1.0.0 -f Containerfile .
docker build -t 192.168.1.2:3000/reeds/llama-ctrl:v1.0.0 -f ctrl/Containerfile ctrl/
docker push 192.168.1.2:3000/reeds/llama-server:v1.0.0
docker push 192.168.1.2:3000/reeds/llama-ctrl:v1.0.0
```

Same-tag re-push does NOT re-pull on kubelet — bump the tag on rebuilds.

## llama-ctrl

Endpoints (LAN-only, `llama.homelab.internal`):

| Endpoint | Behavior |
|---|---|
| `POST/GET /v1/*` | wake-on-demand proxy → llama-server (streaming SSE relayed) |
| `GET /v1/models` | static model list — NEVER wakes the server |
| `GET /status` | state (sleeping/warming/ready), replicas, in_flight, idle_for |
| `POST /wake` | force scale-up, returns when Ready |
| `POST /sleep` | force scale-down |

Env: `LLAMA_NAMESPACE`, `LLAMA_DEPLOYMENT`, `LLAMA_UPSTREAM`,
`LLAMA_MODEL_ID` (must match the server's `--alias`), `LLAMA_IDLE_TIMEOUT` (s),
`LLAMA_WAKE_TIMEOUT` (s).

## Model

Data, not code: `LFM2.5-2.6B-Q4_K_M.gguf` (sha256
`79fdf00351b46cf26f020aead28d01889886be87c55fa0eb907e6f9b00bfee14`) lives on
the RAID at `/mnt/raid_store/llm-models`, exported via NFS as PV `llama-models`
(RWX), mounted read-only at `/models`. Server runs with `--no-mmap` (NFS-safe).
