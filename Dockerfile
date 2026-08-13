FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY scraper/ ./scraper/
COPY toolkit/ ./toolkit/
COPY api/ ./api/
COPY scripts/ ./scripts/
# location_data/ backs the W1v operator-correction route (/location/corrections
# lazily imports the claim producer + the resolver's single-listing drain for
# the synchronous 5.5.5 read-your-writes). The resolve path is deliberately
# pyproj-free (location_data/resolver/geo.py), so the [api] extra covers it.
# scraper/db.py also lazy-imports payload_norm here for the W2a-0 churn
# instrument, which the realtime worker's detail drain runs from this image.
COPY location_data/ ./location_data/
# data/ carries runtime-read files (clip_taxonomy.json for the dedup engine's CLIP
# settings, condition rubrics/markers). load_taxonomy() reads data/clip_taxonomy.json
# from the image root, so the realtime-worker dedup lane FileNotFoundError'd without it.
COPY data/ ./data/

RUN pip install --upgrade pip && pip install ".[api]"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
