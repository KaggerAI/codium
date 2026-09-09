# Use Python 3.10 (More stable for financial libraries than 3.11)
FROM python:3.10-slim

# Set work directory
WORKDIR /app

# 1. Install System Dependencies required for compiling TA-Lib and Playwright
#    nodejs is needed by yt-dlp for YouTube JS extraction
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    curl \
    tar \
    git \
    libffi-dev \
    libssl-dev \
    nodejs \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 2. Compile and Install TA-Lib (The C-Library) from source
# This corresponds to step 1 in your handler.py comments
RUN curl -L http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz | tar xz \
    && cd ta-lib \
    && ./configure --prefix=/usr \
    && make \
    && make install \
    && cd .. \
    && rm -rf ta-lib

# 3. Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# 4. Install Playwright Browsers (Required for ScanX/News/Peer Comparison fetching)
RUN playwright install chromium --with-deps

# 5. Copy the application code
COPY . .

# 6. Expose the port Azure expects
EXPOSE 8000

# 7. Start the app. Flags mirror startup.sh so the container and the Azure
# startup command cannot drift apart.
#
# --worker-class gthread --threads 16 is NOT optional. Socket.IO here runs in
# async_mode='threading' over the polling transport only, so every open browser
# tab parks a long-poll request on the server for up to ping_timeout (60s).
# Gunicorn's default sync worker serves ONE request at a time, so a single tab's
# long-poll blocks every other request behind it -- agent status polls included,
# which then time out at the Azure front end and reach the browser as an empty
# or truncated body ("Unexpected end of JSON input" in the client).
#
# --timeout 600 because financial analysis takes time.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:8000", "--timeout", "600", "handler:app"]
