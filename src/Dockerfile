# ------------------- Stage 0: Base Stage ------------------------------
FROM python:3.12-alpine AS base

WORKDIR /code

# Install tini, a tiny init for containers
RUN apk add --update --no-cache tini

# Install required packages for cryptography package
# https://cryptography.io/en/latest/installation/#building-cryptography-on-linux
RUN apk add gcc musl-dev python3-dev libffi-dev openssl-dev cargo pkgconfig

# Install OpenCV dependencies
RUN apk add --no-cache \
    jpeg-dev \
    zlib-dev \
    freetype-dev \
    lcms2-dev \
    openjpeg-dev \
    tiff-dev \
    tk-dev \
    tcl-dev \
    harfbuzz-dev \
    fribidi-dev \
    libimagequant-dev \
    libxcb-dev \
    libpng-dev \
    ffmpeg-dev \
    cmake \
    make

# ------------------- Stage 1: Build Stage ------------------------------
FROM base AS build

COPY requirements.txt .

RUN pip3 install -r requirements.txt

COPY . .
# ------------------- Stage 2: Final Stage ------------------------------
FROM base AS final

RUN addgroup -S app && adduser -S app -G app

COPY --from=build --chown=app:app /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=build --chown=app:app /usr/local/bin /usr/local/bin
COPY --from=build --chown=app:app /code /code

USER app

EXPOSE 50505

ENTRYPOINT ["tini", "gunicorn", "quartapp:create_app()"]
