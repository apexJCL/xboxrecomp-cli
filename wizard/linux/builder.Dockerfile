# The Linux builder: an arm64 container whose cross gcc targets amd64
# (Debian multiarch), so an Apple-silicon Mac and an arm64 runner build the
# x86_64 binary natively, without emulation, and get the same bytes. The
# base is pinned by digest and apt reads a dated snapshot of Debian, so the
# image's packages do not move with the archive (xbr wizard build passes
# BASE and SNAPSHOT from wizard/pins.json).
ARG BASE
FROM ${BASE}
ARG SNAPSHOT
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${SNAPSHOT}|" \
      -e "s|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${SNAPSHOT}|" \
      /etc/apt/sources.list.d/debian.sources \
 && dpkg --add-architecture amd64 \
 && apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      crossbuild-essential-amd64 libc6-dev:amd64 \
      libgl1-mesa-dev:amd64 libx11-dev:amd64 libxcursor-dev:amd64 libxrandr-dev:amd64 \
      libxinerama-dev:amd64 libxi-dev:amd64 libxxf86vm-dev:amd64 libxkbcommon-dev:amd64 \
      libwayland-dev:amd64 libegl1-mesa-dev:amd64 \
 && rm -rf /var/lib/apt/lists/*
ENV CGO_ENABLED=1 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local GOFLAGS=-mod=readonly \
    CC=x86_64-linux-gnu-gcc CXX=x86_64-linux-gnu-g++ \
    PKG_CONFIG_PATH=/usr/lib/x86_64-linux-gnu/pkgconfig
