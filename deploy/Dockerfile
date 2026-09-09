# The collector, as a container of its own next to coin_keeper's.
#
# It shares exactly two things with that stack: the docker network (so
# postgres answers to the name `postgres` and no container IP has to be
# looked up) and the database itself. Nothing is installed on the host,
# which is the point -- the host refused all three improvised routes:
# PEP 668 on its system python, an egg-info write into a read-only
# checkout, and a read-only $HOME inside the api image. All three refusals
# were correct; see docs/01_findings.md.
#
# Only the base dependencies are installed -- httpx, selectolax, psycopg.
# The photo pipeline's opencv/Pillow/numpy live in the "photos" extra and
# never reach this image, which is why it is ~180 MB rather than ~700.
# (That is also why the dependency layer is not split out from the code
# layer: with the heavy wheels gone the whole install takes seconds, and
# the machinery would cost more than the cache saves.)
FROM python:3.12-slim

# Matched to the server's `deploy` user so files written into the mounted
# staging directory belong to it and can be read without docker.
ARG UID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml /src/
COPY collector/ /src/collector/

# A regular install, not editable: the package carries its dictionaries
# (countries/ua/series.json, db_map.json) as declared package data, so
# they end up in site-packages and the source tree is not needed at run
# time. It is deleted right after, and the working directory below is not
# the source -- an accidental fallback to importing the checkout is then
# impossible rather than merely unlikely.
RUN pip install /src \
 && rm -rf /src \
 && useradd --create-home --uid ${UID} collector \
 && mkdir -p /staging \
 && chown collector:collector /staging

# Every path the steps write is under this; nothing needs a writable
# working directory. Mount a volume or a host directory here, or the
# night's kept HTML goes with the container.
ENV COLLECTOR_STAGING_ROOT=/staging

USER collector
WORKDIR /home/collector

ENTRYPOINT ["python", "-m", "collector"]
CMD ["ua", "--step", "update-prices"]
