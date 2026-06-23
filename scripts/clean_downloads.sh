#!/usr/bin/env bash
# Clean vid-lab downloads older than 1 hour
find /home/hermes/vid-lab/downloads -type f -mmin +60 -delete 2>/dev/null
find /home/hermes/vid-lab/downloads -type d -empty -delete 2>/dev/null
