#!/usr/bin/env bash
# VOD2strm configuration
#
# Copy/edit this file next to VOD2strm.py.
# Values here can also be overridden with environment variables.

########################################
# Dispatcharr connection
########################################

# Include protocol and port if needed.
# Example: https://dispatcharr.example.com or http://127.0.0.1:9191
DISPATCHARR_URL="http://127.0.0.1:9191"

# Preferred: use a Dispatcharr API/JWT token if you have one.
# This is sent as: Authorization: Bearer <token>
DISPATCHARR_API_KEY=""

# Fallback login credentials. Used only when DISPATCHARR_API_KEY is blank.
DISPATCHARR_API_USER="admin"
DISPATCHARR_API_PASS="change_me"

########################################
# Libraries / output paths
########################################

# {XC_NAME} is replaced with the matched Dispatcharr M3U/XC account name.
MOVIES_DIR="/mnt/Share-VOD/{XC_NAME}/Movies"
SERIES_DIR="/mnt/Share-VOD/{XC_NAME}/Series"

# Safe defaults are used if these are blank.
CACHE_DIR=""
LOG_FILE=""

########################################
# Account selection
########################################

# Comma-separated wildcard patterns.
# Examples:
#   "*"              all accounts
#   "Strong 8K"      one exact account
#   "Strong*"        accounts beginning with Strong
#   "UK*,US*"        multiple patterns
XC_NAMES="*"

########################################
# Export toggles
########################################

EXPORT_MOVIES="true"
EXPORT_SERIES="true"

# Current script is STRM-only. NFO/TMDB generation is intentionally disabled.
ENABLE_NFO="false"

########################################
# Safety controls
########################################

# DRY_RUN logs actions without creating, updating, or deleting files.
DRY_RUN="false"

# DELETE_OLD removes stale .strm files only when a full Dispatcharr catalog
# fetch succeeds and TEST_MODE is false.
DELETE_OLD="true"

# TEST_MODE limits processing for quick validation and automatically disables
# stale cleanup, even if DELETE_OLD=true.
TEST_MODE="false"
TEST_LIMIT_MOVIES="20"
TEST_LIMIT_SERIES="20"

# Clears only the local JSON cache. It never removes library folders.
CLEAR_CACHE="false"

########################################
# Runtime / logging
########################################

PAGE_SIZE="250"
LOG_LEVEL="INFO"
HTTP_USER_AGENT="VOD2strm/1.2"
