# llama-server — llama.cpp with Vulkan (RADV) for the RX 470 (gfx803/Polaris).
#
# Builder + runtime are Arch Linux:
#   - shaderc (glslc) is REQUIRED by llama.cpp's Vulkan build (find_package
#     Vulkan COMPONENTS glslc) and is NOT packaged in Debian/Ubuntu anymore.
#   - Arch glibc is newer than Debian's — builder and runtime must share the
#     same base so the binary links cleanly.
#   - mesa on Arch ships a modern RADV (26.x) — matches the host Mesa on
#     reed-a320ms2h (Manjaro) and keeps gfx803 support intact.
#
# The GGUF model is DATA, not code — it lives on the RAID NFS share
# (/mnt/raid_store/llm-models, PV llama-models) and is mounted read-only.
# This image carries only the engine (llama.cpp + Mesa RADV userspace).

FROM archlinux:latest AS builder
ARG LLAMACPP_COMMIT=3653e6d6d547ec763317d9ecd0ace334a7e21359
RUN pacman -Syu --noconfirm --needed \
        base-devel cmake git shaderc vulkan-headers vulkan-icd-loader \
        spirv-headers spirv-tools curl \
    && rm -rf /var/cache/pacman/pkg
WORKDIR /src
# Pin the exact commit Reed benchmarked (3653e6d, 2026-08-07). Master is
# fetched shallow, then verified against the pin so a moving master can never
# silently change the build.
RUN git init -q \
    && git remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git fetch -q --depth 1 origin master \
    && test "$(git rev-parse FETCH_HEAD)" = "${LLAMACPP_COMMIT}" \
    && git checkout -q FETCH_HEAD
# GGML_NATIVE=OFF: image must run on any worker; inference is GPU-bound so
# the CPU codegen barely matters (and -t 4 keeps host cores free anyway).
RUN cmake -B build -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
    && cmake --build build --target llama-server -j2 \
    && ls -la build/bin/llama-server

FROM archlinux:latest AS runtime
RUN pacman -Syu --noconfirm --needed \
        mesa vulkan-radeon vulkan-icd-loader ca-certificates \
    && rm -rf /var/cache/pacman/pkg
COPY --from=builder /src/build/bin/ /usr/local/bin/
# llama.cpp's shared libs land next to the binary (build/bin) — that dir is
# NOT in the default linker path, so the loader must be told explicitly or
# llama-server dies with "error while loading shared libraries".
ENV LD_LIBRARY_PATH=/usr/local/bin
# RADV ICD ships at /usr/share/vulkan/icd.d/radeon_icd.json (Arch) — the
# loader auto-discovers it; no VK_ICD_FILENAMES pin needed (the old pin
# referenced the Debian-style .x86_64.json name and broke the container).
EXPOSE 8080
ENTRYPOINT ["llama-server"]
