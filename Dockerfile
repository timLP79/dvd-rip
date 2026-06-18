FROM nvidia/cuda:12.6.0-base-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# HandBrake CLI ships in the universe repo; libdvd-pkg pulls libdvdcss2 source and builds it.
# We need software-properties-common for add-apt-repository if HandBrake's apt repo is desired,
# but the Ubuntu 24.04 universe HandBrakeCLI is recent enough.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        handbrake-cli \
        libdvd-pkg \
        libdvdread8t64 \
        libdvdnav4 \
        lsdvd \
        python3.12 \
        python3-pip \
    && echo 'libdvd-pkg libdvd-pkg/post-invoke_hook-install boolean true' | debconf-set-selections \
    && echo 'libdvd-pkg libdvd-pkg/build boolean true' | debconf-set-selections \
    && echo 'libdvd-pkg libdvd-pkg/first-install note' | debconf-set-selections \
    && dpkg-reconfigure -f noninteractive libdvd-pkg \
    && rm -rf /var/lib/apt/lists/*

# --- MakeMKV (built from source; CLI only, no Qt GUI) ---
ARG MAKEMKV_VERSION=1.18.4
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config wget ca-certificates \
        libc6-dev libssl-dev libexpat1-dev zlib1g-dev \
        libavcodec-dev libavutil-dev libavformat-dev \
    && cd /tmp \
    && wget -q "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
    && wget -q "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
    && tar xzf makemkv-oss-${MAKEMKV_VERSION}.tar.gz \
    && tar xzf makemkv-bin-${MAKEMKV_VERSION}.tar.gz \
    && cd makemkv-oss-${MAKEMKV_VERSION} \
    && ./configure --disable-gui >/dev/null \
    && make -j"$(nproc)" >/dev/null && make install \
    && cd ../makemkv-bin-${MAKEMKV_VERSION} \
    && mkdir -p tmp && echo accepted > tmp/eula_accepted \
    && make -j"$(nproc)" >/dev/null && make install \
    && cd / && rm -rf /tmp/makemkv-* \
    # Keep libdvdcss2/libdvd-pkg: HandBrake dlopens libdvdcss at runtime (no hard
    # dpkg dependency), so the autoremove below would otherwise sweep it as an
    # orphan and silently break CSS-DVD decryption. See dvd-rip-96z.
    && apt-get purge -y build-essential pkg-config wget && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# CSS-DVD decryption. Built in its OWN layer AFTER the MakeMKV layer's autoremove
# so it can't be swept as an orphan — HandBrake dlopens libdvdcss at runtime, so
# nothing declares a hard dpkg dependency on it and autoremove considered it unused,
# silently breaking DVD decryption. See dvd-rip-96z.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libdvd-pkg \
    && echo 'libdvd-pkg libdvd-pkg/post-invoke_hook-install boolean true' | debconf-set-selections \
    && echo 'libdvd-pkg libdvd-pkg/build boolean true' | debconf-set-selections \
    && echo 'libdvd-pkg libdvd-pkg/first-install note' | debconf-set-selections \
    && dpkg-reconfigure -f noninteractive libdvd-pkg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DVD_DEVICE=/dev/sr0 \
    OUTPUT_ROOT=/output \
    HOST=0.0.0.0 \
    PORT=8000 \
    PUID=1000 \
    PGID=1000 \
    CDROM_GID=24 \
    TMDB_API_KEY="" \
    LIBRARY_DB=/data/library.db \
    MAKEMKV_APP_KEY="" \
    MAKEMKV_SCRATCH=/data/scratch \
    MAKEMKV_SDF_STOP=""

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
