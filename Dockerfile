FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt requirements-llm.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-llm.txt
COPY app ./app
COPY web ./web
RUN useradd --create-home strazh && mkdir /data && chown strazh /data
USER strazh
EXPOSE 8000
CMD ["python", "-m", "app.main"]
