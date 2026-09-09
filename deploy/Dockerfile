# The collector, as a container next to coin_keeper's own.
#
# It installs the base dependencies only -- httpx, selectolax, psycopg --
# because that is all the steps that run on a server need. The photo
# pipeline's opencv/Pillow/numpy stay in the "photos" extra and never
# reach this image; see pyproject.toml.
#
# The install is editable on purpose. The package carries data files next
# to its code (countries/ua/series.json, db_map.json), and an editable
# install keeps the package pointed at the source tree we copy in here,
# so those are found without teaching setuptools about package data.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY collector/ ./collector/

RUN pip install -e . \
 && useradd --create-home --uid 10001 collector \
 && mkdir -p /app/staging \
 && chown -R collector:collector /app

# Unattended runs write only under /app/staging (the night's HTML, kept
# for a fortnight and pruned by the step itself) -- mount a volume there
# or it goes with the container.
USER collector
VOLUME ["/app/staging"]

ENTRYPOINT ["python", "-m", "collector"]
CMD ["ua", "--step", "update-prices"]
