# CPU inference image — ultralytics base already ships torch + opencv
FROM ultralytics/ultralytics:latest-cpu

WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "producer.py"]
