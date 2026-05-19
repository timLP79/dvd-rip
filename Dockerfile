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
    TMDB_API_KEY=""

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
