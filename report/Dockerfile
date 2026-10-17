# Results page for Nebius Serverless Endpoints. Build from the repo root (see report/README.md):
#   docker build --platform linux/amd64 -f report/Dockerfile -t <image> .
# The root .dockerignore cuts the build context down to serve.py and results/site/,
# so .env and the rest of the repo never enter it.
FROM python:3.12-slim

RUN useradd --system --no-create-home --uid 10001 page
WORKDIR /app
COPY report/__init__.py report/serve.py report/
COPY results/site/ site/

USER 10001
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "report.serve", "--dir", "site"]
