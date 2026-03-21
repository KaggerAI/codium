#!/bin/bash
scrapling install
gunicorn --worker-class gthread --workers 1 --threads 16 --bind 0.0.0.0:8000 --timeout 600 handler:app
