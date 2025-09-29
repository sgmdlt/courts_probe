FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY app.py ./
COPY get_courts.py ./
COPY main.py ./

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
