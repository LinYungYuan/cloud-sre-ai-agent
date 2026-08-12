SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

UV_CACHE_DIR ?= $(CURDIR)/backend/.uv-cache
NODE_BIN_DIR ?=
CI ?= 1
NG_BUILD_MAX_WORKERS ?= 1

export UV_CACHE_DIR

ifneq ($(strip $(NODE_BIN_DIR)),)
PATH := $(NODE_BIN_DIR):$(PATH)
export PATH
endif

.PHONY: test-backend test-contracts test-frontend check

test-backend:
	uv run --project backend pytest backend/tests
	uv run --project backend ruff check backend/src backend/tests
	uv run --project backend pyright backend/src

test-contracts:
	uv run --project backend pytest contracts/compatibility-tests

test-frontend:
	CI=$(CI) NG_BUILD_MAX_WORKERS=$(NG_BUILD_MAX_WORKERS) npm --prefix frontend test -- --watch=false
	CI=$(CI) NG_BUILD_MAX_WORKERS=$(NG_BUILD_MAX_WORKERS) npm --prefix frontend run build

check: test-contracts test-backend test-frontend
