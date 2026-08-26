FROM i386/debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    WINEARCH=win32 \
    WINEPREFIX=/opt/wineprefix \
    WINEDEBUG=-all \
    DISPLAY=:99 \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      fonts-wqy-zenhei \
      wine \
      wine32 \
      wine32-preloader \
      xauth \
      xdotool \
      xvfb \
 && rm -rf /var/lib/apt/lists/*

# Build a clean 32-bit prefix once. Runtime containers only need to start the
# virtual display and the application, which keeps rare fallback jobs snappy.
RUN ( \
      Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb-build.log 2>&1 & \
      xvfb_pid=$!; \
      sleep 1; \
      wineboot --init; \
      wineserver -w; \
      kill "$xvfb_pid"; \
      rm -f /tmp/xvfb-build.log \
    )

COPY app/Pdg2PicAuto/Pdg2Pic /opt/pdg2pic
COPY entrypoint.sh /usr/local/bin/pdg2pic-convert

RUN chmod +x /usr/local/bin/pdg2pic-convert \
 && mkdir -p /work

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/pdg2pic-convert"]
