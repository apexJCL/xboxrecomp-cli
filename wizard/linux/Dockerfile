# The wizard's Linux asset as a Docker build: the staged module (with its
# generated config.json, the icon and menu entry wizard build puts in icon/,
# and NOTICES.txt) is the build context and the tar.gz (the binary, its icon,
# its .desktop file and NOTICES.txt) comes out through --output, so no bind
# mount is needed and the result depends on the builder image alone.
ARG BUILDER
FROM ${BUILDER} AS build
ARG NAME
ARG VERSION
ARG SOURCE_DATE_EPOCH
# FRESH (wizard check's second build) compiles into an empty GOCACHE: the
# go-build cache mount outlives docker build --no-cache.
ARG FRESH=
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    if [ -n "$FRESH" ]; then export GOCACHE=/tmp/go-build-fresh; fi \
 && go build -trimpath -ldflags="-s -w -buildid= -X main.version=${VERSION}" -o /out/${NAME} ./cmd/wizard \
 && cp icon/${NAME}.png icon/${NAME}.desktop NOTICES.txt /out/ \
 && chmod 755 /out/${NAME} && chmod 644 /out/${NAME}.png /out/${NAME}.desktop /out/NOTICES.txt \
 && tar --sort=name --mtime=@${SOURCE_DATE_EPOCH} --owner=0 --group=0 --numeric-owner \
      -C /out -cf - ${NAME} ${NAME}.png ${NAME}.desktop NOTICES.txt | gzip -n -9 > /out/${NAME}.tar.gz

# Runs the amd64 binary's --dry where the host executes amd64 code (Docker
# Desktop's emulation); xbr wizard build reports a skip elsewhere.
FROM build AS dry
RUN /out/${NAME} --dry --no-update-check

FROM scratch
COPY --from=build /out/ /
