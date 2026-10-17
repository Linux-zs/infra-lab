#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
test -f frontend/dist/index.html || { echo '先在本地执行前端构建，并上传 dist'; exit 1; }
docker build -t infra-lab-app:1.2.0 services/app
docker build --network "${INFRA_BUILD_NETWORK:-default}" -f deploy/Dockerfile -t infra-lab-control:0.1.0 .
docker pull nginx:1.28.0-alpine
docker compose up -d
echo '管理地址：http://127.0.0.1:8088（通过 SSH 隧道访问）'
