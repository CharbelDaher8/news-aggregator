# The aggregator as a service: the JSON API by default, `refresh` on demand.
#
# Built and run by the vim-notes compose stack, which mounts this repository's
# data directory as a volume and puts the API on a private network with no
# published port. See deploy/docker-compose.yml over there.
#
# Two processes, one image. `refresh` fetches 29 sources and runs the LLM pass,
# which takes minutes and happens once a day; the API answers in milliseconds
# and runs forever. Splitting them into two images would mean two copies of the
# same code and a second thing to keep in step, so the image ships both and the
# command decides which one this container is.
FROM python:3.13-slim

# curl for the claude installer below; git because the LLM pass and several
# sources shell out to neither, but a container you cannot poke around in is a
# container you debug by rebuilding.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends ca-certificates curl git \
	&& rm -rf /var/lib/apt/lists/*

# The `claude` CLI, for `refresh`. The native installer rather than npm: it
# needs no node, and this image has none.
#
# Only `refresh` uses it. The API serves what is already in the database, so a
# container whose token has expired still answers every read -- which is the
# behaviour to want at 7am when a token quietly lapsed overnight.
RUN curl -fsSL https://claude.ai/install.sh | bash \
	&& ln -sf /root/.local/bin/claude /usr/local/bin/claude \
	&& claude --version

WORKDIR /app

# Requirements before the source, so editing a fetcher does not re-resolve
# three packages.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agg ./agg
COPY news ./news

# Where enrich.py looks for the binary. It defaults to the Homebrew path, which
# is right on the machine this was written on and absent here.
ENV NEWS_CLAUDE_BIN=/usr/local/bin/claude \
	PYTHONUNBUFFERED=1

# items.db lives here, and the compose stack mounts a volume over it. Created
# now so the directory exists even when nothing is mounted.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8787

# 0.0.0.0 because this binds inside a container's own network namespace, where
# loopback would make it unreachable from a sibling container rather than
# merely private. The boundary is that the compose service publishes no ports;
# see the note at the top of agg/api.py.
CMD ["python", "-m", "agg.cli", "api", "--host", "0.0.0.0", "--port", "8787"]
