#!/bin/sh
isort src
docker-compose down
docker-compose up --build -d
docker logs -f auto-winds_auto-winds_1
