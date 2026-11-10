# Ruta: proyectorestaurante/Dockerfile
FROM python:3.11-slim

# Sin .pyc y logs sin buffer (útiles con `docker compose logs -f app`)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# PyMySQL es Python puro: no se necesitan compiladores ni librerías del sistema.
# Se copia primero requirements.txt para aprovechar la caché de capas de Docker
# (solo se reinstala si cambian las dependencias).
COPY requirements.txt .
RUN pip install -r requirements.txt

# En desarrollo docker-compose.yml monta ./app encima de /app; se copia
# también aquí para que la imagen funcione sola (despliegue sin bind mount).
COPY app/ .

EXPOSE 8501

# curl no viene en python:3.11-slim: el healthcheck se hace con Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
