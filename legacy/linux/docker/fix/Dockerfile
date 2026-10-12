FROM python:3.14-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libcurl4 && rm -rf /var/lib/apt/lists/*
COPY *.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
WORKDIR /data
CMD ["endstone", "--server-folder", "/data", "--yes"]
