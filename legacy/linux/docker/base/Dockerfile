FROM python:3.14-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libcurl4 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir endstone==0.11.13
WORKDIR /data
CMD ["endstone", "--server-folder", "/data", "--yes"]
