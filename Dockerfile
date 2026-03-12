# Use Python 3.10 (More stable for financial libraries than 3.11)
FROM python:3.10-slim

# Set work directory
WORKDIR /app

# 1. Install System Dependencies required for compiling TA-Lib and Playwright
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    curl \
    tar \
    git \
    libffi-dev \
    libssl-dev \
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

# 7. Start the app using the timeout settings from your startup.txt
# We increase timeout to 600s because financial analysis takes time
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "600", "handler:app"]