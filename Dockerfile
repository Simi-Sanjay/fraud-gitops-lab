FROM python:3.12-slim
WORKDIR /srv
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/main.py .
COPY models/ ./models/
ENV MODELS_DIR=/srv/models MODEL_CONFIG=/etc/fraud/model.yaml
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
