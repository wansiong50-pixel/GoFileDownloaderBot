# Use a lightweight Python version
FROM python:3.10-slim

# Install FFmpeg (for media conversion) and Curl (for streaming uploads)
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
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
