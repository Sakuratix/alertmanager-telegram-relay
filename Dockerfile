FROM python:3.15-slim
RUN useradd --uid 10001 --no-create-home relay
WORKDIR /app
COPY relay.py .
USER 10001
EXPOSE 9087
ENTRYPOINT ["python", "relay.py"]
