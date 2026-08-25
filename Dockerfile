FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LETTERBOXD_HOSTED=1 \
    LETTERBOXD_XLSX_BACKEND=xlsxwriter \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY outputs/letterboxd_actors.py outputs/letterboxd_workbook.mjs ./outputs/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=3)"

CMD ["python", "outputs/letterboxd_actors.py", "--ui", "--no-browser", "--output-dir", "/tmp/letterboxd"]
