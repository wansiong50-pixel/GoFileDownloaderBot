# Use a lightweight Python version
FROM python:3.10-slim

# UPDATE: Install Node.js (Crucial for YouTube Signatures), FFmpeg, and Curl
RUN apt-get update && \
    apt-get install -y ffmpeg curl nodejs && \
    rm -rf /var/lib/apt/lists/*

# Set up work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your code
COPY . .

# Run the bot
CMD ["python", "bot.py"]
