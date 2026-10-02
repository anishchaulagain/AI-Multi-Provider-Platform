#!/usr/bin/env bash
# Pull the local models referenced by infra/gateway/routes.yaml into the Ollama container.
#   scripts/pull-ollama-models.sh                 # defaults
#   scripts/pull-ollama-models.sh llama3.2:3b     # custom list
set -euo pipefail

MODELS=("$@")
if [ ${#MODELS[@]} -eq 0 ]; then
  MODELS=(qwen2.5:7b mistral:7b nomic-embed-text)
fi

COMPOSE=(docker compose --env-file docker/.env -f docker/compose.yml -f docker/compose.ports.yml --profile llm-local)

"${COMPOSE[@]}" up -d ollama
for model in "${MODELS[@]}"; do
  echo ">> pulling $model"
  "${COMPOSE[@]}" exec ollama ollama pull "$model"
done
"${COMPOSE[@]}" exec ollama ollama list
