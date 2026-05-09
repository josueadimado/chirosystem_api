FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Avoid WhiteNoise "No directory at: .../staticfiles/" on first boot when STATIC_ROOT is empty.
RUN mkdir -p staticfiles
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
