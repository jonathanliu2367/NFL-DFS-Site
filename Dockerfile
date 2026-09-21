FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html players.csv player_images.json leaderboard.html score.py scores.json ./
# Seed empty lineups if missing; runtime DATA_DIR may override path
COPY data.json ./data.json
ENV PORT=8080
ENV DATA_DIR=/data
RUN mkdir -p /data && cp data.json /data/data.json
EXPOSE 8080
CMD ["python3", "server.py"]
