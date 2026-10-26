FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS ssh-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config libssl-dev zlib1g-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSLo openssh.tar.gz https://cdn.openbsd.org/pub/OpenBSD/OpenSSH/portable/openssh-10.6p1.tar.gz \
    && echo "a9dc9565dffe8640f64d863cd29a32bc4a3dbdec0566a7fc44c5d6ee767d5f39  openssh.tar.gz" | sha256sum -c - \
    && tar xzf openssh.tar.gz \
    && cd openssh-10.6p1 \
    && ./configure --prefix=/usr/local --sysconfdir=/etc/ssh \
    && make -j2 ssh ssh-keygen

FROM python:3.14.9-slim-trixie@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

WORKDIR /app

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git ca-certificates libssl3t64 zlib1g \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' perl-base)" ge '5.40.1-6+deb13u1' \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'app:x:1000:1000:app:/home/app:/usr/sbin/nologin' >> /etc/passwd \
    && install -d -o 1000 -g 1000 /home/app

COPY --from=ssh-builder /build/openssh-10.6p1/ssh /build/openssh-10.6p1/ssh-keygen /usr/local/bin/
COPY --from=ssh-builder /build/openssh-10.6p1/LICENCE /usr/local/share/openssh/LICENCE
RUN ssh -V 2>&1 | grep "OpenSSH_10.6p1"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check

COPY server.py .

ENV PORT=8080
ENV SITES_DIR=/data/sites
EXPOSE 8080

USER 1000:1000

CMD ["python", "server.py"]
