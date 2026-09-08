FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY paperlab.py test_paperlab.py ./
RUN python -m unittest -v test_paperlab
CMD ["python", "-u", "paperlab.py"]
