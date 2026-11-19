# Files often changed but not fully copied in source_overlay

Diff and merge from Ziyad's latest branch if services fail to start:

- docker-compose.yml (weather-agent, validator, video mount)
- db/init.sql (weather tables + detection_weather view)
- streaming/producer.py (video mode, stream_frames)
- streaming/contracts.py (classify_visibility)
- streaming/analytics_consumer.py
- .env.example
- Makefile, README.md, docs/ARCHITECTURE.md, docs/EVENT_CONTRACT.md

Optional large assets (not in zip): GoPro folder under data/external_datasets/
