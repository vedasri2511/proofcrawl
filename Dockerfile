FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m crawler && mkdir -p runs && chown -R crawler /app
USER crawler
EXPOSE 8000 8765
# --demo-site also serves the bundled fictional site; drop it for public-only crawling
CMD ["python", "run.py", "--host", "0.0.0.0", "--demo-site"]
