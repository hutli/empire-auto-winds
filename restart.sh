#!/bin/sh
clear
docker-compose down
docker-compose up --build -d
docker attach auto-winds_auto-winds_1
